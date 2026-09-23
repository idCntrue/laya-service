"""Tests for the settings model and its singleton accessor.

Almost every test here passes ``_env_file=None`` so the developer's own ``.env``
cannot influence the result, and ``host="127.0.0.1"`` so the public-bind
validator does not fire incidentally. The validator has its own class below.
"""

from __future__ import annotations

from typing import Any

import pytest

from laya_service.infrastructure.config.settings import Settings, get_settings

#: Keyword arguments that make a Settings instance that always validates,
#: regardless of the ambient environment.
SAFE: dict[str, Any] = {"host": "127.0.0.1", "laya_api_key": "", "_env_file": None}


class TestDefaults:
    """Default values."""

    def test_defaults_are_sane(self) -> None:
        """An environment-free construction yields usable defaults."""
        settings = Settings(**SAFE)
        assert settings.laya_backend == "auto"
        assert settings.port == 8000
        assert settings.log_level == "INFO"
        assert settings.max_body_bytes == 65536
        assert settings.preload_model is False

    def test_hf_endpoint_defaults_to_the_mirror(self) -> None:
        """The mirror is the default because it is faster on this network."""
        assert Settings(**SAFE).hf_endpoint == "https://hf-mirror.com"

    def test_cors_is_disabled_by_default(self) -> None:
        """A server-to-server API should not enable CORS by accident."""
        assert Settings(**SAFE).cors_enabled is False


class TestSecurityValidator:
    """The rule that prevents an unauthenticated public endpoint."""

    def test_public_bind_without_key_is_rejected(self) -> None:
        """Binding 0.0.0.0 with no key must fail loudly at startup."""
        with pytest.raises(ValueError, match="LAYA_API_KEY"):
            Settings(host="0.0.0.0", laya_api_key="", _env_file=None)  # type: ignore[call-arg]

    def test_public_bind_with_key_is_accepted(self) -> None:
        """A key makes a public bind acceptable."""
        settings = Settings(host="0.0.0.0", laya_api_key="secret", _env_file=None)  # type: ignore[call-arg]
        assert settings.laya_api_key == "secret"

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_bind_without_key_is_accepted(self, host: str) -> None:
        """Loopback-only deployments may run keyless for local development."""
        assert Settings(host=host, laya_api_key="", _env_file=None).host == host  # type: ignore[call-arg]

    def test_whitespace_key_does_not_count_as_set(self) -> None:
        """A whitespace-only key is treated as absent."""
        with pytest.raises(ValueError, match="LAYA_API_KEY"):
            Settings(host="0.0.0.0", laya_api_key="   ", _env_file=None)  # type: ignore[call-arg]

    def test_error_message_names_the_fix(self) -> None:
        """The startup error tells the operator what to do."""
        with pytest.raises(ValueError, match=r"bind HOST=127\.0\.0\.1"):
            Settings(host="0.0.0.0", laya_api_key="", _env_file=None)  # type: ignore[call-arg]

    def test_is_loopback_only_flag(self) -> None:
        """The loopback predicate is exposed for the app factory."""
        assert Settings(**SAFE).is_loopback_only is True
        assert Settings(host="0.0.0.0", laya_api_key="k", _env_file=None).is_loopback_only is False  # type: ignore[call-arg]


class TestCorsParsing:
    """CORS origin list handling."""

    def test_empty_disables_cors(self) -> None:
        """No origins means CORS is off."""
        settings = Settings(cors_origins="", **SAFE)
        assert settings.cors_origin_list == []
        assert settings.cors_enabled is False

    def test_comma_separated_list_is_split(self) -> None:
        """A comma-separated list is parsed into entries."""
        settings = Settings(cors_origins="https://a.example,https://b.example", **SAFE)
        assert settings.cors_origin_list == ["https://a.example", "https://b.example"]
        assert settings.cors_enabled is True

    def test_whitespace_is_stripped(self) -> None:
        """Stray whitespace around entries is removed."""
        settings = Settings(cors_origins=" https://a.example , https://b.example ", **SAFE)
        assert settings.cors_origin_list == ["https://a.example", "https://b.example"]

    def test_empty_entries_are_dropped(self) -> None:
        """A trailing comma does not produce an empty origin."""
        settings = Settings(cors_origins="https://a.example,", **SAFE)
        assert settings.cors_origin_list == ["https://a.example"]


class TestValidation:
    """Field-level constraints."""

    @pytest.mark.parametrize("port", [0, -1, 70000])
    def test_rejects_invalid_port(self, port: int) -> None:
        """Ports outside the valid range are rejected."""
        with pytest.raises(ValueError):
            Settings(port=port, **SAFE)

    def test_rejects_invalid_log_level(self) -> None:
        """Only real log levels are accepted."""
        with pytest.raises(ValueError):
            Settings(log_level="CHATTY", **SAFE)  # type: ignore[arg-type]

    def test_rejects_oversized_body_limit(self) -> None:
        """The body limit has a hard ceiling."""
        with pytest.raises(ValueError):
            Settings(max_body_bytes=100 * 1024 * 1024, **SAFE)

    def test_rejects_zero_timeout(self) -> None:
        """A zero inference timeout is not meaningful."""
        with pytest.raises(ValueError):
            Settings(request_timeout_s=0, **SAFE)


class TestRedaction:
    """The log-safe view."""

    def test_api_key_is_redacted(self) -> None:
        """The secret never appears in the redacted dump."""
        settings = Settings(host="127.0.0.1", laya_api_key="super-secret", _env_file=None)  # type: ignore[call-arg]
        dumped = settings.redacted()
        assert dumped["laya_api_key"] == "***redacted***"
        assert "super-secret" not in str(dumped)

    def test_empty_key_stays_empty(self) -> None:
        """An unset key is reported as empty rather than redacted."""
        assert Settings(**SAFE).redacted()["laya_api_key"] == ""

    def test_other_fields_survive(self) -> None:
        """Redaction does not drop unrelated configuration."""
        settings = Settings(host="127.0.0.1", laya_api_key="x", _env_file=None)  # type: ignore[call-arg]
        assert settings.redacted()["port"] == settings.port


class TestSingleton:
    """``get_settings`` memoisation."""

    def test_returns_the_same_instance(self) -> None:
        """Repeated calls return one object.

        The ambient environment may or may not be valid (a bare checkout has no
        ``.env``, so the default ``0.0.0.0`` bind without a key raises), so this
        asserts on identity only when construction succeeds.
        """
        get_settings.cache_clear()
        try:
            try:
                first = get_settings()
            except ValueError:
                pytest.skip("ambient environment has no LAYA_API_KEY; covered elsewhere")
            assert first is get_settings()
        finally:
            get_settings.cache_clear()

    def test_cache_clear_produces_a_new_instance(self) -> None:
        """Clearing the cache re-reads the environment."""
        get_settings.cache_clear()
        try:
            try:
                first = get_settings()
            except ValueError:
                pytest.skip("ambient environment has no LAYA_API_KEY; covered elsewhere")
            get_settings.cache_clear()
            assert first is not get_settings()
        finally:
            get_settings.cache_clear()
