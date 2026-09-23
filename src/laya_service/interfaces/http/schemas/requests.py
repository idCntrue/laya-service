"""Inbound request schemas (Pydantic v2).

These models are the *only* place the service trusts raw client input. They
validate shape and basic ranges; semantic validation lives in the domain value
objects, which the routes construct from these. Keeping the two separate means
the domain stays free of Pydantic while clients still get precise field-level
errors.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["LocalizationReliabilityRequest", "PredictRequest"]

#: Question types Laya supports, mirrored from ``laya.QTYPES``. Duplicated from
#: the domain deliberately: the schema rejects bad input at the boundary with a
#: 422, while the domain DTO independently raises a 400. Two independent checks
#: is the correct amount of paranoia for a public endpoint.
_ALLOWED_QUESTION_TYPES = frozenset({"noul", "choice", "score"})

#: Question types that cannot be answered without a ``criteria`` field.
_TYPES_REQUIRING_CRITERIA = frozenset({"choice", "score"})


class PredictRequest(BaseModel):
    """Request body for ``POST /v1/predict``.

    Attributes:
        state: Free-form situation description handed to the model.
        questions: Question specifications keyed by question id.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "state": {"observation": "The x coordinate jumped 2.3 meters."},
                    "questions": {
                        "location_reliable": {
                            "type": "noul",
                            "instructions": "Is the current localization reliable?",
                        }
                    },
                }
            ]
        },
    )

    state: Mapping[str, Any] = Field(
        ...,
        description="Free-form, JSON-serialisable description of the situation.",
    )
    questions: Mapping[str, Mapping[str, Any]] = Field(
        ...,
        min_length=1,
        description="Questions keyed by id. Each must carry 'type' and 'instructions'.",
    )

    @field_validator("questions")
    @classmethod
    def _validate_questions(
        cls, value: Mapping[str, Mapping[str, Any]]
    ) -> Mapping[str, Mapping[str, Any]]:
        """Ensure every question is well-formed and of a supported type.

        Args:
            value: The raw questions mapping.

        Returns:
            The validated mapping, unchanged.

        Raises:
            ValueError: If a question lacks a type or instructions, or names an
                unsupported type.
        """
        for question_id, spec in value.items():
            if not isinstance(spec, Mapping):
                raise ValueError(f"question {question_id!r} must be an object")
            question_type = spec.get("type")
            if question_type not in _ALLOWED_QUESTION_TYPES:
                raise ValueError(
                    f"question {question_id!r} has unsupported type {question_type!r}; "
                    f"expected one of {sorted(_ALLOWED_QUESTION_TYPES)}"
                )
            if not str(spec.get("instructions", "")).strip():
                raise ValueError(f"question {question_id!r} requires non-empty 'instructions'")
            if question_type in _TYPES_REQUIRING_CRITERIA and not spec.get("criteria"):
                raise ValueError(
                    f"question {question_id!r} of type {question_type!r} requires a "
                    "'criteria' field (a list for 'score', a mapping for 'choice')"
                )
        return value


class LocalizationReliabilityRequest(BaseModel):
    """Request body for ``POST /v1/robot-dog/localization-reliability``.

    Attributes:
        x_jump_m: Discontinuous jump along x, in metres.
        y_stable: Whether y stayed stable over the window.
        heading_reversals: Number of heading reversals observed.
        confidence_start: Estimator confidence at window start, in ``[0, 1]``.
        confidence_end: Estimator confidence at window end, in ``[0, 1]``.
        environment: Environment tag, e.g. ``"indoor_weak_gps"``.
        confidence_threshold: Bar at or above which the answer is actionable.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "x_jump_m": 2.3,
                    "y_stable": True,
                    "heading_reversals": 3,
                    "confidence_start": 0.9,
                    "confidence_end": 0.4,
                    "environment": "indoor_weak_gps",
                }
            ]
        },
    )

    x_jump_m: float = Field(
        ..., ge=0, le=1000, description="Discontinuous jump along x, in metres."
    )
    y_stable: bool = Field(..., description="Whether the y coordinate stayed stable.")
    heading_reversals: int = Field(
        ..., ge=0, le=1000, description="Number of heading reversals in the window."
    )
    confidence_start: float = Field(
        ..., ge=0.0, le=1.0, description="Estimator confidence at window start."
    )
    confidence_end: float = Field(
        ..., ge=0.0, le=1.0, description="Estimator confidence at window end."
    )
    environment: str = Field(
        ..., min_length=1, max_length=128, description="Environment tag, e.g. 'indoor_weak_gps'."
    )
    confidence_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence bar at or above which the judgement is actionable.",
    )
