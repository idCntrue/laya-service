"""The FastAPI application factory.

This module is the outermost ring of the architecture. It knows about FastAPI,
middleware ordering, and the OpenAPI document; nothing inside it knows anything
about Laya.

A factory function is used rather than a module-level ``app`` object so that
tests can build a fresh application with injected settings, and so that
``uvicorn --factory`` can build it inside the worker process (which matters for
the preload hook, since the model must live in the process that serves traffic).

Middleware order is significant and is documented at the registration site.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from laya_service import __version__
from laya_service.infrastructure.config.settings import Settings, get_settings
from laya_service.interfaces.http.dependencies import get_decision_model, reset_model
from laya_service.interfaces.http.exception_handlers import register_exception_handlers
from laya_service.interfaces.http.middleware import (
    AccessLogMiddleware,
    AuthMiddleware,
    RequestIdMiddleware,
)
from laya_service.interfaces.http.routes import health, predict, robot_dog
from laya_service.logging_config import configure_logging, get_logger

__all__ = ["create_app"]

_logger = get_logger(__name__, component="app")

#: Written to the OpenAPI document and shown at the top of ``/docs``.
_DESCRIPTION: Final[str] = """
Decision-model inference over HTTP, built as a Clean Architecture service.

* `POST /v1/predict` -- answer arbitrary questions about a state.
* `POST /v1/robot-dog/localization-reliability` -- judge whether a robot's
  localization estimate can be trusted.

**Authentication.** Every `/v1/*` route requires `Authorization: Bearer <key>`.
The probes (`/healthz`, `/readyz`) and the docs are public.

**Reliability caveat.** Laya emits zero-shot probabilities. They are useful for
ranking and for triggering a conservative fallback, but they are *not*
calibrated. Do not threshold them as if they were frequencies without
fine-tuning and calibration on your own data.
"""


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown.

    On startup the configuration is validated and logged (redacted). If
    ``PRELOAD_MODEL`` is set, the model is warmed here -- deliberately with a
    failure that degrades readiness rather than killing the process, so that a
    slow or broken weight download leaves an inspectable service rather than a
    crash loop.

    On shutdown the memoised adapters are released so that a clean exit does not
    wait on the model's finalisers.

    Args:
        app: The application being started.

    Yields:
        Control back to the server for the lifetime of the process.
    """
    settings: Settings = app.state.settings
    _logger.info("service starting", version=__version__, **settings.redacted())

    if settings.preload_model:
        _logger.info("preloading model as requested by PRELOAD_MODEL")
        model = get_decision_model()
        _logger.info(
            "preload finished",
            model_loaded=model.is_ready,
            model=model.model_name,
            backend=model.backend,
        )

    yield

    _logger.info("service shutting down")
    reset_model()
    _logger.info("shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application.

    Args:
        settings: Optional settings override. When omitted, the memoised
            singleton from the environment is used. Tests pass an explicit
            instance to pin configuration.

    Returns:
        A fully configured :class:`FastAPI` application.
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved.log_level)

    app = FastAPI(
        title="Laya Service",
        description=_DESCRIPTION,
        version=__version__,
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = resolved

    register_exception_handlers(app)

    # Middleware runs outermost-first on the way in and reverse on the way out.
    # Registration order below is innermost-first, because Starlette prepends:
    # each add_middleware call wraps everything added before it.
    #
    #   RequestId (added last -> outermost)   <- runs first, so every layer
    #   AccessLog                                below can log a request id
    #   Auth                                  <- rejects before any work
    #   CORS (added first -> innermost)
    #
    # RequestId must be outermost so that a 401 emitted by Auth still carries a
    # correlation id; AccessLog sits outside Auth so rejected requests are
    # logged too.
    if resolved.cors_enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-Trace-ID"],
            max_age=600,
        )

    app.add_middleware(AuthMiddleware, api_key=resolved.laya_api_key)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(predict.router)
    app.include_router(robot_dog.router)

    _logger.info(
        "application configured",
        auth_enabled=bool(resolved.laya_api_key),
        cors_enabled=resolved.cors_enabled,
        preload_model=resolved.preload_model,
        max_body_bytes=resolved.max_body_bytes,
    )
    return app
