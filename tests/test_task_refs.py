"""Ticket-reference resolution and the compact read projections (v0.5.0).

Two changes are covered here, and they are related: agents were confusing the per-project
ticket number (`#454`) with the global task id (`473`) — vikunja#331, where an agent passed
one for the other and silently mutated three unrelated tickets. v0.5.0 attacks that from
both ends: accept `"#454"` explicitly so the confusion has a correct answer, and stop
returning the bare `index` so there is nothing left to confuse it with.

The fixtures in ``tests.fixtures`` carry the real API key set on purpose. A hand-written
mock that simply omits ``index`` cannot fail the strip assertions, which would leave the
suite green while the bug shipped.
"""

from __future__ import annotations

import inspect
import os
from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import config, hooks, server
from vikunja_mcp.exceptions import VikunjaAPIError

from . import fixtures


@pytest.fixture(autouse=True)
def _builtins():
    """Exercise the shipped guardrails, not a bare tool — that is the thing under test."""
    hooks.clear_hooks()
    server.register_builtin_hooks()
    yield
    hooks.clear_hooks()
    server.register_builtin_hooks()


@pytest.fixture
def _upstream(monkeypatch):
    mock = AsyncMock(return_value=fixtures.task())
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    return mock


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def call(tool, **kwargs):
    return await _fn(tool)(**kwargs)


# ===========================================================================
# Resolution — what `task_id` accepts
# ===========================================================================


async def test_bare_int_passes_through_without_an_api_call(_upstream):
    """A global id is already the answer. No lookup, and no chance to get it wrong."""
    await call(server.task_get, task_id=473)
    assert _upstream.call_args.args[:2] == ("GET", "/tasks/473")
    assert _upstream.await_count == 1  # the task_get itself, no resolve


async def test_digit_string_is_a_global_id_not_a_ticket_number(_upstream):
    """`"473"` is the id 473 — never ticket #473.

    Pydantic's smart union preserves the string rather than coercing it to an int, which is
    what lets the resolver tell the two forms apart at all. The rule is that the `#` is what
    marks a ticket number; a bare number, string or int, is always a global id. Reading a
    bare number as a ticket number is exactly the guess vikunja#331 made.
    """
    await call(server.task_get, task_id="473")
    assert _upstream.call_args.args[:2] == ("GET", "/tasks/473")
    assert _upstream.await_count == 1


async def test_ticket_ref_resolves_via_the_index_filter(monkeypatch):
    """`"#454"` -> one filtered lookup -> id 473, then the real call against 473."""
    mock = AsyncMock(side_effect=[[fixtures.task(id=473)], fixtures.task(id=473)])
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    await call(server.task_get, task_id="#454")

    resolve, fetch = mock.await_args_list
    assert resolve.args[:2] == ("GET", "/tasks")
    assert resolve.kwargs["params"] == {"filter": "index = 454"}
    assert fetch.args[:2] == ("GET", "/tasks/473")


async def test_combined_forge_form_skips_the_api_call_entirely(monkeypatch):
    """`"#456 (id 475)"` spells the id out — take it, do not go ask.

    This is the format forge tickets, CLAUDE.md files and Matrix messages already use, so
    it is what agents paste. Asserting the transport is never touched is the point of the
    test: a resolver that "works" by making a redundant round-trip is not this behaviour.
    """
    mock = AsyncMock(return_value=fixtures.task(id=475))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    await call(server.task_get, task_id="#456 (id 475)")

    assert mock.await_count == 1
    assert mock.call_args.args[:2] == ("GET", "/tasks/475")


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "abc",
        "#",
        "#4a",
        "-5",
        "4.5",
        None,
        4.5,
        "../../user",
        "473/../../projects/1",
        "473?x=1",
        "473 OR 1=1",
        "473\x00",
        "²",  # str.isdigit() is True here; int() would raise a confusing message
        "٤٧٣",  # Unicode decimal digits convert cleanly but are almost certainly a mistake
        "see id 999 somewhere",  # prose mentioning an id is not a ticket reference
        "id 5 and id 6",
    ],
)
async def test_unrecognised_forms_raise_and_name_what_is_accepted(ref):
    with pytest.raises(ValueError, match="task_id must be"):
        await server._resolve_task_ref(ref)


async def test_no_string_form_can_reach_the_url_path_unresolved():
    """The security property behind widening `task_id` to `int | str` (IV-01/IV-19).

    `task_id` is interpolated into `f"/tasks/{task_id}"`, so accepting strings is only safe
    if `int()` is the resolver's sole exit. Anything else — a traversal segment, a query
    fragment, a filter clause — must raise rather than be handed to the URL builder.
    """
    for hostile in ("../../user", "473/../../projects/1", "%2e%2e%2fuser", "#454 && project = 2"):
        with pytest.raises(ValueError):
            await server._resolve_task_ref(hostile)

    for accepted in (473, "473", " 473 ", "#456 (id 475)"):
        resolved = await server._resolve_task_ref(accepted)
        assert type(resolved) is int, f"{accepted!r} resolved to {type(resolved).__name__}"


async def test_a_reference_naming_several_ids_raises_instead_of_taking_the_first(monkeypatch):
    """`"#456 (id 475) blocks #331 (id 342)"` is a coin flip, not an answer.

    Every other ambiguous path in this module raises and names the candidates. A regex
    searching for the first `id N` anywhere in the string would have made this one the
    silent exception — which is the exact behaviour vikunja#331 is an incident report about.
    """
    mock = AsyncMock()
    monkeypatch.setattr(server, "request", mock)

    with pytest.raises(ValueError) as excinfo:
        await server._resolve_task_ref("#456 (id 475) blocks #331 (id 342)")

    assert "475" in str(excinfo.value)
    assert "342" in str(excinfo.value)
    mock.assert_not_awaited()


async def test_repeating_the_same_id_is_not_ambiguous():
    """Two mentions of one id name one task. Only *different* ids are a conflict."""
    assert await server._resolve_task_ref("#456 (id 475) — see id 475") == 475


async def test_boolean_is_refused_rather_than_read_as_task_1():
    """`bool` is an `int` subclass, so `True` would otherwise silently mean task 1."""
    with pytest.raises(ValueError, match="boolean"):
        await server._resolve_task_ref(True)


# ===========================================================================
# Resolution — the failure paths, which are the safety-critical ones
# ===========================================================================


async def test_ambiguous_ticket_number_raises_and_names_every_candidate(monkeypatch):
    """`#1` matches two projects live (id 9 and id 344). Never pick one.

    The error must name both candidates so the caller can choose. Deliberately asserts no
    winner: a resolver that guesses here reproduces vikunja#331 with extra steps.
    """
    mock = AsyncMock(
        return_value=[
            fixtures.task(id=9, project_id=7),
            fixtures.task(id=344, project_id=2),
        ]
    )
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    with pytest.raises(ValueError) as excinfo:
        await call(server.task_get, task_id="#1")

    message = str(excinfo.value)
    assert "ambiguous" in message
    assert "id 9 (project 7)" in message
    assert "id 344 (project 2)" in message
    assert mock.await_count == 1  # resolve only — the tool body never ran


async def test_unresolvable_ticket_number_raises_rather_than_returning_nothing(monkeypatch):
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    with pytest.raises(ValueError, match="no task with ticket number #999"):
        await call(server.task_get, task_id="#999")


async def test_failed_resolve_never_falls_back_to_treating_the_ref_as_an_id(monkeypatch):
    """The #331 failure mode, reintroduced as an error path. This is the guard against it.

    If the `index` filter starts failing — most likely a Vikunja upgrade dropping the
    undocumented field — the only safe behaviour is to raise. Falling back to `/tasks/454`
    would mutate whatever unrelated task happens to hold id 454.
    """
    mock = AsyncMock(side_effect=VikunjaAPIError(400, "The task field 'index' is invalid"))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    with pytest.raises(VikunjaAPIError) as excinfo:
        await call(server.task_delete, task_id="#454")

    # The message has to name the version the filter was verified against, or the next
    # reader has no way to connect a 400 here to an upgrade.
    assert server._VERIFIED_VIKUNJA_VERSION in str(excinfo.value)
    assert mock.await_count == 1  # crucially: no DELETE /tasks/454 followed


async def test_default_project_id_scopes_the_resolve(monkeypatch):
    monkeypatch.setenv("VIKUNJA_DEFAULT_PROJECT_ID", "7")
    config.reset_settings()
    mock = AsyncMock(side_effect=[[fixtures.task(id=473)], fixtures.task(id=473)])
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    await call(server.task_get, task_id="#454")

    assert mock.await_args_list[0].kwargs["params"] == {"filter": "project = 7 && index = 454"}


async def test_resolve_reads_the_pagination_envelope_not_just_a_bare_list(monkeypatch):
    """A collision spanning more than one page still has to be seen as ambiguous."""
    enveloped = fixtures.paginated(
        [fixtures.task(id=9, project_id=7), fixtures.task(id=344, project_id=2)]
    )
    mock = AsyncMock(return_value=enveloped)
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    with pytest.raises(ValueError, match="ambiguous"):
        await call(server.task_get, task_id="#1")


# ===========================================================================
# Resolution — wiring: every task_id tool, not just the ones someone remembered
# ===========================================================================


def _task_id_tools():
    """Every tool defined in server.py whose signature takes a `task_id`.

    Derived from the live signatures rather than a second hand-written list, which would
    just be the first list's typo repeated.
    """
    found = []
    for name, obj in vars(server).items():
        fn = _fn(obj) if callable(obj) else None
        if name.startswith("_") or fn is None or not inspect.iscoroutinefunction(fn):
            continue
        if fn.__module__ != server.__name__:
            continue  # imported helper (client.request), not a tool defined here
        if "task_id" in inspect.signature(fn).parameters:
            found.append(name)
    return sorted(found)


def test_every_task_id_tool_is_registered_for_ref_resolution():
    """A new task_id-taking tool that nobody adds to `_TASK_REF_TOOLS` cannot slip through.

    Such a tool would reject `"#454"` while its neighbours accept it. "Works on most tools"
    is a worse contract than "works on none", because it teaches the agent a rule that is
    false somewhere it will not find out until it matters.
    """
    assert _task_id_tools() == sorted(server._TASK_REF_TOOLS)


@pytest.mark.parametrize("name", sorted(server._TASK_REF_TOOLS))
async def test_published_schema_accepts_a_string_task_id(name):
    """Asserted against the schema FastMCP actually publishes, not against the source.

    The annotation is load-bearing, not cosmetic: FastMCP derives the tool schema from
    `wrapper.__signature__`, so a `task_id: int` parameter rejects `"#454"` at schema
    validation *before* any before-hook can run. If this fails, resolution is dead for that
    tool no matter what the hook does — and because `server.py` uses
    `from __future__ import annotations`, reading the annotation off the signature would
    only prove the *string* `"int | str"` is present, not that pydantic resolved it into a
    union the transport honours.
    """
    tool = await server.mcp.get_tool(name)
    schema = tool.parameters["properties"]["task_id"]
    assert schema == {"anyOf": [{"type": "integer"}, {"type": "string"}]}, name


def test_ref_hook_is_registered_once_per_tool_after_repeated_calls():
    server.register_builtin_hooks()
    server.register_builtin_hooks()
    for name in server._TASK_REF_TOOLS:
        registered = [h for h in hooks.before_handlers(name) if h is server._resolve_task_ref_kwarg]
        assert len(registered) == 1, name


async def test_hook_leaves_a_call_without_a_task_id_alone():
    assert await server._resolve_task_ref_kwarg({"project_id": 7}) == {"project_id": 7}


# ===========================================================================
# Phase 1 — no read path returns a bare `index`
# ===========================================================================


async def test_task_get_strips_index_at_every_nesting_depth(_upstream):
    """Including the task Vikunja inlines under `related_tasks`, which carries its own."""
    out = await call(server.task_get, task_id=361)

    assert "index" not in out
    nested = out["related_tasks"]["related"][0]
    assert "index" not in nested
    assert nested["id"] == 348  # the nested task is still identifiable, just not by index


async def test_task_list_strips_index_inside_the_pagination_envelope(monkeypatch):
    mock = AsyncMock(return_value=fixtures.paginated(fixtures.task_list(3)))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    out = await call(server.task_list, verbose=True)

    assert all("index" not in row for row in out["items"])
    assert out["pagination"]["truncated"] is True


async def test_index_is_gone_even_in_verbose_mode(_upstream):
    """`verbose` restores the payload, not the ambiguity. There is no way to read `index`."""
    out = await call(server.task_get, task_id=361, verbose=True)
    assert "index" not in out
    assert "index" not in out["related_tasks"]["related"][0]


async def test_strip_leaves_identifier_intact(_upstream):
    """Five forge consumers render `identifier`; it is a string, so it cannot be misused."""
    out = await call(server.task_get, task_id=361)
    assert out["identifier"] == "#342"


async def test_strip_does_not_touch_pagination_metadata(monkeypatch):
    """`pagination` is this server's own metadata, not a task. It must survive untouched."""
    envelope = fixtures.paginated(fixtures.task_list(2), page=2, total_pages=9)
    mock = AsyncMock(return_value=envelope)
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    out = await call(server.task_list)

    assert out["pagination"] == {
        "page": 2,
        "total_pages": 9,
        "count": 2,
        "total": 18,
        "truncated": True,
    }


async def test_mutating_tools_that_return_a_task_body_strip_index_too(monkeypatch):
    """task_update and friends return a full task. Naming only the read tools left five open."""
    mock = AsyncMock(return_value=fixtures.task())
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    for tool, kwargs in (
        (server.task_update, {"task_id": 361, "title": "x"}),
        (server.task_delete, {"task_id": 361}),
        (server.task_bucket_move, {"project_id": 7, "view_id": 1, "bucket_id": 1, "task_id": 361}),
    ):
        assert "index" not in await call(tool, **kwargs), tool.__name__


# ===========================================================================
# Phase 2 — compact-by-default projections
# ===========================================================================


async def test_task_list_returns_summary_rows_without_description(monkeypatch):
    mock = AsyncMock(return_value=fixtures.task_list(3))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    rows = await call(server.task_list)

    assert len(rows) == 3
    for row in rows:
        assert "description" not in row
        assert "related_tasks" not in row
        assert row["title"]
        assert row["labels"] == [
            {"id": 35, "title": "type:security"},
            {"id": 36, "title": "agent-filed"},
        ]
        assert row["assignee_count"] == 0


async def test_task_list_summary_is_dramatically_smaller(monkeypatch):
    """The measured win, asserted rather than assumed — 182 KB was ~45k tokens per call.

    The multiplier moved from 5 to 4 in v0.7.0, and the honest reason is that the two
    staleness fields cost real bytes: 41 of 446 per row (~9%), taking this fixture's ratio
    from 5.2x to 4.75x. It is a retarget, not a rounding — the guard still fails on any
    change that meaningfully re-inflates a list row.

    Note this fixture *understates* the real win. Its synthetic `description` is a single
    line; on the live corpus `description` was 132 KB of the measured 182 KB page, which is
    the field the projection actually exists to drop.
    """
    import json

    full = fixtures.task_list(50)
    mock = AsyncMock(return_value=full)
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    compact = await call(server.task_list)

    assert len(json.dumps(compact)) * 4 < len(json.dumps(full))


async def test_task_search_projects_the_same_way(monkeypatch):
    mock = AsyncMock(return_value=fixtures.task_list(2))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    rows = await call(server.task_search, query="audit")

    assert all("description" not in row for row in rows)


async def test_task_get_keeps_description_but_thins_related_tasks(_upstream):
    """The asymmetry the plan is built on: lists pay for `description`, single reads don't."""
    out = await call(server.task_get, task_id=361)

    assert out["description"]  # reading one ticket's body is the point of task_get
    nested = out["related_tasks"]["related"][0]
    assert set(nested) == {"id", "identifier", "title", "done"}
    assert "description" not in nested


async def test_counted_collections_stay_discoverable(monkeypatch):
    """Dropping attachments silently would hide that any exist. Counts say "go look"."""
    mock = AsyncMock(
        return_value=fixtures.task(attachments=[{"id": 1}, {"id": 2}], reactions={"+1": ["a"]})
    )
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    out = await call(server.task_get, task_id=361)

    assert out["attachment_count"] == 2
    assert out["reaction_count"] == 1
    assert "attachments" not in out
    assert "reactions" not in out


async def test_verbose_round_trips_the_upstream_body(monkeypatch):
    """`verbose=True` is an escape hatch, so it has to actually return everything.

    Retargeted in v0.7.0. This was an exact-equality assertion, which made it a tripwire on
    *any* added field rather than a guard on dropped ones — and the thing it exists to
    catch is a field going missing. It now asserts the upstream body survives intact and
    pins the added keys explicitly, so a future addition still has to be declared here but
    a silent removal still fails.
    """
    body = fixtures.task()
    mock = AsyncMock(return_value=body)
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    out = await call(server.task_get, task_id=361, verbose=True)

    expected = fixtures.task()
    expected.pop("index")  # the Phase 1 strip still applies; only the payload comes back
    expected["related_tasks"]["related"][0].pop("index")

    # Nothing from upstream is dropped or altered.
    assert {k: out[k] for k in expected} == expected
    # And the only things added are the two derived staleness fields (vikunja#464).
    assert set(out) - set(expected) == {"days_since_update", "stale"}


async def test_a_body_with_no_id_is_not_given_a_url_to_nothing(monkeypatch):
    """A 204 yields `{"ok": True}`. Projecting it would mint a `/tasks/None` link."""
    mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    assert await call(server.task_get, task_id=361) == {"ok": True}


async def test_verbose_list_keeps_description(monkeypatch):
    mock = AsyncMock(return_value=fixtures.task_list(2))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    rows = await call(server.task_list, verbose=True)

    assert all(row["description"] for row in rows)


async def test_projected_list_inside_an_envelope_keeps_pagination(monkeypatch):
    mock = AsyncMock(return_value=fixtures.paginated(fixtures.task_list(2), total_pages=6))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    out = await call(server.task_list)

    assert out["pagination"]["total_pages"] == 6
    assert all("description" not in row for row in out["items"])


# ===========================================================================
# Phase 4 — the `url` field
# ===========================================================================


async def test_url_is_built_from_id_never_from_the_ticket_number(_upstream):
    """`/tasks/342` and `/tasks/361` are different tickets. This is the trap being closed."""
    out = await call(server.task_get, task_id=361)
    assert out["url"] == "https://vikunja.test/tasks/361"


async def test_summary_rows_carry_a_url(monkeypatch):
    mock = AsyncMock(return_value=fixtures.task_list(2))
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    rows = await call(server.task_list)

    assert [row["url"] for row in rows] == [
        "https://vikunja.test/tasks/361",
        "https://vikunja.test/tasks/362",
    ]


# ===========================================================================
# Undocumented-dependency canary — live, opt-in
# ===========================================================================


@pytest.mark.skipif(
    not os.environ.get("VIKUNJA_LIVE_TOKEN"),
    reason="live guard — set VIKUNJA_LIVE_TOKEN and VIKUNJA_LIVE_INDEX/ID to run",
)
async def test_live_index_filter_still_resolves(monkeypatch):
    """Canary for the one undocumented thing this build depends on.

    Vikunja's published filter-field list does **not** include `index`. It works on the
    version in `_VERIFIED_VIKUNJA_VERSION`, verified with a negative control
    (`bogusfield = 1` -> 400, so unknown fields are rejected rather than ignored, which is
    what proves the filter is genuinely applied). If a future upgrade drops it, this fails
    loudly here rather than quietly at 3am inside a task_update.
    """
    from vikunja_mcp import client

    monkeypatch.setenv("VIKUNJA_URL", os.environ["VIKUNJA_LIVE_URL"])
    config.reset_settings()
    await client.aclose()

    index = int(os.environ["VIKUNJA_LIVE_INDEX"])
    expected_id = int(os.environ["VIKUNJA_LIVE_ID"])
    try:
        resolved = await server._resolve_index(index, os.environ["VIKUNJA_LIVE_TOKEN"])
    except VikunjaAPIError as exc:
        pytest.fail(
            f"the `index` filter no longer works against this Vikunja "
            f"({exc}). It is undocumented and was verified on "
            f"{server._VERIFIED_VIKUNJA_VERSION}; an upgrade has most likely removed it. "
            "Ticket-reference resolution is broken until this is addressed."
        )
    assert resolved == expected_id
