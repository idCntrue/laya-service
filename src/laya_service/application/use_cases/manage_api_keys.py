"""The API key administration use case.

Orchestrates the key lifecycle over the
:class:`~laya_service.domain.ports.api_key_store.ApiKeyStore` port. Contains no
storage logic and no HTTP: it validates the shape of a request, delegates to the
port, and returns DTOs. That is what lets it be tested against a two-line fake
and swapped between storage backends without change.
"""

from __future__ import annotations

from collections.abc import Sequence

from laya_service.application.dto.api_key_dto import (
    ApiKeyOutput,
    CreateApiKeyInput,
    IssuedApiKeyOutput,
    UpdateApiKeyInput,
)
from laya_service.domain.entities.api_key import SCOPE_ADMIN, SCOPE_INFERENCE, VALID_SCOPES
from laya_service.domain.exceptions import InvalidApiKeyError
from laya_service.domain.ports.api_key_store import ApiKeyStore
from laya_service.logging_config import get_logger

__all__ = ["DEFAULT_SCOPES", "ManageApiKeysUseCase"]

_logger = get_logger(__name__, component="api_keys")

#: Scopes a new key receives when the caller does not name any. Inference only:
#: a key that can mint other keys should be asked for explicitly.
DEFAULT_SCOPES: tuple[str, ...] = (SCOPE_INFERENCE,)


class ManageApiKeysUseCase:
    """Create, list, update and revoke API keys.

    Attributes:
        _store: The credential store this use case drives.
    """

    def __init__(self, store: ApiKeyStore) -> None:
        """Initialize the use case.

        Args:
            store: Any object satisfying the ``ApiKeyStore`` port.
        """
        self._store = store

    def create(self, input: CreateApiKeyInput) -> IssuedApiKeyOutput:
        """Issue a new key.

        Args:
            input: The creation request.

        Returns:
            The public record plus the one-time plaintext secret.

        Raises:
            InvalidApiKeyError: If the name is blank or a scope is unknown.
        """
        scopes = tuple(input.scopes) or DEFAULT_SCOPES
        issued = self._store.create(input.name, scopes, input.expires_at)
        _logger.info(
            "api key issued",
            key_id=issued.api_key.id,
            name=issued.api_key.name,
            scopes=list(scopes),
        )
        return IssuedApiKeyOutput(
            api_key=ApiKeyOutput.from_entity(issued.api_key),
            secret=issued.secret,
        )

    def list_keys(self, *, include_revoked: bool = False) -> Sequence[ApiKeyOutput]:
        """List keys.

        Args:
            include_revoked: Include revoked keys for auditing.

        Returns:
            The matching records, oldest first.
        """
        return [
            ApiKeyOutput.from_entity(record)
            for record in self._store.list_keys(include_revoked=include_revoked)
        ]

    def get(self, key_id: str) -> ApiKeyOutput:
        """Look up one key.

        Args:
            key_id: The record identifier.

        Returns:
            The record.

        Raises:
            InvalidApiKeyError: If no such key exists.
        """
        record = self._store.get(key_id)
        if record is None:
            raise InvalidApiKeyError(f"no api key with id {key_id!r}")
        return ApiKeyOutput.from_entity(record)

    def update(self, key_id: str, input: UpdateApiKeyInput) -> ApiKeyOutput:
        """Change a key's mutable fields.

        Args:
            key_id: The record identifier.
            input: The changes. ``None`` fields are left alone.

        Returns:
            The updated record.

        Raises:
            InvalidApiKeyError: If the key does not exist or the values are bad.
        """
        record = self._store.update(
            key_id,
            name=input.name,
            scopes=input.scopes,
            expires_at=input.expires_at,
        )
        _logger.info("api key updated", key_id=key_id)
        return ApiKeyOutput.from_entity(record)

    def revoke(self, key_id: str) -> ApiKeyOutput:
        """Revoke a key.

        Args:
            key_id: The record identifier.

        Returns:
            The revoked record.

        Raises:
            InvalidApiKeyError: If the key does not exist or is already revoked.
        """
        record = self._store.revoke(key_id)
        _logger.warning("api key revoked", key_id=key_id, name=record.name)
        return ApiKeyOutput.from_entity(record)

    @staticmethod
    def available_scopes() -> tuple[str, ...]:
        """Return every grantable scope, for the API documentation.

        Returns:
            The known scopes, sorted.
        """
        return tuple(sorted(VALID_SCOPES))

    @staticmethod
    def default_scopes() -> tuple[str, ...]:
        """Return the scopes a key gets when none are named.

        Returns:
            The default scopes.
        """
        return DEFAULT_SCOPES

    @staticmethod
    def admin_scope() -> str:
        """Return the scope required for key administration.

        Returns:
            The admin scope name.
        """
        return SCOPE_ADMIN
