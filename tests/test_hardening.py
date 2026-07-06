"""Regression tests for the security-audit remediations (F-01..F-05)."""

from __future__ import annotations

import base64

import pytest

from vikunja_mcp import server, telemetry
from vikunja_mcp.exceptions import VikunjaAPIError


@pytest.fixture(autouse=True)
def _patch_calls(monkeypatch):
    from unittest.mock import AsyncMock

    mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    return mock


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def call(tool, **kwargs):
    return await _fn(tool)(**kwargs)


# --- F-02: webhook SSRF guard ---------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5/hook",  # RFC1918
        "https://192.168.1.10/hook",  # RFC1918
        "https://172.16.5.5/hook",  # RFC1918
        "https://127.0.0.1/hook",  # loopback
        "https://localhost/hook",  # loopback name
        "https://169.254.1.1/hook",  # link-local
        "https://vikunja.local/hook",  # internal suffix
        "https://box.internal/hook",  # internal suffix
        "http://[::1]/hook",  # ipv6 loopback
        "ftp://example.com/hook",  # wrong scheme
    ],
)
async def test_webhook_create_rejects_internal_targets(_patch_calls, url):
    with pytest.raises(VikunjaAPIError):
        await call(server.webhook_create, project_id=1, target_url=url, events=["task.created"])
    _patch_calls.assert_not_called()  # rejected before any upstream call


async def test_webhook_create_allows_public_ip_literal(_patch_calls):
    await call(server.webhook_create, project_id=1, target_url="https://8.8.8.8/hook", events=["x"])
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/1/webhooks")


def test_host_blocked_when_hostname_resolves_to_private(monkeypatch):
    # a public-looking hostname that resolves to an internal IP must be blocked (DNS branch).
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("10.1.2.3", 0))],
    )
    assert server._host_is_blocked("sneaky.example.com") is True


def test_host_allowed_when_hostname_resolves_to_public(monkeypatch):
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    assert server._host_is_blocked("example.com") is False


def test_host_allowed_when_unresolvable(monkeypatch):
    def _boom(*a, **k):
        raise OSError("nxdomain")

    monkeypatch.setattr(server.socket, "getaddrinfo", _boom)
    # cannot classify → allow (Vikunja re-resolves at delivery)
    assert server._host_is_blocked("does-not-resolve.example.com") is False


# --- F-04: attachment decode guard ----------------------------------------


async def test_attachment_upload_rejects_invalid_base64(_patch_calls):
    with pytest.raises(VikunjaAPIError, match="not valid base64"):
        await call(server.attachment_upload, task_id=7, filename="a", content_base64="not!b64!")
    _patch_calls.assert_not_called()


async def test_attachment_upload_rejects_oversize(_patch_calls):
    huge = base64.b64encode(b"x" * (26 * 1024 * 1024)).decode()
    with pytest.raises(VikunjaAPIError, match="size limit"):
        await call(server.attachment_upload, task_id=7, filename="a", content_base64=huge)
    _patch_calls.assert_not_called()


async def test_attachment_upload_accepts_valid_base64(_patch_calls):
    await call(
        server.attachment_upload,
        task_id=7,
        filename="a.txt",
        content_base64=base64.b64encode(b"hi").decode(),
    )
    assert _patch_calls.call_args.args[:2] == ("PUT", "/tasks/7/attachments")


# --- F-05: share password/sharing_type coupling ---------------------------


async def test_share_with_password_type_requires_password(_patch_calls):
    with pytest.raises(VikunjaAPIError, match="requires a non-empty password"):
        await call(server.project_share_create, project_id=5, sharing_type=1)
    _patch_calls.assert_not_called()


async def test_share_password_without_password_type_is_rejected(_patch_calls):
    with pytest.raises(VikunjaAPIError, match="only meaningful"):
        await call(server.project_share_create, project_id=5, password="pw", sharing_type=0)
    _patch_calls.assert_not_called()


async def test_share_valid_password_combo_is_accepted(_patch_calls):
    await call(server.project_share_create, project_id=5, password="pw", sharing_type=1)
    body = _patch_calls.call_args.kwargs["json"]
    assert body["password"] == "pw" and body["sharing_type"] == 1


# --- F-01/F-03: telemetry scheduling --------------------------------------


def test_schedule_without_running_loop_is_noop_and_closes_coro():
    telemetry.reset_for_tests()

    async def _c():
        return None

    coro = _c()
    telemetry._schedule(coro)  # no running loop → must not raise
    # coroutine was closed, not left pending (no "never awaited" warning)
    with pytest.raises(RuntimeError):
        coro.send(None)


async def test_influx_write_is_offloaded_via_schedule(monkeypatch):
    telemetry.reset_for_tests()
    scheduled = []
    monkeypatch.setattr(telemetry, "_schedule", lambda coro: (scheduled.append(coro), coro.close()))
    monkeypatch.setattr(telemetry, "_influx_client", object())
    monkeypatch.delenv("VIKUNJA_NATS_URL", raising=False)

    telemetry._emit("whoami", 0.01, None)
    assert len(scheduled) == 1  # influx write went through _schedule, not inline
    telemetry.reset_for_tests()
