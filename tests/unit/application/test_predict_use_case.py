"""Tests for the generic prediction use case.

Every test here runs against :class:`FakeDecisionModel`, so the whole file
executes in milliseconds. No model weights, no network, no GPU -- which is the
point of depending on a port rather than on Laya.
"""

from __future__ import annotations

import pytest

from laya_service.application.dto.predict_dto import PredictInput, PredictOutput
from laya_service.application.use_cases.predict_decision import PredictDecisionUseCase
from laya_service.domain.exceptions import (
    InvalidQuestionError,
    ModelInferenceError,
)
from laya_service.domain.ports.decision_model import DecisionModel
from tests.conftest import FakeDecisionModel

VALID_QUESTIONS = {
    "location_reliable": {
        "type": "noul",
        "instructions": "Is the current localization reliable?",
    }
}


class TestPortConformance:
    """The fake satisfies the port, and the port is usable structurally."""

    def test_fake_satisfies_the_port(self) -> None:
        """``FakeDecisionModel`` is structurally a ``DecisionModel``."""
        assert isinstance(FakeDecisionModel(), DecisionModel)


class TestPredictInputValidation:
    """DTO-level validation."""

    def test_rejects_empty_questions(self) -> None:
        """A request with no questions is rejected."""
        with pytest.raises(InvalidQuestionError, match="at least one question"):
            PredictInput(state={}, questions={})

    def test_rejects_question_without_type(self) -> None:
        """A question without a type is rejected."""
        with pytest.raises(InvalidQuestionError, match="missing a 'type'"):
            PredictInput(state={}, questions={"q": {"instructions": "?"}})

    def test_rejects_question_without_instructions(self) -> None:
        """A question without instructions is rejected."""
        with pytest.raises(InvalidQuestionError, match="instructions"):
            PredictInput(state={}, questions={"q": {"type": "noul"}})

    def test_rejects_blank_instructions(self) -> None:
        """Whitespace-only instructions do not count."""
        with pytest.raises(InvalidQuestionError, match="instructions"):
            PredictInput(state={}, questions={"q": {"type": "noul", "instructions": "   "}})

    def test_rejects_non_mapping_question(self) -> None:
        """A question must be a mapping."""
        with pytest.raises(InvalidQuestionError, match="must be a mapping"):
            PredictInput(state={}, questions={"q": "noul"})

    def test_accepts_a_well_formed_request(self) -> None:
        """A valid request constructs."""
        assert PredictInput(state={"observation": "x"}, questions=VALID_QUESTIONS).questions


class TestExecute:
    """The happy path."""

    def test_returns_answers_for_every_question(self) -> None:
        """Every asked question gets an answer."""
        model = FakeDecisionModel()
        result = PredictDecisionUseCase(model).execute(
            PredictInput(state={"observation": "x"}, questions=VALID_QUESTIONS)
        )
        assert set(result.answers) == {"location_reliable"}

    def test_answer_carries_a_probability_of_true(self) -> None:
        """A ``noul`` answer is the probability of true, as Laya returns it."""
        model = FakeDecisionModel(probability_true=0.25)
        result = PredictDecisionUseCase(model).execute(
            PredictInput(state={"observation": "x"}, questions=VALID_QUESTIONS)
        )
        answer = result.answers["location_reliable"]
        assert answer["noul"] == pytest.approx(0.25)
        # No explicit confidence given, so the fake derives it from how far the
        # answer sits from the decision boundary.
        assert answer["confidence"] == pytest.approx(0.75)

    def test_output_is_a_predict_output(self) -> None:
        """The use case returns its declared DTO type."""
        result = PredictDecisionUseCase(FakeDecisionModel()).execute(
            PredictInput(state={}, questions=VALID_QUESTIONS)
        )
        assert isinstance(result, PredictOutput)

    def test_forwards_state_and_questions_to_the_model(self) -> None:
        """The port receives exactly what the caller supplied."""
        model = FakeDecisionModel()
        state = {"observation": "the robot is indoors"}
        PredictDecisionUseCase(model).execute(PredictInput(state=state, questions=VALID_QUESTIONS))
        assert model.calls[0][0] == state
        assert model.calls[0][1] == VALID_QUESTIONS

    def test_calls_the_model_exactly_once(self) -> None:
        """One use-case invocation is one port call -- no accidental retries."""
        model = FakeDecisionModel()
        PredictDecisionUseCase(model).execute(PredictInput(state={}, questions=VALID_QUESTIONS))
        assert len(model.calls) == 1

    def test_propagates_backend_and_model_name(self) -> None:
        """Provenance metadata comes from the model, not from the use case."""
        result = PredictDecisionUseCase(FakeDecisionModel()).execute(
            PredictInput(state={}, questions=VALID_QUESTIONS)
        )
        assert result.backend == "fake"
        assert result.model_name == "fake-model-v1"

    def test_answers_are_a_plain_dict(self) -> None:
        """The output is a dict, not a live view of the model's internals."""
        result = PredictDecisionUseCase(FakeDecisionModel()).execute(
            PredictInput(state={}, questions=VALID_QUESTIONS)
        )
        assert isinstance(result.answers, dict)


class TestErrorPropagation:
    """Adapter failures reach the caller unchanged."""

    def test_propagates_inference_errors(self) -> None:
        """An inference failure is not swallowed."""
        model = FakeDecisionModel()
        model.raise_on_predict = ModelInferenceError("boom")
        with pytest.raises(ModelInferenceError):
            PredictDecisionUseCase(model).execute(PredictInput(state={}, questions=VALID_QUESTIONS))


class TestMultipleQuestions:
    """Several questions in one call."""

    def test_answers_all_questions(self) -> None:
        """A multi-question request answers each one."""
        questions = {
            "q1": {"type": "noul", "instructions": "first?"},
            "q2": {"type": "noul", "instructions": "second?"},
            "q3": {"type": "noul", "instructions": "third?"},
        }
        result = PredictDecisionUseCase(FakeDecisionModel()).execute(
            PredictInput(state={}, questions=questions)
        )
        assert set(result.answers) == {"q1", "q2", "q3"}
