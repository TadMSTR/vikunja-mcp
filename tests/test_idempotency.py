"""Optional idempotency keys on ``task_create`` (vikunja#465).

A retried agent turn — by the harness, by a transport error after the write landed, or by
a resumed session replaying work — files the same ticket twice. The key lets the caller
say "this create is the same create", and the server answers with the ticket it already
made.

Distinct from vikunja#463's duplicate detection, which catches *semantically* similar
tickets filed deliberately at different times and only ever reports. This catches the
*same* create replayed, and actually suppresses it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import hooks, markers, server

from . import fixtures


@pytest.fixture(autouse=True)
def _builtins():
    hooks.clear_hooks()
    server.register_builtin_hooks()
    yield
    hooks.clear_hooks()


@pytest.fixture
def wire(monkeypatch):
    """Route GET (the lookup) and PUT (the create) to separate canned bodies.

    Returns the mock so a test can assert on *which* calls were made — "did it create?"
    is the question every one of these tests is really asking.
    """

    def _install(lookup_rows, created=None):
        async def _dispatch(method, path, *args, **kwargs):
            if method == "GET":
                return lookup_rows
            return created if created is not None else fixtures.task(id=999)

        mock = AsyncMock(side_effect=_dispatch)
        monkeypatch.setattr(server, "request", mock)
        monkeypatch.setattr(server, "caller_token", lambda: "TOK")
        return mock

    return _install


def _writes(mock):
    return [c for c in mock.await_args_list if c.args[0] == "PUT"]


def _reads(mock):
    return [c for c in mock.await_args_list if c.args[0] == "GET"]


# --- the key is optional and changes nothing when absent -------------------


async def test_create_without_a_key_does_not_look_anything_up(wire):
    mock = wire([])
    await server.task_create(project_id=7, title="t")
    assert len(_writes(mock)) == 1
    assert _reads(mock) == []


async def test_create_without_a_key_writes_no_marker(wire):
    mock = wire([])
    await server.task_create(project_id=7, title="t", description="body")
    body = _writes(mock)[0].kwargs["json"]
    assert "vikunja-mcp:" not in (body.get("description") or "")


# --- miss: create, and record the key -------------------------------------


async def test_a_key_that_matches_nothing_creates_and_writes_the_marker(wire):
    mock = wire([])
    await server.task_create(project_id=7, title="t", description="body", idempotency_key="k1")
    assert len(_writes(mock)) == 1
    stored = _writes(mock)[0].kwargs["json"]["description"]
    assert markers.parse(stored) == {"idem": ["k1"]}


async def test_the_marker_does_not_disturb_the_body(wire):
    mock = wire([])
    await server.task_create(project_id=7, title="t", description="body", idempotency_key="k1")
    stored = _writes(mock)[0].kwargs["json"]["description"]
    assert markers.strip(stored) == "<p>body</p>"


async def test_a_key_on_an_empty_description_still_records(wire):
    """The common filing shape is title-only; the key must survive it."""
    mock = wire([])
    await server.task_create(project_id=7, title="t", idempotency_key="k1")
    stored = _writes(mock)[0].kwargs["json"]["description"]
    assert markers.parse(stored) == {"idem": ["k1"]}


async def test_the_lookup_is_scoped_to_the_target_project(wire):
    """Keys are not global. An unscoped lookup collapses two projects' tickets."""
    mock = wire([])
    await server.task_create(project_id=7, title="t", idempotency_key="k1")
    sent = _reads(mock)[0].kwargs["params"]["filter"]
    assert "project = 7" in sent
    assert 'description like "%idem=k1%"' in sent


# --- hit: return what exists, create nothing ------------------------------


async def test_a_matching_key_returns_the_existing_task_without_creating(wire):
    existing = fixtures.task(id=361, description=markers.append("<p>body</p>", "idem", "k1"))
    mock = wire([existing])
    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert _writes(mock) == []
    assert result["id"] == 361
    assert result["idempotent_hit"] is True


async def test_the_returned_existing_task_carries_no_marker(wire):
    """The strip hook applies to the hit path too, not only to a fresh create."""
    existing = fixtures.task(id=361, description=markers.append("<p>body</p>", "idem", "k1"))
    wire([existing])
    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert "vikunja-mcp:" not in result["description"]


async def test_a_hit_is_found_on_a_web_edited_ticket(wire):
    """The Phase 0 case: a human opened the ticket, so TipTap dropped the newlines.

    If the confirm step re-parses with a written-form-only regex this returns a miss and
    files a second ticket — the exact bug the key exists to prevent, reintroduced by the
    verification step meant to make it safe.
    """
    existing = fixtures.task(id=361, description="<p>body</p><hr><p>vikunja-mcp: idem=k1</p>")
    mock = wire([existing])
    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert _writes(mock) == []
    assert result["idempotent_hit"] is True


# --- the filter is a substring match, so a hit must be confirmed ----------


async def test_a_ticket_merely_quoting_the_key_is_not_a_hit(wire):
    """`description like "%idem=k1%"` also matches a ticket that *documents* a marker.

    A build report pasting a footer is the realistic case. Trusting the filter there
    suppresses a legitimate filing and returns an unrelated ticket as though it were the
    caller's own — silently, since both look like success.
    """
    quoting = fixtures.task(
        id=500,
        description="<p>The footer reads</p>\n<pre><code>vikunja-mcp: idem=k1\n</code></pre>",
    )
    mock = wire([quoting])
    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert len(_writes(mock)) == 1
    assert "idempotent_hit" not in result


async def test_a_partial_key_match_is_not_a_hit(wire):
    """`like` is a substring match, so key `k1` matches a stored `k12`."""
    other = fixtures.task(id=500, description=markers.append("<p>b</p>", "idem", "k12"))
    mock = wire([other])
    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert len(_writes(mock)) == 1
    assert "idempotent_hit" not in result


async def test_the_right_task_is_picked_out_of_a_mixed_result(wire):
    real = fixtures.task(id=361, description=markers.append("<p>b</p>", "idem", "k1"))
    decoy = fixtures.task(id=500, description="<p>mentions idem=k1 in prose</p>")
    mock = wire([decoy, real])
    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert _writes(mock) == []
    assert result["id"] == 361


# --- injection ------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        pytest.param('x" || done = true || "', id="filter-breakout"),
        pytest.param("100%", id="like-wildcard"),
        pytest.param("has space", id="space"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_key_that_could_escape_the_filter_is_refused(wire, key):
    """Refused before the lookup runs — nothing is read and nothing is written."""
    mock = wire([])
    with pytest.raises(ValueError):
        await server.task_create(project_id=7, title="t", idempotency_key=key)
    assert mock.await_args_list == []


# --- degradation ----------------------------------------------------------


async def test_a_failed_lookup_still_creates_but_says_so(monkeypatch):
    """Losing the filing is worse than failing to dedupe it — but silence is worse still.

    duplicate_check's rule is that a convenience feature must never cost a filing, and it
    applies here too. What does *not* apply is its silence: that hook is advisory, whereas
    a caller passing a key asked for a guarantee. Creating anyway while reporting the
    guarantee held would be the "counted one scope, claimed another" failure the filter
    grouping in `_compose` exists to prevent.
    """

    async def _dispatch(method, path, *args, **kwargs):
        if method == "GET":
            raise RuntimeError("upstream is down")
        return fixtures.task(id=999)

    mock = AsyncMock(side_effect=_dispatch)
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert len(_writes(mock)) == 1
    assert result["idempotency_degraded"] is True
    assert "idempotent_hit" not in result


async def test_the_marker_is_still_written_when_the_lookup_degrades(monkeypatch):
    """A key recorded on a degraded create is what lets the *next* retry succeed."""

    async def _dispatch(method, path, *args, **kwargs):
        if method == "GET":
            raise RuntimeError("upstream is down")
        return fixtures.task(id=999)

    mock = AsyncMock(side_effect=_dispatch)
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")

    await server.task_create(project_id=7, title="t", idempotency_key="k1")
    stored = _writes(mock)[0].kwargs["json"]["description"]
    assert markers.parse(stored) == {"idem": ["k1"]}


async def test_a_clean_create_does_not_claim_degradation(wire):
    """Control: the flag must be absent on the happy path, not merely false."""
    wire([])
    result = await server.task_create(project_id=7, title="t", idempotency_key="k1")
    assert "idempotency_degraded" not in result
