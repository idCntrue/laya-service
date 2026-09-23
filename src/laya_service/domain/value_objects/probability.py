"""The ``Probability`` value object.

A value object is immutable and validates itself on construction. Once you hold
a ``Probability``, you know for a fact that its value lies in ``[0, 1]`` -- no
downstream code needs to re-check. This is the whole point: it moves validation
to the boundary and makes the invariant impossible to violate afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from laya_service.domain.exceptions import InvalidProbabilityError

__all__ = ["Probability"]


@dataclass(frozen=True, slots=True)
class Probability:
    """An immutable probability in the closed interval ``[0.0, 1.0]``.

    Attributes:
        value: The probability mass, guaranteed to satisfy ``0.0 <= value <= 1.0``.
    """

    value: float

    def __post_init__(self) -> None:
        """Validate the probability bounds.

        Raises:
            InvalidProbabilityError: If ``value`` is not a finite real number in
                the closed interval ``[0.0, 1.0]``.
        """
        # ``bool`` is a subclass of ``int``; reject it explicitly so that
        # ``Probability(True)`` cannot silently become ``1.0``.
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise InvalidProbabilityError(
                f"probability must be a real number, got {type(self.value).__name__}"
            )
        as_float = float(self.value)
        if not math.isfinite(as_float):
            raise InvalidProbabilityError(f"probability must be finite, got {self.value!r}")
        if not 0.0 <= as_float <= 1.0:
            raise InvalidProbabilityError(f"probability must lie in [0.0, 1.0], got {as_float!r}")
        # ``frozen=True`` still permits ``object.__setattr__`` here in
        # ``__post_init__``; normalise ints to floats for consistent JSON output.
        object.__setattr__(self, "value", as_float)

    def is_confident(self, threshold: float) -> bool:
        """Report whether this probability meets or exceeds a confidence bar.

        Args:
            threshold: The confidence bar, itself a probability in ``[0.0, 1.0]``.

        Returns:
            ``True`` when ``self.value >= threshold``.

        Raises:
            InvalidProbabilityError: If ``threshold`` is not in ``[0.0, 1.0]``.
        """
        # Reuse this class's own validation rather than duplicating the range check.
        bar = Probability(threshold).value
        return self.value >= bar

    def as_percent(self) -> float:
        """Return the probability expressed as a percentage in ``[0.0, 100.0]``."""
        return self.value * 100.0

    def __float__(self) -> float:
        """Allow ``float(probability)``."""
        return self.value
