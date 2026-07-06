"""Configuration via environment variables.

Only the *upstream* Vikunja location and transport binding live here. Deliberately
absent: any Vikunja API token. This server never holds agent credentials — see
``auth.py`` for the token-passthrough model.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIKUNJA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base URL of the Vikunja instance (without the /api/v1 suffix — the client appends it).
    url: str = "https://vikunja.helmforge.me"
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
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test hook: drop the cached Settings so the next get_settings() re-reads the env."""
    global _settings
    _settings = None
