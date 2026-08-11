"""Configuration via environment variables.

Only the *upstream* Vikunja location and transport binding live here. Deliberately
absent: any Vikunja API token. This server never holds agent credentials — see
``auth.py`` for the token-passthrough model.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .exceptions import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIKUNJA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base URL of the Vikunja instance (without the /api/v1 suffix — the client appends it).
    # No default: an environment-specific hostname baked in here would (a) publish it to
    # every reader of this public repo and (b) let a misconfigured deployment silently fall
    # back to someone else's instance instead of failing. See vikunja#344 (id 363, SC-01).
    url: str = ""
    request_timeout: float = 30.0

    # Transport binding. Loopback-only by default: the token-passthrough model means any
    # local process reaching this port could forward a token it already holds, so exposure
    # beyond localhost is never intended (see SECURITY.md).
    transport: str = "http"
    host: str = "127.0.0.1"
    port: int = 8501


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        settings = Settings()
        if not settings.url:
            raise ConfigError(
                "VIKUNJA_URL is not set. This server has no default Vikunja instance — "
                "set VIKUNJA_URL to the base URL of your Vikunja deployment (without the "
                "/api/v1 suffix) before starting."
            )
        _settings = settings
    return _settings


def reset_settings() -> None:
    """Test hook: drop the cached Settings so the next get_settings() re-reads the env."""
    global _settings
    _settings = None
