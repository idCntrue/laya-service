"""Liveness and readiness probes.

These two endpoints answer different questions, and conflating them is a common
cause of outages:

* ``/healthz`` -- *is the process alive?* It never touches the model, never
  touches the network, and always returns 200. A failure here means the process
  is wedged and the orchestrator should restart it.
* ``/readyz`` -- *should this replica receive traffic?* It reports 503 until the
  model is resident and warm. A failure here means "route around me", not
  "restart me" -- a distinction that matters enormously when the model takes
  minutes to load, because a liveness probe wired to readiness would restart the
  container forever and never let it finish loading.

Both are unauthenticated: a probe has no credentials.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from laya_service import __version__
from laya_service.domain.ports.decision_model import DecisionModel
from laya_service.interfaces.http.dependencies import get_decision_model
from laya_service.interfaces.http.schemas.responses import HealthResponse, ReadyResponse

__all__ = ["router"]

router = APIRouter(tags=["health"])


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 whenever the process is alive. Never touches the model.",
)
async def healthz() -> HealthResponse:
    """Report that the process is running.

    Returns:
        A constant ``{"status": "ok"}`` payload plus the service version.
    """
    return HealthResponse(status="ok", version=__version__)


@router.get(
    "/readyz",
    response_model=ReadyResponse,
    summary="Readiness probe",
    description=(
        "Returns 200 once the decision model is loaded and warm, and 503 before "
        "that. Use this for load-balancer membership, not for restart decisions."
    ),
    responses={503: {"description": "The model is not loaded yet."}},
)
async def readyz(
    response: Response,
    model: DecisionModel = Depends(get_decision_model),
) -> ReadyResponse:
    """Report whether this replica can serve inference.

    Args:
        response: The response object, mutated to set the status code.
        model: The decision-model port, injected.

    Returns:
        A readiness payload. The status code is 200 when ready, 503 otherwise.
    """
    ready = model.is_ready
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status="ready" if ready else "not_ready",
        model_loaded=ready,
        backend=model.backend,
        model=model.model_name,
        detail=None
        if ready
        else (
            "the decision model has not been loaded yet; it loads on first request. "
            "Send a request to /v1/predict to trigger the load, or set PRELOAD_MODEL=true."
        ),
    )
