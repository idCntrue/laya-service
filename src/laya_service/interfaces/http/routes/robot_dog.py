"""The robot-dog localization-reliability endpoint.

This route is intentionally thin. It does three things: translate the request
schema into domain value objects, call the use case, and map the result into the
response schema. Every judgement -- what "unreliable" means, which recommendation
to emit, what to do when the model is unsure -- lives in the application layer,
where it can be tested without an HTTP server.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from laya_service.application.dto.robot_dog_dto import LocalizationInput
from laya_service.application.use_cases.evaluate_localization import (
    EvaluateLocalizationUseCase,
)
from laya_service.domain.value_objects.localization_summary import LocalizationSummary
from laya_service.domain.value_objects.probability import Probability
from laya_service.interfaces.http.dependencies import get_localization_use_case
from laya_service.interfaces.http.schemas.requests import LocalizationReliabilityRequest
from laya_service.interfaces.http.schemas.responses import (
    LocalizationData,
    LocalizationResponse,
    Meta,
)

__all__ = ["router"]

router = APIRouter(tags=["robot-dog"])


@router.post(
    "/v1/robot-dog/localization-reliability",
    response_model=LocalizationResponse,
    summary="Judge whether a robot's localization estimate is trustworthy",
    description=(
        "Takes a structured summary of recent localization behaviour and returns "
        "a boolean reliability judgement, the model's confidence in it, and a "
        "machine-readable recommendation for the controller. Requires a bearer token."
    ),
    responses={
        400: {"description": "The summary is internally incoherent."},
        401: {"description": "Missing or invalid bearer token."},
        422: {"description": "Malformed request body."},
        503: {"description": "The model is unavailable or inference failed."},
    },
)
async def localization_reliability(
    payload: LocalizationReliabilityRequest,
    request: Request,
    use_case: EvaluateLocalizationUseCase = Depends(get_localization_use_case),
) -> LocalizationResponse:
    """Evaluate localization reliability.

    Args:
        payload: The validated request body.
        request: The incoming request, used for the correlation id.
        use_case: The evaluation use case, injected.

    Returns:
        The reliability judgement and recommendation in the standard envelope.
    """
    started = time.perf_counter()
    # Build the domain value objects here. Their constructors perform the
    # semantic validation (coherence between confidence_start and confidence_end,
    # sane bounds on the jump and reversal count) that the schema deliberately
    # does not duplicate.
    summary = LocalizationSummary(
        x_jump_m=payload.x_jump_m,
        y_stable=payload.y_stable,
        heading_reversals=payload.heading_reversals,
        confidence_start=Probability(payload.confidence_start),
        confidence_end=Probability(payload.confidence_end),
        environment=payload.environment,
    )
    result = use_case.execute(
        LocalizationInput(summary=summary, confidence_threshold=payload.confidence_threshold)
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    return LocalizationResponse(
        ok=True,
        data=LocalizationData(
            reliable=result.reliable,
            reliability=result.reliability.value,
            recommendation=result.recommendation,
            confident=result.confident,
        ),
        meta=Meta(
            backend=result.backend,
            model=result.model_name,
            request_id=getattr(request.state, "request_id", ""),
            latency_ms=latency_ms,
        ),
    )
