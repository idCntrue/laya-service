"""Access-logging middleware.

Emits one structured JSON line per request carrying the fields an on-call
engineer actually greps for: ``method``, ``path``, ``status``, ``latency_ms``,
``request_id``, and the client address.

Deliberately *not* logged: the request body, the response body, and the
``Authorization`` header. Bodies can contain robot telemetry that is expensive
to store and may be confidential; the auth header is a credential. The
``SecretRedactingFilter`` in the logging setup is the backstop if one ever slips
through.
"""

from __future__ import annotations

import time
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from laya_service.logging_config import get_logger

__all__ = ["AccessLogMiddleware"]

_logger = get_logger(__name__, component="access")

#: Requests slower than this are logged at WARNING so they surface without a
#: separate latency dashboard.
_SLOW_REQUEST_MS: Final[float] = 1000.0


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log a structured summary of every request after it completes."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Time the request and log the outcome.

        Args:
            request: The incoming request.
            call_next: The next middleware or route handler.

        Returns:
            The downstream response, unmodified.

        Raises:
            Exception: Re-raised unchanged after logging, so the exception
                handlers upstream still see it.
        """
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers render the response, but they sit *inside*
            # this middleware, so an exception reaching here means something
            # bypassed them. Record it against the request, then re-raise --
            # never swallow, or the handler upstream would never see it.
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            _log_completion(request, 500, latency_ms, failed=True)
            raise
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        _log_completion(request, response.status_code, latency_ms)
        return response


def _log_completion(
    request: Request,
    status_code: int,
    latency_ms: float,
    *,
    failed: bool = False,
) -> None:
    """Write the completion line at a level matching the outcome.

    Args:
        request: The request that just finished.
        status_code: The HTTP status returned, or 500 when nothing was returned.
        latency_ms: Server-side handling time in milliseconds.
        failed: Whether the request raised instead of producing a response.
    """
    fields = {
        "method": request.method,
        "path": request.url.path,
        "status": status_code,
        "latency_ms": latency_ms,
        "request_id": getattr(request.state, "request_id", None),
        "trace_id": getattr(request.state, "trace_id", None),
        "client": request.client.host if request.client else None,
    }
    if failed:
        _logger.error("request aborted by unhandled exception", exc_info=True, **fields)
    elif status_code >= 500:
        _logger.error("request completed", **fields)
    elif status_code >= 400 or latency_ms >= _SLOW_REQUEST_MS:
        _logger.warning("request completed", **fields)
    else:
        _logger.info("request completed", **fields)
