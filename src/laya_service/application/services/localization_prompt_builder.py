"""Translate a :class:`LocalizationSummary` into an English Laya prompt.

Laya is trained on English natural-language prompts; feeding it anything else
degrades accuracy unpredictably. This service is therefore the single place
where a domain value object becomes English prose, and it emits *only* English.

Keeping this as a separate application service (rather than inlining the
formatting into the use case) means the phrasing can be tuned, localised, or
A/B-tested without touching orchestration logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from laya_service.domain.value_objects.localization_summary import LocalizationSummary

__all__ = ["LocalizationPromptBuilder"]

#: Identifier of the boolean reliability question asked of the model.
RELIABILITY_QUESTION_ID: Final[str] = "location_reliable"

#: Identifier of the free-text remediation question asked of the model.
RECOMMENDATION_QUESTION_ID: Final[str] = "recommended_action"


class LocalizationPromptBuilder:
    """Builds English ``state``/``questions`` pairs from localization summaries.

    This class is stateless and holds no configuration, so it is safe to share
    across threads and requests.
    """

    #: Mapping from the machine-readable environment tag to the English phrase
    #: used in the prompt. Unknown tags fall back to a readable de-underscored
    #: form rather than failing, so a new tag never breaks the endpoint.
    _ENVIRONMENT_PHRASES: Final[Mapping[str, str]] = {
        "indoor_weak_gps": "indoors with weak GPS reception",
        "indoor_strong_gps": "indoors with strong GPS reception",
        "outdoor_open_sky": "outdoors under open sky",
        "outdoor_urban_canyon": "outdoors in an urban canyon with obstructed sky view",
        "underground": "underground with no GPS reception",
        "indoor": "indoors",
        "outdoor": "outdoors",
    }

    def build_state(self, summary: LocalizationSummary) -> Mapping[str, Any]:
        """Render a localization summary as an English observation.

        Args:
            summary: The validated localization summary.

        Returns:
            A ``{"observation": <english prose>}`` mapping ready to hand to the
            model. The prose is deliberately explicit about every numeric input
            so the model is never asked to infer a value it was given.
        """
        return {"observation": self.build_observation(summary)}

    def build_observation(self, summary: LocalizationSummary) -> str:
        """Compose the English observation sentence for a summary.

        Args:
            summary: The validated localization summary.

        Returns:
            A single English sentence describing the trajectory.
        """
        jump = f"{summary.x_jump_m:.2f}".rstrip("0").rstrip(".")
        y_phrase = "the y coordinate was stable" if summary.y_stable else "the y coordinate drifted"
        reversals = summary.heading_reversals
        reversal_phrase = (
            "the heading did not reverse"
            if reversals == 0
            else f"the heading reversed {reversals} time{'s' if reversals != 1 else ''}"
        )
        start = f"{summary.confidence_start.value:.2f}"
        end = f"{summary.confidence_end.value:.2f}"
        confidence_phrase = f"the positioning confidence changed from {start} to {end}"
        if summary.is_degrading:
            confidence_phrase += " (a decrease)"
        elif summary.confidence_delta > 0:
            confidence_phrase += " (an increase)"
        else:
            confidence_phrase += " (unchanged)"
        environment = self._describe_environment(summary.environment)

        return (
            f"Over the recent observation window, the x coordinate jumped "
            f"discontinuously by about {jump} meters, {y_phrase}, {reversal_phrase}, "
            f"and {confidence_phrase}. The robot is {environment}."
        )

    def build_questions(self) -> Mapping[str, Any]:
        """Build the English question set asked of the model.

        Returns:
            A mapping of question id to specification. Both questions use Laya's
            ``noul`` (boolean) type, which is the only type that yields a
            directly usable probability for a binary decision.
        """
        return {
            RELIABILITY_QUESTION_ID: {
                "type": "noul",
                "instructions": "Is the current localization estimate reliable?",
            },
            RECOMMENDATION_QUESTION_ID: {
                "type": "noul",
                "instructions": (
                    "Should the robot switch to visual-inertial odometry because "
                    "the current localization estimate is unreliable?"
                ),
            },
        }

    def _describe_environment(self, environment: str) -> str:
        """Map an environment tag to an English phrase.

        Args:
            environment: The machine-readable tag, e.g. ``"indoor_weak_gps"``.

        Returns:
            A human-readable English phrase. Unknown tags are de-underscored and
            lower-cased rather than rejected.
        """
        known = self._ENVIRONMENT_PHRASES.get(environment)
        if known is not None:
            return known
        return environment.replace("_", " ").strip().lower() or "in an unspecified environment"
