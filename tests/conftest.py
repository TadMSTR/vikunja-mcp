"""Shared fixtures. Sets a deterministic upstream URL and resets cached state per test."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("VIKUNJA_URL", "https://vikunja.test")


@pytest.fixture(autouse=True)
async def _reset_state():
    """Clear cached settings and the shared httpx client between tests."""
    from vikunja_mcp import client, config

    config.reset_settings()
    yield
    await client.aclose()
    config.reset_settings()
