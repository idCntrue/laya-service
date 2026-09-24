"""Authentication middleware.

Resolves the presented credential to a principal, then hands the request on.
The middleware knows about HTTP -- headers, status codes, the error envelope --
and nothing about where credentials live. That separation is why it takes an
:class:`~laya_service.domain.ports.api_key_store.ApiKeyStore` rather than a key
string: the store can be a JSON file, a database, or a two-line fake in a test,
and this file does not change.

Two credential shapes are accepted:

* ``Authorization: Bearer <key>`` -- this service's own convention, and what
  every existing client sends.
* ``x-api-key: <key>`` -- what the Anthropic SDK sends. Accepting it is what
  lets an unmodified Anthropic client authenticate without extra headers.

The bootstrap key from configuration is checked first, in constant time, and
authenticates as an administrator. It is never written to the key file, so it
remains the way back in if every stored key is revoked.

Failures are deliberately uniform: the client learns that authentication failed
and nothing else. No "key not found", no "key expired", no hint about whether
the credential exists.
"""

from __future__ import annotations

import hmac
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from laya_service.domain.entities.api_key import SCOPE_ADMIN, SCOPE_INFERENCE
from laya_service.domain.ports.api_key_store import ApiKeyStore
from laya_service.logging_config import get_logger

__all__ = ["PUBLIC_PATHS", "AuthMiddleware", "Principal"]

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

#: Path prefix reserved for key administration. Every route beneath it requires
#: the admin scope, so a leaked inference key cannot mint or revoke credentials.
ADMIN_PREFIX: Final[str] = "/admin/"


class Principal:
    """Who is making a request.

    Attributes:
        name: Human-readable identity, for logs.
        scopes: Granted scopes.
        key_id: The stored key's id, or ``None`` for the bootstrap key.
        is_bootstrap: Whether this is the configured key rather than a stored
            one. Bootstrap principals are always administrators.
    """

    __slots__ = ("is_bootstrap", "key_id", "name", "scopes")

    def __init__(
        self,
        name: str,
        scopes: tuple[str, ...],
        key_id: str | None = None,
        *,
        is_bootstrap: bool = False,
    ) -> None:
        """Initialize the principal.

        Args:
            name: Human-readable identity.
            scopes: Granted scopes.
            key_id: The stored key's id, or ``None``.
            is_bootstrap: Whether this is the configured bootstrap key.
        """
        self.name = name
        self.scopes = scopes
        self.key_id = key_id
        self.is_bootstrap = is_bootstrap

    def has_scope(self, scope: str) -> bool:
        """Whether this principal carries a scope.

        Args:
            scope: The scope to test for.

        Returns:
            ``True`` when granted.
        """
        return scope in self.scopes

    def __repr__(self) -> str:
        """Return a debug representation that never includes a secret."""
        return f"Principal(name={self.name!r}, scopes={list(self.scopes)!r})"


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every non-public request and attach its principal.

    Attributes:
        _api_key: The bootstrap token. Empty disables authentication entirely,
            which is only reachable when the service binds to loopback --
            :class:`~laya_service.infrastructure.config.settings.Settings`
            refuses to start otherwise.
        _store: The key store consulted after the bootstrap key, or ``None``.
    """

    def __init__(
        self,
        app: object,
        api_key: str,
        store: ApiKeyStore | None = None,
    ) -> None:
        """Initialize the middleware.

        Args:
            app: The wrapped ASGI application.
            api_key: The bootstrap bearer token. Empty disables authentication.
            store: Optional store of additional keys.
        """
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key = api_key
        self._store = store

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Authenticate the request, or reject it with a uniform 401.

        Args:
            request: The incoming request.
            call_next: The next middleware or route handler.

        Returns:
            The downstream response when authenticated, otherwise a 401 built
            from the shared error envelope.
        """
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # No bootstrap key and no store means authentication is switched off,
        # which the settings validator only permits on a loopback bind.
        if not self._api_key and self._store is None:
            return await call_next(request)

        presented = _extract_credential(request)
        principal = self._resolve(presented, request)
        if principal is None:
            return _reject(request, presented)

        if request.url.path.startswith(ADMIN_PREFIX) and not principal.has_scope(SCOPE_ADMIN):
            _logger.warning(
                "admin access denied",
                path=request.url.path,
                principal=principal.name,
                request_id=getattr(request.state, "request_id", None),
            )
            return _forbidden(request)

        request.state.principal = principal
        return await call_next(request)

    def _resolve(self, presented: str | None, request: Request) -> Principal | None:
        """Resolve a presented credential to a principal.

        The bootstrap key is checked first so it keeps working even if the store
        is unreadable -- it is the recovery credential.

        Args:
            presented: The credential from the request, or ``None``.
            request: The request, for logging context.

        Returns:
            The principal, or ``None`` when the credential is not accepted.
        """
        if presented is None:
            return None

        if self._api_key and hmac.compare_digest(presented, self._api_key):
            return Principal(
                name="bootstrap",
                scopes=(SCOPE_INFERENCE, SCOPE_ADMIN),
                is_bootstrap=True,
            )

        if self._store is None:
            return None

        try:
            record = self._store.authenticate(presented)
        except Exception:
            _logger.error(
                "api key store unavailable",
                path=request.url.path,
                request_id=getattr(request.state, "request_id", None),
                exc_info=True,
            )
            return None

        if record is None:
            return None
        return Principal(name=record.name, scopes=record.scopes, key_id=record.id)


def _extract_credential(request: Request) -> str | None:
    """Pull the credential out of a request.

    ``Authorization: Bearer`` is preferred; ``x-api-key`` is accepted so an
    unmodified Anthropic SDK authenticates. The scheme match is case-insensitive,
    as RFC 7235 requires.

    Args:
        request: The incoming request.

    Returns:
        The credential, or ``None`` when neither header carries one.
    """
    header = request.headers.get("Authorization")
    if header:
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        # An Authorization header that is not a well-formed bearer token is a
        # malformed attempt, not a fallback to another header.
        return None

    api_key = request.headers.get("x-api-key")
    if api_key and api_key.strip():
        return api_key.strip()
    return None


def _reject(request: Request, presented: str | None) -> Response:
    """Build the uniform 401.

    Args:
        request: The incoming request.
        presented: The credential that failed, for the log reason only.

    Returns:
        The 401 response.
    """
    _logger.warning(
        "authentication failed",
        path=request.url.path,
        method=request.method,
        request_id=getattr(request.state, "request_id", None),
        reason="missing_credential" if presented is None else "credential_mismatch",
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


def _forbidden(request: Request) -> Response:
    """Build the 403 returned when a principal lacks the admin scope.

    Args:
        request: The incoming request.

    Returns:
        The 403 response.
    """
    from laya_service.interfaces.http.exception_handlers import build_error_response

    return build_error_response(
        status_code=403,
        code="forbidden",
        message="this credential does not grant administrative access",
        request_id=getattr(request.state, "request_id", None),
    )
