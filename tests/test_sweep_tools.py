"""Sweep tools (v0.13.0): `tasks_export`, `task_close`, `return_mode`, `task_get` projections.

What a ticket sweep needs from this server: one complete, compact export to decide which
tickets changed; one call to close a ticket with a recorded verdict; and write replies that
do not echo the whole ticket back.

Shapes are asserted as **whole dicts or exact key sets**, never as "contains these keys".
An extra or missing key in a reply that downstream scripts parse is a breaking change, and
a partial match cannot fail on it.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from vikunja_mcp import config, markers, server
from vikunja_mcp.exceptions import VikunjaAPIError

from . import fixtures


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def call(tool, **kwargs):
    return await _fn(tool)(**kwargs)


class Upstream:
    """Routes ``server.request`` by (method, path) and records every call made.

    Each route is a callable taking the call's kwargs, so a test can serve pages, raise on
    a chosen call, or count. ``sent`` is deep-copied at call time: a hook that edits a
    reply in place must not be able to rewrite what the test sees as the request.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def on(self, method: str, path: str, handler: Any) -> None:
        self.routes[(method, path)] = handler

    async def __call__(self, method, path, *args, **kwargs):
        self.sent.append((method, path, copy.deepcopy(kwargs)))
        handler = self.routes.get((method, path))
        if handler is None:
            raise AssertionError(f"unexpected upstream call: {method} {path}")
        result = handler(kwargs)
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    def calls(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.sent]

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls() if m != "GET"]


@pytest.fixture
def upstream(monkeypatch):
    fake = Upstream()
    # A tiny page size, so a multi-page walk takes a handful of fixture rows rather than
    # thousands. The real value is pinned by test_page_size_is_the_v2_maximum.
    monkeypatch.setattr(server, "_MAX_PAGE_SIZE", 2)
    monkeypatch.setattr(server, "request", fake)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    return fake


def _envelope(items: list[dict], total: int, total_pages: int, page: int = 1) -> dict:
    """A raw v2 list body, as ``request(..., unwrap_list=False)`` returns it."""
    return {
        "items": items,
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "per_page": server._MAX_PAGE_SIZE,
    }


def _pager(pages: list[Any]):
    """A route handler serving ``pages[page - 1]`` for each requested page."""

    def handler(kwargs):
        return pages[kwargs["params"]["page"] - 1]

    return handler


# ===========================================================================
# tasks_export
# ===========================================================================

_ROW_KEYS = set(server._EXPORT_ROW_FIELDS)
_ENVELOPE_KEYS = {
    "project_id",
    "generated_at",
    "since",
    "include_done",
    "excluded_task_ids",
    "count",
    "rows",
}


def _tasks(ids: list[int]) -> list[dict]:
    return [fixtures.task(id=i, identifier=f"#{i - 19}") for i in ids]


async def test_export_pages_through_every_page_and_count_matches(upstream):
    ids = list(range(100, 107))
    pages = [
        _envelope(_tasks(ids[0:3]), total=7, total_pages=3, page=1),
        _envelope(_tasks(ids[3:6]), total=7, total_pages=3, page=2),
        _envelope(_tasks(ids[6:7]), total=7, total_pages=3, page=3),
    ]
    upstream.on("GET", "/tasks", _pager(pages))

    out = await call(server.tasks_export, project_id=7)

    assert set(out) == _ENVELOPE_KEYS
    assert out["count"] == len(out["rows"]) == 7
    assert [row["id"] for row in out["rows"]] == ids
    assert all(set(row) == _ROW_KEYS for row in out["rows"])
    assert upstream.calls() == [("GET", "/tasks")] * 3
    params = [kwargs["params"] for _, _, kwargs in upstream.sent]
    assert [p["page"] for p in params] == [1, 2, 3]
    assert {p["per_page"] for p in params} == {server._MAX_PAGE_SIZE}
    assert {(p["sort_by"], p["order_by"]) for p in params} == {("id", "asc")}
    assert all(kwargs["unwrap_list"] is False for _, _, kwargs in upstream.sent)


async def test_export_error_mid_pagination_raises_rather_than_returning_part(upstream):
    pages = [
        _envelope(_tasks([1, 2]), total=5, total_pages=3, page=1),
        VikunjaAPIError(502, "bad gateway"),
        _envelope(_tasks([5]), total=5, total_pages=3, page=3),
    ]
    upstream.on("GET", "/tasks", _pager(pages))

    with pytest.raises(VikunjaAPIError, match="bad gateway"):
        await call(server.tasks_export, project_id=7)


async def test_export_empty_project_is_an_empty_complete_export(upstream):
    upstream.on("GET", "/tasks", lambda kw: _envelope([], total=0, total_pages=0))

    out = await call(server.tasks_export, project_id=7)

    assert out["count"] == 0
    assert out["rows"] == []
    assert upstream.calls() == [("GET", "/tasks")]


async def test_export_restarts_once_when_the_set_moves_and_then_succeeds(upstream):
    served = iter(
        [
            # Attempt 1: a ticket is filed between page 1 and page 2.
            _envelope(_tasks([1, 2]), total=3, total_pages=2, page=1),
            _envelope(_tasks([3, 4]), total=4, total_pages=2, page=2),
            # Attempt 2: stable.
            _envelope(_tasks([1, 2]), total=4, total_pages=2, page=1),
            _envelope(_tasks([3, 4]), total=4, total_pages=2, page=2),
        ]
    )
    upstream.on("GET", "/tasks", lambda kw: next(served))

    out = await call(server.tasks_export, project_id=7)

    assert [row["id"] for row in out["rows"]] == [1, 2, 3, 4]
    assert len(upstream.calls()) == 4


async def test_export_gives_up_when_the_set_keeps_moving(upstream):
    counter = iter(range(3, 100))
    upstream.on(
        "GET",
        "/tasks",
        lambda kw: _envelope(_tasks([kw["params"]["page"]]), total=next(counter), total_pages=2),
    )

    with pytest.raises(VikunjaAPIError, match="changed while it was being paged"):
        await call(server.tasks_export, project_id=7)
    assert len(upstream.calls()) == 2 * server._EXPORT_ATTEMPTS


def test_page_size_is_the_v2_maximum():
    # v2's schema caps per_page at 1000 and answers 1001 with a 422 (measured live).
    assert server._MAX_PAGE_SIZE == 1000


async def test_export_refuses_a_page_count_its_total_does_not_need(upstream):
    # F-02: an envelope claiming 10**9 pages for 0 rows drove one request per claimed page.
    upstream.on("GET", "/tasks", lambda kw: _envelope([], total=0, total_pages=10**9))

    with pytest.raises(VikunjaAPIError, match="does not match its own total"):
        await call(server.tasks_export, project_id=7)
    assert len(upstream.calls()) == 1


async def test_export_refuses_a_page_count_that_moves_between_pages(upstream):
    pages = [
        _envelope(_tasks([1, 2]), total=4, total_pages=2, page=1),
        _envelope(_tasks([3, 4]), total=4, total_pages=3, page=2),
    ]
    upstream.on("GET", "/tasks", _pager(pages))

    with pytest.raises(VikunjaAPIError, match="total_pages 2 -> 3"):
        await call(server.tasks_export, project_id=7)
    assert len(upstream.calls()) == 2 * server._EXPORT_ATTEMPTS


async def test_export_refuses_an_empty_page_before_total_is_reached(upstream):
    pages = [
        _envelope(_tasks([1, 2]), total=4, total_pages=2, page=1),
        _envelope([], total=4, total_pages=2, page=2),
    ]
    upstream.on("GET", "/tasks", _pager(pages))

    with pytest.raises(VikunjaAPIError, match="page 2 added no rows with 2 of 4 read"):
        await call(server.tasks_export, project_id=7)


async def test_export_refuses_a_page_of_non_task_filler(upstream):
    # R2-01: a page that returns items but no task rows makes no progress. Counting items
    # instead of rows kept let 10**9 such pages walk, one request each.
    upstream.on(
        "GET",
        "/tasks",
        lambda kw: _envelope([0], total=10**12, total_pages=10**12 // server._MAX_PAGE_SIZE),
    )

    with pytest.raises(VikunjaAPIError, match="page 1 added no rows"):
        await call(server.tasks_export, project_id=7)
    # One request per attempt: refused on page 1 each time, never walked.
    assert len(upstream.calls()) == server._EXPORT_ATTEMPTS


async def test_export_refuses_an_id_that_arrives_twice(upstream):
    # A row shifted back across a page boundary is read twice — and some other row never.
    pages = [
        _envelope(_tasks([1, 2]), total=4, total_pages=2, page=1),
        _envelope(_tasks([2, 4]), total=4, total_pages=2, page=2),
    ]
    upstream.on("GET", "/tasks", _pager(pages))

    with pytest.raises(VikunjaAPIError, match="arrived twice"):
        await call(server.tasks_export, project_id=7)


async def test_export_refuses_rows_that_do_not_add_up_to_total(upstream):
    upstream.on("GET", "/tasks", lambda kw: _envelope(_tasks([1, 2]), total=3, total_pages=1))

    with pytest.raises(VikunjaAPIError, match="2 rows read, upstream reported 3"):
        await call(server.tasks_export, project_id=7)


async def test_export_refuses_a_body_that_is_not_an_envelope(upstream):
    upstream.on("GET", "/tasks", lambda kw: _tasks([1, 2]))

    with pytest.raises(VikunjaAPIError, match="not a paginated list envelope"):
        await call(server.tasks_export, project_id=7)


async def test_export_filter_scopes_project_open_since_and_exclusions(upstream, monkeypatch):
    monkeypatch.setenv("VIKUNJA_SUMMARY_EXCLUDE_IDS", "180,42")
    config.reset_settings()
    upstream.on("GET", "/tasks", lambda kw: _envelope([], total=0, total_pages=0))

    out = await call(
        server.tasks_export,
        project_id=7,
        include_done=False,
        since="2026-10-27T15:00:00.75-05:00",
    )

    params = upstream.sent[0][2]["params"]
    assert params["filter"] == (
        '(project = 7) && (done = false) && (updated >= "2026-10-27T20:00:00Z") '
        "&& (id != 180) && (id != 42)"
    )
    assert out["since"] == "2026-10-27T20:00:00Z"
    assert out["include_done"] is False
    assert out["excluded_task_ids"] == [180, 42]


async def test_export_and_backlog_summary_share_one_exclusion_rule():
    assert server._exclusion_predicates([180, 42]) == ["id != 180", "id != 42"]


async def test_export_default_scope_is_the_whole_project(upstream):
    upstream.on("GET", "/tasks", lambda kw: _envelope([], total=0, total_pages=0))

    out = await call(server.tasks_export, project_id=7)

    assert upstream.sent[0][2]["params"]["filter"] == "(project = 7)"
    assert out["since"] is None
    assert out["include_done"] is True


@pytest.mark.parametrize(
    "since", ["yesterday", "", "0001-01-01T00:00:00Z", '2026-10-27" || done = true']
)
async def test_export_refuses_an_unusable_since_before_any_call(upstream, since):
    with pytest.raises(ValueError, match="since must be"):
        await call(server.tasks_export, project_id=7, since=since)
    assert upstream.calls() == []


def test_export_row_is_compact_and_normalised():
    task = fixtures.task(
        id=500,
        identifier="#481",
        title="Example",
        done=False,
        done_at="0001-01-01T00:00:00Z",
        created="2026-10-01T10:00:00Z",
        updated="2026-10-02T11:00:00Z",
        priority=3,
        labels=[{"id": 1, "title": "type:bug"}, {"id": 2, "title": "repo:x"}],
        created_by={"id": 3, "username": "agent-one", "name": ""},
        related_tasks={
            "related": [{"id": 9, "title": "t"}, {"id": 10, "title": "u"}],
            "subtask": [],
            "blocking": None,
        },
        description=markers.append(
            "<p>body</p>",
            "ref",
            markers.encode_ref("pr", "https://github.com/example/repo/pull/1"),
        ),
    )

    assert server._export_row(task) == {
        "id": 500,
        "identifier": "#481",
        "title": "Example",
        "done": False,
        "done_at": None,
        "created": "2026-10-01T10:00:00Z",
        "updated": "2026-10-02T11:00:00Z",
        "priority": 3,
        "labels": ["type:bug", "repo:x"],
        "created_by": "agent-one",
        "relations": {"related": [9, 10]},
        "linked_refs": [{"ref_type": "pr", "ref_url": "https://github.com/example/repo/pull/1"}],
    }


def test_export_row_keeps_every_key_when_the_task_has_nothing():
    row = server._export_row(
        {"id": 1, "labels": None, "related_tasks": None, "created_by": None, "description": ""}
    )
    assert set(row) == _ROW_KEYS
    assert (row["labels"], row["relations"], row["linked_refs"]) == ([], {}, [])
    assert row["created_by"] is None


def test_export_row_drops_a_forged_backlink():
    # Same re-validation as task_get: a marker hand-written into a description is not
    # trusted because it looks structured.
    task = fixtures.task(description="<p>vikunja-mcp: ref=commit|javascript:alert(1)</p>")
    assert server._export_row(task)["linked_refs"] == []


def test_export_row_keeps_a_real_done_at():
    row = server._export_row(fixtures.task(done=True, done_at="2026-10-20T08:00:00Z"))
    assert row["done_at"] == "2026-10-20T08:00:00Z"


# ===========================================================================
# task_close
# ===========================================================================


def test_closing_line_is_byte_exact():
    # The contract part 2's scripts parse. Compared as bytes so a look-alike dash or a
    # changed space cannot pass.
    line = server._closing_line("resolved", "fixed in a1b2c3d")
    assert line.encode() == b"Closing: RESOLVED \xe2\x80\x94 fixed in a1b2c3d"


def test_closing_comment_is_the_line_escaped_not_rendered():
    line = server._closing_line("obsolete", "fix_the_thing *x* <b>gone</b> & `sha`")
    body = server._closing_comment_html(line)

    assert body == (
        "<p>Closing: OBSOLETE — fix_the_thing *x* &lt;b&gt;gone&lt;/b&gt; &amp; `sha`</p>"
    )
    # And reading it back yields the line exactly — the round trip a parser depends on.
    assert server._html_text(body) == line


def _open_task(task_id: int = 473, **overrides) -> dict:
    return fixtures.task(
        **{"id": task_id, "identifier": "#454", "done": False, "related_tasks": {}, **overrides}
    )


def _close_routes(upstream, task: dict | None = None, target: int | None = None, **failures):
    """Serve the calls task_close makes. ``failures`` maps a step name to an exception."""
    task = task or _open_task()
    tid = task["id"]
    upstream.on("GET", f"/tasks/{tid}", lambda kw: task)
    if target is not None:
        upstream.on(
            "GET",
            f"/tasks/{target}",
            lambda kw: failures.get("lookup") or fixtures.task(id=target),
        )
    upstream.on(
        "POST",
        f"/tasks/{tid}/comments",
        lambda kw: failures.get("comment") or {"id": 9001, "created": "2026-10-27T21:00:00Z"},
    )
    upstream.on(
        "POST",
        f"/tasks/{tid}/relations",
        lambda kw: failures.get("relation") or {"task_id": tid, **kw["json"]},
    )
    upstream.on(
        "PATCH",
        f"/tasks/{tid}",
        lambda kw: failures.get("done") or {**task, "done": True},
    )


async def test_close_resolved_comments_then_closes_and_replies_minimally(upstream):
    _close_routes(upstream)

    out = await call(
        server.task_close, task_id=473, verdict="resolved", evidence="fixed in a1b2c3d"
    )

    assert out == {
        "id": 473,
        "identifier": "#454",
        "done": True,
        "verdict": "resolved",
        "comment_id": 9001,
        "relation_added": False,
        "already_done": False,
    }
    assert upstream.calls() == [
        ("GET", "/tasks/473"),
        ("POST", "/tasks/473/comments"),
        ("PATCH", "/tasks/473"),
    ]
    sent = dict(((m, p), kw) for m, p, kw in upstream.sent)
    assert sent[("POST", "/tasks/473/comments")]["json"] == {
        "comment": "<p>Closing: RESOLVED — fixed in a1b2c3d</p>"
    }
    assert sent[("PATCH", "/tasks/473")]["json"] == {"done": True}


async def test_close_duplicate_relates_in_order_comment_relation_done(upstream):
    _close_routes(upstream, target=480)

    out = await call(
        server.task_close,
        task_id=473,
        verdict="duplicate",
        evidence="same bug as #461",
        duplicate_of="480",
    )

    assert out["relation_added"] is True
    assert out["done"] is True
    assert upstream.calls() == [
        ("GET", "/tasks/473"),
        ("GET", "/tasks/480"),
        ("POST", "/tasks/473/comments"),
        ("POST", "/tasks/473/relations"),
        ("PATCH", "/tasks/473"),
    ]
    relation = upstream.sent[3][2]["json"]
    # "480" (a bare digit string) is resolved to the int id by the ref hook.
    assert relation == {"other_task_id": 480, "relation_kind": "duplicateof"}


async def test_close_superseded_adds_a_related_relation(upstream):
    _close_routes(upstream, target=490)

    out = await call(
        server.task_close,
        task_id=473,
        verdict="superseded",
        evidence="replaced by the v2 design",
        superseded_by=490,
    )

    assert out["relation_added"] is True
    assert upstream.sent[3][2]["json"] == {"other_task_id": 490, "relation_kind": "related"}


async def test_close_superseded_without_a_target_adds_no_relation(upstream):
    _close_routes(upstream)

    out = await call(server.task_close, task_id=473, verdict="superseded", evidence="see docs")

    assert out["relation_added"] is False
    assert ("POST", "/tasks/473/relations") not in upstream.calls()


async def test_close_skips_a_relation_that_already_exists(upstream):
    task = _open_task(related_tasks={"duplicateof": [{"id": 480, "title": "x"}]})
    _close_routes(upstream, task=task, target=480)

    out = await call(
        server.task_close, task_id=473, verdict="duplicate", evidence="dup", duplicate_of=480
    )

    assert out["relation_added"] is False
    assert out["done"] is True
    assert ("POST", "/tasks/473/relations") not in upstream.calls()


async def test_close_already_done_is_a_no_op_with_zero_writes(upstream):
    _close_routes(upstream, task=fixtures.task(id=473, identifier="#454", done=True))

    out = await call(server.task_close, task_id=473, verdict="obsolete", evidence="gone")

    assert out == {
        "id": 473,
        "identifier": "#454",
        "done": True,
        "verdict": "obsolete",
        "comment_id": None,
        "relation_added": False,
        "already_done": True,
    }
    assert upstream.writes() == []


async def test_close_relation_failure_reports_steps_and_does_not_close(upstream):
    _close_routes(upstream, target=480, relation=VikunjaAPIError(500, "relation boom"))

    out = await call(
        server.task_close, task_id=473, verdict="duplicate", evidence="dup", duplicate_of=480
    )

    assert out == {
        "id": 473,
        "identifier": "#454",
        "done": False,
        "verdict": "duplicate",
        "comment_id": 9001,
        "relation_added": False,
        "already_done": False,
        "failed_step": "relation",
        "error": "Vikunja API error 500: relation boom",
    }
    assert ("PATCH", "/tasks/473") not in upstream.calls()


async def test_close_done_failure_reports_the_relation_that_did_land(upstream):
    _close_routes(upstream, target=480, done=VikunjaAPIError(503, "unavailable"))

    out = await call(
        server.task_close, task_id=473, verdict="duplicate", evidence="dup", duplicate_of=480
    )

    assert (out["failed_step"], out["relation_added"], out["done"]) == ("done", True, False)
    assert out["comment_id"] == 9001
    # A definite HTTP refusal is not re-read: Vikunja said no.
    assert "outcome" not in out
    assert upstream.calls().count(("GET", "/tasks/473")) == 1


def _reads_of_task(task_after: dict | BaseException):
    """GET /tasks/473: the open task on the first read, ``task_after`` on every later one."""
    reads = iter([_open_task()])
    return lambda kw: next(reads, task_after)


async def test_close_done_timeout_reports_the_state_it_reads_back(upstream):
    # F-03: the PATCH timed out but landed. Reporting `done: false` would be wrong.
    _close_routes(upstream, done=VikunjaAPIError(0, "timeout"))
    upstream.on("GET", "/tasks/473", _reads_of_task({**_open_task(), "done": True}))

    out = await call(server.task_close, task_id=473, verdict="obsolete", evidence="gone")

    assert out == {
        "id": 473,
        "identifier": "#454",
        "done": True,
        "verdict": "obsolete",
        "comment_id": 9001,
        "relation_added": False,
        "already_done": False,
        "failed_step": "done",
        "error": "Vikunja API error 0: timeout",
        "outcome": "reread",
    }


async def test_close_relation_timeout_reads_back_whether_the_relation_landed(upstream):
    _close_routes(upstream, target=480, relation=VikunjaAPIError(0, "timeout"))
    landed = _open_task(related_tasks={"duplicateof": [{"id": 480}]})
    upstream.on("GET", "/tasks/473", _reads_of_task(landed))

    out = await call(
        server.task_close, task_id=473, verdict="duplicate", evidence="d", duplicate_of=480
    )

    assert (out["failed_step"], out["relation_added"], out["done"]) == ("relation", True, False)
    assert out["outcome"] == "reread"


async def test_close_done_timeout_with_failed_reread_reports_unknown(upstream):
    _close_routes(upstream, done=VikunjaAPIError(0, "timeout"))
    upstream.on("GET", "/tasks/473", _reads_of_task(VikunjaAPIError(0, "still down")))

    out = await call(server.task_close, task_id=473, verdict="obsolete", evidence="gone")

    assert (out["done"], out["outcome"], out["failed_step"]) == (None, "unknown", "done")


def _comments_back(*bodies: str, created: str | None = None):
    """Comments as the read-back sees them, created now unless ``created`` says otherwise."""
    stamp = created or f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}"
    return lambda kw: [
        {"id": 9100 + i, "comment": b, "created": stamp} for i, b in enumerate(bodies)
    ]


async def test_close_comment_timeout_never_adopts_an_older_identical_comment(upstream):
    # R2-02: an identical closing comment from an earlier close (before a reopen, say) is
    # not this call's comment. Adopting it would mark the ticket done on old evidence.
    _close_routes(upstream, comment=VikunjaAPIError(0, "timeout"))
    body = "<p>Closing: OBSOLETE \u2014 gone</p>"
    upstream.on("GET", "/tasks/473/comments", _comments_back(body, created="2025-01-01T00:00:00Z"))

    with pytest.raises(VikunjaAPIError, match="timeout"):
        await call(server.task_close, task_id=473, verdict="obsolete", evidence="gone")
    assert upstream.writes() == [("POST", "/tasks/473/comments")]


async def test_close_comment_timeout_that_landed_continues_the_close(upstream):
    # F-03: the comment POST timed out, but the comment is there. Raising would tell the
    # caller nothing was written, and a retry would post it twice.
    _close_routes(upstream, comment=VikunjaAPIError(0, "timeout"))
    body = "<p>Closing: OBSOLETE \u2014 gone</p>"
    upstream.on("GET", "/tasks/473/comments", _comments_back("<p>other</p>", body))

    out = await call(server.task_close, task_id=473, verdict="obsolete", evidence="gone")

    assert (out["comment_id"], out["done"]) == (9101, True)
    assert upstream.writes() == [("POST", "/tasks/473/comments"), ("PATCH", "/tasks/473")]
    reread = upstream.sent[2][2]["params"]
    assert (reread["order_by"], reread["page"]) == ("desc", 1)


async def test_close_comment_timeout_that_did_not_land_raises(upstream):
    _close_routes(upstream, comment=VikunjaAPIError(0, "timeout"))
    upstream.on("GET", "/tasks/473/comments", _comments_back("<p>other</p>"))

    with pytest.raises(VikunjaAPIError, match="timeout"):
        await call(server.task_close, task_id=473, verdict="obsolete", evidence="gone")
    assert upstream.writes() == [("POST", "/tasks/473/comments")]


async def test_close_comment_timeout_with_failed_reread_says_outcome_unknown(upstream):
    _close_routes(upstream, comment=VikunjaAPIError(0, "timeout"))
    upstream.on("GET", "/tasks/473/comments", lambda kw: VikunjaAPIError(0, "still down"))

    with pytest.raises(VikunjaAPIError, match="may or may not exist"):
        await call(server.task_close, task_id=473, verdict="obsolete", evidence="gone")
    assert upstream.writes() == [("POST", "/tasks/473/comments")]


async def test_close_comment_failure_raises_because_nothing_was_written(upstream):
    _close_routes(upstream, comment=VikunjaAPIError(403, "forbidden"))

    with pytest.raises(VikunjaAPIError, match="forbidden"):
        await call(server.task_close, task_id=473, verdict="wontfix", evidence="not planned")
    assert upstream.writes() == [("POST", "/tasks/473/comments")]


async def test_close_missing_target_is_refused_before_any_write(upstream):
    _close_routes(upstream, target=999, lookup=VikunjaAPIError(404, "task does not exist"))

    with pytest.raises(VikunjaAPIError, match="does not exist"):
        await call(
            server.task_close, task_id=473, verdict="duplicate", evidence="d", duplicate_of=999
        )
    assert upstream.writes() == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"verdict": "duplicate", "evidence": "x"}, "requires duplicate_of"),
        ({"verdict": "resolved", "evidence": "x", "duplicate_of": 5}, "only valid with"),
        ({"verdict": "obsolete", "evidence": "x", "superseded_by": 5}, "only valid with"),
        ({"verdict": "fixed", "evidence": "x"}, "verdict must be one of"),
        ({"verdict": "resolved", "evidence": "   "}, "evidence is required"),
        ({"verdict": "resolved", "evidence": "a\nb"}, "one line"),
        ({"verdict": "resolved", "evidence": "a\u2028b"}, "one line"),
        ({"verdict": "resolved", "evidence": "x" * 1001}, "limit is 1000"),
        ({"verdict": "duplicate", "evidence": "x", "duplicate_of": 473}, "of itself"),
    ],
)
async def test_close_refuses_bad_arguments_before_any_call(upstream, kwargs, message):
    with pytest.raises(ValueError, match=message):
        await call(server.task_close, task_id=473, **kwargs)
    assert upstream.calls() == []


async def test_close_normalises_verdict_case_and_trims_evidence(upstream):
    _close_routes(upstream)

    out = await call(server.task_close, task_id=473, verdict=" Resolved ", evidence="  ok  ")

    assert out["verdict"] == "resolved"
    assert upstream.sent[1][2]["json"]["comment"] == "<p>Closing: RESOLVED — ok</p>"


def test_task_close_is_audited_and_resolves_refs():
    assert "task_close" in server._AUDITED_TOOLS
    assert "task_close" in server._TASK_REF_TOOLS


# ===========================================================================
# return_mode="minimal"
# ===========================================================================

_BIG = "<p>" + "x" * 50_000 + "</p>"


async def test_task_update_minimal_is_bounded_and_has_no_description(upstream):
    upstream.on(
        "PATCH",
        "/tasks/473",
        lambda kw: fixtures.task(id=473, identifier="#454", done=True, description=_BIG),
    )

    out = await call(server.task_update, task_id=473, done=True, return_mode="minimal")

    assert set(out) == {"id", "identifier", "done", "updated"}
    assert (out["id"], out["identifier"], out["done"]) == (473, "#454", True)
    assert len(json.dumps(out)) < 200


async def test_task_update_default_is_still_the_full_body(upstream):
    upstream.on("PATCH", "/tasks/473", lambda kw: fixtures.task(id=473, description="<p>body</p>"))

    out = await call(server.task_update, task_id=473, done=True)

    assert out["description"] == "<p>body</p>"


async def test_bulk_minimal_reports_what_the_server_wrote(upstream):
    upstream.on(
        "PUT",
        "/tasks/bulk",
        lambda kw: {
            "task_ids": [1, 2],
            "fields": ["done"],
            "values": {"done": True},
            "tasks": [fixtures.task(id=1, description=_BIG), fixtures.task(id=2)],
        },
    )

    out = await call(
        server.tasks_bulk_update, task_ids=[1, 2], values={"done": True}, return_mode="minimal"
    )

    assert out == {"count": 2, "ids": [1, 2], "fields": ["done"]}


async def test_bulk_minimal_does_not_invent_ids_the_reply_lacks(upstream):
    upstream.on("PUT", "/tasks/bulk", lambda kw: {"ok": True})

    out = await call(
        server.tasks_bulk_update, task_ids=[1, 2], values={"done": True}, return_mode="minimal"
    )

    assert out == {"count": None, "ids": None, "fields": None}


async def test_comment_create_minimal(upstream):
    upstream.on(
        "POST",
        "/tasks/473/comments",
        lambda kw: {
            "id": 7,
            "comment": _BIG,
            "author": {"id": 3, "username": "u"},
            "created": "2026-10-27T21:00:00Z",
            "updated": "2026-10-27T21:00:00Z",
        },
    )

    out = await call(server.comment_create, task_id=473, comment="hi", return_mode="minimal")

    assert out == {"id": 7, "created": "2026-10-27T21:00:00Z"}


async def test_task_label_add_minimal(upstream):
    upstream.on(
        "POST", "/tasks/473/labels", lambda kw: {"label_id": 36, "created": "2026-10-27T21:00Z"}
    )

    out = await call(server.task_label_add, task_id=473, label_id=36, return_mode="minimal")

    assert out == {"task_id": 473, "label_id": 36, "created": "2026-10-27T21:00Z"}


async def test_task_relation_add_minimal(upstream):
    upstream.on(
        "POST",
        "/tasks/473/relations",
        lambda kw: {
            "task_id": 473,
            "other_task_id": 480,
            "relation_kind": "related",
            "created_by": {"id": 3, "username": "u", "name": ""},
            "created": "2026-10-27T21:00:00Z",
        },
    )

    out = await call(
        server.task_relation_add,
        task_id=473,
        other_task_id=480,
        relation_kind="related",
        return_mode="minimal",
    )

    assert out == {
        "task_id": 473,
        "other_task_id": 480,
        "relation_kind": "related",
        "created": "2026-10-27T21:00:00Z",
    }


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("task_update", {"task_id": 473, "done": True}),
        ("tasks_bulk_update", {"task_ids": [473], "values": {"done": True}}),
        ("comment_create", {"task_id": 473, "comment": "x"}),
        ("task_label_add", {"task_id": 473, "label_id": 1}),
        ("task_relation_add", {"task_id": 473, "other_task_id": 480, "relation_kind": "related"}),
    ],
)
async def test_unknown_return_mode_is_refused_before_any_call(upstream, tool, kwargs):
    with pytest.raises(ValueError, match="return_mode must be one of"):
        await call(getattr(server, tool), return_mode="minmal", **kwargs)
    assert upstream.calls() == []


# ===========================================================================
# task_get(fields=, include_comments=)
# ===========================================================================


async def test_task_get_fields_projects_and_always_keeps_id(upstream):
    upstream.on("GET", "/tasks/473", lambda kw: fixtures.task(id=473, done=False))

    out = await call(server.task_get, task_id=473, fields=["done", "updated", "nonsense"])

    assert set(out) == {"id", "done", "updated", "unknown_fields"}
    assert out["unknown_fields"] == ["nonsense"]


async def test_task_get_fields_without_unknowns_has_no_unknown_key(upstream):
    upstream.on("GET", "/tasks/473", lambda kw: fixtures.task(id=473))

    out = await call(server.task_get, task_id=473, fields=["url", "stale"])

    assert set(out) == {"id", "url", "stale"}


@pytest.mark.parametrize("fields", ["done", [""], [1], {"done": True}])
async def test_task_get_refuses_malformed_fields(upstream, fields):
    with pytest.raises(ValueError, match="fields must be a list"):
        await call(server.task_get, task_id=473, fields=fields)
    assert upstream.calls() == []


def _comment(cid: int, html_body: str) -> dict:
    return {
        "id": cid,
        "comment": html_body,
        "author": {"id": 3, "name": "", "username": "agent-one"},
        "reactions": None,
        "created": f"2026-10-2{cid}T10:00:00Z",
        "updated": f"2026-10-2{cid}T10:00:00Z",
    }


async def test_task_get_include_comments_compacts_every_comment(upstream):
    upstream.on("GET", "/tasks/473", lambda kw: fixtures.task(id=473))
    long_body = "<p>" + "y" * 600 + "</p>"
    upstream.on(
        "GET",
        "/tasks/473/comments",
        lambda kw: _envelope(
            [
                _comment(1, "<p>Closing: RESOLVED &mdash; fixed in <code>abc</code></p>"),
                _comment(2, long_body),
            ],
            total=2,
            total_pages=1,
        ),
    )

    out = await call(server.task_get, task_id=473, include_comments=True)

    assert out["comments"] == [
        {
            "id": 1,
            "author": "agent-one",
            "created": "2026-10-21T10:00:00Z",
            "text": "Closing: RESOLVED — fixed in abc",
            "truncated": False,
        },
        {
            "id": 2,
            "author": "agent-one",
            "created": "2026-10-22T10:00:00Z",
            "text": "y" * 500,
            "truncated": True,
        },
    ]
    params = upstream.sent[1][2]["params"]
    assert (params["order_by"], params["per_page"]) == ("asc", server._MAX_PAGE_SIZE)
    assert "description" in out  # the projection is otherwise unchanged


async def test_task_get_fields_and_comments_together(upstream):
    upstream.on("GET", "/tasks/473", lambda kw: fixtures.task(id=473))
    upstream.on(
        "GET",
        "/tasks/473/comments",
        lambda kw: _envelope([_comment(1, "<p>hi</p>")], total=1, total_pages=1),
    )

    out = await call(server.task_get, task_id=473, fields=["done"], include_comments=True)

    assert set(out) == {"id", "done", "comments"}


async def test_task_get_without_new_arguments_makes_one_call(upstream):
    upstream.on("GET", "/tasks/473", lambda kw: fixtures.task(id=473))

    await call(server.task_get, task_id=473)

    assert upstream.calls() == [("GET", "/tasks/473")]


@pytest.mark.parametrize(
    ("fragment", "text"),
    [
        ("<p>a</p><p>b</p>", "a\nb"),
        ("<p>line<br />\nnext</p>", "line\nnext"),
        ("<ul><li>one</li><li>two</li></ul>", "one\ntwo"),
        ("<p>&lt;b&gt; &amp; &quot;q&quot;</p>", '<b> & "q"'),
        ("", ""),
        (None, ""),
    ],
)
def test_html_text(fragment, text):
    assert server._html_text(fragment) == text
