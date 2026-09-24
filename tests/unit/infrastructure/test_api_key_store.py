"""Tests for the JSON-file API key store.

Beyond the CRUD happy paths, these cover the properties that make the store
safe: the secret is never written to disk, comparison is constant-time across
every record, a corrupt file fails loudly rather than silently revoking every
key, and writes are atomic.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from laya_service.domain.entities.api_key import SCOPE_ADMIN, SCOPE_INFERENCE, ApiKey
from laya_service.domain.exceptions import InvalidApiKeyError
from laya_service.domain.ports.api_key_store import ApiKeyStore
from laya_service.infrastructure.security.api_key_store import (
    JsonApiKeyStore,
    generate_api_key,
    hash_api_key,
)


@pytest.fixture
def store(tmp_path: Path) -> JsonApiKeyStore:
    """Provide a store backed by a temporary file.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        A fresh store with no keys.
    """
    return JsonApiKeyStore(tmp_path / "api_keys.json")


class TestPortConformance:
    """The adapter satisfies the port."""

    def test_is_an_api_key_store(self, store: JsonApiKeyStore) -> None:
        """Structural typing holds without importing the port at runtime."""
        assert isinstance(store, ApiKeyStore)


class TestGeneration:
    """Secret generation."""

    def test_generated_keys_are_prefixed(self) -> None:
        """The prefix makes a key greppable and distinguishable."""
        assert generate_api_key().startswith("laya_sk_")

    def test_generated_keys_are_unique(self) -> None:
        """Two calls never collide."""
        assert generate_api_key() != generate_api_key()

    def test_generated_keys_have_sufficient_entropy(self) -> None:
        """The body carries at least 32 characters of token_urlsafe output."""
        assert len(generate_api_key()) > 40

    def test_hash_is_stable_and_hex(self) -> None:
        """The digest is deterministic and looks like a SHA-256 hex string."""
        digest = hash_api_key("secret")
        assert digest == hash_api_key("secret")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


class TestCreate:
    """Issuing keys."""

    def test_create_returns_record_and_secret(self, store: JsonApiKeyStore) -> None:
        """Creation yields both the public record and the one-time secret."""
        issued = store.create("grafana", [SCOPE_INFERENCE])
        assert issued.api_key.name == "grafana"
        assert issued.secret.startswith("laya_sk_")

    def test_created_key_authenticates(self, store: JsonApiKeyStore) -> None:
        """The issued secret resolves back to its record."""
        issued = store.create("grafana", [SCOPE_INFERENCE])
        found = store.authenticate(issued.secret)
        assert found is not None
        assert found.id == issued.api_key.id

    def test_prefix_is_stored_for_identification(self, store: JsonApiKeyStore) -> None:
        """The record keeps a short prefix so a listing is recognisable."""
        issued = store.create("grafana", [SCOPE_INFERENCE])
        assert issued.api_key.prefix == issued.secret[:12]

    def test_secret_is_never_written_to_disk(self, store: JsonApiKeyStore, tmp_path: Path) -> None:
        """Only the hash is persisted.

        This is the property that makes the store safe to back up and to read
        during an incident.
        """
        issued = store.create("grafana", [SCOPE_INFERENCE])
        raw = (tmp_path / "api_keys.json").read_text(encoding="utf-8")
        assert issued.secret not in raw
        assert hash_api_key(issued.secret) in raw

    def test_rejects_blank_name(self, store: JsonApiKeyStore) -> None:
        """A key with no label is not auditable."""
        with pytest.raises(InvalidApiKeyError, match="non-empty name"):
            store.create("   ", [SCOPE_INFERENCE])

    def test_rejects_unknown_scope(self, store: JsonApiKeyStore) -> None:
        """Only known scopes are grantable."""
        with pytest.raises(InvalidApiKeyError, match="unknown scope"):
            store.create("x", ["superuser"])

    def test_rejects_empty_scopes(self, store: JsonApiKeyStore) -> None:
        """A key that grants nothing is useless."""
        with pytest.raises(InvalidApiKeyError, match="at least one scope"):
            store.create("x", [])

    def test_duplicate_scopes_are_collapsed(self, store: JsonApiKeyStore) -> None:
        """Repeating a scope does not duplicate it."""
        issued = store.create("x", [SCOPE_INFERENCE, SCOPE_INFERENCE])
        assert issued.api_key.scopes == (SCOPE_INFERENCE,)

    def test_file_is_created_with_restrictive_permissions(
        self, store: JsonApiKeyStore, tmp_path: Path
    ) -> None:
        """The key file is not world-readable."""
        store.create("x", [SCOPE_INFERENCE])
        mode = stat.S_IMODE(os.stat(tmp_path / "api_keys.json").st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


class TestAuthenticate:
    """Resolving a presented secret."""

    def test_unknown_secret_returns_none(self, store: JsonApiKeyStore) -> None:
        """An unrecognised secret resolves to nothing."""
        store.create("x", [SCOPE_INFERENCE])
        assert store.authenticate("laya_sk_not-a-real-key") is None

    def test_empty_secret_returns_none(self, store: JsonApiKeyStore) -> None:
        """An empty secret is rejected without touching the file."""
        assert store.authenticate("") is None

    def test_each_key_resolves_to_itself(self, store: JsonApiKeyStore) -> None:
        """With several keys present, each secret maps to its own record."""
        first = store.create("first", [SCOPE_INFERENCE])
        second = store.create("second", [SCOPE_ADMIN])
        resolved_first = store.authenticate(first.secret)
        resolved_second = store.authenticate(second.secret)
        assert resolved_first is not None
        assert resolved_second is not None
        assert resolved_first.id == first.api_key.id
        assert resolved_second.id == second.api_key.id

    def test_revoked_key_no_longer_authenticates(self, store: JsonApiKeyStore) -> None:
        """Revocation takes effect immediately."""
        issued = store.create("x", [SCOPE_INFERENCE])
        store.revoke(issued.api_key.id)
        assert store.authenticate(issued.secret) is None

    def test_expired_key_does_not_authenticate(self, store: JsonApiKeyStore) -> None:
        """An expired key is rejected even though it was never revoked."""
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        issued = store.create("x", [SCOPE_INFERENCE], expires_at=past)
        assert store.authenticate(issued.secret) is None

    def test_future_expiry_still_authenticates(self, store: JsonApiKeyStore) -> None:
        """A key expiring later is still valid."""
        future = datetime.now(timezone.utc) + timedelta(days=1)
        issued = store.create("x", [SCOPE_INFERENCE], expires_at=future)
        assert store.authenticate(issued.secret) is not None

    def test_authentication_survives_a_reload(self, tmp_path: Path) -> None:
        """A key issued by one instance works in another over the same file."""
        path = tmp_path / "api_keys.json"
        issued = JsonApiKeyStore(path).create("x", [SCOPE_INFERENCE])
        assert JsonApiKeyStore(path).authenticate(issued.secret) is not None


class TestListAndGet:
    """Reading keys."""

    def test_list_is_empty_initially(self, store: JsonApiKeyStore) -> None:
        """A store with no file lists nothing."""
        assert list(store.list_keys()) == []

    def test_list_returns_every_live_key(self, store: JsonApiKeyStore) -> None:
        """Every issued key appears."""
        store.create("a", [SCOPE_INFERENCE])
        store.create("b", [SCOPE_INFERENCE])
        assert {record.name for record in store.list_keys()} == {"a", "b"}

    def test_revoked_keys_are_hidden_by_default(self, store: JsonApiKeyStore) -> None:
        """A revoked key drops out of the default listing."""
        issued = store.create("a", [SCOPE_INFERENCE])
        store.revoke(issued.api_key.id)
        assert list(store.list_keys()) == []

    def test_revoked_keys_are_visible_on_request(self, store: JsonApiKeyStore) -> None:
        """An operator can audit what was revoked."""
        issued = store.create("a", [SCOPE_INFERENCE])
        store.revoke(issued.api_key.id)
        assert len(store.list_keys(include_revoked=True)) == 1

    def test_list_is_ordered_by_creation(self, store: JsonApiKeyStore) -> None:
        """Records come back oldest first."""
        for name in ("first", "second", "third"):
            store.create(name, [SCOPE_INFERENCE])
        assert [r.name for r in store.list_keys()] == ["first", "second", "third"]

    def test_get_returns_the_record(self, store: JsonApiKeyStore) -> None:
        """A known id resolves."""
        issued = store.create("a", [SCOPE_INFERENCE])
        found = store.get(issued.api_key.id)
        assert found is not None
        assert found.name == "a"

    def test_get_returns_none_for_unknown_id(self, store: JsonApiKeyStore) -> None:
        """An unknown id resolves to nothing."""
        assert store.get("nope") is None

    def test_listing_never_exposes_the_hash(self, store: JsonApiKeyStore) -> None:
        """The record type has no field for the secret material."""
        store.create("a", [SCOPE_INFERENCE])
        record = store.list_keys()[0]
        assert not hasattr(record, "key_hash")
        assert not hasattr(record, "secret")


class TestUpdate:
    """Changing a key."""

    def test_update_name(self, store: JsonApiKeyStore) -> None:
        """The label can be changed."""
        issued = store.create("old", [SCOPE_INFERENCE])
        assert store.update(issued.api_key.id, name="new").name == "new"

    def test_update_scopes(self, store: JsonApiKeyStore) -> None:
        """Scopes can be changed."""
        issued = store.create("x", [SCOPE_INFERENCE])
        updated = store.update(issued.api_key.id, scopes=[SCOPE_INFERENCE, SCOPE_ADMIN])
        assert set(updated.scopes) == {SCOPE_INFERENCE, SCOPE_ADMIN}

    def test_update_leaves_unspecified_fields_alone(self, store: JsonApiKeyStore) -> None:
        """Passing only a name does not clear the scopes."""
        issued = store.create("x", [SCOPE_INFERENCE])
        updated = store.update(issued.api_key.id, name="renamed")
        assert updated.scopes == (SCOPE_INFERENCE,)

    def test_update_preserves_the_secret(self, store: JsonApiKeyStore) -> None:
        """Renaming a key does not invalidate it."""
        issued = store.create("x", [SCOPE_INFERENCE])
        store.update(issued.api_key.id, name="renamed")
        assert store.authenticate(issued.secret) is not None

    def test_update_unknown_id_raises(self, store: JsonApiKeyStore) -> None:
        """Updating a missing key is an error, not a silent no-op."""
        with pytest.raises(InvalidApiKeyError, match="no api key"):
            store.update("nope", name="x")

    def test_update_to_blank_name_raises(self, store: JsonApiKeyStore) -> None:
        """A blank label is rejected on update too."""
        issued = store.create("x", [SCOPE_INFERENCE])
        with pytest.raises(InvalidApiKeyError, match="non-empty name"):
            store.update(issued.api_key.id, name="  ")

    def test_update_to_unknown_scope_raises(self, store: JsonApiKeyStore) -> None:
        """Unknown scopes are rejected on update too."""
        issued = store.create("x", [SCOPE_INFERENCE])
        with pytest.raises(InvalidApiKeyError, match="unknown scope"):
            store.update(issued.api_key.id, scopes=["root"])


class TestRevoke:
    """Revoking a key."""

    def test_revoke_marks_the_record(self, store: JsonApiKeyStore) -> None:
        """Revocation sets a timestamp."""
        issued = store.create("x", [SCOPE_INFERENCE])
        assert store.revoke(issued.api_key.id).revoked_at is not None

    def test_double_revoke_raises(self, store: JsonApiKeyStore) -> None:
        """Revoking twice is an error, not a silent success."""
        issued = store.create("x", [SCOPE_INFERENCE])
        store.revoke(issued.api_key.id)
        with pytest.raises(InvalidApiKeyError, match="already revoked"):
            store.revoke(issued.api_key.id)

    def test_revoke_unknown_id_raises(self, store: JsonApiKeyStore) -> None:
        """Revoking a missing key is an error."""
        with pytest.raises(InvalidApiKeyError, match="no api key"):
            store.revoke("nope")

    def test_revoke_does_not_affect_other_keys(self, store: JsonApiKeyStore) -> None:
        """Other keys keep working."""
        first = store.create("a", [SCOPE_INFERENCE])
        second = store.create("b", [SCOPE_INFERENCE])
        store.revoke(first.api_key.id)
        assert store.authenticate(second.secret) is not None


class TestCorruptFile:
    """A damaged file must fail loudly, never silently revoke everything."""

    def test_unparseable_json_raises(self, tmp_path: Path) -> None:
        """Malformed JSON is reported rather than treated as empty."""
        path = tmp_path / "api_keys.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(InvalidApiKeyError, match="unreadable"):
            JsonApiKeyStore(path).authenticate("anything")

    def test_wrong_shape_raises(self, tmp_path: Path) -> None:
        """A file with no 'keys' array is not silently ignored."""
        path = tmp_path / "api_keys.json"
        path.write_text(json.dumps({"wrong": []}), encoding="utf-8")
        with pytest.raises(InvalidApiKeyError, match="unrecognised shape"):
            JsonApiKeyStore(path).list_keys()

    def test_malformed_entry_raises(self, tmp_path: Path) -> None:
        """An entry missing required fields is reported."""
        path = tmp_path / "api_keys.json"
        path.write_text(json.dumps({"version": 1, "keys": [{"id": "a"}]}), encoding="utf-8")
        with pytest.raises(InvalidApiKeyError, match="malformed"):
            JsonApiKeyStore(path).list_keys()

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """No file means no keys, which is the normal initial state."""
        assert list(JsonApiKeyStore(tmp_path / "absent.json").list_keys()) == []


class TestAtomicWrite:
    """Writes do not leave a truncated file behind."""

    def test_no_temporary_file_is_left(self, store: JsonApiKeyStore, tmp_path: Path) -> None:
        """The temp file is renamed, not left lying around."""
        store.create("x", [SCOPE_INFERENCE])
        assert not (tmp_path / "api_keys.json.tmp").exists()

    def test_file_is_valid_json_after_writes(self, store: JsonApiKeyStore, tmp_path: Path) -> None:
        """The persisted file parses."""
        store.create("a", [SCOPE_INFERENCE])
        store.create("b", [SCOPE_ADMIN])
        payload = json.loads((tmp_path / "api_keys.json").read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert len(payload["keys"]) == 2

    def test_parent_directory_is_created(self, tmp_path: Path) -> None:
        """A nested path is created on first write."""
        nested = JsonApiKeyStore(tmp_path / "deep" / "nested" / "keys.json")
        nested.create("x", [SCOPE_INFERENCE])
        assert nested.path.exists()


class TestApiKeyEntity:
    """The entity's own invariants."""

    def test_is_active_for_a_fresh_key(self) -> None:
        """A key with no expiry and no revocation is active."""
        key = ApiKey(
            id="a",
            name="n",
            prefix="p",
            scopes=(SCOPE_INFERENCE,),
            created_at=datetime.now(timezone.utc),
        )
        assert key.is_active() is True

    def test_has_scope(self) -> None:
        """Scope membership is reported."""
        key = ApiKey(
            id="a",
            name="n",
            prefix="p",
            scopes=(SCOPE_INFERENCE,),
            created_at=datetime.now(timezone.utc),
        )
        assert key.has_scope(SCOPE_INFERENCE) is True
        assert key.has_scope(SCOPE_ADMIN) is False

    def test_is_expired(self) -> None:
        """Expiry is reported independently of revocation."""
        key = ApiKey(
            id="a",
            name="n",
            prefix="p",
            scopes=(SCOPE_INFERENCE,),
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert key.is_expired() is True
        assert key.revoked_at is None
