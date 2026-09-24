"""Central translation from exceptions to HTTP responses.

One table maps every exception class to a status code and a stable error code.
Routes therefore never write ``HTTPException``; they let domain and application
errors propagate and this module decides what the client sees.

Two invariants hold for every error response:

1. It uses the uniform envelope ``{"ok": false, "error": {...}}``.
2. It never contains a stack trace, a file path, or an internal identifier.
   The trace goes to the log, correlated by ``request_id``; the client gets the
   correlation id and nothing more.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from laya_service.domain.exceptions import (
    DomainError,
    InvalidApiKeyError,
    InvalidDecisionError,
    InvalidLocalizationSummaryError,
    InvalidProbabilityError,
    InvalidQuestionError,
    ModelInferenceError,
    ModelLoadError,
    UnsupportedModelError,
    UnsupportedSchemaError,
)
from laya_service.interfaces.http.schemas.responses import ErrorDetail, ErrorResponse
from laya_service.logging_config import get_logger

__all__ = [
    "STATUS_BY_CODE",
    "build_error_response",
    "register_exception_handlers",
]

_logger = get_logger(__name__, component="http")

#: Domain error class -> HTTP status. Anything not listed here falls back to 400
#: via the ``DomainError`` entry, so a newly added domain error is never a 500.
STATUS_BY_CODE: Final[dict[type[Exception], int]] = {
    InvalidProbabilityError: 400,
    InvalidLocalizationSummaryError: 400,
    InvalidDecisionError: 400,
    InvalidQuestionError: 400,
    # A schema the model cannot answer is the caller's mistake: they described
    # something this model does not do. 400, not 422, because it is a semantic
    # limitation rather than a malformed request body.
    UnsupportedSchemaError: 400,
    UnsupportedModelError: 400,
    InvalidApiKeyError: 400,
    # A model that cannot load or cannot run is a *dependency* failure, not a
    # client error: 503 tells the caller to retry, and tells an orchestrator
    # that this replica should be taken out of rotation.
    ModelLoadError: 503,
    ModelInferenceError: 503,
    DomainError: 400,
}


def _request_id(request: Request) -> str | None:
    """Read the correlation id the request-id middleware stored on the request.

    Args:
        request: The incoming request.

    Returns:
        The correlation id, or ``None`` when the middleware did not run.
    """
    return getattr(request.state, "request_id", None)


def build_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str | None = None,
) -> JSONResponse:
    """Build a uniform error response.

    Args:
        status_code: HTTP status to return.
        code: Stable machine-readable error code.
        message: Human-readable message, safe to show to an authenticated caller.
        request_id: Correlation id to echo back.

    Returns:
        A :class:`JSONResponse` carrying the standard error envelope.
    """
    body = ErrorResponse(
        ok=False,
        error=ErrorDetail(code=code, message=message),
        request_id=request_id,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


#: Path prefixes served in a foreign wire format. An error raised under one of
#: these must be translated, because the SDK on the other end parses a different
#: envelope -- an OpenAI client reading `{"ok": false, ...}` reports an opaque
#: parse failure instead of the actual problem.
_COMPAT_PREFIXES: Final[tuple[str, ...]] = ("/v1/chat/completions", "/v1/messages")


def _compat_error_response(status_code: int, message: str, path: str) -> JSONResponse | None:
    """Render an error in the wire format the request's path implies.

    Args:
        status_code: The HTTP status.
        message: The human-readable message.
        path: The request path, which selects the format.

    Returns:
        The translated response, or ``None`` when the path is not a compat route
        and the service's own envelope applies.
    """
    if not any(path.startswith(prefix) for prefix in _COMPAT_PREFIXES):
        return None

    if path.startswith("/v1/messages"):
        anthropic_type = {
            400: "invalid_request_error",
            401: "authentication_error",
            403: "permission_error",
            404: "not_found_error",
            429: "rate_limit_error",
            500: "api_error",
            503: "overloaded_error",
        }.get(status_code, "api_error")
        return JSONResponse(
            status_code=status_code,
            content={"type": "error", "error": {"type": anthropic_type, "message": message}},
        )

    openai_type = "invalid_request_error" if status_code < 500 else "server_error"
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": openai_type,
                "code": None,
                "param": None,
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every exception handler to the application.

    Args:
        app: The FastAPI application to configure.
    """

    @app.exception_handler(DomainError)
    async def _handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Map a domain error onto its declared HTTP status.

        Args:
            request: The incoming request.
            exc: The domain error raised.

        Returns:
            A uniform error response.
        """
        status_code = STATUS_BY_CODE.get(type(exc), 400)
        _logger.warning(
            "domain error",
            error_code=exc.code,
            status=status_code,
            path=request.url.path,
            request_id=_request_id(request),
        )
        compat = _compat_error_response(status_code, exc.message, request.url.path)
        if compat is not None:
            return compat
        return build_error_response(
            status_code=status_code,
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render Pydantic validation failures in the uniform envelope.

        Args:
            request: The incoming request.
            exc: The validation error raised by FastAPI.

        Returns:
            A 422 response whose message summarises the offending fields.
        """
        # Summarise rather than dumping the raw error list: the raw form includes
        # the submitted value, which may be sensitive and is certainly noisy.
        problems = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            problems.append(f"{location or '<body>'}: {error.get('msg', 'invalid')}")
        message = "; ".join(problems) or "request validation failed"
        _logger.warning(
            "request validation failed",
            status=422,
            path=request.url.path,
            request_id=_request_id(request),
        )
        compat = _compat_error_response(422, message, request.url.path)
        if compat is not None:
            return compat
        return build_error_response(
            status_code=422,
            code="validation_error",
            message=message,
            request_id=_request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Wrap framework-raised HTTP errors (404, 405, ...) in the envelope.

        Args:
            request: The incoming request.
            exc: The HTTP exception raised by Starlette or FastAPI.

        Returns:
            A uniform error response.
        """
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            413: "payload_too_large",
        }.get(exc.status_code, "http_error")
        return build_error_response(
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail) if exc.detail else code.replace("_", " "),
            request_id=_request_id(request),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Catch anything unhandled and return an opaque 500.

        The full traceback goes to the log -- correlated by ``request_id`` -- and
        never to the client. Leaking a traceback is an information disclosure
        bug; the correlation id is what makes the log useful without it.

        Args:
            request: The incoming request.
            exc: The unhandled exception.

        Returns:
            A 500 response containing only the correlation id.
        """
        request_id = _request_id(request)
        _logger.error(
            "unhandled exception",
            path=request.url.path,
            method=request.method,
            request_id=request_id,
            error=str(exc),
            exc_info=True,
        )
        compat = _compat_error_response(
            500, "an internal error occurred; quote the request id when reporting", request.url.path
        )
        if compat is not None:
            return compat
        return build_error_response(
            status_code=500,
            code="internal_error",
            message="an internal error occurred; quote the request id when reporting it",
            request_id=request_id,
        )
