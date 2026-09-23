"""Bearer-token authentication middleware.

Compares the presented token against the configured key using
:func:`hmac.compare_digest`, which runs in constant time. A plain ``==`` on
strings short-circuits on the first differing byte, which leaks the key one byte
at a time to anyone who can measure response latency -- a real attack against a
network service, not a theoretical one.

Failures are deliberately uniform: the client learns that authentication failed
and nothing else. No "key not found", no "key expired", no hint about whether
the key exists.
"""

from __future__ import annotations

import hmac
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from laya_service.logging_config import get_logger

__all__ = ["PUBLIC_PATHS", "AuthMiddleware"]

_logger = get_logger(__name__, component="auth")

#: Paths served without authentication. Probes must be reachable by the
#: orchestrator before it has any credentials, and the docs are useful to
#: inspect during an incident.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    }
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <key>`` on every non-public route.

    Attributes:
        _api_key: The expected token. Empty disables authentication entirely,
            which is only reachable when the service binds to loopback --
            :class:`~laya_service.infrastructure.config.settings.Settings`
            refuses to start otherwise.
    """

    def __init__(self, app: object, api_key: str) -> None:
        """Initialize the middleware.

        Args:
            app: The wrapped ASGI application.
            api_key: The expected bearer token. Empty disables authentication.
        """
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Authenticate the request, or reject it with a uniform 401.

        Args:
            request: The incoming request.
            call_next: The next middleware or route handler.

        Returns:
            The downstream response when authenticated, otherwise a 401 built
            from the shared error envelope.
        """
        if not self._api_key or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        presented = self._extract_bearer(request)
        if presented is None or not hmac.compare_digest(presented, self._api_key):
            _logger.warning(
                "authentication failed",
                path=request.url.path,
                method=request.method,
                request_id=getattr(request.state, "request_id", None),
                reason="missing_token" if presented is None else "token_mismatch",
            )
            # Imported lazily to keep this module free of a cycle with
            # exception_handlers, which imports the schemas.
            from laya_service.interfaces.http.exception_handlers import build_error_response

            response = build_error_response(
                status_code=401,
                code="unauthorized",
                message="a valid bearer token is required",
                request_id=getattr(request.state, "request_id", None),
            )
            response.headers["WWW-Authenticate"] = "Bearer"
            return response

        return await call_next(request)

    @staticmethod
    def _extract_bearer(request: Request) -> str | None:
        """Pull the bearer token out of the ``Authorization`` header.

        Args:
            request: The incoming request.

        Returns:
            The token, or ``None`` when the header is absent or malformed. The
            scheme match is case-insensitive, as RFC 7235 requires.
        """
        header = request.headers.get("Authorization")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return token.strip()
