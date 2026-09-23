"""Correlation-id middleware.

Every request gets an ``X-Request-ID``. If the caller supplied one we honour it
-- that is what makes a request traceable across a multi-hop call chain -- but
we sanitise it first, because the value ends up in log lines and response
headers and must not be able to inject either.

A ``trace_id`` is derived alongside it. They are separate fields because a
future version may propagate a distributed trace id that spans several requests,
while the request id stays per-hop.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["REQUEST_ID_HEADER", "TRACE_ID_HEADER", "RequestIdMiddleware"]

#: Header carrying the correlation id in both directions.
REQUEST_ID_HEADER: Final[str] = "X-Request-ID"

#: Header carrying the (currently request-scoped) trace id.
TRACE_ID_HEADER: Final[str] = "X-Trace-ID"

#: Only accept ids that look like ids. Anything else is replaced with a fresh
#: one, which blocks CRLF header injection and log forging in one check.
_SAFE_ID: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9._:\-]{1,128}\Z")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id to every request and echo it in the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Assign the correlation id, then delegate down the stack.

        Args:
            request: The incoming request.
            call_next: The next middleware or route handler.

        Returns:
            The downstream response, with the correlation headers added.
        """
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex

        request.state.request_id = request_id
        # A dedicated trace id keeps the door open for real distributed tracing
        # without changing the request-id contract clients already depend on.
        request.state.trace_id = request_id

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[TRACE_ID_HEADER] = request_id
        return response
