"""A JSON-file API key store.

Chosen over SQLite because the working set is tens of keys, the file is an
operational artefact an engineer can read during an incident, and there is no
schema to version. Every mutation is a read-modify-write of the whole file,
which at this scale is free.

Two properties matter more than the storage choice:

**Secrets are never stored.** Only ``sha256(secret)`` is written. The plaintext
is returned once, at creation, and is unrecoverable afterwards -- which is what
makes the create response the only place a caller ever sees it.

**Comparison is constant-time and uniform.** Every stored record is compared
against the presented secret, in order, with :func:`hmac.compare_digest`, and
the loop never short-circuits. A dictionary lookup keyed on the hash, or an
early return on the first match, would leak the key's existence and position
through response timing. The cost is O(n) per request, where n is the number of
keys -- microseconds at this scale, and uniform regardless of which key matched.

A plain SHA-256 is the correct hash here, not a shortcut. The secret is 256 bits
of CSPRNG output, so there is no dictionary to attack and nothing for a work
factor to buy; a slow KDF would only add latency to every authenticated request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from laya_service.domain.entities.api_key import VALID_SCOPES, ApiKey
from laya_service.domain.exceptions import InvalidApiKeyError
from laya_service.domain.ports.api_key_store import IssuedApiKey
from laya_service.logging_config import get_logger

__all__ = ["JsonApiKeyStore", "generate_api_key", "hash_api_key"]

_logger = get_logger(__name__, component="security")

#: Prefix on every issued secret. Makes a key greppable in logs and
#: distinguishable from other credentials, which matters when the alternative is
#: discovering a leak by accident.
_KEY_PREFIX: Final[str] = "laya_sk_"

#: Bytes of entropy. 32 bytes = 256 bits, which is why a plain hash suffices.
_KEY_BYTES: Final[int] = 32

#: How much of the secret is kept in the clear for identification.
_PREFIX_CHARS: Final[int] = 12

#: Schema version of the on-disk file, so a future format change can migrate
#: rather than guess.
_FILE_VERSION: Final[int] = 1


def generate_api_key() -> str:
    """Generate a new API key secret.

    Returns:
        A ``laya_sk_``-prefixed token with 256 bits of entropy.
    """
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(_KEY_BYTES)}"


def hash_api_key(secret: str) -> str:
    """Hash a secret for storage or comparison.

    Args:
        secret: The plaintext secret.

    Returns:
        The SHA-256 hex digest.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class JsonApiKeyStore:
    """An :class:`~laya_service.domain.ports.api_key_store.ApiKeyStore` backed by a JSON file.

    Attributes:
        _path: Location of the key file.
        _lock: Guards read-modify-write cycles. The service runs a single
            worker, so a process-local lock is sufficient; a multi-worker
            deployment would need a real file lock and is not supported.
    """

    def __init__(self, path: str | Path) -> None:
        """Initialize the store.

        Args:
            path: Location of the key file. Created on first write, along with
                its parent directory.
        """
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The file backing this store."""
        return self._path

    # -- Port implementation ------------------------------------------------

    def create(
        self,
        name: str,
        scopes: Sequence[str],
        expires_at: datetime | None = None,
    ) -> IssuedApiKey:
        """Issue a new key.

        Args:
            name: Operator-facing label.
            scopes: Scopes to grant.
            expires_at: Optional expiry.

        Returns:
            The new record and its one-time plaintext secret.

        Raises:
            InvalidApiKeyError: If the name is blank or the scopes are unknown.
        """
        cleaned = name.strip()
        if not cleaned:
            raise InvalidApiKeyError("an api key must have a non-empty name")
        granted = tuple(dict.fromkeys(scopes))
        unknown = sorted(set(granted) - VALID_SCOPES)
        if unknown:
            raise InvalidApiKeyError(
                f"unknown scope(s) {unknown}; valid scopes are {sorted(VALID_SCOPES)}"
            )
        if not granted:
            raise InvalidApiKeyError("an api key must grant at least one scope")

        secret = generate_api_key()
        record = ApiKey(
            id=uuid.uuid4().hex,
            name=cleaned,
            prefix=secret[:_PREFIX_CHARS],
            scopes=granted,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )

        with self._lock:
            entries = self._read()
            entries.append(self._to_entry(record, hash_api_key(secret)))
            self._write(entries)

        _logger.info("api key created", key_id=record.id, name=record.name, scopes=list(granted))
        return IssuedApiKey(api_key=record, secret=secret)

    def list_keys(self, *, include_revoked: bool = False) -> Sequence[ApiKey]:
        """List known keys.

        Args:
            include_revoked: Include revoked keys for auditing.

        Returns:
            The matching records, oldest first.
        """
        with self._lock:
            entries = self._read()
        records = [self._from_entry(entry) for entry in entries]
        if not include_revoked:
            records = [record for record in records if record.revoked_at is None]
        return sorted(records, key=lambda record: record.created_at)

    def get(self, key_id: str) -> ApiKey | None:
        """Look up one key by identifier.

        Args:
            key_id: The record identifier.

        Returns:
            The record, or ``None``.
        """
        with self._lock:
            entries = self._read()
        for entry in entries:
            if entry.get("id") == key_id:
                return self._from_entry(entry)
        return None

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
            InvalidApiKeyError: If the key does not exist or the values are bad.
        """
        if name is not None and not name.strip():
            raise InvalidApiKeyError("an api key must have a non-empty name")
        if scopes is not None:
            granted = tuple(dict.fromkeys(scopes))
            unknown = sorted(set(granted) - VALID_SCOPES)
            if unknown:
                raise InvalidApiKeyError(
                    f"unknown scope(s) {unknown}; valid scopes are {sorted(VALID_SCOPES)}"
                )
            if not granted:
                raise InvalidApiKeyError("an api key must grant at least one scope")

        with self._lock:
            entries = self._read()
            for entry in entries:
                if entry.get("id") != key_id:
                    continue
                if name is not None:
                    entry["name"] = name.strip()
                if scopes is not None:
                    entry["scopes"] = list(dict.fromkeys(scopes))
                if expires_at is not None:
                    entry["expires_at"] = expires_at.isoformat()
                self._write(entries)
                _logger.info("api key updated", key_id=key_id)
                return self._from_entry(entry)

        raise InvalidApiKeyError(f"no api key with id {key_id!r}")

    def revoke(self, key_id: str) -> ApiKey:
        """Revoke a key.

        Args:
            key_id: The record identifier.

        Returns:
            The revoked record.

        Raises:
            InvalidApiKeyError: If the key does not exist or is already revoked.
        """
        with self._lock:
            entries = self._read()
            for entry in entries:
                if entry.get("id") != key_id:
                    continue
                if entry.get("revoked_at") is not None:
                    raise InvalidApiKeyError(f"api key {key_id!r} is already revoked")
                entry["revoked_at"] = datetime.now(timezone.utc).isoformat()
                self._write(entries)
                _logger.warning("api key revoked", key_id=key_id)
                return self._from_entry(entry)

        raise InvalidApiKeyError(f"no api key with id {key_id!r}")

    def authenticate(self, presented: str) -> ApiKey | None:
        """Resolve a presented secret to its key, in constant time.

        Every record is compared, and the loop does not break early, so the work
        performed is identical whether the secret matches the first record, the
        last, or none. A ``dict`` lookup keyed on the digest would be faster and
        would leak which keys exist.

        Args:
            presented: The secret supplied by the caller.

        Returns:
            The matching active key, or ``None``.
        """
        if not presented:
            return None

        digest = hash_api_key(presented)
        now = datetime.now(timezone.utc)
        matched: ApiKey | None = None

        with self._lock:
            entries = self._read()

        for entry in entries:
            stored = entry.get("key_hash")
            if not isinstance(stored, str):
                continue
            # compare_digest on two equal-length hex strings. No short-circuit:
            # the result is recorded, not returned.
            if hmac.compare_digest(digest, stored) and matched is None:
                candidate = self._from_entry(entry)
                if candidate.is_active(now):
                    matched = candidate

        return matched

    def touch(self, key_id: str) -> None:
        """Record that a key was used.

        Best-effort: a failure here must never fail the request it is recording,
        so errors are logged and swallowed.

        Args:
            key_id: The record identifier.
        """
        try:
            with self._lock:
                entries = self._read()
                for entry in entries:
                    if entry.get("id") == key_id:
                        entry["last_used_at"] = datetime.now(timezone.utc).isoformat()
                        self._write(entries)
                        return
        except Exception:
            _logger.warning("could not record key usage", key_id=key_id, exc_info=True)

    # -- File handling ------------------------------------------------------

    def _read(self) -> list[dict[str, Any]]:
        """Read the key file.

        A missing file is normal -- it means no keys have been issued yet -- and
        yields an empty list. A corrupt file is not normal, but returning empty
        would silently revoke every key, so it raises instead.

        Returns:
            The stored entries.

        Raises:
            InvalidApiKeyError: If the file exists but cannot be parsed.
        """
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidApiKeyError(
                f"api key store at {self._path} is unreadable: {exc}. "
                "Refusing to continue with an empty store, which would revoke every key."
            ) from exc

        if not isinstance(raw, dict) or not isinstance(raw.get("keys"), list):
            raise InvalidApiKeyError(
                f"api key store at {self._path} has an unrecognised shape; "
                "expected an object with a 'keys' array"
            )
        entries = raw["keys"]
        return [entry for entry in entries if isinstance(entry, dict)]

    def _write(self, entries: list[dict[str, Any]]) -> None:
        """Write the key file atomically.

        The temporary file is created in the same directory as the target, so
        :func:`os.replace` is an atomic rename on the same filesystem. A crash
        mid-write leaves the previous file intact rather than a truncated one.

        Args:
            entries: The entries to persist.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _FILE_VERSION, "keys": entries}
        temporary = self._path.with_name(f"{self._path.name}.tmp")

        # Create with 0600 from the start rather than chmod-ing afterwards, so
        # the file is never briefly world-readable.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, self._path)

    @staticmethod
    def _to_entry(record: ApiKey, key_hash: str) -> dict[str, Any]:
        """Render a record as a storable entry.

        Args:
            record: The public record.
            key_hash: The digest of the secret.

        Returns:
            A JSON-serialisable mapping.
        """
        return {
            "id": record.id,
            "name": record.name,
            "prefix": record.prefix,
            "key_hash": key_hash,
            "scopes": list(record.scopes),
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
            "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        }

    @staticmethod
    def _from_entry(entry: dict[str, Any]) -> ApiKey:
        """Rebuild a record from a stored entry.

        Args:
            entry: The stored mapping.

        Returns:
            The record.

        Raises:
            InvalidApiKeyError: If the entry is missing required fields or holds
                an unparseable timestamp.
        """
        try:
            return ApiKey(
                id=str(entry["id"]),
                name=str(entry["name"]),
                prefix=str(entry.get("prefix", "")),
                scopes=tuple(entry.get("scopes") or ()),
                created_at=_require_time(entry["created_at"], entry),
                expires_at=_parse_time(entry.get("expires_at")),
                revoked_at=_parse_time(entry.get("revoked_at")),
                last_used_at=_parse_time(entry.get("last_used_at")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidApiKeyError(
                f"api key entry {entry.get('id')!r} is malformed: {exc}"
            ) from exc


def _require_time(value: Any, entry: dict[str, Any]) -> datetime:
    """Parse a required ISO-8601 timestamp from the key file.

    Args:
        value: The stored value.
        entry: The whole entry, for the error message.

    Returns:
        The datetime.

    Raises:
        InvalidApiKeyError: If the value is missing or unparseable. A key with
            no creation time cannot be ordered or audited, so it is treated as
            corrupt rather than defaulted.
    """
    parsed = _parse_time(value)
    if parsed is None:
        raise InvalidApiKeyError(f"api key entry {entry.get('id')!r} is missing 'created_at'")
    return parsed


def _parse_time(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from the key file.

    Args:
        value: The stored value, or ``None``.

    Returns:
        The datetime, or ``None`` when absent.

    Raises:
        ValueError: If the value is present but not a valid timestamp.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"expected an ISO-8601 string, got {type(value).__name__}")
    return datetime.fromisoformat(value)
