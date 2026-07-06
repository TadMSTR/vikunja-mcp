"""Tool-layer tests: verify each tool maps to the correct Vikunja verb + path + body.

Vikunja's REST idiom is easy to get wrong (PUT creates, POST updates), so these tests
pin the mapping. The upstream ``request`` call is captured; no network is touched.

fastmcp 3.x ``@mcp.tool()`` returns the original coroutine function, so tools are called
directly. ``_fn`` unwraps a FunctionTool object as a fallback if that ever changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import server


@pytest.fixture(autouse=True)
def _patch_calls(monkeypatch):
    """Replace request() with a capturing mock and pin a known caller token."""
    mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    return mock


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def call(tool, **kwargs):
    return await _fn(tool)(**kwargs)


# --- projects -------------------------------------------------------------


async def test_project_create_uses_put(_patch_calls):
    await call(server.project_create, title="Roadmap")
    method, path, token = _patch_calls.call_args.args
    assert (method, path, token) == ("PUT", "/projects", "TOK")
    assert _patch_calls.call_args.kwargs["json"] == {"title": "Roadmap"}


async def test_project_update_uses_post_and_only_sends_changed_fields(_patch_calls):
    await call(server.project_update, project_id=3, is_archived=True)
    assert _patch_calls.call_args.args[:2] == ("POST", "/projects/3")
    assert _patch_calls.call_args.kwargs["json"] == {"is_archived": True}


async def test_project_list_passes_search_as_s(_patch_calls):
    await call(server.project_list, search="alpha")
    assert _patch_calls.call_args.kwargs["params"]["s"] == "alpha"


# --- tasks ----------------------------------------------------------------


async def test_task_create_targets_project_subpath_with_put(_patch_calls):
    await call(server.task_create, project_id=8, title="Ship it", priority=4)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/8/tasks")
    assert _patch_calls.call_args.kwargs["json"] == {"title": "Ship it", "priority": 4}


async def test_task_update_marks_done_via_post(_patch_calls):
    await call(server.task_update, task_id=5, done=True)
    assert _patch_calls.call_args.args[:2] == ("POST", "/tasks/5")
    assert _patch_calls.call_args.kwargs["json"] == {"done": True}


async def test_task_search_uses_s_param(_patch_calls):
    await call(server.task_search, query="deploy")
    assert _patch_calls.call_args.args[:2] == ("GET", "/tasks")
    assert _patch_calls.call_args.kwargs["params"]["s"] == "deploy"


async def test_task_delete(_patch_calls):
    await call(server.task_delete, task_id=9)
    assert _patch_calls.call_args.args[:2] == ("DELETE", "/tasks/9")


# --- labels ---------------------------------------------------------------


async def test_label_create_uses_put(_patch_calls):
    await call(server.label_create, title="bug")
    assert _patch_calls.call_args.args[:2] == ("PUT", "/labels")


async def test_task_label_add_sends_label_id(_patch_calls):
    await call(server.task_label_add, task_id=2, label_id=11)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/tasks/2/labels")
    assert _patch_calls.call_args.kwargs["json"] == {"label_id": 11}


# --- comments -------------------------------------------------------------


async def test_comment_create(_patch_calls):
    await call(server.comment_create, task_id=4, comment="looks good")
    assert _patch_calls.call_args.args[:2] == ("PUT", "/tasks/4/comments")
    assert _patch_calls.call_args.kwargs["json"] == {"comment": "looks good"}


# --- filters --------------------------------------------------------------


async def test_filter_create_wraps_query(_patch_calls):
    await call(server.filter_create, title="open", filter_query="done = false")
    assert _patch_calls.call_args.args[:2] == ("PUT", "/filters")
    assert _patch_calls.call_args.kwargs["json"]["filters"] == {"filter": "done = false"}


# --- webhooks -------------------------------------------------------------


async def test_webhook_create_is_project_scoped(_patch_calls):
    # 8.8.8.8 is a public IP literal → passes the SSRF guard with no DNS lookup.
    await call(
        server.webhook_create,
        project_id=1,
        target_url="https://8.8.8.8/vikunja",
        events=["task.created"],
        secret="s3cret",
    )
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/1/webhooks")
    body = _patch_calls.call_args.kwargs["json"]
    assert body["events"] == ["task.created"]
    assert body["secret"] == "s3cret"


async def test_webhook_events_is_global(_patch_calls):
    await call(server.webhook_events)
    assert _patch_calls.call_args.args[:2] == ("GET", "/webhooks/events")


# --- identity -------------------------------------------------------------


async def test_whoami(_patch_calls):
    await call(server.whoami)
    assert _patch_calls.call_args.args == ("GET", "/user", "TOK")


# --- remaining read/delete mappings (regression pins) ---------------------


@pytest.mark.parametrize(
    ("tool", "kwargs", "expected"),
    [
        (server.project_get, {"project_id": 1}, ("GET", "/projects/1")),
        (server.project_delete, {"project_id": 1}, ("DELETE", "/projects/1")),
        (server.task_get, {"task_id": 2}, ("GET", "/tasks/2")),
        (server.task_list, {}, ("GET", "/tasks")),
        (server.label_get, {"label_id": 3}, ("GET", "/labels/3")),
        (server.label_update, {"label_id": 3, "title": "x"}, ("PUT", "/labels/3")),
        (server.label_delete, {"label_id": 3}, ("DELETE", "/labels/3")),
        (server.label_list, {}, ("GET", "/labels")),
        (server.task_label_remove, {"task_id": 2, "label_id": 3}, ("DELETE", "/tasks/2/labels/3")),
        (server.comment_list, {"task_id": 2}, ("GET", "/tasks/2/comments")),
        (server.comment_delete, {"task_id": 2, "comment_id": 5}, ("DELETE", "/tasks/2/comments/5")),
        (server.filter_get, {"filter_id": 7}, ("GET", "/filters/7")),
        (server.filter_update, {"filter_id": 7, "title": "x"}, ("POST", "/filters/7")),
        (server.filter_delete, {"filter_id": 7}, ("DELETE", "/filters/7")),
        (server.webhook_list, {"project_id": 1}, ("GET", "/projects/1/webhooks")),
        (
            server.webhook_delete,
            {"project_id": 1, "webhook_id": 9},
            ("DELETE", "/projects/1/webhooks/9"),
        ),
    ],
)
async def test_verb_path_mappings(_patch_calls, tool, kwargs, expected):
    await call(tool, **kwargs)
    assert _patch_calls.call_args.args[:2] == expected


# --- entry point ----------------------------------------------------------


def test_main_http_transport(monkeypatch):
    from unittest.mock import MagicMock

    from vikunja_mcp import config

    run = MagicMock()
    monkeypatch.setattr(server.mcp, "run", run)
    monkeypatch.setattr(config, "get_settings", config.get_settings)
    server.main()
    assert run.call_args.kwargs["transport"] == "http"
    assert run.call_args.kwargs["port"] == 8501


def test_main_stdio_transport(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    from vikunja_mcp import config

    config.reset_settings()
    run = MagicMock()
    monkeypatch.setattr(server.mcp, "run", run)
    server.main()
    assert run.call_args == (("stdio",), {}) or run.call_args.kwargs.get("transport") == "stdio"
    config.reset_settings()
