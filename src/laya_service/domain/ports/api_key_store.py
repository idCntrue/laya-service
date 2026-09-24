"""The ``ApiKeyStore`` port.

Declares what the application layer needs from a credential store without
naming a storage technology. The adapter behind it could be a JSON file, a
database, or a call to an identity provider; nothing above this line changes
when it is swapped.

Declared in the domain -- not in infrastructure -- for the same reason
``ModelUnavailableError`` is: so the application layer can hold key-management
use cases without importing a concrete adapter, keeping the dependency arrow
pointing inward.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from laya_service.domain.entities.api_key import ApiKey

__all__ = ["ApiKeyStore", "IssuedApiKey"]


class IssuedApiKey:
    """A freshly created key: its public record plus the one-time secret.

    The plaintext exists only here, only once, and only in the response to the
    create call. It is not recoverable afterwards, which is what makes storing
    only a hash acceptable.

    Attributes:
        api_key: The public record.
        secret: The plaintext secret. Show it to the caller and forget it.
    """

    __slots__ = ("api_key", "secret")

    def __init__(self, api_key: ApiKey, secret: str) -> None:
        """Initialize the issued key.

        Args:
            api_key: The public record.
            secret: The plaintext secret.
        """
        self.api_key = api_key
        self.secret = secret


@runtime_checkable
class ApiKeyStore(Protocol):
    """Port describing a store of API keys.

    ``typing.Protocol`` rather than ``abc.ABC`` keeps the dependency arrow
    inward: an adapter satisfies this structurally without importing it.
    """

    def create(
        self,
        name: str,
        scopes: Sequence[str],
        expires_at: datetime | None = None,
    ) -> IssuedApiKey:
        """Issue a new key.

        Args:
            name: Operator-facing label.
            scopes: Scopes to grant. Must be non-empty.
            expires_at: Optional expiry.

        Returns:
            The new record and its one-time plaintext secret.

        Raises:
            InvalidApiKeyError: If the name or scopes are unusable.
        """
        ...

    def list_keys(self, *, include_revoked: bool = False) -> Sequence[ApiKey]:
        """List known keys.

        Args:
            include_revoked: When ``True``, revoked keys are included so an
                operator can audit what was removed and when.

        Returns:
            The matching records, oldest first.
        """
        ...

    def get(self, key_id: str) -> ApiKey | None:
        """Look up one key by identifier.

        Args:
            key_id: The record identifier.

        Returns:
            The record, or ``None`` when no such key exists.
        """
        ...

    def update(
        self,
        key_id: str,
        *,
        name: str | None = None,
        scopes: Sequence[str] | None = None,
        expires_at: datetime | None = None,
    ) -> ApiKey:
        """Change a key's mutable fields.

        Args:
            key_id: The record identifier.
            name: New label, or ``None`` to leave unchanged.
            scopes: New scopes, or ``None`` to leave unchanged.
            expires_at: New expiry, or ``None`` to leave unchanged.

        Returns:
            The updated record.

        Raises:
            InvalidApiKeyError: If the key does not exist or the new values are
                unusable.
        """
        ...

    def revoke(self, key_id: str) -> ApiKey:
        """Revoke a key, making it stop authenticating immediately.

        Args:
            key_id: The record identifier.

        Returns:
            The revoked record.

        Raises:
            InvalidApiKeyError: If the key does not exist or is already revoked.
        """
        ...

    def authenticate(self, presented: str) -> ApiKey | None:
        """Resolve a presented secret to the key it belongs to.

        Implementations must compare in constant time across *every* record, so
        that response latency reveals neither whether a key exists nor which
        one matched.

        Args:
            presented: The secret supplied by the caller.

        Returns:
            The matching active key, or ``None`` when the secret is unknown,
            revoked, or expired.
        """
        ...
