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
    assert (method, path, token) == ("POST", "/projects", "TOK")
    assert _patch_calls.call_args.kwargs["json"] == {"title": "Roadmap"}


async def test_project_update_uses_post_and_only_sends_changed_fields(_patch_calls):
    await call(server.project_update, project_id=3, is_archived=True)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/3")
    assert _patch_calls.call_args.kwargs["json"] == {"is_archived": True}


async def test_project_list_passes_search_as_q(_patch_calls):
    await call(server.project_list, search="alpha")
    assert _patch_calls.call_args.kwargs["params"]["q"] == "alpha"
    assert "s" not in _patch_calls.call_args.kwargs["params"]


# --- pagination on the four tools that were missing it (vikunja#341) ------


async def test_comment_list_page_reaches_the_wire(_patch_calls):
    await call(server.comment_list, task_id=2, page=2, per_page=10)
    assert _patch_calls.call_args.kwargs["params"] == {"page": 2, "per_page": 10}


async def test_attachment_list_page_reaches_the_wire(_patch_calls):
    await call(server.attachment_list, task_id=2, page=2, per_page=10)
    assert _patch_calls.call_args.kwargs["params"] == {"page": 2, "per_page": 10}


async def test_task_assignee_list_page_reaches_the_wire(_patch_calls):
    await call(server.task_assignee_list, task_id=2, page=2, per_page=10)
    assert _patch_calls.call_args.kwargs["params"] == {"page": 2, "per_page": 10}


async def test_view_list_page_reaches_the_wire(_patch_calls):
    await call(server.view_list, project_id=1, page=2, per_page=10)
    assert _patch_calls.call_args.kwargs["params"] == {"page": 2, "per_page": 10}


# --- tasks ----------------------------------------------------------------


async def test_task_create_targets_project_subpath_with_post(_patch_calls):
    await call(server.task_create, project_id=8, title="Ship it", priority=4)
    assert _patch_calls.call_args.args[:2] == ("POST", "/projects/8/tasks")
    assert _patch_calls.call_args.kwargs["json"] == {"title": "Ship it", "priority": 4}


async def test_task_update_patches_without_reading_first(_patch_calls):
    """One PATCH, no GET. Untouched columns are the server's problem now, not ours.

    This is the v2 replacement for the read-merge-write regression test that guarded
    ticket #173. The v1 endpoint was a full replace, so the guarantee had to be bought
    with a GET and a merged body; ``PATCH`` gives it directly. Asserting the call *count*
    is what makes this a regression test rather than a restatement — a reintroduced
    read-merge-write would still produce a correct final body and would still be the bug.
    """
    _patch_calls.return_value = {"id": 5, "done": True}

    await call(server.task_update, task_id=5, done=True)

    assert _patch_calls.call_count == 1
    assert _patch_calls.call_args.args[:2] == ("PATCH", "/tasks/5")
    assert _patch_calls.call_args.kwargs["json"] == {"done": True}


async def test_task_update_sends_only_the_named_fields(_patch_calls):
    """A merge patch must carry the caller's fields and nothing else.

    The v1 path had to echo the whole task back and then strip the three heavy read-only
    collections out of it — ``related_tasks`` inlines the full body of every neighbour,
    and one live probe returned 155k characters. A patch body that reached for the current
    task at all would put them back. Nothing is fetched, so nothing can be echoed.
    """
    await call(server.task_update, task_id=5, title="Renamed")

    body = _patch_calls.call_args.kwargs["json"]
    assert body == {"title": "Renamed"}
    for heavy in ("related_tasks", "attachments", "reactions", "labels", "description"):
        assert heavy not in body


async def test_task_search_uses_q_param(_patch_calls):
    """v2 renamed the search parameter ``s`` -> ``q``, and ignores ``s`` silently.

    Verified live on v2.5.0: ``s=<anything>`` returns the unfiltered result set, so a
    missed rename is a wrong answer rather than an error.
    """
    await call(server.task_search, query="deploy")
    assert _patch_calls.call_args.args[:2] == ("GET", "/tasks")
    assert _patch_calls.call_args.kwargs["params"]["q"] == "deploy"
    assert "s" not in _patch_calls.call_args.kwargs["params"]


async def test_task_delete(_patch_calls):
    await call(server.task_delete, task_id=9)
    assert _patch_calls.call_args.args[:2] == ("DELETE", "/tasks/9")


async def test_tasks_bulk_update_restricts_write_to_named_fields(_patch_calls):
    """#333: without `fields`, PUT /tasks/bulk zeroes every column absent from `values`.

    The verb moved POST -> PUT with the port, but the mechanism did not: v2 routes only
    PUT on this path (no PATCH), so `fields` is still what scopes the write. What did
    change is that `fields` is now documented upstream rather than probe-derived.
    """
    await call(server.tasks_bulk_update, task_ids=[1, 2], values={"done": True})
    assert _patch_calls.call_args.args[:2] == ("PUT", "/tasks/bulk")
    assert _patch_calls.call_args.kwargs["json"] == {
        "task_ids": [1, 2],
        "fields": ["done"],
        "values": {"done": True},
    }


async def test_tasks_bulk_update_serialises_multi_key_values_in_order(_patch_calls):
    """`fields` must list every key in `values`, preserving the caller's ordering."""
    values = {"done": True, "priority": 4}
    await call(server.tasks_bulk_update, task_ids=[7], values=values)
    body = _patch_calls.call_args.kwargs["json"]
    assert body["fields"] == ["done", "priority"]
    assert body["values"] == values
    assert set(body["fields"]) == set(body["values"])


# --- labels ---------------------------------------------------------------


async def test_label_create_uses_put(_patch_calls):
    await call(server.label_create, title="bug")
    assert _patch_calls.call_args.args[:2] == ("POST", "/labels")


async def test_task_label_add_sends_label_id(_patch_calls):
    await call(server.task_label_add, task_id=2, label_id=11)
    assert _patch_calls.call_args.args[:2] == ("POST", "/tasks/2/labels")
    assert _patch_calls.call_args.kwargs["json"] == {"label_id": 11}


# --- comments -------------------------------------------------------------


async def test_comment_create(_patch_calls):
    await call(server.comment_create, task_id=4, comment="looks good")
    assert _patch_calls.call_args.args[:2] == ("POST", "/tasks/4/comments")
    # comment is converted to Vikunja's HTML rich-text format on the way in.
    assert _patch_calls.call_args.kwargs["json"] == {"comment": "<p>looks good</p>"}


async def test_comment_create_converts_markdown(_patch_calls):
    await call(server.comment_create, task_id=4, comment="- a\n- b")
    body = _patch_calls.call_args.kwargs["json"]
    assert "<li>a</li>" in body["comment"]
    assert "<li>b</li>" in body["comment"]


# --- markdown-to-HTML conversion -------------------------------------------


def test_md_to_html_converts_headers_and_lists():
    html = server._md_to_html("## Context\nsomething\n\n- one\n- two")
    assert "<h2>Context</h2>" in html
    assert "<li>one</li>" in html


def test_md_to_html_passthrough_for_none_and_empty():
    assert server._md_to_html(None) is None
    assert server._md_to_html("") == ""


def test_md_to_html_strips_script_and_event_handlers():
    # Regression for the audit's stored-XSS finding: Python-Markdown passes embedded raw
    # HTML through unmodified, so _md_to_html must sanitize the rendered output.
    html = server._md_to_html("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>")
    assert "<script>" not in html
    assert "onerror" not in html


async def test_task_create_converts_description(_patch_calls):
    await call(server.task_create, project_id=1, title="T", description="## H\nbody")
    body = _patch_calls.call_args.kwargs["json"]
    assert "<h2>H</h2>" in body["description"]


async def test_task_update_converts_description(_patch_calls):
    await call(server.task_update, task_id=5, description="## H\nbody")
    body = _patch_calls.call_args.kwargs["json"]
    assert "<h2>H</h2>" in body["description"]


async def test_project_create_converts_description(_patch_calls):
    await call(server.project_create, title="Roadmap", description="## H\nbody")
    body = _patch_calls.call_args.kwargs["json"]
    assert "<h2>H</h2>" in body["description"]


async def test_project_update_converts_description(_patch_calls):
    await call(server.project_update, project_id=3, description="## H\nbody")
    body = _patch_calls.call_args.kwargs["json"]
    assert "<h2>H</h2>" in body["description"]


# --- filters --------------------------------------------------------------


async def test_filter_create_wraps_query(_patch_calls):
    await call(server.filter_create, title="open", filter_query="done = false")
    assert _patch_calls.call_args.args[:2] == ("POST", "/filters")
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
    assert _patch_calls.call_args.args[:2] == ("POST", "/projects/1/webhooks")
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
        (server.filter_update, {"filter_id": 7, "title": "x"}, ("PUT", "/filters/7")),
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


async def test_main_stdio_transport(monkeypatch, _patch_calls):
    """stdio must actually *work*, not merely launch.

    This test previously asserted only that ``mcp.run`` was called with
    ``transport="stdio"``. That is an assertion about the launcher, and it stayed green
    for an entire release during which every tool call under stdio raised AuthError
    (vikunja#461) — the process started, logged cleanly, registered all 71 tools, and
    failed 100% of invocations. The launcher assertion is kept; what follows it is the
    part that was missing.
    """
    from unittest.mock import MagicMock

    from vikunja_mcp import auth, config

    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    monkeypatch.setenv("VIKUNJA_TOKEN", "stdio-tok")
    config.reset_settings()

    run = MagicMock()
    monkeypatch.setattr(server.mcp, "run", run)
    server.main()
    assert run.call_args == (("stdio",), {}) or run.call_args.kwargs.get("transport") == "stdio"

    # Restore the real caller_token: the autouse fixture pins it to a constant, which
    # would mask exactly the failure this test exists to catch.
    monkeypatch.setattr(server, "caller_token", auth.caller_token)
    await call(server.whoami)
    assert _patch_calls.await_args.args[2] == "stdio-tok"
    config.reset_settings()
