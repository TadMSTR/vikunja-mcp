"""Near-duplicate detection on task_create (vikunja#463, id 482).

The load-bearing test in this file is `test_a_failing_search_still_creates_the_task`. Hook
handlers are not fire-and-forget — an exception in a `before` handler aborts the chain and
the create never happens — so a convenience feature is one unhandled timeout away from
silently eating the finding it was meant to protect. Everything else here is secondary to
that one holding.

The case-sensitivity tests are the other reason this file exists. Vikunja's `like` is
case-sensitive (measured on live v2.3.0, 2026-08-22: `%containerize%` matches nothing while
`%Containerize%` matches the ticket titled "Containerize searxng-mcp ..."), so the obvious
implementation — lowercase the terms, query once — misses most real duplicates, because
titles capitalise. That failure would look exactly like "no duplicates found".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import hooks, server
from vikunja_mcp.contrib import duplicate_check as dc


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


def _task(task_id: int, title: str, done: bool = False) -> dict:
    return {"id": task_id, "identifier": f"#{task_id}", "title": title, "done": done}


# --- term extraction ------------------------------------------------------


def test_stopwords_and_short_tokens_are_dropped():
    terms = dc.extract_terms("Add a fix for the thing")
    assert terms == []


def test_identifiers_outrank_ordinary_words():
    terms = dc.extract_terms("Containerize searxng-mcp to unblock LibreChat wiring")
    assert terms[0] == "searxng-mcp"


def test_extraction_is_deterministic_on_ties():
    title = "alpha bravo charlie"
    assert dc.extract_terms(title) == dc.extract_terms(title)


def test_extraction_is_capped():
    terms = dc.extract_terms("vikunja-mcp searxng-mcp dockhand-mcp patchmon-mcp githost-mcp")
    assert len(terms) == 3


def test_original_casing_is_preserved():
    """Terms keep their case from the title; the *query* supplies the variants."""
    assert "Containerize" in dc.extract_terms("Containerize searxng-mcp deployment")


@pytest.mark.parametrize("bad", [None, 123, ""])
def test_extraction_survives_a_non_string_title(bad):
    assert dc.extract_terms(bad) == []


# --- filter construction --------------------------------------------------


def test_filter_ands_terms_and_ors_casings():
    """AND across terms is what makes this a duplicate signal rather than a topic search."""
    built = dc.build_filter(7, ["searxng-mcp", "Cloudflare"])
    assert built.startswith("project = 7 && ")
    assert built.count("&&") == 2  # project + one per term
    assert '(title like "%searxng-mcp%" || title like "%Searxng-mcp%"' in built


def test_filter_covers_lower_capital_and_upper():
    """Because `like` is case-sensitive; verified live, see the module docstring."""
    built = dc.build_filter(None, ["mcp"])
    assert '"%mcp%"' in built and '"%Mcp%"' in built and '"%MCP%"' in built


def test_filter_omits_project_when_unscoped():
    assert not dc.build_filter(None, ["alpha"]).startswith("project")


def test_terms_cannot_carry_filter_syntax():
    """The tokenizer is the escaping story: a term can only hold [A-Za-z0-9_-].

    A title is agent-supplied text that ends up inside a filter expression, so this is the
    injection boundary. It holds structurally rather than by escaping — which is why there
    is no escaping routine to get wrong.
    """
    hostile = 'Drop %" || done = true || title like "% everything'
    terms = dc.extract_terms(hostile)
    assert all(c.isalnum() or c in "-_" for term in terms for c in term)
    built = dc.build_filter(7, terms)
    assert built.count('"') % 2 == 0
    assert "done = true" not in built


# --- the search -----------------------------------------------------------


async def test_finds_a_title_sharing_two_distinctive_terms(monkeypatch):
    monkeypatch.setattr(
        dc, "request", AsyncMock(return_value=[_task(332, "Containerize searxng-mcp for real")])
    )
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    found = await dc.find_possible_duplicates(7, "Containerize searxng-mcp again")
    assert found[0]["id"] == 332
    assert found[0]["matched_terms"] >= 2
    assert found[0]["url"].endswith("/tasks/332")


async def test_results_are_ordered_by_terms_matched(monkeypatch):
    monkeypatch.setattr(
        dc,
        "request",
        AsyncMock(
            return_value=[
                _task(1, "searxng-mcp something unrelated"),
                _task(2, "Containerize searxng-mcp deployment"),
            ]
        ),
    )
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    found = await dc.find_possible_duplicates(7, "Containerize searxng-mcp deployment")
    assert [c["id"] for c in found] == [2, 1]


async def test_scoring_is_case_insensitive_on_our_side(monkeypatch):
    """Upstream must be queried in several cases; comparing here needs no such workaround."""
    monkeypatch.setattr(
        dc, "request", AsyncMock(return_value=[_task(1, "CONTAINERIZE SEARXNG-MCP NOW")])
    )
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    found = await dc.find_possible_duplicates(7, "Containerize searxng-mcp deployment")
    assert found[0]["matched_terms"] == 2


async def test_done_tasks_are_included(monkeypatch):
    """A ticket re-filed because the original was closed is the case this exists to catch."""
    monkeypatch.setattr(
        dc, "request", AsyncMock(return_value=[_task(332, "Containerize searxng-mcp", done=True)])
    )
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    found = await dc.find_possible_duplicates(7, "Containerize searxng-mcp again")
    assert found[0]["done"] is True
    call = dc.request.await_args
    assert "done" not in call.kwargs["params"]["filter"]


async def test_a_title_with_too_little_signal_searches_nothing(monkeypatch):
    """A one-word title has no duplicate signal. Reporting the project would be worse."""
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(dc, "request", mock)
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    assert await dc.find_possible_duplicates(7, "Fix the bug") == []
    mock.assert_not_awaited()


async def test_a_single_long_identifier_is_enough(monkeypatch):
    monkeypatch.setattr(dc, "request", AsyncMock(return_value=[]))
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    await dc.find_possible_duplicates(7, "searxng-mcp")
    dc.request.assert_awaited()


async def test_results_are_capped(monkeypatch):
    rows = [_task(i, f"Containerize searxng-mcp variant {i}") for i in range(20)]
    monkeypatch.setattr(dc, "request", AsyncMock(return_value=rows))
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    found = await dc.find_possible_duplicates(7, "Containerize searxng-mcp variant")
    assert len(found) <= 5
    assert dc.request.await_args.kwargs["params"]["per_page"] == 5


async def test_one_upstream_call_only(monkeypatch):
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(dc, "request", mock)
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    await dc.find_possible_duplicates(7, "Containerize searxng-mcp deployment")
    assert mock.await_count == 1


# --- the hook pair, end to end -------------------------------------------


@pytest.fixture
def wired(monkeypatch):
    """Ensure the hooks are wired, and stub both the tool's upstream and the search's.

    Goes through the guarded registration rather than `register_duplicate_check` directly:
    now that the default is on, `_clean_hooks` has already wired the pair, and registering
    again would run the search twice per create.
    """
    monkeypatch.setenv("VIKUNJA_DUPLICATE_CHECK", "1")
    server._register_duplicate_check_if_enabled()
    created = {"id": 999, "identifier": "#999", "title": "Containerize searxng-mcp again"}
    monkeypatch.setattr(server, "request", AsyncMock(return_value=created))
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    monkeypatch.setattr(dc, "caller_token", lambda: "TOK")
    return created


async def test_duplicates_are_attached_to_the_created_task(wired, monkeypatch):
    monkeypatch.setattr(
        dc, "request", AsyncMock(return_value=[_task(332, "Containerize searxng-mcp for real")])
    )
    out = await _fn(server.task_create)(project_id=7, title="Containerize searxng-mcp again")
    assert out["id"] == 999  # the task itself is returned unchanged
    assert out["possible_duplicates"][0]["id"] == 332


async def test_no_duplicates_means_no_key_at_all(wired, monkeypatch):
    """An empty list would read as "checked, definitely novel" — a stronger claim than a
    lexical title match can support, and identical to the degraded case."""
    monkeypatch.setattr(dc, "request", AsyncMock(return_value=[]))
    out = await _fn(server.task_create)(project_id=7, title="Containerize searxng-mcp again")
    assert "possible_duplicates" not in out


async def test_the_search_runs_before_the_create(wired, monkeypatch):
    """Searching first is what keeps the new task out of its own duplicate list."""
    order = []

    async def search(*a, **kw):
        order.append("search")
        return []

    async def create(*a, **kw):
        order.append("create")
        return dict(wired)

    monkeypatch.setattr(dc, "request", search)
    monkeypatch.setattr(server, "request", create)
    await _fn(server.task_create)(project_id=7, title="Containerize searxng-mcp again")
    assert order == ["search", "create"]


async def test_the_search_is_scoped_to_the_target_project(wired, monkeypatch):
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(dc, "request", mock)
    await _fn(server.task_create)(project_id=2, title="Containerize searxng-mcp again")
    assert "project = 2" in mock.await_args.kwargs["params"]["filter"]


# --- the one that must never regress -------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("upstream exploded"),
        TimeoutError("vikunja is slow"),
        ValueError("filter rejected after a Vikunja upgrade"),
    ],
)
async def test_a_failing_search_still_creates_the_task(wired, monkeypatch, boom):
    """**The load-bearing test.** Duplicate detection must never cost a filing.

    `hooks.py` documents handlers as not fire-and-forget: an exception in a `before`
    handler aborts the chain, so without the catch inside the handler this feature would
    turn any upstream hiccup into a silently lost ticket. The parametrisation covers the
    shapes a real failure takes — a dead upstream, a slow one, and a filter Vikunja stopped
    accepting after an upgrade, which is the one this build's undocumented filters make
    plausible.
    """
    monkeypatch.setattr(dc, "request", AsyncMock(side_effect=boom))
    out = await _fn(server.task_create)(project_id=7, title="Containerize searxng-mcp again")
    assert out["id"] == 999
    assert "possible_duplicates" not in out


async def test_a_failing_attach_still_returns_the_task(wired, monkeypatch):
    """The after-hook runs *after* the upstream write. Raising there would report failure
    for a task that exists — and the agent's natural response is to file it again."""
    monkeypatch.setattr(dc, "request", AsyncMock(return_value=[_task(1, "Containerize x")]))
    monkeypatch.setattr(dc, "_pending", _Exploding())
    out = await _fn(server.task_create)(project_id=7, title="Containerize searxng-mcp again")
    assert out["id"] == 999


class _Exploding:
    def get(self, *a):
        raise RuntimeError("contextvar is broken")

    def set(self, *a):
        raise RuntimeError("contextvar is broken")


async def test_a_failed_create_does_not_leak_duplicates_into_the_next_one(wired, monkeypatch):
    """The stash is per-context, and a create that raises upstream never reaches the
    after-hook that clears it. The before-hook clears first for that reason."""
    monkeypatch.setattr(dc, "request", AsyncMock(return_value=[_task(332, "Containerize x")]))
    monkeypatch.setattr(server, "request", AsyncMock(side_effect=RuntimeError("upstream down")))
    with pytest.raises(RuntimeError):
        await _fn(server.task_create)(project_id=7, title="Containerize searxng-mcp again")

    # Second create: nothing matches this time, so nothing should be attached.
    monkeypatch.setattr(dc, "request", AsyncMock(return_value=[]))
    monkeypatch.setattr(server, "request", AsyncMock(return_value=dict(wired)))
    out = await _fn(server.task_create)(project_id=7, title="A completely different subject line")
    assert "possible_duplicates" not in out


# --- registration ---------------------------------------------------------


def test_registration_is_idempotent():
    dc.register_duplicate_check()
    before = len(hooks.before_handlers("task_create"))
    server._register_duplicate_check_if_enabled()
    assert len(hooks.before_handlers("task_create")) == before


def _is_wired() -> bool:
    return any(
        getattr(h, "is_duplicate_check_hook", False) for h in hooks.before_handlers("task_create")
    )


def test_wired_by_default(monkeypatch):
    """Default-on, unlike the audit log. Justified by measurement — see
    `server._register_duplicate_check_if_enabled` for the 4.0%/470 figure."""
    monkeypatch.delenv("VIKUNJA_DUPLICATE_CHECK", raising=False)
    hooks.clear_hooks()
    server.register_builtin_hooks()
    assert _is_wired()


@pytest.mark.parametrize("off", ["0", "false", "no", ""])
def test_can_be_turned_off(monkeypatch, off):
    monkeypatch.setenv("VIKUNJA_DUPLICATE_CHECK", off)
    hooks.clear_hooks()
    server.register_builtin_hooks()
    assert not _is_wired()


@pytest.mark.parametrize("on", ["1", "true", "yes", "YES"])
def test_explicit_on_values(monkeypatch, on):
    monkeypatch.setenv("VIKUNJA_DUPLICATE_CHECK", on)
    hooks.clear_hooks()
    server.register_builtin_hooks()
    assert _is_wired()
