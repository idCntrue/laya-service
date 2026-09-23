"""Tests for the localization-reliability use case.

The interesting behaviour is in the branches, and in keeping Laya's two
per-answer quantities straight:

* ``noul`` is the **probability that the answer is true**, which the use case
  thresholds to derive the ``reliable`` boolean.
* ``confidence`` is the model's confidence **in its verdict**, which is what the
  caller's threshold is compared against to derive ``confident``.

A model can be 95% confident that the answer is false --
``probability_true=0.05, confidence=0.95``.
"""

from __future__ import annotations

import pytest

from laya_service.application.dto.robot_dog_dto import LocalizationInput, LocalizationOutput
from laya_service.application.services.localization_prompt_builder import (
    LocalizationPromptBuilder,
)
from laya_service.application.use_cases.evaluate_localization import (
    RECOMMENDATION_CONTINUE_WITH_CURRENT_ESTIMATOR,
    RECOMMENDATION_SWITCH_TO_VISUAL_IMU,
    EvaluateLocalizationUseCase,
)
from laya_service.domain.exceptions import ModelInferenceError
from laya_service.domain.value_objects.localization_summary import LocalizationSummary
from laya_service.domain.value_objects.probability import Probability
from tests.conftest import FakeDecisionModel


def make_summary(**overrides: object) -> LocalizationSummary:
    """Build a valid summary, applying overrides.

    Args:
        **overrides: Field values to replace.

    Returns:
        A :class:`LocalizationSummary`.
    """
    defaults: dict[str, object] = {
        "x_jump_m": 2.3,
        "y_stable": True,
        "heading_reversals": 3,
        "confidence_start": Probability(0.9),
        "confidence_end": Probability(0.4),
        "environment": "indoor_weak_gps",
    }
    defaults.update(overrides)
    return LocalizationSummary(**defaults)  # type: ignore[arg-type]


def make_use_case(model: FakeDecisionModel) -> EvaluateLocalizationUseCase:
    """Wire a use case around a fake model.

    Args:
        model: The fake to inject.

    Returns:
        A configured :class:`EvaluateLocalizationUseCase`.
    """
    return EvaluateLocalizationUseCase(model=model, prompt_builder=LocalizationPromptBuilder())


class TestConfidentBranches:
    """The model is confident and the answer is actionable."""

    def test_confident_reliable_recommends_continuing(self) -> None:
        """A confident 'reliable' keeps the current estimator."""
        model = FakeDecisionModel(probability_true=0.95, confidence=0.95)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is True
        assert result.confident is True
        assert result.recommendation == RECOMMENDATION_CONTINUE_WITH_CURRENT_ESTIMATOR

    def test_confident_unreliable_recommends_switching(self) -> None:
        """A confident 'unreliable' switches to visual-inertial odometry."""
        model = FakeDecisionModel(probability_true=0.05, confidence=0.95)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is False
        assert result.confident is True
        assert result.recommendation == RECOMMENDATION_SWITCH_TO_VISUAL_IMU

    def test_reliability_is_the_confidence_not_the_answer(self) -> None:
        """``reliability`` reports confidence in the verdict, not the raw answer."""
        model = FakeDecisionModel(probability_true=0.05, confidence=0.93)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.reliability.value == pytest.approx(0.93)


class TestDecisionThreshold:
    """Deriving ``reliable`` from the probability of true."""

    def test_exactly_at_the_boundary_counts_as_reliable(self) -> None:
        """A probability of exactly 0.5 is treated as reliable (>=)."""
        model = FakeDecisionModel(probability_true=0.5, confidence=0.9)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is True

    def test_just_below_the_boundary_is_unreliable(self) -> None:
        """A probability just under 0.5 is unreliable."""
        model = FakeDecisionModel(probability_true=0.49, confidence=0.9)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is False


class TestUnconfidentBranch:
    """The model is unsure, so the safe fallback must win."""

    def test_low_confidence_falls_back_to_switching(self) -> None:
        """An unconfident answer recommends the conservative action."""
        model = FakeDecisionModel(probability_true=0.9, confidence=0.2)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.confident is False
        assert result.recommendation == RECOMMENDATION_SWITCH_TO_VISUAL_IMU

    def test_low_confidence_overrides_a_reliable_verdict(self) -> None:
        """A 'reliable' verdict that is not confident still falls back."""
        model = FakeDecisionModel(probability_true=0.9, confidence=0.1)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is True
        assert result.confident is False
        assert result.recommendation == RECOMMENDATION_SWITCH_TO_VISUAL_IMU


class TestThresholdBehaviour:
    """The caller-supplied threshold controls the confident flag."""

    def test_threshold_boundary_is_inclusive(self) -> None:
        """A confidence exactly at the threshold is confident."""
        model = FakeDecisionModel(probability_true=0.9, confidence=0.5)
        result = make_use_case(model).execute(
            LocalizationInput(summary=make_summary(), confidence_threshold=0.5)
        )
        assert result.confident is True

    def test_just_below_threshold_is_not_confident(self) -> None:
        """A confidence just under the threshold is not confident."""
        model = FakeDecisionModel(probability_true=0.9, confidence=0.49)
        result = make_use_case(model).execute(
            LocalizationInput(summary=make_summary(), confidence_threshold=0.5)
        )
        assert result.confident is False

    def test_high_threshold_downgrades_a_confident_answer(self) -> None:
        """Raising the bar can make an otherwise-confident answer advisory."""
        model = FakeDecisionModel(probability_true=0.9, confidence=0.8)
        result = make_use_case(model).execute(
            LocalizationInput(summary=make_summary(), confidence_threshold=0.99)
        )
        assert result.confident is False
        assert result.recommendation == RECOMMENDATION_SWITCH_TO_VISUAL_IMU

    def test_zero_threshold_accepts_anything(self) -> None:
        """A zero threshold accepts even a zero-confidence answer."""
        model = FakeDecisionModel(probability_true=0.9, confidence=0.0)
        result = make_use_case(model).execute(
            LocalizationInput(summary=make_summary(), confidence_threshold=0.0)
        )
        assert result.confident is True


class TestOutputShape:
    """The returned DTO is complete and well-typed."""

    def test_returns_localization_output(self) -> None:
        """The use case returns its declared DTO type."""
        result = make_use_case(FakeDecisionModel()).execute(
            LocalizationInput(summary=make_summary())
        )
        assert isinstance(result, LocalizationOutput)

    def test_reliability_is_a_probability(self) -> None:
        """Reliability is the value object, not a bare float."""
        result = make_use_case(FakeDecisionModel()).execute(
            LocalizationInput(summary=make_summary())
        )
        assert isinstance(result.reliability, Probability)

    def test_carries_provenance(self) -> None:
        """Backend and model identifiers come from the model port."""
        result = make_use_case(FakeDecisionModel()).execute(
            LocalizationInput(summary=make_summary())
        )
        assert result.backend == "fake"
        assert result.model_name == "fake-model-v1"


class TestPromptWiring:
    """The use case drives the model through the prompt builder."""

    def test_sends_an_english_observation(self) -> None:
        """The state handed to the model is the built English observation."""
        model = FakeDecisionModel()
        make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        state = model.calls[0][0]
        assert "observation" in state
        assert state["observation"].isascii()

    def test_asks_the_boolean_questions(self) -> None:
        """Both noul questions are asked."""
        model = FakeDecisionModel()
        make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        questions = model.calls[0][1]
        assert set(questions) == {"location_reliable", "recommended_action"}
        assert all(spec["type"] == "noul" for spec in questions.values())


class TestRobustness:
    """Malformed model responses degrade rather than raise."""

    def test_missing_answer_is_treated_as_unreliable(self) -> None:
        """A response lacking the expected question yields an unconfident result."""

        class EmptyModel(FakeDecisionModel):
            def predict(self, state, questions):  # type: ignore[no-untyped-def]
                self.calls.append((state, questions))
                return {}

        result = make_use_case(EmptyModel()).execute(LocalizationInput(summary=make_summary()))
        assert result.confident is False
        assert result.reliable is False
        assert result.reliability.value == pytest.approx(0.0)

    def test_missing_confidence_falls_back_to_answer_distance(self) -> None:
        """Without a reported confidence, the answer's decisiveness is used."""

        class NoConfidenceModel(FakeDecisionModel):
            def predict(self, state, questions):  # type: ignore[no-untyped-def]
                self.calls.append((state, questions))
                return {qid: {"type": "noul", "noul": 0.92} for qid in questions}

        result = make_use_case(NoConfidenceModel()).execute(
            LocalizationInput(summary=make_summary())
        )
        assert result.reliable is True
        assert result.reliability.value == pytest.approx(0.92)

    def test_out_of_range_probability_is_clamped(self) -> None:
        """A backend reporting noul=1.5 is clamped rather than raising."""

        class BadProbModel(FakeDecisionModel):
            def predict(self, state, questions):  # type: ignore[no-untyped-def]
                self.calls.append((state, questions))
                return {qid: {"type": "noul", "noul": 1.5, "confidence": 0.9} for qid in questions}

        result = make_use_case(BadProbModel()).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is True
        assert result.reliability.value == pytest.approx(0.9)

    def test_boolean_answer_is_understood(self) -> None:
        """A backend returning a genuine boolean is still interpreted."""

        class BoolModel(FakeDecisionModel):
            def predict(self, state, questions):  # type: ignore[no-untyped-def]
                self.calls.append((state, questions))
                return {qid: {"type": "noul", "noul": True, "confidence": 0.9} for qid in questions}

        result = make_use_case(BoolModel()).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is True

    def test_propagates_inference_errors(self) -> None:
        """An adapter failure is not swallowed by the use case."""
        model = FakeDecisionModel()
        model.raise_on_predict = ModelInferenceError("boom")
        with pytest.raises(ModelInferenceError):
            make_use_case(model).execute(LocalizationInput(summary=make_summary()))

    def test_recommendation_question_cannot_override_a_reliable_verdict(self) -> None:
        """A model that answers every question 'true' must not flip the verdict.

        The recommendation question is conditional on the estimate being
        unreliable, so its answer is meaningless when the verdict is 'reliable'.
        Treating it as authoritative would let a degenerate model talk the
        caller out of a trustworthy pose estimate.
        """
        model = FakeDecisionModel(probability_true=0.95, confidence=0.95)
        result = make_use_case(model).execute(LocalizationInput(summary=make_summary()))
        assert result.reliable is True
        assert result.recommendation == RECOMMENDATION_CONTINUE_WITH_CURRENT_ESTIMATOR
