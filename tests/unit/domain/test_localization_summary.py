"""Tests for the ``LocalizationSummary`` value object."""

from __future__ import annotations

import math

import pytest

from laya_service.domain.exceptions import InvalidLocalizationSummaryError
from laya_service.domain.value_objects.localization_summary import LocalizationSummary
from laya_service.domain.value_objects.probability import Probability


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


class TestConstruction:
    """Happy path."""

    def test_accepts_a_coherent_summary(self) -> None:
        """A well-formed summary constructs without error."""
        summary = make_summary()
        assert summary.x_jump_m == pytest.approx(2.3)
        assert summary.y_stable is True
        assert summary.heading_reversals == 3
        assert summary.environment == "indoor_weak_gps"

    def test_normalises_int_jump_to_float(self) -> None:
        """An integer jump is stored as a float."""
        assert isinstance(make_summary(x_jump_m=3).x_jump_m, float)

    def test_accepts_zero_jump(self) -> None:
        """A zero jump is valid -- it means the estimate was continuous."""
        assert make_summary(x_jump_m=0.0).x_jump_m == 0.0

    def test_accepts_zero_reversals(self) -> None:
        """Zero reversals is valid."""
        assert make_summary(heading_reversals=0).heading_reversals == 0


class TestJumpValidation:
    """Validation of ``x_jump_m``."""

    def test_rejects_negative_jump(self) -> None:
        """A negative magnitude is a units or sign bug, not a real trajectory."""
        with pytest.raises(InvalidLocalizationSummaryError, match="non-negative"):
            make_summary(x_jump_m=-1.0)

    def test_rejects_nan(self) -> None:
        """NaN is rejected."""
        with pytest.raises(InvalidLocalizationSummaryError, match="finite"):
            make_summary(x_jump_m=math.nan)

    def test_rejects_infinity(self) -> None:
        """Infinity is rejected."""
        with pytest.raises(InvalidLocalizationSummaryError, match="finite"):
            make_summary(x_jump_m=math.inf)

    def test_rejects_implausibly_large_jump(self) -> None:
        """A jump beyond the ceiling indicates corrupt input."""
        with pytest.raises(InvalidLocalizationSummaryError, match="exceed"):
            make_summary(x_jump_m=10_000.0)

    def test_rejects_bool_jump(self) -> None:
        """A boolean is not a distance."""
        with pytest.raises(InvalidLocalizationSummaryError, match="real number"):
            make_summary(x_jump_m=True)

    def test_rejects_string_jump(self) -> None:
        """A string is not a distance."""
        with pytest.raises(InvalidLocalizationSummaryError, match="real number"):
            make_summary(x_jump_m="2.3")


class TestReversalValidation:
    """Validation of ``heading_reversals``."""

    def test_rejects_negative_reversals(self) -> None:
        """A count cannot be negative."""
        with pytest.raises(InvalidLocalizationSummaryError, match="non-negative"):
            make_summary(heading_reversals=-1)

    def test_rejects_implausibly_large_count(self) -> None:
        """A count beyond the ceiling indicates corrupt input."""
        with pytest.raises(InvalidLocalizationSummaryError, match="exceed"):
            make_summary(heading_reversals=10_000)

    def test_rejects_float_reversals(self) -> None:
        """A count must be an integer."""
        with pytest.raises(InvalidLocalizationSummaryError, match="integer"):
            make_summary(heading_reversals=2.5)

    def test_rejects_bool_reversals(self) -> None:
        """A boolean is not a count."""
        with pytest.raises(InvalidLocalizationSummaryError, match="integer"):
            make_summary(heading_reversals=True)


class TestConfidenceValidation:
    """Validation of the confidence pair."""

    def test_rejects_raw_float_for_start(self) -> None:
        """Confidences must be ``Probability`` objects, not bare floats."""
        with pytest.raises(InvalidLocalizationSummaryError, match="confidence_start"):
            make_summary(confidence_start=0.9)

    def test_rejects_raw_float_for_end(self) -> None:
        """The end confidence is validated too."""
        with pytest.raises(InvalidLocalizationSummaryError, match="confidence_end"):
            make_summary(confidence_end=0.4)


class TestEnvironmentValidation:
    """Validation of ``environment``."""

    def test_rejects_empty_string(self) -> None:
        """An empty environment tag is not useful."""
        with pytest.raises(InvalidLocalizationSummaryError, match="non-empty"):
            make_summary(environment="")

    def test_rejects_whitespace_only(self) -> None:
        """A whitespace-only tag is treated as empty."""
        with pytest.raises(InvalidLocalizationSummaryError, match="non-empty"):
            make_summary(environment="   ")

    def test_rejects_non_string(self) -> None:
        """A non-string tag is rejected."""
        with pytest.raises(InvalidLocalizationSummaryError, match="string"):
            make_summary(environment=42)


class TestDerivedProperties:
    """Computed properties."""

    def test_confidence_delta_is_signed(self) -> None:
        """The delta is ``end - start`` and can be negative."""
        summary = make_summary(confidence_start=Probability(0.9), confidence_end=Probability(0.4))
        assert summary.confidence_delta == pytest.approx(-0.5)

    def test_positive_delta(self) -> None:
        """A rising confidence yields a positive delta."""
        summary = make_summary(confidence_start=Probability(0.2), confidence_end=Probability(0.8))
        assert summary.confidence_delta == pytest.approx(0.6)

    def test_is_degrading_true(self) -> None:
        """Falling confidence is degrading."""
        summary = make_summary(confidence_start=Probability(0.9), confidence_end=Probability(0.4))
        assert summary.is_degrading is True

    def test_is_degrading_false_when_improving(self) -> None:
        """Rising confidence is not degrading."""
        summary = make_summary(confidence_start=Probability(0.4), confidence_end=Probability(0.9))
        assert summary.is_degrading is False

    def test_is_degrading_false_when_flat(self) -> None:
        """Unchanged confidence is not degrading."""
        summary = make_summary(confidence_start=Probability(0.5), confidence_end=Probability(0.5))
        assert summary.is_degrading is False


class TestImmutability:
    """The object is frozen."""

    def test_cannot_mutate(self) -> None:
        """Assigning to a field raises."""
        summary = make_summary()
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            summary.x_jump_m = 99.0  # type: ignore[misc]
