"""Tests for the ``Decision`` and ``Prediction`` entities."""

from __future__ import annotations

import pytest

from laya_service.domain.entities.decision import Decision, Prediction
from laya_service.domain.exceptions import InvalidDecisionError
from laya_service.domain.value_objects.probability import Probability


def make_decision(**overrides: object) -> Decision:
    """Build a valid decision, applying overrides.

    Args:
        **overrides: Field values to replace.

    Returns:
        A :class:`Decision`.
    """
    defaults: dict[str, object] = {
        "question_id": "location_reliable",
        "question_type": "noul",
        "value": True,
        "probability": Probability(0.8),
    }
    defaults.update(overrides)
    return Decision(**defaults)  # type: ignore[arg-type]


class TestDecision:
    """Construction and validation of a single decision."""

    def test_constructs_valid_decision(self) -> None:
        """A well-formed decision constructs."""
        decision = make_decision()
        assert decision.question_id == "location_reliable"
        assert decision.value is True

    @pytest.mark.parametrize("question_id", ["", "   "])
    def test_rejects_empty_question_id(self, question_id: str) -> None:
        """An empty identifier is rejected."""
        with pytest.raises(InvalidDecisionError, match="non-empty"):
            make_decision(question_id=question_id)

    def test_rejects_unsupported_question_type(self) -> None:
        """An unknown question type is rejected."""
        with pytest.raises(InvalidDecisionError, match="unsupported question_type"):
            make_decision(question_type="nonsense")

    @pytest.mark.parametrize("question_type", ["noul", "choice", "score"])
    def test_accepts_every_supported_type(self, question_type: str) -> None:
        """All documented Laya question types are accepted."""
        assert make_decision(question_type=question_type).question_type == question_type

    def test_rejects_bare_float_probability(self) -> None:
        """Probability must be the value object, not a float."""
        with pytest.raises(InvalidDecisionError, match="Probability"):
            make_decision(probability=0.8)

    def test_is_confident_delegates_to_probability(self) -> None:
        """The confidence check uses the wrapped probability."""
        assert make_decision(probability=Probability(0.9)).is_confident(0.5) is True
        assert make_decision(probability=Probability(0.1)).is_confident(0.5) is False


class TestPrediction:
    """Construction and behaviour of a prediction."""

    def test_empty_prediction_is_valid(self) -> None:
        """An empty prediction is allowed."""
        assert len(Prediction()) == 0

    def test_holds_decisions_in_order(self) -> None:
        """Decisions keep their insertion order."""
        first = make_decision(question_id="a")
        second = make_decision(question_id="b")
        prediction = Prediction(decisions=(first, second))
        assert [d.question_id for d in prediction] == ["a", "b"]

    def test_normalises_list_to_tuple(self) -> None:
        """A list input is frozen into a tuple."""
        prediction = Prediction(decisions=[make_decision()])  # type: ignore[arg-type]
        assert isinstance(prediction.decisions, tuple)

    def test_rejects_duplicate_question_ids(self) -> None:
        """Two decisions for the same question is a bug, not a merge."""
        with pytest.raises(InvalidDecisionError, match="duplicate"):
            Prediction(decisions=(make_decision(question_id="a"), make_decision(question_id="a")))

    def test_rejects_non_decision_elements(self) -> None:
        """Non-decision elements are rejected."""
        with pytest.raises(InvalidDecisionError, match="Decision instances"):
            Prediction(decisions=("not a decision",))  # type: ignore[arg-type]

    def test_by_id_finds_a_decision(self) -> None:
        """Lookup by identifier works."""
        prediction = Prediction(decisions=(make_decision(question_id="a"),))
        assert prediction.by_id("a") is not None

    def test_by_id_returns_none_when_absent(self) -> None:
        """A missing identifier yields ``None``."""
        assert Prediction(decisions=(make_decision(question_id="a"),)).by_id("zzz") is None

    def test_to_answer_mapping_uses_laya_wire_shape(self) -> None:
        """The mapping matches Laya's own response shape."""
        prediction = Prediction(decisions=(make_decision(question_id="q", value=True),))
        assert prediction.to_answer_mapping() == {"q": {"type": "noul", "noul": True}}

    def test_to_answer_mapping_covers_every_decision(self) -> None:
        """Every decision appears in the mapping."""
        prediction = Prediction(
            decisions=(make_decision(question_id="a"), make_decision(question_id="b"))
        )
        assert set(prediction.to_answer_mapping()) == {"a", "b"}
