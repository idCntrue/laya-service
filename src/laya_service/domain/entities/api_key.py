"""The ``ApiKey`` entity.

An API key as the rest of the system sees it: an identity with a name, a set of
scopes, and a lifetime. The secret itself is deliberately absent -- this entity
describes a key, it does not carry one. Only the store that owns the secret
material ever sees the plaintext, and only at the moment of creation.

That separation is the point: an entity that cannot hold a secret cannot leak
one, and code that handles this type cannot accidentally log a credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from laya_service.domain.exceptions import InvalidApiKeyError

__all__ = ["ApiKey", "Scope"]

#: Scope granting inference access -- the ``/v1/*`` and compat routes.
SCOPE_INFERENCE: Final[str] = "inference"

#: Scope granting key administration. Required for ``/admin/*``.
SCOPE_ADMIN: Final[str] = "admin"

#: Every scope this service understands.
VALID_SCOPES: Final[frozenset[str]] = frozenset({SCOPE_INFERENCE, SCOPE_ADMIN})

#: Alias kept for readability at call sites that want the type spelled out.
Scope = str


@dataclass(frozen=True, slots=True)
class ApiKey:
    """An API key's public identity.

    Attributes:
        id: Stable identifier, safe to expose and to use in ``/admin/keys/{id}``.
        name: Operator-supplied label, e.g. ``"grafana-prod"``.
        prefix: The first few characters of the secret, so an operator can tell
            which key a listing refers to without the secret being recoverable.
        scopes: Granted scopes. Always non-empty.
        created_at: When the key was issued.
        expires_at: When the key stops working, or ``None`` for no expiry.
        revoked_at: When the key was revoked, or ``None`` while it is live.
        last_used_at: When the key last authenticated a request, or ``None``.
    """

    id: str
    name: str
    prefix: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate the key's coherence.

        Raises:
            InvalidApiKeyError: If an identifier or name is empty, or if no
                scopes were granted.
        """
        if not self.id.strip():
            raise InvalidApiKeyError("an api key must have a non-empty id")
        if not self.name.strip():
            raise InvalidApiKeyError("an api key must have a non-empty name")
        if not self.scopes:
            raise InvalidApiKeyError("an api key must grant at least one scope")
        unknown = sorted(set(self.scopes) - VALID_SCOPES)
        if unknown:
            raise InvalidApiKeyError(
                f"unknown scope(s) {unknown}; valid scopes are {sorted(VALID_SCOPES)}"
            )

    def is_active(self, now: datetime | None = None) -> bool:
        """Whether the key may currently authenticate a request.

        Args:
            now: The moment to test against. Defaults to the current UTC time.

        Returns:
            ``True`` when the key is neither revoked nor expired.
        """
        moment = now or datetime.now(timezone.utc)
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and moment >= self.expires_at)

    def has_scope(self, scope: str) -> bool:
        """Whether the key carries a scope.

        Args:
            scope: The scope to test for.

        Returns:
            ``True`` when the scope is granted.
        """
        return scope in self.scopes

    def is_expired(self, now: datetime | None = None) -> bool:
        """Whether the key has passed its expiry.

        Args:
            now: The moment to test against. Defaults to the current UTC time.

        Returns:
            ``True`` when an expiry is set and has passed.
        """
        if self.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.expires_at
