"""API key administration endpoints.

Deliberately **not** under ``/v1/``: these are not part of the model API, and
they must not appear as OpenAI- or Anthropic-compatible surface. They are
operator endpoints.

Every route here requires the ``admin`` scope, enforced by the auth middleware
from the path prefix. That is what stops a leaked inference key from minting
itself a permanent replacement.

The secret is returned exactly once, by ``POST /admin/keys``, and is
unrecoverable afterwards -- the store keeps only its hash. That is the same
contract every credential provider uses, and it is what makes the store safe to
read during an incident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from laya_service.application.dto.api_key_dto import (
    ApiKeyOutput,
    CreateApiKeyInput,
    UpdateApiKeyInput,
)
from laya_service.application.use_cases.manage_api_keys import ManageApiKeysUseCase
from laya_service.interfaces.http.dependencies import get_manage_api_keys_use_case

__all__ = ["router"]

router = APIRouter(tags=["admin"], prefix="/admin")


class CreateKeyRequest(BaseModel):
    """Request body for issuing a key.

    Attributes:
        name: Operator-facing label. Required.
        scopes: Scopes to grant. Defaults to inference only, so minting an
            administrator takes a deliberate act.
        expires_at: Optional expiry.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, description="Operator-facing label.")
    scopes: list[str] | None = Field(
        default=None, description="Scopes to grant. Defaults to ['inference']."
    )
    expires_at: datetime | None = Field(default=None, description="Optional expiry.")


class UpdateKeyRequest(BaseModel):
    """Request body for changing a key.

    A field left out is left unchanged; there is no way to clear one.

    Attributes:
        name: New label.
        scopes: New scopes.
        expires_at: New expiry.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128, description="New label.")
    scopes: list[str] | None = Field(default=None, description="New scopes.")
    expires_at: datetime | None = Field(default=None, description="New expiry.")


class ApiKeyView(BaseModel):
    """A key's public record.

    Attributes:
        id: Stable identifier.
        name: Operator label.
        prefix: First characters of the secret, for identification.
        scopes: Granted scopes.
        created_at: When issued.
        expires_at: Optional expiry.
        revoked_at: When revoked, if it was.
        last_used_at: When the key last authenticated.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable identifier.")
    name: str = Field(..., description="Operator-facing label.")
    prefix: str = Field(..., description="First characters of the secret, for identification.")
    scopes: list[str] = Field(..., description="Granted scopes.")
    created_at: datetime = Field(..., description="When the key was issued.")
    expires_at: datetime | None = Field(default=None, description="Optional expiry.")
    revoked_at: datetime | None = Field(default=None, description="When the key was revoked.")
    last_used_at: datetime | None = Field(default=None, description="Last successful use.")

    @classmethod
    def from_output(cls, output: ApiKeyOutput) -> ApiKeyView:
        """Build the view from the use case's DTO.

        Args:
            output: The DTO.

        Returns:
            The corresponding view.
        """
        return cls(
            id=output.id,
            name=output.name,
            prefix=output.prefix,
            scopes=list(output.scopes),
            created_at=output.created_at,
            expires_at=output.expires_at,
            revoked_at=output.revoked_at,
            last_used_at=output.last_used_at,
        )


class IssuedKeyView(ApiKeyView):
    """A newly issued key, including its one-time secret.

    Attributes:
        secret: The plaintext secret. Present only in this response.
    """

    secret: str = Field(..., description="The plaintext secret. Shown once, never again.")


class KeyListView(BaseModel):
    """Response body for listing keys.

    Attributes:
        keys: The matching records.
        available_scopes: Every grantable scope, so a caller need not guess.
        default_scopes: What a key receives when no scopes are named.
    """

    model_config = ConfigDict(extra="forbid")

    keys: list[ApiKeyView] = Field(..., description="The matching records.")
    available_scopes: list[str] = Field(..., description="Every grantable scope.")
    default_scopes: list[str] = Field(..., description="Scopes applied when none are named.")


@router.get("/keys")
async def list_keys(
    use_case: Annotated[ManageApiKeysUseCase, Depends(get_manage_api_keys_use_case)],
    include_revoked: Annotated[
        bool, Query(description="Include revoked keys, for auditing.")
    ] = False,
) -> KeyListView:
    """List API keys.

    The secret is never included -- only its prefix, so an operator can tell
    which key a row refers to.

    Args:
        use_case: The injected use case.
        include_revoked: Whether to include revoked keys.

    Returns:
        The key list.
    """
    return KeyListView(
        keys=[
            ApiKeyView.from_output(record)
            for record in use_case.list_keys(include_revoked=include_revoked)
        ],
        available_scopes=list(use_case.available_scopes()),
        default_scopes=list(use_case.default_scopes()),
    )


@router.post("/keys", status_code=201)
async def create_key(
    body: CreateKeyRequest,
    use_case: Annotated[ManageApiKeysUseCase, Depends(get_manage_api_keys_use_case)],
) -> IssuedKeyView:
    """Issue a new API key.

    Args:
        body: The creation request.
        use_case: The injected use case.

    Returns:
        The new record, including the one-time plaintext secret.

    Raises:
        InvalidApiKeyError: If the name is blank or a scope is unknown. Mapped to
            400 by the shared exception handlers.
    """
    issued = use_case.create(
        CreateApiKeyInput(
            name=body.name,
            scopes=body.scopes or use_case.default_scopes(),
            expires_at=body.expires_at,
        )
    )
    view = ApiKeyView.from_output(issued.api_key)
    return IssuedKeyView(**view.model_dump(), secret=issued.secret)


@router.get("/keys/{key_id}")
async def get_key(
    key_id: str,
    use_case: Annotated[ManageApiKeysUseCase, Depends(get_manage_api_keys_use_case)],
) -> ApiKeyView:
    """Look up one API key.

    Args:
        key_id: The record identifier.
        use_case: The injected use case.

    Returns:
        The record.

    Raises:
        InvalidApiKeyError: If no such key exists.
    """
    return ApiKeyView.from_output(use_case.get(key_id))


@router.patch("/keys/{key_id}")
async def update_key(
    key_id: str,
    body: UpdateKeyRequest,
    use_case: Annotated[ManageApiKeysUseCase, Depends(get_manage_api_keys_use_case)],
) -> ApiKeyView:
    """Change an API key's label, scopes, or expiry.

    Args:
        key_id: The record identifier.
        body: The changes. Omitted fields are left alone.
        use_case: The injected use case.

    Returns:
        The updated record.

    Raises:
        InvalidApiKeyError: If the key does not exist or the values are bad.
    """
    return ApiKeyView.from_output(
        use_case.update(
            key_id,
            UpdateApiKeyInput(name=body.name, scopes=body.scopes, expires_at=body.expires_at),
        )
    )


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: str,
    use_case: Annotated[ManageApiKeysUseCase, Depends(get_manage_api_keys_use_case)],
) -> ApiKeyView:
    """Revoke an API key.

    The key stops authenticating immediately. The record is kept with a
    revocation timestamp so the action is auditable.

    Args:
        key_id: The record identifier.
        use_case: The injected use case.

    Returns:
        The revoked record.

    Raises:
        InvalidApiKeyError: If the key does not exist or is already revoked.
    """
    return ApiKeyView.from_output(use_case.revoke(key_id))


def _unused(_: Any) -> None:
    """Keep the Any import meaningful for future typed payloads."""
