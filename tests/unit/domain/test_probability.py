"""Tests for the ``Probability`` value object."""

from __future__ import annotations

import math

import pytest

from laya_service.domain.exceptions import InvalidProbabilityError
from laya_service.domain.value_objects.probability import Probability


class TestConstruction:
    """Construction and bound validation."""

    @pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.999, 1.0, 0, 1])
    def test_accepts_values_in_closed_unit_interval(self, value: float) -> None:
        """Every value in [0, 1] is accepted."""
        assert Probability(value).value == pytest.approx(float(value))

    @pytest.mark.parametrize("value", [-0.001, -1.0, 1.001, 2.0, 100.0])
    def test_rejects_values_outside_unit_interval(self, value: float) -> None:
        """Values outside [0, 1] are rejected."""
        with pytest.raises(InvalidProbabilityError):
            Probability(value)

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_rejects_non_finite_values(self, value: float) -> None:
        """NaN and infinities are rejected rather than silently propagating."""
        with pytest.raises(InvalidProbabilityError):
            Probability(value)

    @pytest.mark.parametrize("value", ["0.5", None, [0.5], {"p": 0.5}])
    def test_rejects_non_numeric_values(self, value: object) -> None:
        """Non-numeric input is rejected with a clear error."""
        with pytest.raises(InvalidProbabilityError):
            # Deliberately passing the wrong type: this is the validation path.
            Probability(value)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        """``bool`` is an ``int`` subclass but must not silently become 1.0."""
        with pytest.raises(InvalidProbabilityError):
            Probability(True)

    def test_normalises_int_to_float(self) -> None:
        """Integer input is stored as a float so JSON output is consistent."""
        assert isinstance(Probability(1).value, float)


class TestImmutability:
    """The object is frozen."""

    def test_cannot_mutate_value(self) -> None:
        """Assigning to ``value`` raises."""
        probability = Probability(0.5)
        with pytest.raises(Exception):  # noqa: B017 - dataclasses raise FrozenInstanceError
            probability.value = 0.9  # type: ignore[misc]

    def test_equality_is_by_value(self) -> None:
        """Two probabilities with the same value are equal."""
        assert Probability(0.5) == Probability(0.5)
        assert Probability(0.5) != Probability(0.6)

    def test_is_hashable(self) -> None:
        """Frozen value objects can be used as dict keys."""
        assert {Probability(0.5), Probability(0.5)} == {Probability(0.5)}


class TestIsConfident:
    """The confidence comparison."""

    def test_meets_threshold_exactly(self) -> None:
        """A value exactly at the threshold counts as confident."""
        assert Probability(0.5).is_confident(0.5) is True

    def test_above_threshold(self) -> None:
        """A value above the threshold is confident."""
        assert Probability(0.9).is_confident(0.5) is True

    def test_below_threshold(self) -> None:
        """A value below the threshold is not confident."""
        assert Probability(0.1).is_confident(0.5) is False

    @pytest.mark.parametrize("threshold", [-0.1, 1.1, 2.0])
    def test_rejects_out_of_range_threshold(self, threshold: float) -> None:
        """A threshold outside [0, 1] is rejected rather than clamped."""
        with pytest.raises(InvalidProbabilityError):
            Probability(0.5).is_confident(threshold)


class TestHelpers:
    """Auxiliary behaviour."""

    def test_as_percent(self) -> None:
        """Percent conversion is exact for representable values."""
        assert Probability(0.25).as_percent() == pytest.approx(25.0)

    def test_float_conversion(self) -> None:
        """``float()`` unwraps the value."""
        assert float(Probability(0.75)) == pytest.approx(0.75)

    def test_error_message_names_the_offending_value(self) -> None:
        """The error message is actionable."""
        with pytest.raises(InvalidProbabilityError, match=r"1\.5"):
            Probability(1.5)
