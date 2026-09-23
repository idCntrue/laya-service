"""The ``LocalizationSummary`` value object.

Describes the recent trajectory of a legged robot's pose estimate. The fields
mirror what a typical state estimator can emit without extra instrumentation:

* a discontinuous jump in the ``x`` coordinate,
* whether ``y`` stayed stable,
* how many times the heading reversed,
* the estimator's own confidence at the start and end of the window,
* a free-form environment tag.

The object validates its own coherence on construction. It has no notion of
"reliability" -- that judgement belongs to the model, not the domain.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from laya_service.domain.exceptions import InvalidLocalizationSummaryError
from laya_service.domain.value_objects.probability import Probability

__all__ = ["LocalizationSummary"]

#: Upper bound on plausible heading reversals in a short observation window.
#: Values beyond this are far more likely to be a sensor/units bug than a real
#: trajectory, so we reject them at the boundary.
MAX_HEADING_REVERSALS: Final[int] = 1000

#: Upper bound on a single discontinuous jump, in metres. A jump larger than
#: this within one window indicates corrupt input rather than a real manoeuvre.
MAX_JUMP_M: Final[float] = 1000.0


@dataclass(frozen=True, slots=True)
class LocalizationSummary:
    """An immutable, self-validating summary of recent localization behaviour.

    Attributes:
        x_jump_m: Magnitude of the discontinuous jump along ``x``, in metres.
            Must be finite and non-negative.
        y_stable: Whether the ``y`` coordinate remained stable over the window.
        heading_reversals: Number of heading reversals observed. Must be a
            non-negative integer no greater than :data:`MAX_HEADING_REVERSALS`.
        confidence_start: Estimator confidence at the start of the window.
        confidence_end: Estimator confidence at the end of the window.
        environment: Free-form environment tag, e.g. ``"indoor_weak_gps"``.
            Must be a non-empty string.
    """

    x_jump_m: float
    y_stable: bool
    heading_reversals: int
    confidence_start: Probability
    confidence_end: Probability
    environment: str

    def __post_init__(self) -> None:
        """Validate the coherence of the summary.

        Raises:
            InvalidLocalizationSummaryError: If any field is out of range or of
                the wrong type.
        """
        self._validate_jump()
        self._validate_reversals()
        self._validate_environment()
        self._validate_confidences()

    def _validate_jump(self) -> None:
        """Validate ``x_jump_m``."""
        if isinstance(self.x_jump_m, bool) or not isinstance(self.x_jump_m, (int, float)):
            raise InvalidLocalizationSummaryError(
                f"x_jump_m must be a real number, got {type(self.x_jump_m).__name__}"
            )
        jump = float(self.x_jump_m)
        if not math.isfinite(jump):
            raise InvalidLocalizationSummaryError(f"x_jump_m must be finite, got {self.x_jump_m!r}")
        if jump < 0.0:
            raise InvalidLocalizationSummaryError(f"x_jump_m must be non-negative, got {jump!r}")
        if jump > MAX_JUMP_M:
            raise InvalidLocalizationSummaryError(
                f"x_jump_m must not exceed {MAX_JUMP_M} metres, got {jump!r}"
            )
        object.__setattr__(self, "x_jump_m", jump)

    def _validate_reversals(self) -> None:
        """Validate ``heading_reversals``."""
        if isinstance(self.heading_reversals, bool) or not isinstance(self.heading_reversals, int):
            raise InvalidLocalizationSummaryError(
                f"heading_reversals must be an integer, got {type(self.heading_reversals).__name__}"
            )
        if self.heading_reversals < 0:
            raise InvalidLocalizationSummaryError(
                f"heading_reversals must be non-negative, got {self.heading_reversals!r}"
            )
        if self.heading_reversals > MAX_HEADING_REVERSALS:
            raise InvalidLocalizationSummaryError(
                f"heading_reversals must not exceed {MAX_HEADING_REVERSALS}, "
                f"got {self.heading_reversals!r}"
            )

    def _validate_environment(self) -> None:
        """Validate ``environment``."""
        if not isinstance(self.environment, str):
            raise InvalidLocalizationSummaryError(
                f"environment must be a string, got {type(self.environment).__name__}"
            )
        if not self.environment.strip():
            raise InvalidLocalizationSummaryError("environment must be a non-empty string")

    def _validate_confidences(self) -> None:
        """Validate that both confidences are ``Probability`` instances."""
        for name in ("confidence_start", "confidence_end"):
            value = getattr(self, name)
            if not isinstance(value, Probability):
                raise InvalidLocalizationSummaryError(
                    f"{name} must be a Probability, got {type(value).__name__}"
                )

    @property
    def confidence_delta(self) -> float:
        """Signed change in confidence across the window.

        Returns:
            ``confidence_end - confidence_start``. Negative means the estimator
            lost confidence over the observation window.
        """
        return self.confidence_end.value - self.confidence_start.value

    @property
    def is_degrading(self) -> bool:
        """Whether estimator confidence fell across the window."""
        return self.confidence_delta < 0.0
