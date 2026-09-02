"""Shared fixtures. Sets a deterministic upstream URL and resets cached state per test."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("VIKUNJA_URL", "https://vikunja.test")


@pytest.fixture(autouse=True)
def _hook_registry():
    """Put the hook registry into the shipped state around every test (vikunja#473).

    Restoring on teardown is the half that was missing. Modules that cleared the registry
    left it wiped for whatever ran next, so the *next* test module added passed alone and
    failed in the suite — and the failure read as a bug in the code under test, not as
    cross-module leakage. ~15 minutes were lost to that misdiagnosis during v0.8.0.

    Setting the state up as well as tearing it down is what makes the leak impossible
    rather than merely repaired: a module that wants an *empty* registry (test_hooks.py,
    test_contrib_audit.py, which test the registry itself) clears it locally and cannot
    leak that choice past its own tests.
    """
    from vikunja_mcp import hooks, server

    hooks.clear_hooks()
    server.register_builtin_hooks()
    yield
    hooks.clear_hooks()
    server.register_builtin_hooks()


@pytest.fixture(autouse=True)
async def _reset_state():
    """Clear cached settings and the shared httpx client between tests."""
    from vikunja_mcp import client, config

    config.reset_settings()
    yield
    await client.aclose()
    config.reset_settings()
