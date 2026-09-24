"""Tests for the JSON Schema to Laya question compiler.

This module is the correctness core of the compatibility layer: it decides
whether a caller's ``input_schema`` can be answered honestly, and it turns the
model's answers back into the JSON a tool call carries. The rejection paths
matter as much as the happy paths -- a schema that is silently coerced produces
an answer indistinguishable from a real language model's, which is the failure
this whole layer exists to prevent.
"""

from __future__ import annotations

import pytest

from laya_service.domain.exceptions import UnsupportedSchemaError
from laya_service.domain.value_objects.decision_schema import (
    MAX_CRITERIA_ITEMS,
    MAX_INSTRUCTION_CHARS,
    CompiledSchema,
    compile_schema,
    render_arguments,
)


def object_schema(**properties: object) -> dict[str, object]:
    """Build an object schema from property definitions.

    Args:
        **properties: Property name to schema.

    Returns:
        A JSON Schema object.
    """
    return {"type": "object", "properties": dict(properties)}


class TestEnumToChoice:
    """``enum`` properties become ``choice`` questions."""

    def test_enum_becomes_choice(self) -> None:
        """A string enum maps to a choice question with those options."""
        compiled = compile_schema(
            object_schema(
                category={
                    "type": "string",
                    "enum": ["billing", "technical"],
                    "description": "Which team should handle this?",
                }
            )
        )
        question = compiled.questions["category"]
        assert question["type"] == "choice"
        assert list(question["criteria"]) == ["billing", "technical"]

    def test_enum_order_is_preserved_exactly(self) -> None:
        """The enum order is the label order, and must not be normalised.

        Laya answers ``keys[int(p.argmax())]`` where ``keys`` is the criteria
        insertion order. Sorting the enum would still return a valid member --
        just the wrong one -- so this is the single most dangerous silent
        corruption path in the compiler.
        """
        compiled = compile_schema(
            object_schema(
                pick={
                    "type": "string",
                    "enum": ["z", "a", "m"],
                    "description": "Pick one",
                }
            )
        )
        assert list(compiled.questions["pick"]["criteria"]) == ["z", "a", "m"]

    def test_enum_descriptions_become_criteria_text(self) -> None:
        """``enumDescriptions`` attaches a rubric to each option."""
        compiled = compile_schema(
            object_schema(
                severity={
                    "type": "string",
                    "enum": ["low", "high"],
                    "enumDescriptions": ["minor annoyance", "blocks all work"],
                    "description": "How bad is it?",
                }
            )
        )
        criteria = compiled.questions["severity"]["criteria"]
        assert criteria == {"low": "minor annoyance", "high": "blocks all work"}

    def test_mismatched_enum_descriptions_are_rejected(self) -> None:
        """A description count that does not line up is a caller bug."""
        with pytest.raises(UnsupportedSchemaError, match="must line up"):
            compile_schema(
                object_schema(
                    x={
                        "type": "string",
                        "enum": ["a", "b"],
                        "enumDescriptions": ["only one"],
                        "description": "?",
                    }
                )
            )

    def test_non_string_enum_is_rejected(self) -> None:
        """Only string labels are supported."""
        with pytest.raises(UnsupportedSchemaError, match="must be a string"):
            compile_schema(
                object_schema(x={"type": "string", "enum": ["a", 2], "description": "?"})
            )

    def test_empty_enum_is_rejected(self) -> None:
        """An enum with no options has nothing to choose between."""
        with pytest.raises(UnsupportedSchemaError, match="non-empty"):
            compile_schema(object_schema(x={"type": "string", "enum": [], "description": "?"}))

    def test_oversized_enum_is_rejected(self) -> None:
        """Too many options exhausts the prompt budget."""
        with pytest.raises(UnsupportedSchemaError, match="the limit is"):
            compile_schema(
                object_schema(
                    x={
                        "type": "string",
                        "enum": [f"opt{i}" for i in range(MAX_CRITERIA_ITEMS + 1)],
                        "description": "?",
                    }
                )
            )

    def test_choice_requires_a_description(self) -> None:
        """For a choice the description *is* the rubric, so it cannot be omitted."""
        with pytest.raises(UnsupportedSchemaError, match="needs a 'description'"):
            compile_schema(object_schema(x={"type": "string", "enum": ["a", "b"]}))


class TestOneOfToChoice:
    """``oneOf`` with ``const`` variants is the standard rubric form."""

    def test_one_of_becomes_choice_with_rubric(self) -> None:
        """Each variant's description becomes its criteria text."""
        compiled = compile_schema(
            object_schema(
                cause={
                    "description": "What caused the failure?",
                    "oneOf": [
                        {"const": "gps", "description": "satellite multipath"},
                        {"const": "slip", "description": "wheel slip"},
                    ],
                }
            )
        )
        assert compiled.questions["cause"]["criteria"] == {
            "gps": "satellite multipath",
            "slip": "wheel slip",
        }

    def test_variant_without_const_is_rejected(self) -> None:
        """A variant with no const has no label."""
        with pytest.raises(UnsupportedSchemaError, match="must be an object with"):
            compile_schema(
                object_schema(x={"description": "?", "oneOf": [{"description": "no const here"}]})
            )

    def test_non_string_const_is_rejected(self) -> None:
        """Only string labels are supported."""
        with pytest.raises(UnsupportedSchemaError, match="non-string"):
            compile_schema(object_schema(x={"description": "?", "oneOf": [{"const": 42}]}))

    def test_duplicate_const_is_rejected(self) -> None:
        """Two variants with the same label are ambiguous."""
        with pytest.raises(UnsupportedSchemaError, match="duplicate"):
            compile_schema(
                object_schema(x={"description": "?", "oneOf": [{"const": "a"}, {"const": "a"}]})
            )


class TestBooleanToNoul:
    """``boolean`` is lossy and must be opted into."""

    def test_boolean_without_threshold_is_rejected(self) -> None:
        """The model returns a probability, so a threshold must be supplied.

        Deriving a boolean without one would invent a threshold on the caller's
        behalf *and* destroy the probability, which they could not recover.
        """
        with pytest.raises(UnsupportedSchemaError, match="x-laya-threshold"):
            compile_schema(object_schema(x={"type": "boolean", "description": "Is it spam?"}))

    def test_boolean_with_threshold_compiles(self) -> None:
        """An explicit threshold makes the derivation visible and opt-in."""
        compiled = compile_schema(
            object_schema(
                spam={
                    "type": "boolean",
                    "description": "Is this spam?",
                    "x-laya-threshold": 0.7,
                }
            )
        )
        assert compiled.questions["spam"]["type"] == "noul"
        assert compiled.thresholds["spam"] == pytest.approx(0.7)

    def test_out_of_range_threshold_is_rejected(self) -> None:
        """A threshold outside [0, 1] is not a probability."""
        with pytest.raises(UnsupportedSchemaError, match=r"\[0, 1\]"):
            compile_schema(
                object_schema(x={"type": "boolean", "description": "?", "x-laya-threshold": 1.5})
            )

    def test_noul_renders_boolean_at_the_threshold(self) -> None:
        """Above the threshold is true; below is false."""
        compiled = compile_schema(
            object_schema(
                spam={
                    "type": "boolean",
                    "description": "Is this spam?",
                    "x-laya-threshold": 0.7,
                }
            )
        )
        assert render_arguments(compiled, {"spam": {"noul": 0.8}}) == {"spam": True}
        assert render_arguments(compiled, {"spam": {"noul": 0.6}}) == {"spam": False}

    def test_threshold_boundary_is_inclusive(self) -> None:
        """A probability exactly at the threshold counts as true."""
        compiled = compile_schema(
            object_schema(x={"type": "boolean", "description": "?", "x-laya-threshold": 0.5})
        )
        assert render_arguments(compiled, {"x": {"noul": 0.5}}) == {"x": True}


class TestNumericToScore:
    """Bounded numbers become ``score`` questions."""

    def test_bounded_number_becomes_score(self) -> None:
        """A range with bounds describes an ordinal scale."""
        compiled = compile_schema(
            object_schema(
                severity={
                    "type": "number",
                    "minimum": 0,
                    "maximum": 3,
                    "description": "How severe?",
                }
            )
        )
        question = compiled.questions["severity"]
        assert question["type"] == "score"
        assert len(question["criteria"]) == 4

    def test_number_returns_the_expected_value(self) -> None:
        """A float property receives the expected value, scaled to its range."""
        compiled = compile_schema(
            object_schema(x={"type": "number", "minimum": 0, "maximum": 4, "description": "?"})
        )
        result = render_arguments(compiled, {"x": {"score": 2.0}})
        assert result["x"] == pytest.approx(0.5)

    def test_integer_returns_a_rounded_integer(self) -> None:
        """An ``integer`` property must not receive 2.37.

        A real language model would never return a non-integer for an integer
        property, so emitting one would break the caller's own validator.
        """
        compiled = compile_schema(
            object_schema(x={"type": "integer", "minimum": 0, "maximum": 4, "description": "?"})
        )
        result = render_arguments(compiled, {"x": {"score": 2.4}})
        assert result["x"] == 2
        assert isinstance(result["x"], int)

    def test_number_without_maximum_is_rejected(self) -> None:
        """With no upper bound there is no scale to score on."""
        with pytest.raises(UnsupportedSchemaError, match="no 'maximum'"):
            compile_schema(object_schema(x={"type": "number", "description": "?"}))

    def test_maximum_below_minimum_is_rejected(self) -> None:
        """An inverted range is a caller bug."""
        with pytest.raises(UnsupportedSchemaError, match="must exceed"):
            compile_schema(
                object_schema(x={"type": "number", "minimum": 5, "maximum": 2, "description": "?"})
            )

    def test_oversized_range_is_rejected(self) -> None:
        """A wide range needs too many levels."""
        with pytest.raises(UnsupportedSchemaError, match="the limit is"):
            compile_schema(
                object_schema(
                    x={
                        "type": "number",
                        "minimum": 0,
                        "maximum": MAX_CRITERIA_ITEMS,
                        "description": "?",
                    }
                )
            )


class TestUnsupportedShapes:
    """Shapes with no honest mapping are rejected, not guessed at."""

    def test_free_form_string_is_rejected(self) -> None:
        """A plain string is free text, which this model cannot produce."""
        with pytest.raises(UnsupportedSchemaError, match="no mapping"):
            compile_schema(object_schema(x={"type": "string", "description": "Write a poem"}))

    def test_array_is_rejected(self) -> None:
        """Arrays have no mapping -- there is no multi-select question type.

        ``items`` is not a supported keyword, so the whitelist rejects this
        before the type check runs. That is the intended order: an unrecognised
        keyword could change what the property means.
        """
        with pytest.raises(UnsupportedSchemaError, match="unsupported schema keywords"):
            compile_schema(
                object_schema(x={"type": "array", "items": {"type": "string"}, "description": "?"})
            )

    def test_bare_array_type_is_rejected(self) -> None:
        """An array with no other keywords reaches the type check."""
        with pytest.raises(UnsupportedSchemaError, match="no mapping"):
            compile_schema(object_schema(x={"type": "array", "description": "?"}))

    def test_nested_object_is_rejected(self) -> None:
        """Nested objects would need flattening and un-flattening, ambiguously."""
        with pytest.raises(UnsupportedSchemaError, match="unsupported schema keywords"):
            compile_schema(
                object_schema(
                    x={
                        "type": "object",
                        "properties": {"y": {"type": "boolean"}},
                        "description": "?",
                    }
                )
            )

    def test_bare_object_type_is_rejected(self) -> None:
        """A nested object with no other keywords reaches the type check."""
        with pytest.raises(UnsupportedSchemaError, match="no mapping"):
            compile_schema(object_schema(x={"type": "object", "description": "?"}))

    def test_unknown_keyword_is_rejected(self) -> None:
        """A whitelist is used so an unmappable construct cannot slip through."""
        with pytest.raises(UnsupportedSchemaError, match="unsupported schema keywords"):
            compile_schema(
                object_schema(
                    x={
                        "type": "boolean",
                        "description": "?",
                        "x-laya-threshold": 0.5,
                        "patternProperties": {},
                    }
                )
            )

    def test_non_object_schema_is_rejected(self) -> None:
        """The top level must describe a set of named decisions."""
        with pytest.raises(UnsupportedSchemaError, match="must describe an object"):
            compile_schema({"type": "array"})

    def test_schema_without_properties_is_rejected(self) -> None:
        """With no properties there is nothing to decide."""
        with pytest.raises(UnsupportedSchemaError, match="at least one property"):
            compile_schema({"type": "object", "properties": {}})

    def test_error_message_explains_the_limitation(self) -> None:
        """A rejection must tell the caller what to do instead."""
        with pytest.raises(UnsupportedSchemaError) as exc:
            compile_schema(object_schema(x={"type": "string", "description": "?"}))
        assert "classifies" in str(exc.value)


class TestInstructions:
    """Instruction derivation and the truncation guard."""

    def test_description_becomes_instructions(self) -> None:
        """The property description is the question text."""
        compiled = compile_schema(
            object_schema(
                x={
                    "type": "boolean",
                    "description": "Is the customer angry?",
                    "x-laya-threshold": 0.5,
                }
            )
        )
        assert compiled.questions["x"]["instructions"] == "Is the customer angry?"

    def test_title_is_used_when_description_is_absent(self) -> None:
        """A title is an acceptable fallback for a non-choice question."""
        compiled = compile_schema(
            object_schema(x={"type": "boolean", "title": "Is it urgent?", "x-laya-threshold": 0.5})
        )
        assert compiled.questions["x"]["instructions"] == "Is it urgent?"

    def test_missing_description_synthesises_for_noul(self) -> None:
        """A non-choice question can fall back to the property name."""
        compiled = compile_schema(object_schema(x={"type": "boolean", "x-laya-threshold": 0.5}))
        assert "x" in compiled.questions["x"]["instructions"]

    def test_overlong_description_is_rejected(self) -> None:
        """Laya truncates the instruction, so an over-long one answers a different question."""
        with pytest.raises(UnsupportedSchemaError, match="over the"):
            compile_schema(
                object_schema(
                    x={
                        "type": "boolean",
                        "description": "a" * (MAX_INSTRUCTION_CHARS + 1),
                        "x-laya-threshold": 0.5,
                    }
                )
            )


class TestExplicitType:
    """The ``x-laya-type`` escape hatch."""

    def test_explicit_choice_compiles(self) -> None:
        """A caller may name the Laya type directly."""
        compiled = compile_schema(
            object_schema(
                x={
                    "x-laya-type": "choice",
                    "x-laya-criteria": {"a": "first", "b": "second"},
                    "description": "Which one?",
                }
            )
        )
        assert compiled.questions["x"]["type"] == "choice"
        assert compiled.questions["x"]["criteria"] == {"a": "first", "b": "second"}

    def test_unknown_explicit_type_is_rejected(self) -> None:
        """Only the three real Laya types are accepted."""
        with pytest.raises(UnsupportedSchemaError, match="must be one of"):
            compile_schema(object_schema(x={"x-laya-type": "multi", "description": "?"}))

    def test_explicit_choice_requires_criteria(self) -> None:
        """A choice with no options is unanswerable."""
        with pytest.raises(UnsupportedSchemaError, match="requires 'x-laya-criteria'"):
            compile_schema(object_schema(x={"x-laya-type": "choice", "description": "?"}))


class TestCompiledSchema:
    """The compiled result carries what is needed to decode answers."""

    def test_multiple_properties_all_compile(self) -> None:
        """A realistic mixed schema compiles every property."""
        compiled = compile_schema(
            object_schema(
                category={
                    "type": "string",
                    "enum": ["refund", "technical"],
                    "description": "Which team?",
                },
                churn_risk={
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Churn likelihood",
                },
                needs_human={
                    "type": "boolean",
                    "description": "Needs a human?",
                    "x-laya-threshold": 0.5,
                },
            )
        )
        assert set(compiled.questions) == {"category", "churn_risk", "needs_human"}
        assert compiled.questions["category"]["type"] == "choice"
        assert compiled.questions["churn_risk"]["type"] == "score"
        assert compiled.questions["needs_human"]["type"] == "noul"

    def test_returns_compiled_schema_type(self) -> None:
        """The compiler returns its declared type."""
        compiled = compile_schema(
            object_schema(x={"type": "boolean", "description": "?", "x-laya-threshold": 0.5})
        )
        assert isinstance(compiled, CompiledSchema)

    def test_declaration_order_is_preserved(self) -> None:
        """Properties keep their schema order."""
        compiled = compile_schema(
            object_schema(
                z={"type": "boolean", "description": "?", "x-laya-threshold": 0.5},
                a={"type": "boolean", "description": "?", "x-laya-threshold": 0.5},
            )
        )
        assert list(compiled.questions) == ["z", "a"]


class TestRenderArguments:
    """Answers render back into the shape a tool call expects."""

    def test_renders_every_type(self) -> None:
        """A mixed answer set renders each property correctly."""
        compiled = compile_schema(
            object_schema(
                cat={"type": "string", "enum": ["a", "b"], "description": "?"},
                score={"type": "number", "minimum": 0, "maximum": 2, "description": "?"},
                flag={"type": "boolean", "description": "?", "x-laya-threshold": 0.5},
            )
        )
        arguments = render_arguments(
            compiled,
            {
                "cat": {"choice": "b"},
                "score": {"score": 2.0},
                "flag": {"noul": 0.9},
            },
        )
        assert arguments == {"cat": "b", "score": 1.0, "flag": True}

    def test_missing_answer_is_omitted(self) -> None:
        """An unanswered property is absent, not defaulted.

        A caller must be able to tell "no answer" from "answered false".
        """
        compiled = compile_schema(
            object_schema(a={"type": "boolean", "description": "?", "x-laya-threshold": 0.5})
        )
        assert render_arguments(compiled, {}) == {}

    def test_malformed_entry_is_omitted(self) -> None:
        """An entry that is not a mapping yields no argument."""
        compiled = compile_schema(
            object_schema(a={"type": "boolean", "description": "?", "x-laya-threshold": 0.5})
        )
        assert render_arguments(compiled, {"a": "not a mapping"}) == {}

    def test_string_probability_is_understood(self) -> None:
        """A backend returning "true" as a string still renders a boolean."""
        compiled = compile_schema(
            object_schema(a={"type": "boolean", "description": "?", "x-laya-threshold": 0.5})
        )
        assert render_arguments(compiled, {"a": {"noul": "true"}}) == {"a": True}

    def test_boolean_answer_is_understood(self) -> None:
        """A backend returning a genuine boolean still renders correctly."""
        compiled = compile_schema(
            object_schema(a={"type": "boolean", "description": "?", "x-laya-threshold": 0.5})
        )
        assert render_arguments(compiled, {"a": {"noul": True}}) == {"a": True}

    def test_output_is_json_serialisable(self) -> None:
        """Every rendered value must survive json.dumps."""
        import json

        compiled = compile_schema(
            object_schema(
                cat={"type": "string", "enum": ["a"], "description": "?"},
                n={"type": "number", "minimum": 0, "maximum": 2, "description": "?"},
                b={"type": "boolean", "description": "?", "x-laya-threshold": 0.5},
            )
        )
        arguments = render_arguments(
            compiled, {"cat": {"choice": "a"}, "n": {"score": 1.0}, "b": {"noul": 0.1}}
        )
        assert json.loads(json.dumps(arguments)) == arguments
