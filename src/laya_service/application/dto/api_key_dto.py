"""Data transfer objects for API key administration.

Framework-free by design: these carry data between the HTTP layer and the
use case, and they know nothing about JSON, HTTP, or Pydantic. The route
converts a request body into :class:`CreateApiKeyInput`, and the use case
returns an :class:`ApiKeyOutput`; neither side reaches across the boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from laya_service.domain.entities.api_key import ApiKey

__all__ = [
    "ApiKeyOutput",
    "CreateApiKeyInput",
    "IssuedApiKeyOutput",
    "UpdateApiKeyInput",
]


@dataclass(frozen=True, slots=True)
class CreateApiKeyInput:
    """A request to issue a new API key.

    Attributes:
        name: Operator-facing label. Must be non-empty.
        scopes: Scopes to grant. Must be non-empty and all known.
        expires_at: Optional expiry.
    """

    name: str
    scopes: Sequence[str]
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UpdateApiKeyInput:
    """A request to change an existing API key.

    ``None`` on any field means "leave unchanged" -- which is why these are
    optional rather than defaulted. A caller cannot clear a field by omission.

    Attributes:
        name: New label, or ``None``.
        scopes: New scopes, or ``None``.
        expires_at: New expiry, or ``None``.
    """

    name: str | None = None
    scopes: Sequence[str] | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApiKeyOutput:
    """A key's public record, safe to serialise.

    Deliberately has no field for the secret: this type is what every listing
    and every update returns, and a type that cannot hold a credential cannot
    leak one.

    Attributes:
        id: Stable identifier.
        name: Operator label.
        prefix: First characters of the secret, for identification.
        scopes: Granted scopes.
        created_at: When the key was issued.
        expires_at: Optional expiry.
        revoked_at: When revoked, if it was.
        last_used_at: When the key last authenticated a request.
    """

    id: str
    name: str
    prefix: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    @classmethod
    def from_entity(cls, key: ApiKey) -> ApiKeyOutput:
        """Build the DTO from a domain entity.

        Args:
            key: The entity.

        Returns:
            The corresponding DTO.
        """
        return cls(
            id=key.id,
            name=key.name,
            prefix=key.prefix,
            scopes=key.scopes,
            created_at=key.created_at,
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
        )


@dataclass(frozen=True, slots=True)
class IssuedApiKeyOutput:
    """A newly issued key: its public record plus the one-time secret.

    This is the only type in the service that carries a plaintext credential,
    and it exists for exactly one response -- the create call. Every other path
    returns :class:`ApiKeyOutput`.

    Attributes:
        api_key: The public record.
        secret: The plaintext secret. Shown once, never recoverable.
    """

    api_key: ApiKeyOutput = field(metadata={"doc": "The public record."})
    secret: str = ""
