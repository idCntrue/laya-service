"""Application settings, sourced from the environment and ``.env``.

Every tunable in the service is declared here exactly once. Nothing is
hard-coded at a call site; if you find yourself wanting a literal, add a field
instead. ``get_settings`` is memoised so that the ``.env`` file is parsed once
per process rather than per request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

#: Loopback addresses for which running without an API key is tolerated. The
#: service refuses to start keyless on anything else, because an unauthenticated
#: inference endpoint is an open door to CPU exhaustion.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Hard ceiling on the request body. Even a well-behaved caller cannot make the
#: service allocate more than this per request.
_MAX_BODY_CEILING: Final[int] = 10 * 1024 * 1024


class Settings(BaseSettings):
    """Runtime configuration for the Laya service.

    Attributes:
        laya_backend: Compute backend hint passed through to Laya. ``"auto"``
            lets the adapter pick.
        laya_model: Model identifier. Empty means "use the adapter's default".
        hf_endpoint: Hugging Face endpoint. Defaults to a mirror, which is
            dramatically faster from mainland China and harmless elsewhere.
        host: Interface to bind.
        port: Port to bind.
        laya_api_key: Shared secret required in the ``Authorization`` header.
            Never logged.
        log_level: Minimum level for the structured logger.
        cors_origins: Comma-separated allowed origins. Empty disables CORS
            entirely, which is the safe default for a server-side API.
        max_body_bytes: Maximum accepted request body size.
        preload_model: When ``True``, load weights during startup instead of on
            first request. Costs startup time but makes readiness meaningful
            immediately.
        model_cache_dir: Where Laya caches downloaded weights.
        request_timeout_s: Wall-clock budget for a single inference call.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    laya_backend: str = Field(default="auto", description="Compute backend: auto|cpu|cuda|mlx")
    laya_model: str = Field(default="", description="Model identifier; empty selects the default")
    hf_endpoint: str = Field(
        default="https://hf-mirror.com", description="Hugging Face endpoint base URL"
    )
    host: str = Field(default="0.0.0.0", description="Interface to bind")
    port: int = Field(default=8000, ge=1, le=65535, description="Port to bind")
    laya_api_key: str = Field(default="", description="Bearer token required by /v1/* routes")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Minimum log level"
    )
    cors_origins: str = Field(default="", description="Comma-separated list of allowed origins")
    max_body_bytes: int = Field(
        default=65536, ge=1, le=_MAX_BODY_CEILING, description="Maximum request body size in bytes"
    )
    preload_model: bool = Field(default=False, description="Load weights during startup")
    model_cache_dir: str = Field(
        default="", description="Override the model weight cache directory"
    )
    request_timeout_s: float = Field(
        default=120.0, gt=0, description="Per-request inference budget in seconds"
    )
    api_keys_path: str = Field(
        default="data/api_keys.json",
        description="Location of the API key file backing /admin/keys",
    )
    compat_enabled: bool = Field(
        default=True,
        description="Serve the OpenAI and Anthropic compatible endpoints",
    )
    compat_models: str = Field(
        default="english",
        description="Comma-separated model ids the compat endpoints advertise",
    )

    @field_validator("cors_origins")
    @classmethod
    def _strip_origins(cls, value: str) -> str:
        """Trim whitespace around each comma-separated origin.

        Args:
            value: The raw environment value.

        Returns:
            The same value with each entry stripped and empty entries dropped.
        """
        return ",".join(part.strip() for part in value.split(",") if part.strip())

    @model_validator(mode="after")
    def _enforce_auth_on_public_bind(self) -> Settings:
        """Refuse to start an unauthenticated service on a routable interface.

        Binding ``0.0.0.0`` with no API key publishes an endpoint that will
        happily consume all available CPU for anyone who finds it. Failing loudly
        at startup is far better than discovering this from a bill.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If the service would bind to a non-loopback interface
                without an API key configured.
        """
        if self.host not in _LOOPBACK_HOSTS and not self.laya_api_key.strip():
            raise ValueError(
                f"HOST={self.host!r} is not a loopback address but LAYA_API_KEY is empty. "
                "Set LAYA_API_KEY, or bind HOST=127.0.0.1 to run without authentication."
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """Return the allowed CORS origins as a list.

        Returns:
            The parsed origins, or an empty list when CORS is disabled.
        """
        if not self.cors_origins:
            return []
        return [origin for origin in self.cors_origins.split(",") if origin]

    @property
    def cors_enabled(self) -> bool:
        """Whether any CORS origins were configured."""
        return bool(self.cors_origin_list)

    @property
    def compat_model_list(self) -> list[str]:
        """Return the model ids the compatibility endpoints advertise.

        The OpenAI ``model`` parameter selects among these. Only the English
        checkpoint is served by default: ``multilingual`` needs another 322 MB
        of weights and roughly 1.5 GB of resident memory, and ``typed-decisions``
        is fine-tuned on four specific question-id sets, so handing it an
        arbitrary schema is out of distribution.

        Returns:
            The advertised model ids, de-duplicated and stripped.
        """
        seen: dict[str, None] = {}
        for part in self.compat_models.split(","):
            cleaned = part.strip()
            if cleaned:
                seen.setdefault(cleaned, None)
        return list(seen) or ["english"]

    @property
    def is_loopback_only(self) -> bool:
        """Whether the service binds exclusively to a loopback address."""
        return self.host in _LOOPBACK_HOSTS

    def redacted(self) -> dict[str, object]:
        """Return a log-safe view of the settings.

        Returns:
            A mapping of field names to values with ``laya_api_key`` replaced by
            a fixed placeholder, so the settings can be logged at startup without
            leaking the secret.
        """
        data = self.model_dump()
        data["laya_api_key"] = "***redacted***" if self.laya_api_key else ""
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Memoised with :func:`functools.lru_cache` so the ``.env`` file is read once.
    Tests clear the cache via ``get_settings.cache_clear()`` to inject overrides.

    Returns:
        The validated settings instance.

    Raises:
        ValueError: If the configuration is invalid -- notably when binding a
            public interface without an API key.
    """
    return Settings()
