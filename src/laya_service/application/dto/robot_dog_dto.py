"""Data-transfer objects for the robot-dog localization use case."""

from __future__ import annotations

from dataclasses import dataclass

from laya_service.domain.value_objects.localization_summary import LocalizationSummary
from laya_service.domain.value_objects.probability import Probability

__all__ = ["LocalizationInput", "LocalizationOutput"]


@dataclass(frozen=True, slots=True)
class LocalizationInput:
    """Input to the localization-reliability use case.

    Attributes:
        summary: The validated summary of recent localization behaviour.
        confidence_threshold: The bar at or above which the model's answer is
            treated as actionable. Defaults to ``0.5`` -- see the README for why
            an uncalibrated zero-shot probability should not be trusted far from
            this value.
    """

    summary: LocalizationSummary
    confidence_threshold: float = 0.5


@dataclass(frozen=True, slots=True)
class LocalizationOutput:
    """Output of the localization-reliability use case.

    Attributes:
        reliable: The model's boolean judgement on localization reliability.
        reliability: The model's confidence in that judgement.
        recommendation: A machine-readable next action for the robot's
            controller, e.g. ``"switch_to_visual_imu"``.
        confident: Whether ``reliability`` cleared the caller's threshold. When
            ``False``, the caller should treat ``recommendation`` as advisory
            and fall back to a safe default.
        model_name: Identifier of the model that served the request.
        backend: Compute backend that served the request.
    """

    reliable: bool
    reliability: Probability
    recommendation: str
    confident: bool
    model_name: str = "unknown"
    backend: str = "unknown"
