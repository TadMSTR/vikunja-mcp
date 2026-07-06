"""Auth extraction — the token-passthrough security boundary.

These are the negative tests the repo standard requires for credential-handling code:
a missing or empty token must be rejected, never silently allowed through.
"""

from __future__ import annotations

import pytest

from vikunja_mcp import auth
from vikunja_mcp.exceptions import AuthError


def _patch_headers(monkeypatch, headers: dict[str, str]) -> dict:
    """Replace get_http_headers and capture the include= argument it was called with."""
    captured: dict = {}

    def fake(include_all: bool = False, include=None):
        captured["include"] = include
        captured["include_all"] = include_all
        return headers

    monkeypatch.setattr(auth, "get_http_headers", fake)
    return captured


def test_bearer_token_extracted(monkeypatch):
    _patch_headers(monkeypatch, {"authorization": "Bearer abc123"})
    assert auth.caller_token() == "abc123"


def test_bearer_prefix_is_case_insensitive(monkeypatch):
    _patch_headers(monkeypatch, {"authorization": "bearer XyZ"})
    assert auth.caller_token() == "XyZ"


def test_raw_token_without_bearer_prefix(monkeypatch):
    # Some clients send the bare token; accept it rather than forwarding a broken header.
    _patch_headers(monkeypatch, {"authorization": "tk_raw"})
    assert auth.caller_token() == "tk_raw"


def test_authorization_header_is_explicitly_requested(monkeypatch):
    # Regression guard: get_http_headers() strips `authorization` unless opted in. If this
    # include set ever drops it, every call silently loses its credential.
    captured = _patch_headers(monkeypatch, {"authorization": "Bearer t"})
    auth.caller_token()
    assert captured["include"] == {"authorization"}


def test_missing_header_fails_closed(monkeypatch):
    _patch_headers(monkeypatch, {})
    with pytest.raises(AuthError):
        auth.caller_token()


def test_blank_header_fails_closed(monkeypatch):
    _patch_headers(monkeypatch, {"authorization": "   "})
    with pytest.raises(AuthError):
        auth.caller_token()


def test_bearer_with_no_token_fails_closed(monkeypatch):
    _patch_headers(monkeypatch, {"authorization": "Bearer "})
    with pytest.raises(AuthError):
        auth.caller_token()
