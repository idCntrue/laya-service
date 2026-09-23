"""Outbound response schemas (Pydantic v2).

Every successful response uses the same envelope -- ``ok``, ``data``, ``meta`` --
and every error uses ``ok``, ``error``. A single stable envelope means clients
need one parser, and adding fields later is backward-compatible.

``extra="forbid"`` is set on the response models so that an accidental extra
field is a test failure rather than a silent API change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "LocalizationData",
    "LocalizationResponse",
    "Meta",
    "PredictData",
    "PredictResponse",
    "ReadyResponse",
]


class Meta(BaseModel):
    """Provenance metadata attached to every successful response.

    Attributes:
        backend: Compute backend that served the request.
        model: Model identifier that served the request.
        request_id: Correlation id, echoed from ``X-Request-ID``.
        latency_ms: Server-side handling time in milliseconds.
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = Field(..., description="Compute backend, e.g. 'cpu' or 'cuda'.")
    model: str = Field(..., description="Model identifier that served the request.")
    request_id: str = Field(..., description="Correlation id for this request.")
    latency_ms: float | None = Field(
        default=None, description="Server-side handling time in milliseconds."
    )


class ErrorDetail(BaseModel):
    """Machine-readable error payload.

    Attributes:
        code: Stable error identifier, safe to branch on in client code.
        message: Human-readable explanation. Never contains a stack trace.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="Stable machine-readable error code.")
    message: str = Field(..., description="Human-readable error message.")


class ErrorResponse(BaseModel):
    """The uniform error envelope returned by every failure path.

    Attributes:
        ok: Always ``False``.
        error: The error detail.
        request_id: Correlation id, so a client can quote it in a bug report.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(default=False, description="Always false for error responses.")
    error: ErrorDetail = Field(..., description="The error detail.")
    request_id: str | None = Field(default=None, description="Correlation id for this request.")


class PredictData(BaseModel):
    """Payload of a successful prediction.

    Attributes:
        answers: Raw answers keyed by question id, in Laya's wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    answers: Mapping[str, Any] = Field(
        ..., description="Answers keyed by question id, e.g. {'q': {'type': 'noul', 'noul': true}}."
    )


class PredictResponse(BaseModel):
    """Response body for ``POST /v1/predict``."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(default=True, description="Always true on success.")
    data: PredictData = Field(..., description="The prediction payload.")
    meta: Meta = Field(..., description="Provenance metadata.")


class LocalizationData(BaseModel):
    """Payload of a localization-reliability evaluation.

    Attributes:
        reliable: The model's judgement on whether localization can be trusted.
        reliability: The model's confidence in that judgement, in ``[0, 1]``.
        recommendation: Machine-readable next action for the controller.
        confident: Whether ``reliability`` cleared the requested threshold.
    """

    model_config = ConfigDict(extra="forbid")

    reliable: bool = Field(..., description="Whether localization is judged reliable.")
    reliability: float = Field(..., ge=0.0, le=1.0, description="Confidence in the judgement.")
    recommendation: str = Field(..., description="Machine-readable next action.")
    confident: bool = Field(
        ..., description="Whether the confidence cleared the caller's threshold."
    )


class LocalizationResponse(BaseModel):
    """Response body for ``POST /v1/robot-dog/localization-reliability``."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = Field(default=True, description="Always true on success.")
    data: LocalizationData = Field(..., description="The evaluation payload.")
    meta: Meta = Field(..., description="Provenance metadata.")


class HealthResponse(BaseModel):
    """Response body for the liveness probe."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., description="Always 'ok' when the process is alive.")
    version: str = Field(..., description="Service version.")


class ReadyResponse(BaseModel):
    """Response body for the readiness probe.

    Attributes:
        status: ``"ready"`` or ``"not_ready"``.
        model_loaded: Whether the decision model is resident and warm.
        backend: Compute backend the model will use.
        model: Model identifier, or a placeholder before first load.
        detail: Optional explanation when not ready.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., description="'ready' or 'not_ready'.")
    model_loaded: bool = Field(..., description="Whether the model is loaded and warm.")
    backend: str = Field(..., description="Compute backend the model will use.")
    model: str = Field(..., description="Model identifier.")
    detail: str | None = Field(default=None, description="Why the service is not ready.")
