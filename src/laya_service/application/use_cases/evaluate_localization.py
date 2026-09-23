"""The robot-dog localization-reliability use case.

Combines the decision-model port with the prompt builder to turn a structured
localization summary into a reliability judgement plus a machine-readable
remediation hint.

**A note on Laya's ``noul`` answers.** A ``noul`` question does not return a
boolean. Laya returns the *probability that the answer is "true"* as a float:

    {"type": "noul", "noul": 0.87, "confidence": 0.87}

So "reliable" is not read out of the response -- it is *derived* by comparing
that probability against a decision threshold. That threshold is a business
policy, which is exactly why it lives here in the application layer rather than
in the route or the adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from laya_service.application.dto.robot_dog_dto import LocalizationInput, LocalizationOutput
from laya_service.application.services.localization_prompt_builder import (
    RELIABILITY_QUESTION_ID,
    LocalizationPromptBuilder,
)
from laya_service.domain.ports.decision_model import DecisionModel
from laya_service.domain.value_objects.probability import Probability
from laya_service.logging_config import get_logger

__all__ = ["EvaluateLocalizationUseCase"]

_logger = get_logger(__name__, component="use_case")

#: Probability of "true" at or above which a ``noul`` answer counts as true.
DECISION_THRESHOLD: Final[float] = 0.5

#: Recommendation emitted when the model judges localization unreliable.
RECOMMENDATION_SWITCH_TO_VISUAL_IMU: Final[str] = "switch_to_visual_imu"

#: Recommendation emitted when the model judges localization reliable.
RECOMMENDATION_CONTINUE_WITH_CURRENT_ESTIMATOR: Final[str] = "continue_with_current_estimator"

#: Fallback recommendation used when the model's answer cannot be interpreted
#: or did not clear the caller's confidence bar. Failing toward the conservative
#: action is deliberate: a robot that unnecessarily falls back to visual-inertial
#: odometry is inconvenienced, whereas one that trusts a bad pose estimate can
#: collide with something.
RECOMMENDATION_FALLBACK: Final[str] = RECOMMENDATION_SWITCH_TO_VISUAL_IMU


class EvaluateLocalizationUseCase:
    """Judge whether a robot's localization estimate can be trusted."""

    def __init__(self, model: DecisionModel, prompt_builder: LocalizationPromptBuilder) -> None:
        """Initialize the use case.

        Args:
            model: Any object satisfying the :class:`DecisionModel` port.
            prompt_builder: Service that renders English prompts from summaries.
        """
        self._model = model
        self._prompt_builder = prompt_builder

    def execute(self, input: LocalizationInput) -> LocalizationOutput:
        """Evaluate localization reliability for one summary.

        Args:
            input: The localization summary plus the caller's confidence bar.

        Returns:
            A :class:`LocalizationOutput` with the boolean judgement, the
            model's confidence in it, and a recommended next action.

        Raises:
            ModelLoadError: Propagated from the adapter when the model is not
                available.
            ModelInferenceError: Propagated from the adapter on inference failure.
        """
        state = self._prompt_builder.build_state(input.summary)
        questions = self._prompt_builder.build_questions()

        raw: Mapping[str, Any] = self._model.predict(state=state, questions=questions)

        probability_true = self._extract_probability(raw, RELIABILITY_QUESTION_ID)
        reliable = probability_true.value >= DECISION_THRESHOLD

        # The model's own confidence in its answer -- not the answer itself.
        # Laya reports it separately, and it is the thing that should be
        # compared against the caller's threshold.
        confidence = self._extract_confidence(raw, RELIABILITY_QUESTION_ID, probability_true)
        confident = confidence.is_confident(input.confidence_threshold)
        recommendation = self._recommend(raw, reliable, confident)

        return LocalizationOutput(
            reliable=reliable,
            reliability=confidence,
            recommendation=recommendation,
            confident=confident,
            model_name=self._model.model_name,
            backend=self._model.backend,
        )

    @staticmethod
    def _extract_probability(raw: Mapping[str, Any], question_id: str) -> Probability:
        """Read the probability-of-true that Laya returned for a ``noul`` question.

        Args:
            raw: The answer mapping returned by the model port.
            question_id: Which question to read.

        Returns:
            A clamped :class:`Probability`. Missing or malformed entries yield
            ``0.0``, which reads downstream as "not reliable, not confident".
        """
        entry = raw.get(question_id)
        if not isinstance(entry, Mapping):
            return Probability(0.0)

        # A ``noul`` answer is normally a float probability. Accept a genuine
        # boolean too, in case a future backend returns one.
        answer = entry.get("noul", entry.get("value"))
        if isinstance(answer, bool):
            return Probability(1.0 if answer else 0.0)
        if isinstance(answer, (int, float)):
            return Probability(min(1.0, max(0.0, float(answer))))
        if isinstance(answer, str):
            lowered = answer.strip().lower()
            if lowered in {"true", "yes", "1"}:
                return Probability(1.0)
            if lowered in {"false", "no", "0"}:
                return Probability(0.0)
        return Probability(0.0)

    @staticmethod
    def _extract_confidence(
        raw: Mapping[str, Any],
        question_id: str,
        fallback: Probability,
    ) -> Probability:
        """Read Laya's calibrated confidence for an answer.

        Laya reports ``confidence`` alongside the answer, already calibrated by
        its own temperature scaling. When it is absent -- an older response
        shape, or a backend that does not emit it -- the distance of the answer
        from the decision boundary is used instead: an answer at 0.9 or 0.1 is
        more decisive than one at 0.51.

        Args:
            raw: The answer mapping returned by the model port.
            question_id: Which question to read.
            fallback: The probability to derive a fallback confidence from.

        Returns:
            A clamped :class:`Probability`.
        """
        entry = raw.get(question_id)
        if not isinstance(entry, Mapping):
            # No answer at all. There is no information to be confident about,
            # so report zero confidence rather than deriving one from the
            # placeholder probability -- `max(0.0, 1.0 - 0.0)` would claim
            # maximum confidence in an answer that was never given.
            return Probability(0.0)

        for key in ("confidence", "p", "probability", "prob"):
            candidate = entry.get(key)
            if isinstance(candidate, bool):
                continue
            if isinstance(candidate, (int, float)):
                return Probability(min(1.0, max(0.0, float(candidate))))

        # No reported confidence, but an answer was given: use how far it sits
        # from the 0.5 decision boundary as a proxy for decisiveness.
        return Probability(max(fallback.value, 1.0 - fallback.value))

    @staticmethod
    def _recommend(
        raw: Mapping[str, Any],
        reliable: bool,
        confident: bool,
    ) -> str:
        """Choose a machine-readable recommendation.

        Args:
            raw: The answer mapping returned by the model port.
            reliable: The judgement derived from the ``noul`` probability.
            confident: Whether the judgement cleared the caller's threshold.

        Returns:
            One of :data:`RECOMMENDATION_SWITCH_TO_VISUAL_IMU`,
            :data:`RECOMMENDATION_CONTINUE_WITH_CURRENT_ESTIMATOR`, or
            :data:`RECOMMENDATION_FALLBACK` when the model was not confident
            enough to act on.
        """
        if not confident:
            return RECOMMENDATION_FALLBACK
        if not reliable:
            return RECOMMENDATION_SWITCH_TO_VISUAL_IMU
        # The reliability verdict is authoritative, and the answer to
        # RECOMMENDATION_QUESTION_ID is deliberately not consulted here. That
        # question ("should we switch because localization is unreliable?") is
        # conditional on the estimate being unreliable, so when the verdict is
        # "reliable" its answer is not meaningful -- and a model that answers
        # every question affirmatively would otherwise be able to talk the
        # caller out of a trustworthy pose estimate, which is the wrong
        # direction to fail in. It is still asked because Laya evaluates all
        # questions in a single parallel forward pass, so it costs nothing and
        # its answer is available to callers of the generic /v1/predict route.
        return RECOMMENDATION_CONTINUE_WITH_CURRENT_ESTIMATOR
