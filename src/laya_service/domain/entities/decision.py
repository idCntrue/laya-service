"""Domain entities describing a model decision and a full prediction.

An *entity* has identity and is compared by it; a *value object* is compared by
value. Here ``Decision`` is keyed by ``question_id`` and ``Prediction`` is an
immutable collection of decisions. Both are frozen, because a model's output is
a fact about a past moment -- mutating it in place would be a category error.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from laya_service.domain.exceptions import InvalidDecisionError
from laya_service.domain.value_objects.probability import Probability

__all__ = ["Decision", "Prediction"]

#: Question kinds Laya natively understands, mirrored from ``laya.QTYPES``.
#: Kept here so that both the application layer and the interface layer validate
#: against one source of truth rather than each inventing their own list.
#:
#: * ``noul``   -- boolean; answered with a probability of "true".
#: * ``choice`` -- single choice; requires a ``criteria`` mapping of option id
#:                 to description.
#: * ``score``  -- ordered rating; requires a ``criteria`` list, lowest first.
SUPPORTED_QUESTION_TYPES: frozenset[str] = frozenset({"noul", "choice", "score"})


@dataclass(frozen=True, slots=True)
class Decision:
    """A single answered question produced by the decision model.

    Attributes:
        question_id: Identifier of the question this decision answers.
        question_type: Laya question kind, one of :data:`SUPPORTED_QUESTION_TYPES`.
        value: The model's answer. Deliberately loosely typed because Laya
            returns a different shape per question type: a boolean for ``noul``,
            a string for ``mcq``, a list of strings for ``multi``, and a float
            for ``rng``. Narrowing it further here would leak Laya's specifics
            into the domain.
        probability: Model-reported confidence in ``value``.
    """

    question_id: str
    question_type: str
    value: Any
    probability: Probability

    def __post_init__(self) -> None:
        """Validate the decision's structural integrity.

        Raises:
            InvalidDecisionError: If the identifier is empty, the type is
                unsupported, or ``probability`` is not a ``Probability``.
        """
        if not isinstance(self.question_id, str) or not self.question_id.strip():
            raise InvalidDecisionError("question_id must be a non-empty string")
        if self.question_type not in SUPPORTED_QUESTION_TYPES:
            raise InvalidDecisionError(
                f"unsupported question_type {self.question_type!r}; "
                f"expected one of {sorted(SUPPORTED_QUESTION_TYPES)}"
            )
        if not isinstance(self.probability, Probability):
            raise InvalidDecisionError(
                f"probability must be a Probability, got {type(self.probability).__name__}"
            )

    def is_confident(self, threshold: float) -> bool:
        """Delegate the confidence check to the wrapped probability.

        Args:
            threshold: The confidence bar in ``[0.0, 1.0]``.

        Returns:
            ``True`` when the model's confidence meets the bar.
        """
        return self.probability.is_confident(threshold)


@dataclass(frozen=True, slots=True)
class Prediction:
    """An immutable, ordered collection of decisions for one request.

    Attributes:
        decisions: The decisions, in the order the questions were asked.
    """

    decisions: tuple[Decision, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Normalise ``decisions`` to a tuple and reject duplicates.

        Raises:
            InvalidDecisionError: If two decisions share a ``question_id``, or
                an element is not a ``Decision``.
        """
        normalised: list[Decision] = []
        for item in self.decisions:
            if not isinstance(item, Decision):
                raise InvalidDecisionError(
                    f"decisions must contain Decision instances, got {type(item).__name__}"
                )
            normalised.append(item)
        seen: set[str] = set()
        for item in normalised:
            if item.question_id in seen:
                raise InvalidDecisionError(
                    f"duplicate decision for question_id {item.question_id!r}"
                )
            seen.add(item.question_id)
        object.__setattr__(self, "decisions", tuple(normalised))

    def __len__(self) -> int:
        """Return the number of decisions."""
        return len(self.decisions)

    def __iter__(self):  # type: ignore[no-untyped-def]
        """Iterate over the decisions in order."""
        return iter(self.decisions)

    def by_id(self, question_id: str) -> Decision | None:
        """Look up a decision by its question identifier.

        Args:
            question_id: The identifier to search for.

        Returns:
            The matching decision, or ``None`` when absent.
        """
        for decision in self.decisions:
            if decision.question_id == question_id:
                return decision
        return None

    def to_answer_mapping(self) -> Mapping[str, Mapping[str, Any]]:
        """Render the prediction in Laya's wire shape.

        Returns:
            A mapping of ``question_id`` to ``{"type": ..., "<type>": value}``,
            matching the structure Laya itself returns so that callers can
            forward it unchanged.
        """
        return {
            decision.question_id: {
                "type": decision.question_type,
                decision.question_type: decision.value,
            }
            for decision in self.decisions
        }
