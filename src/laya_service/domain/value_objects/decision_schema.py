"""Translate a JSON Schema into Laya questions, and answers back into JSON.

This is the correctness core of the OpenAI / Anthropic compatibility layer. The
compat endpoints let a caller describe *what to decide* with a tool's
``input_schema``; this module turns that schema into the ``questions`` mapping
Laya understands, and turns Laya's answers back into the JSON object a
tool-calling client expects.

**It is a strict validator, not a lenient translator.** Every silent coercion
here would produce an answer that looks like an LLM's and is not. A property
shape with no honest mapping is rejected with
:class:`~laya_service.domain.exceptions.UnsupportedSchemaError` rather than
guessed at. That is the whole reason this module lives in the domain layer with
no I/O: it needs exhaustive unit tests, and it needs to be usable from both the
OpenAI and the Anthropic adapter without either owning it.

The mapping
-----------

======================  ==============  ==========================================
JSON Schema             Laya type       Notes
======================  ==============  ==========================================
``enum`` (strings)      ``choice``      Enum order is preserved -- see below.
``oneOf`` + ``const``   ``choice``      The only standard way to attach a rubric.
``boolean``             ``noul``        **Lossy.** Requires an explicit threshold.
``number``              ``score``       Returns the expected value, not an integer.
``integer``             ``score``       Rounded to an integer to honour the schema.
======================  ==============  ==========================================

**Enum order is load-bearing.** Laya returns ``keys[int(p.argmax())]`` where
``keys = list(criteria.keys())``. The label a caller receives is therefore
decided by the insertion order of the criteria mapping. Sorting or de-duplicating
the enum would silently permute the labels -- the answer would still be a valid
member of the enum, just the wrong one. Everything here preserves schema order.

**``boolean`` is lossy and must be opted into.** A JSON ``boolean`` asks for a
true/false decision; ``noul`` returns P(true) and no decision. Any threshold we
applied on the caller's behalf would be invented, and the probability would be
destroyed -- the caller could not re-threshold. So a ``boolean`` property is
rejected unless it carries an explicit ``x-laya-threshold``, which is recorded
in the response metadata so the derivation is visible rather than silent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from laya_service.domain.exceptions import UnsupportedSchemaError

__all__ = [
    "CompiledSchema",
    "QuestionPlan",
    "compile_schema",
    "render_arguments",
]

#: Property keys that configure the mapping rather than describing a decision.
#: They are consumed here and never forwarded to Laya.
_VENDOR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "x-laya-threshold",
        "x-laya-criteria",
        "x-laya-type",
    }
)

#: JSON Schema keywords this module understands on a property. Anything outside
#: this set is rejected: a whitelist cannot leak an unmappable construct
#: through, whereas a blacklist eventually will.
_SUPPORTED_KEYWORDS: Final[frozenset[str]] = (
    frozenset(
        {
            "type",
            "description",
            "title",
            "enum",
            "enumDescriptions",
            "oneOf",
            "minimum",
            "maximum",
            "default",
            "examples",
        }
    )
    | _VENDOR_KEYS
)

#: Largest instruction we will forward. Laya budgets the question head at 192
#: tokens and truncates the instruction to make room for the options, so an
#: over-long description is silently cut and the model answers a question that
#: was never asked. This is a character-count proxy -- the interface layer may
#: not import a tokenizer -- chosen to be conservative for English prose.
MAX_INSTRUCTION_CHARS: Final[int] = 400

#: Options are truncated to 48 tokens each by Laya's sequence builder.
MAX_CRITERIA_ITEMS: Final[int] = 32


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    """One JSON Schema property, compiled into a Laya question.

    Attributes:
        name: The property name, used as the Laya question id and as the key in
            the response arguments.
        question_type: One of ``"noul"``, ``"choice"``, ``"score"``.
        instructions: The English question text.
        criteria: Laya criteria -- a mapping for ``choice``/``noul``, a list for
            ``score``, or ``None``.
        threshold: For a ``boolean`` property, the probability at or above which
            the answer is true. ``None`` for every other type.
        enum_values: For a ``choice`` property, the enum in schema order. Used
            to render the answer back as a string.
        score_minimum: For a ``score`` property, the caller's declared minimum.
            Laya scores over level indices starting at 0, so the index must be
            shifted by this to land back on the caller's scale.
        score_max: For a ``score`` property, the highest index, used to render
            the expected value back onto the caller's numeric range.
        integral: Whether the caller declared ``integer``, in which case the
            expected value is rounded.
    """

    name: str
    question_type: str
    instructions: str
    criteria: Mapping[str, Any] | Sequence[str] | None = None
    threshold: float | None = None
    enum_values: tuple[str, ...] = ()
    score_minimum: int = 0
    score_max: int = 0
    integral: bool = False

    def to_laya_question(self) -> dict[str, Any]:
        """Render this plan as a Laya question specification.

        Returns:
            A ``{"type", "instructions"}`` mapping, plus ``criteria`` when the
            type requires one.
        """
        spec: dict[str, Any] = {
            "type": self.question_type,
            "instructions": self.instructions,
        }
        if self.criteria is not None:
            spec["criteria"] = self.criteria
        return spec


@dataclass(frozen=True, slots=True)
class CompiledSchema:
    """A JSON Schema compiled into questions, plus what is needed to decode answers.

    Attributes:
        plans: The compiled questions, in schema declaration order.
        questions: The Laya ``questions`` mapping ready for the model port.
        thresholds: Property name to the threshold used for a ``boolean``
            property, so the response metadata can record the derivation.
    """

    plans: tuple[QuestionPlan, ...] = field(default_factory=tuple)
    questions: Mapping[str, Any] = field(default_factory=dict)
    thresholds: Mapping[str, float] = field(default_factory=dict)


def compile_schema(schema: Mapping[str, Any]) -> CompiledSchema:
    """Compile a JSON Schema object into Laya questions.

    Args:
        schema: The tool's ``input_schema`` / ``parameters``. Must be an object
            schema with a non-empty ``properties`` mapping.

    Returns:
        The compiled schema.

    Raises:
        UnsupportedSchemaError: If the schema, or any property within it, has no
            honest mapping onto a Laya question type. The message always names
            the offending property and says what to use instead.
    """
    _require_object_schema(schema)
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        raise UnsupportedSchemaError(
            "the schema must declare at least one property under 'properties'; "
            "each property becomes one question for the model"
        )

    plans: list[QuestionPlan] = []
    for name, raw in properties.items():
        if not isinstance(raw, Mapping):
            raise UnsupportedSchemaError(
                f"property {name!r} must be a schema object, got {type(raw).__name__}"
            )
        plans.append(_compile_property(str(name), raw))

    questions = {plan.name: plan.to_laya_question() for plan in plans}
    thresholds = {plan.name: plan.threshold for plan in plans if plan.threshold is not None}
    return CompiledSchema(plans=tuple(plans), questions=questions, thresholds=thresholds)


def render_arguments(
    compiled: CompiledSchema,
    answers: Mapping[str, Any],
) -> dict[str, Any]:
    """Turn Laya's answers into the JSON object a tool call should carry.

    Args:
        compiled: The schema the answers were produced against.
        answers: The model port's answer mapping, keyed by property name.

    Returns:
        A JSON-serialisable mapping suitable for a ``tool_calls`` /
        ``tool_use`` arguments payload. A property the model did not answer is
        omitted rather than defaulted, so a caller can tell "no answer" from
        "answered false".
    """
    arguments: dict[str, Any] = {}
    for plan in compiled.plans:
        entry = answers.get(plan.name)
        if not isinstance(entry, Mapping):
            continue
        value = _render_one(plan, entry)
        if value is not None:
            arguments[plan.name] = value
    return arguments


# -- Compilation -------------------------------------------------------------


def _require_object_schema(schema: Mapping[str, Any]) -> None:
    """Reject anything that is not a plain object schema.

    Args:
        schema: The candidate schema.

    Raises:
        UnsupportedSchemaError: If the schema is not an object with properties.
    """
    if not isinstance(schema, Mapping):
        raise UnsupportedSchemaError(f"the schema must be a mapping, got {type(schema).__name__}")
    declared = schema.get("type")
    if declared not in (None, "object"):
        raise UnsupportedSchemaError(
            f"the schema must describe an object (a set of named decisions), got type {declared!r}"
        )


def _compile_property(name: str, prop: Mapping[str, Any]) -> QuestionPlan:
    """Compile one schema property into a question plan.

    Args:
        name: The property name.
        prop: The property's schema.

    Returns:
        The compiled plan.

    Raises:
        UnsupportedSchemaError: If the property has no honest Laya mapping.
    """
    _reject_unsupported_keywords(name, prop)

    explicit = prop.get("x-laya-type")
    if explicit is not None:
        return _compile_explicit(name, prop, str(explicit))

    if "oneOf" in prop:
        return _compile_one_of(name, prop)
    if "enum" in prop:
        return _compile_enum(name, prop)

    declared = prop.get("type")
    if declared == "boolean":
        return _compile_boolean(name, prop)
    if declared in ("number", "integer"):
        return _compile_numeric(name, prop, integral=declared == "integer")

    raise UnsupportedSchemaError(
        f"property {name!r} has no mapping to a model question "
        f"(type={declared!r}). Supported: 'boolean' with an 'x-laya-threshold', "
        f"'enum' or 'oneOf' of strings, and bounded 'number'/'integer'. "
        f"Free-form strings, arrays and nested objects cannot be produced by "
        f"this model -- it classifies, it does not generate."
    )


def _reject_unsupported_keywords(name: str, prop: Mapping[str, Any]) -> None:
    """Reject schema keywords this module does not understand.

    A whitelist is used deliberately: an unrecognised keyword might change the
    meaning of the property, and ignoring it would silently answer a different
    question than the caller asked.

    Args:
        name: The property name, for the error message.
        prop: The property's schema.

    Raises:
        UnsupportedSchemaError: If an unknown keyword is present.
    """
    unknown = sorted(set(prop) - _SUPPORTED_KEYWORDS)
    if unknown:
        raise UnsupportedSchemaError(
            f"property {name!r} uses unsupported schema keywords: {unknown}. "
            f"Supported keywords are: {sorted(_SUPPORTED_KEYWORDS)}"
        )


def _compile_enum(name: str, prop: Mapping[str, Any]) -> QuestionPlan:
    """Compile an ``enum`` property into a ``choice`` question.

    Args:
        name: The property name.
        prop: The property's schema.

    Returns:
        The compiled plan. Enum order is preserved exactly.

    Raises:
        UnsupportedSchemaError: If the enum is empty or not all strings.
    """
    values = prop["enum"]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'enum' must be a non-empty list of strings"
        )
    if len(values) > MAX_CRITERIA_ITEMS:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'enum' has {len(values)} options, the limit is "
            f"{MAX_CRITERIA_ITEMS}. Each option costs prompt budget."
        )
    if not all(isinstance(v, str) for v in values):
        raise UnsupportedSchemaError(
            f"property {name!r}: every 'enum' value must be a string, got "
            f"{[type(v).__name__ for v in values if not isinstance(v, str)]}"
        )

    # enumDescriptions is not standard JSON Schema, but it is OpenAI's own
    # convention for attaching a rubric to an enum and it is the only way to
    # express one without oneOf.
    descriptions = prop.get("enumDescriptions")
    criteria: dict[str, Any]
    if isinstance(descriptions, Sequence) and not isinstance(descriptions, (str, bytes)):
        if len(descriptions) != len(values):
            raise UnsupportedSchemaError(
                f"property {name!r}: 'enumDescriptions' has {len(descriptions)} "
                f"entries but 'enum' has {len(values)}; they must line up"
            )
        criteria = {v: d for v, d in zip(values, descriptions, strict=True)}
    else:
        criteria = {v: None for v in values}

    return QuestionPlan(
        name=name,
        question_type="choice",
        instructions=_instructions(name, prop, required=True),
        criteria=criteria,
        enum_values=tuple(values),
    )


def _compile_one_of(name: str, prop: Mapping[str, Any]) -> QuestionPlan:
    """Compile a ``oneOf`` of ``const`` variants into a ``choice`` question.

    This is the standards-compliant way to attach a rubric to a choice: each
    variant carries a ``const`` (the value) and a ``description`` (the rubric
    text).

    Args:
        name: The property name.
        prop: The property's schema.

    Returns:
        The compiled plan.

    Raises:
        UnsupportedSchemaError: If a variant is malformed or uses ``const``
            values that are not strings.
    """
    variants = prop["oneOf"]
    if not isinstance(variants, Sequence) or isinstance(variants, (str, bytes)) or not variants:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'oneOf' must be a non-empty list of variants"
        )

    criteria: dict[str, Any] = {}
    for index, variant in enumerate(variants):
        if not isinstance(variant, Mapping) or "const" not in variant:
            raise UnsupportedSchemaError(
                f"property {name!r}: 'oneOf' variant {index} must be an object with "
                f"a 'const' value; other forms have no mapping"
            )
        const = variant["const"]
        if not isinstance(const, str):
            raise UnsupportedSchemaError(
                f"property {name!r}: 'oneOf' variant {index} has a non-string "
                f"'const' ({type(const).__name__}); only string labels are supported"
            )
        if const in criteria:
            raise UnsupportedSchemaError(
                f"property {name!r}: duplicate 'const' value {const!r} in 'oneOf'"
            )
        criteria[const] = variant.get("description")

    return QuestionPlan(
        name=name,
        question_type="choice",
        instructions=_instructions(name, prop, required=True),
        criteria=criteria,
        enum_values=tuple(criteria),
    )


def _compile_boolean(name: str, prop: Mapping[str, Any]) -> QuestionPlan:
    """Compile a ``boolean`` property into a ``noul`` question.

    Args:
        name: The property name.
        prop: The property's schema.

    Returns:
        The compiled plan, carrying the caller's threshold.

    Raises:
        UnsupportedSchemaError: If no ``x-laya-threshold`` was supplied. The
            derivation is lossy, so it must be opted into explicitly.
    """
    threshold = prop.get("x-laya-threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise UnsupportedSchemaError(
            f"property {name!r} is a boolean, but this model answers with a "
            f'probability, not a decision. Add "x-laya-threshold": 0.5 to say '
            f"where to draw the line, or declare "
            f'{{"type": "number"}} to receive the probability itself.'
        )
    if not 0.0 <= float(threshold) <= 1.0:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'x-laya-threshold' must lie in [0, 1], got {threshold!r}"
        )

    criteria = prop.get("x-laya-criteria")
    if criteria is not None and not isinstance(criteria, Mapping):
        raise UnsupportedSchemaError(
            f"property {name!r}: 'x-laya-criteria' must be a mapping with "
            f"'true' and/or 'false' keys"
        )

    return QuestionPlan(
        name=name,
        question_type="noul",
        instructions=_instructions(name, prop, required=False),
        criteria=criteria,
        threshold=float(threshold),
    )


def _compile_numeric(name: str, prop: Mapping[str, Any], *, integral: bool) -> QuestionPlan:
    """Compile a bounded ``number``/``integer`` property into a ``score`` question.

    Args:
        name: The property name.
        prop: The property's schema.
        integral: Whether the caller declared ``integer``.

    Returns:
        The compiled plan.

    Raises:
        UnsupportedSchemaError: If the property has no usable bounds, or the
            bounds do not describe a small ordinal scale.
    """
    minimum = prop.get("minimum", 0)
    maximum = prop.get("maximum")
    if not isinstance(maximum, (int, float)) or isinstance(maximum, bool):
        raise UnsupportedSchemaError(
            f"property {name!r} is numeric but declares no 'maximum', so there is "
            f'no scale to score on. Add "minimum": 0 and "maximum": N to '
            f"describe an ordinal rating."
        )
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
        raise UnsupportedSchemaError(f"property {name!r}: 'minimum' must be a number")

    span = int(maximum) - int(minimum)
    if span < 1:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'maximum' ({maximum}) must exceed 'minimum' ({minimum})"
        )
    if span + 1 > MAX_CRITERIA_ITEMS:
        raise UnsupportedSchemaError(
            f"property {name!r}: the range {minimum}..{maximum} needs {span + 1} "
            f"levels, the limit is {MAX_CRITERIA_ITEMS}"
        )

    # Laya scores over an ordered list of levels, lowest first. The labels are
    # positional placeholders; the caller's own numeric bounds are restored when
    # the answer is rendered back.
    criteria = [f"level {i + int(minimum)}" for i in range(span + 1)]

    return QuestionPlan(
        name=name,
        question_type="score",
        instructions=_instructions(name, prop, required=False),
        criteria=criteria,
        score_minimum=int(minimum),
        score_max=span,
        integral=integral,
    )


def _compile_explicit(name: str, prop: Mapping[str, Any], kind: str) -> QuestionPlan:
    """Compile a property whose Laya type the caller named explicitly.

    ``x-laya-type`` is an escape hatch for callers who want a specific Laya
    question type and are willing to describe the options themselves. It exists
    so the compat layer never becomes a dead end for a legitimate use case.

    Args:
        name: The property name.
        prop: The property's schema.
        kind: The requested Laya type.

    Returns:
        The compiled plan.

    Raises:
        UnsupportedSchemaError: If the requested type is unknown or its options
            are missing.
    """
    if kind not in {"noul", "choice", "score"}:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'x-laya-type' must be one of noul, choice, score; got {kind!r}"
        )
    criteria = prop.get("x-laya-criteria")
    if kind in {"choice", "score"} and criteria is None:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'x-laya-type: {kind}' requires 'x-laya-criteria'"
        )
    threshold = prop.get("x-laya-threshold")
    if kind == "noul" and not isinstance(threshold, (int, float)):
        raise UnsupportedSchemaError(
            f"property {name!r}: 'x-laya-type: noul' requires 'x-laya-threshold'"
        )
    return QuestionPlan(
        name=name,
        question_type=kind,
        instructions=_instructions(name, prop, required=kind == "choice"),
        criteria=criteria,
        threshold=float(threshold) if isinstance(threshold, (int, float)) else None,
        enum_values=tuple(criteria) if isinstance(criteria, Mapping) else (),
    )


def _instructions(name: str, prop: Mapping[str, Any], *, required: bool) -> str:
    """Derive the English question text for a property.

    Args:
        name: The property name.
        prop: The property's schema.
        required: Whether a missing description is fatal. It is for ``choice``,
            where the description *is* the rubric, and merely inconvenient
            otherwise.

    Returns:
        The instruction text.

    Raises:
        UnsupportedSchemaError: If the description is missing when required, or
            is too long to survive Laya's prompt budget intact.
    """
    description = prop.get("description") or prop.get("title")
    if not isinstance(description, str) or not description.strip():
        if required:
            raise UnsupportedSchemaError(
                f"property {name!r} needs a 'description': for a choice it is the "
                f"rubric the model reads, so it cannot be omitted"
            )
        description = f"What is the value of {name}?"
    description = description.strip()

    if len(description) > MAX_INSTRUCTION_CHARS:
        raise UnsupportedSchemaError(
            f"property {name!r}: 'description' is {len(description)} characters, "
            f"over the {MAX_INSTRUCTION_CHARS}-character limit. The model budgets "
            f"192 tokens for the question and truncates the rest, which would make "
            f"it answer something other than what was asked."
        )
    return description


# -- Rendering ---------------------------------------------------------------


def _render_one(plan: QuestionPlan, entry: Mapping[str, Any]) -> Any:
    """Render one answer entry as the JSON value the caller's schema expects.

    Args:
        plan: The question the answer belongs to.
        entry: The model's answer entry.

    Returns:
        The value to place in the arguments object, or ``None`` when the entry
        carries no usable answer.
    """
    if plan.question_type == "noul":
        probability = _as_float(entry.get("noul", entry.get("value")))
        if probability is None:
            return None
        # `plan.threshold or 0.5` would treat a legitimate threshold of 0.0 as
        # absent, since 0.0 is falsy. An explicit `is None` keeps the caller's
        # value, and the response metadata reports the threshold actually used.
        threshold = 0.5 if plan.threshold is None else plan.threshold
        return probability >= threshold

    if plan.question_type == "choice":
        value = entry.get("choice")
        return value if isinstance(value, str) else None

    if plan.question_type == "score":
        expected = _as_float(entry.get("score"))
        if expected is None:
            return None
        # Laya returns the expected value over level indices. `score_max` is the
        # span, so the index maps back onto the caller's own range.
        # Laya returns the expected value over level *indices*. `_compile_numeric`
        # labels the levels `minimum .. maximum`, so index E corresponds to the
        # caller's value `minimum + E`. Dividing by the span instead would emit a
        # number outside the range the caller declared -- which their own schema
        # validator would then reject.
        restored = plan.score_minimum + expected
        if plan.integral:
            # A caller who declared `integer` must not receive 7.37 -- a real
            # language model would never do that, and their own validator would
            # reject it. `round` already yields an int for a float input.
            return round(restored)
        return round(restored, 4)

    return None


def _as_float(value: Any) -> float | None:
    """Coerce an answer field to a float, tolerating the shapes Laya may emit.

    Args:
        value: The raw field.

    Returns:
        The float, or ``None`` when the field is absent or unusable. A bool is
        accepted and mapped to 1.0/0.0, since a backend may return one.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes"}:
            return 1.0
        if lowered in {"false", "no"}:
            return 0.0
        try:
            return float(lowered)
        except ValueError:
            return None
    return None
