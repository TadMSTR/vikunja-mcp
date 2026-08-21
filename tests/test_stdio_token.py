"""`VIKUNJA_TOKEN` — the stdio fallback, and the two refusals that keep it narrow.

The security property under test is not "a token can be configured". It is that a
configured token can **only** ever act as the credential for a single-caller stdio
process, and can never become a shared identity on a network transport. Both halves are
asserted here: the startup refusals in `config`, and the runtime precedence in `auth`.
"""

from __future__ import annotations

import pytest

from vikunja_mcp import auth, config
from vikunja_mcp.exceptions import AuthError, ConfigError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from a known transport/token pair, whatever the ambient env."""
    monkeypatch.delenv("VIKUNJA_TOKEN", raising=False)
    monkeypatch.delenv("VIKUNJA_TRANSPORT", raising=False)
    config.reset_settings()
    yield
    config.reset_settings()


def _patch_headers(monkeypatch, headers: dict[str, str]) -> None:
    monkeypatch.setattr(auth, "get_http_headers", lambda include_all=False, include=None: headers)


def _patch_request_in_scope(monkeypatch, present: bool) -> None:
    """Simulate an HTTP request being in scope, or not (the stdio condition)."""

    def fake():
        if not present:
            raise RuntimeError("No active HTTP request found.")
        return object()

    monkeypatch.setattr(auth, "get_http_request", fake)


# --- startup refusals ------------------------------------------------------


@pytest.mark.parametrize("transport", ["http", "sse", "streamable-http"])
def test_token_on_a_network_transport_is_refused(monkeypatch, transport):
    """A static token on any network transport must be a hard error, not a warning.

    Parametrised beyond `http` on purpose: the guard is written as `!= "stdio"` rather
    than `== "http"`, because `sse` collapses attribution just as thoroughly and a guard
    that enumerated the unsafe cases would silently stop covering the next transport
    added.
    """
    monkeypatch.setenv("VIKUNJA_TRANSPORT", transport)
    monkeypatch.setenv("VIKUNJA_TOKEN", "shared-tok")
    with pytest.raises(ConfigError, match="VIKUNJA_TOKEN"):
        config.get_settings()


def test_stdio_without_a_token_is_refused_at_startup(monkeypatch):
    """Fail at startup naming the variable — not at the first tool call.

    Failing late is the original bug: the operator sees a healthy-looking process and
    only discovers the misconfiguration when a tool call errors.
    """
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    with pytest.raises(ConfigError, match="VIKUNJA_TOKEN"):
        config.get_settings()


def test_blank_token_does_not_satisfy_the_stdio_requirement(monkeypatch):
    """`VIKUNJA_TOKEN=` in a compose file is unset, not a token.

    Without this, an empty assignment passes the startup check and restores the exact
    deferred failure the check was added to remove.
    """
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    monkeypatch.setenv("VIKUNJA_TOKEN", "   ")
    with pytest.raises(ConfigError, match="VIKUNJA_TOKEN"):
        config.get_settings()


def test_blank_token_does_not_trip_the_network_refusal(monkeypatch):
    """The mirror case: a blank token must not block an otherwise valid HTTP start.

    forge runs `transport=http` with no token. If a stray empty `VIKUNJA_TOKEN` in the
    environment were treated as set, this would refuse to start and take six agents'
    Vikunja access down.
    """
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "http")
    monkeypatch.setenv("VIKUNJA_TOKEN", "")
    assert config.get_settings().token is None


def test_http_without_a_token_is_the_normal_path(monkeypatch):
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "http")
    cfg = config.get_settings()
    assert cfg.token is None


# --- runtime precedence ----------------------------------------------------


def test_fallback_supplies_the_token_when_no_request_is_in_scope(monkeypatch):
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    monkeypatch.setenv("VIKUNJA_TOKEN", "stdio-tok")
    _patch_headers(monkeypatch, {})
    _patch_request_in_scope(monkeypatch, present=False)
    assert auth.caller_token() == "stdio-tok"


def test_header_wins_over_a_configured_token(monkeypatch):
    """The caller's own credential is strictly preferred and never overridden.

    Reachable only if the startup refusal is bypassed (a transport this build does not
    treat as network, say). Asserted anyway: if precedence ever inverted, every agent's
    call would silently execute as the configured identity — the failure would be
    invisible in Vikunja rather than loud.
    """
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    monkeypatch.setenv("VIKUNJA_TOKEN", "stdio-tok")
    _patch_headers(monkeypatch, {"authorization": "Bearer caller-tok"})
    _patch_request_in_scope(monkeypatch, present=True)
    assert auth.caller_token() == "caller-tok"


def test_missing_header_still_fails_closed_when_a_request_is_in_scope(monkeypatch):
    """The load-bearing negative test.

    An HTTP request that simply forgot its Authorization header must never fall back to
    the configured token — that is the path by which a network caller would inherit a
    shared identity. The header dict is empty in both cases, so the request-in-scope
    check is the only thing separating them.
    """
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    monkeypatch.setenv("VIKUNJA_TOKEN", "stdio-tok")
    _patch_headers(monkeypatch, {})
    _patch_request_in_scope(monkeypatch, present=True)
    with pytest.raises(AuthError):
        auth.caller_token()


def test_no_header_and_no_token_fails_closed(monkeypatch):
    """No request, no token — still an error, not an anonymous call."""
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "http")
    _patch_headers(monkeypatch, {})
    _patch_request_in_scope(monkeypatch, present=False)
    with pytest.raises(AuthError):
        auth.caller_token()
