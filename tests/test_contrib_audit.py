"""The contrib audit-log hook records who/what/args-hash without leaking secrets."""

from __future__ import annotations

import pytest

from vikunja_mcp import hooks, server
from vikunja_mcp.contrib import audit_log


class _CaptureLogger:
    def __init__(self):
        self.lines: list[dict] = []

    def info(self, event, **fields):
        self.lines.append({"event": event, **fields})


@pytest.fixture(autouse=True)
def _clean():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


async def test_audit_hook_logs_tool_and_hashes_args_without_raw_values():
    sink = _CaptureLogger()
    handler = audit_log.audit_log_hook("webhook_create", logger=sink)
    await handler({"secret": "s3cret-value", "target_url": "https://hooks.example/x"})

    line = sink.lines[-1]
    assert line["tool"] == "webhook_create"
    assert "args_hash" in line and len(line["args_hash"]) == 16
    # the raw secret must never appear anywhere in the logged fields
    assert "s3cret-value" not in repr(line)
    assert "target_url" not in line  # only the hash is recorded, not the values


async def test_actor_is_anonymous_without_a_caller_token():
    sink = _CaptureLogger()
    handler = audit_log.audit_log_hook("task_get", logger=sink)
    await handler({"task_id": 1})
    # no HTTP request context in a unit test → caller_token() raises → anonymous
    assert sink.lines[-1]["actor"] == "anonymous"


async def test_actor_hashes_token_not_leaks_it(monkeypatch):
    sink = _CaptureLogger()
    monkeypatch.setattr(audit_log, "caller_token", lambda: "super-secret-token")
    handler = audit_log.audit_log_hook("task_get", logger=sink)
    await handler({"task_id": 1})
    actor = sink.lines[-1]["actor"]
    assert actor.startswith("agent:")
    assert "super-secret-token" not in actor


async def test_register_audit_log_wires_a_before_hook(monkeypatch):
    sink = _CaptureLogger()
    monkeypatch.setattr(server, "request", _async_ok())
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    audit_log.register_audit_log(["task_delete"], logger=sink)
    await _fn(server.task_delete)(task_id=5)

    assert sink.lines[-1]["tool"] == "task_delete"


def _async_ok():
    from unittest.mock import AsyncMock

    return AsyncMock(return_value={"ok": True})


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn
