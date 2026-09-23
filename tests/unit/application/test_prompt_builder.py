"""Tests for the English prompt builder.

Laya only understands English, so the single most important property of this
service is that it never emits anything else. Several tests below assert on the
literal English text for that reason -- they are regression guards on the prompt
contract, not incidental assertions.
"""

from __future__ import annotations

import re

import pytest

from laya_service.application.services.localization_prompt_builder import (
    RECOMMENDATION_QUESTION_ID,
    RELIABILITY_QUESTION_ID,
    LocalizationPromptBuilder,
)
from laya_service.domain.value_objects.localization_summary import LocalizationSummary
from laya_service.domain.value_objects.probability import Probability


@pytest.fixture
def builder() -> LocalizationPromptBuilder:
    """Provide a prompt builder.

    Returns:
        A stateless :class:`LocalizationPromptBuilder`.
    """
    return LocalizationPromptBuilder()


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


class TestBuildState:
    """The ``state`` payload."""

    def test_returns_observation_key(self, builder: LocalizationPromptBuilder) -> None:
        """The state is a mapping with a single 'observation' key."""
        state = builder.build_state(make_summary())
        assert set(state) == {"observation"}
        assert isinstance(state["observation"], str)

    def test_is_english_ascii(self, builder: LocalizationPromptBuilder) -> None:
        """The observation contains no non-ASCII characters."""
        observation = builder.build_observation(make_summary())
        assert observation.isascii(), f"non-ASCII in prompt: {observation!r}"

    def test_contains_no_cjk_characters(self, builder: LocalizationPromptBuilder) -> None:
        """No CJK characters leak into the prompt."""
        observation = builder.build_observation(make_summary())
        assert not re.search(r"[一-鿿぀-ヿ가-힯]", observation)


class TestObservationContent:
    """The observation reports every input faithfully."""

    def test_includes_the_jump_magnitude(self, builder: LocalizationPromptBuilder) -> None:
        """The jump distance appears in the sentence."""
        assert "2.3" in builder.build_observation(make_summary(x_jump_m=2.3))

    def test_describes_a_stable_y(self, builder: LocalizationPromptBuilder) -> None:
        """A stable y is described as stable."""
        assert "stable" in builder.build_observation(make_summary(y_stable=True))

    def test_describes_a_drifting_y(self, builder: LocalizationPromptBuilder) -> None:
        """An unstable y is described as drifting, not as stable."""
        observation = builder.build_observation(make_summary(y_stable=False))
        assert "drifted" in observation
        assert "y coordinate was stable" not in observation

    def test_reports_reversal_count(self, builder: LocalizationPromptBuilder) -> None:
        """The reversal count appears."""
        assert "3 times" in builder.build_observation(make_summary(heading_reversals=3))

    def test_uses_singular_for_one_reversal(self, builder: LocalizationPromptBuilder) -> None:
        """One reversal is singular."""
        observation = builder.build_observation(make_summary(heading_reversals=1))
        assert "1 time" in observation
        assert "1 times" not in observation

    def test_describes_zero_reversals(self, builder: LocalizationPromptBuilder) -> None:
        """Zero reversals is stated explicitly rather than as '0 times'."""
        observation = builder.build_observation(make_summary(heading_reversals=0))
        assert "did not reverse" in observation

    def test_reports_both_confidences(self, builder: LocalizationPromptBuilder) -> None:
        """Both confidence values appear."""
        observation = builder.build_observation(
            make_summary(confidence_start=Probability(0.9), confidence_end=Probability(0.4))
        )
        assert "0.90" in observation
        assert "0.40" in observation

    def test_flags_a_decrease(self, builder: LocalizationPromptBuilder) -> None:
        """Falling confidence is labelled as a decrease."""
        observation = builder.build_observation(
            make_summary(confidence_start=Probability(0.9), confidence_end=Probability(0.4))
        )
        assert "decrease" in observation

    def test_flags_an_increase(self, builder: LocalizationPromptBuilder) -> None:
        """Rising confidence is labelled as an increase."""
        observation = builder.build_observation(
            make_summary(confidence_start=Probability(0.4), confidence_end=Probability(0.9))
        )
        assert "increase" in observation

    def test_flags_no_change(self, builder: LocalizationPromptBuilder) -> None:
        """Unchanged confidence is labelled as unchanged."""
        observation = builder.build_observation(
            make_summary(confidence_start=Probability(0.5), confidence_end=Probability(0.5))
        )
        assert "unchanged" in observation


class TestEnvironmentPhrasing:
    """The environment tag is expanded into readable English."""

    def test_known_tag_is_expanded(self, builder: LocalizationPromptBuilder) -> None:
        """A known tag becomes a descriptive phrase."""
        observation = builder.build_observation(make_summary(environment="indoor_weak_gps"))
        assert "indoors with weak GPS reception" in observation

    def test_unknown_tag_is_de_underscored(self, builder: LocalizationPromptBuilder) -> None:
        """An unknown tag degrades gracefully instead of failing."""
        observation = builder.build_observation(make_summary(environment="cave_system"))
        assert "cave system" in observation

    @pytest.mark.parametrize(
        "environment",
        [
            "indoor_weak_gps",
            "indoor_strong_gps",
            "outdoor_open_sky",
            "outdoor_urban_canyon",
            "underground",
            "indoor",
            "outdoor",
        ],
    )
    def test_every_known_tag_renders(
        self, builder: LocalizationPromptBuilder, environment: str
    ) -> None:
        """Every documented tag produces a non-empty, expanded English phrase.

        The point of the expansion is that the raw tag never reaches the model
        as an identifier, so the assertion is that the underscore-joined form
        is absent -- ``outdoor`` legitimately appears inside ``outdoors``.
        """
        observation = builder.build_observation(make_summary(environment=environment))
        assert observation.isascii()
        if "_" in environment:
            assert environment not in observation


class TestBuildQuestions:
    """The question set."""

    def test_returns_both_questions(self, builder: LocalizationPromptBuilder) -> None:
        """Both the reliability and recommendation questions are asked."""
        questions = builder.build_questions()
        assert set(questions) == {RELIABILITY_QUESTION_ID, RECOMMENDATION_QUESTION_ID}

    def test_all_questions_are_noul_type(self, builder: LocalizationPromptBuilder) -> None:
        """Every question uses Laya's boolean type."""
        for spec in builder.build_questions().values():
            assert spec["type"] == "noul"

    def test_instructions_are_english(self, builder: LocalizationPromptBuilder) -> None:
        """Every instruction is non-empty ASCII English."""
        for spec in builder.build_questions().values():
            instructions = spec["instructions"]
            assert instructions.strip()
            assert instructions.isascii()

    def test_reliability_instructions_are_question_shaped(
        self, builder: LocalizationPromptBuilder
    ) -> None:
        """The reliability instruction is phrased as a question."""
        spec = builder.build_questions()[RELIABILITY_QUESTION_ID]
        assert spec["instructions"].endswith("?")


class TestDeterminism:
    """The builder is pure."""

    def test_same_input_yields_same_output(self, builder: LocalizationPromptBuilder) -> None:
        """Repeated calls with equal input produce equal output."""
        summary = make_summary()
        assert builder.build_observation(summary) == builder.build_observation(summary)

    def test_stateless_across_instances(self) -> None:
        """Two builders produce identical output for identical input."""
        summary = make_summary()
        assert LocalizationPromptBuilder().build_observation(
            summary
        ) == LocalizationPromptBuilder().build_observation(summary)
