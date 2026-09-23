"""The generic prediction endpoint."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from laya_service.application.dto.predict_dto import PredictInput
from laya_service.application.use_cases.predict_decision import PredictDecisionUseCase
from laya_service.interfaces.http.dependencies import get_predict_use_case
from laya_service.interfaces.http.schemas.requests import PredictRequest
from laya_service.interfaces.http.schemas.responses import (
    Meta,
    PredictData,
    PredictResponse,
)

__all__ = ["router"]

router = APIRouter(tags=["predict"])


@router.post(
    "/v1/predict",
    response_model=PredictResponse,
    summary="Answer questions about a state",
    description=(
        "Runs the decision model over a free-form state description and a set of "
        "questions. Requires a bearer token. The first call after a cold start "
        "will download model weights and may take several minutes."
    ),
    responses={
        401: {"description": "Missing or invalid bearer token."},
        422: {"description": "Malformed request body."},
        503: {"description": "The model is unavailable or inference failed."},
    },
)
async def predict(
    payload: PredictRequest,
    request: Request,
    use_case: PredictDecisionUseCase = Depends(get_predict_use_case),
) -> PredictResponse:
    """Run a prediction.

    Args:
        payload: The validated request body.
        request: The incoming request, used for the correlation id.
        use_case: The prediction use case, injected.

    Returns:
        The model's answers wrapped in the standard success envelope.
    """
    started = time.perf_counter()
    # Constructing the DTO is where the domain's own validation runs; a bad
    # question shape raises a DomainError that the exception handlers turn into
    # a 400, independently of the schema-level 422 checks.
    result = use_case.execute(PredictInput(state=payload.state, questions=payload.questions))
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    return PredictResponse(
        ok=True,
        data=PredictData(answers=result.answers),
        meta=Meta(
            backend=result.backend,
            model=result.model_name,
            request_id=getattr(request.state, "request_id", ""),
            latency_ms=latency_ms,
        ),
    )
