"""The contrib audit-log hook records who/what/args-hash without leaking secrets."""

from __future__ import annotations

import pytest

from vikunja_mcp import hooks, server
from vikunja_mcp.contrib import audit_log
from vikunja_mcp.exceptions import ConfigError


class _CaptureLogger:
    def __init__(self):
        self.lines: list[dict] = []

    def info(self, event, **fields):
        self.lines.append({"event": event, **fields})


@pytest.fixture(autouse=True)
def _clean():
    """These tests register the audit hook explicitly, so they start from an empty registry.

    Setup only. Restoring the built-ins is `conftest._hook_registry`'s job — doing it
    here too is what let the wipe escape the module in the first place (vikunja#473).
    """
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


# --- FileAuditLogger --------------------------------------------------------


def test_file_audit_logger_appends_dated_file(tmp_path):
    logger = audit_log.FileAuditLogger(tmp_path)
    logger.info("vikunja_tool_call", tool="task_create", actor="agent:abc", args_hash="dead")

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    assert files[0].stem.count("-") == 2  # YYYY-MM-DD
    content = files[0].read_text()
    assert "tool=task_create" in content
    assert "actor=agent:abc" in content


# --- env-gated wiring (vikunja#342, id 361) ---------------------------------


async def test_register_builtin_hooks_wires_audit_log_when_env_set(monkeypatch, tmp_path):
    monkeypatch.setenv("VIKUNJA_AUDIT_LOG", "1")
    monkeypatch.setenv("VIKUNJA_AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(server, "request", _async_ok())
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    server.register_builtin_hooks()
    await _fn(server.task_create)(project_id=1, title="Ship it")
    await _fn(server.task_get)(task_id=1)  # read-only — must NOT be audited

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "tool=task_create" in content
    assert "tool=task_get" not in content


def test_register_builtin_hooks_raises_when_dir_unset(monkeypatch):
    monkeypatch.setenv("VIKUNJA_AUDIT_LOG", "1")
    monkeypatch.delenv("VIKUNJA_AUDIT_LOG_DIR", raising=False)
    with pytest.raises(ConfigError):
        server.register_builtin_hooks()


async def test_register_builtin_hooks_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("VIKUNJA_AUDIT_LOG", "1")
    monkeypatch.setenv("VIKUNJA_AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(server, "request", _async_ok())
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    server.register_builtin_hooks()
    server.register_builtin_hooks()  # must not double-register
    await _fn(server.task_create)(project_id=1, title="Ship it")

    files = list(tmp_path.glob("*.md"))
    lines = [line for line in files[0].read_text().splitlines() if line.strip()]
    assert len(lines) == 1


def test_register_builtin_hooks_noop_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("VIKUNJA_AUDIT_LOG", raising=False)
    server.register_builtin_hooks()
    assert not any(
        getattr(h, "is_audit_log_hook", False) for h in hooks.before_handlers("task_create")
    )
