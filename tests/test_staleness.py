"""Staleness signals on read paths (vikunja#464, id 483).

``updated`` is already in every task payload, but as a raw RFC3339 string an agent has to
diff it against today mentally to answer "is this ticket text still describing reality?".
These tests pin the derived form: an integer age and a boolean past a configured threshold.

What they deliberately also pin is the **unknown** case. Vikunja spells a null timestamp
``0001-01-01T00:00:00Z``, which parses fine and would otherwise report ~740,000 days and
``stale: true`` — a confident answer derived from no information. Every test below that
covers a missing, zero-value or unparsable ``updated`` exists to keep that answer null.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import config, server

from . import fixtures


def _ago(days: float) -> str:
    """An RFC3339 ``updated`` value ``days`` in the past, as Vikunja spells it.

    Offset by an extra hour so a whole-day boundary test cannot flake on the wall clock
    ticking over mid-run: 90 days + 1 hour floors to 90 whether the suite starts at
    23:59 or 00:01.
    """
    return (datetime.now(UTC) - timedelta(days=days, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _patch_calls(monkeypatch):
    mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    return mock


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def call(tool, **kwargs):
    return await _fn(tool)(**kwargs)


def _set_threshold(monkeypatch, days: str) -> None:
    monkeypatch.setenv("VIKUNJA_STALE_AFTER_DAYS", days)
    config.reset_settings()


# --- the derived fields ---------------------------------------------------


async def test_task_get_reports_days_since_update(_patch_calls):
    _patch_calls.return_value = fixtures.task(updated=_ago(12))
    result = await call(server.task_get, task_id=361)
    assert result["days_since_update"] == 12
    assert result["stale"] is False


async def test_task_list_rows_carry_staleness(_patch_calls):
    _patch_calls.return_value = fixtures.task_list(2)
    for row in await call(server.task_list):
        assert "days_since_update" in row
        assert "stale" in row


async def test_task_search_rows_carry_staleness(_patch_calls):
    _patch_calls.return_value = fixtures.task_list(2)
    for row in await call(server.task_search, query="anything"):
        assert "days_since_update" in row
        assert "stale" in row


async def test_updated_is_not_removed(_patch_calls):
    """Additive, not a replacement — something downstream may already parse `updated`."""
    stamp = _ago(5)
    _patch_calls.return_value = fixtures.task(updated=stamp)
    assert (await call(server.task_get, task_id=361))["updated"] == stamp


# --- the threshold boundary -----------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected_days", "expected_stale"),
    [(89, 89, False), (90, 90, True), (91, 91, True)],
)
async def test_threshold_boundary(_patch_calls, age, expected_days, expected_stale):
    """`stale` turns true *at* the threshold, not one day past it.

    Pinned because "stale after 90 days" is ambiguous in English and the field docs commit
    to one reading — a silent flip of this comparison would change what every consumer sees
    on exactly the tasks sitting on the boundary.
    """
    _patch_calls.return_value = fixtures.task(updated=_ago(age))
    result = await call(server.task_get, task_id=361)
    assert result["days_since_update"] == expected_days
    assert result["stale"] is expected_stale


async def test_threshold_is_configurable(_patch_calls, monkeypatch):
    _set_threshold(monkeypatch, "30")
    _patch_calls.return_value = fixtures.task(updated=_ago(45))
    assert (await call(server.task_get, task_id=361))["stale"] is True


async def test_threshold_default_is_90(_patch_calls, monkeypatch):
    monkeypatch.delenv("VIKUNJA_STALE_AFTER_DAYS", raising=False)
    config.reset_settings()
    _patch_calls.return_value = fixtures.task(updated=_ago(80))
    assert (await call(server.task_get, task_id=361))["stale"] is False


async def test_nonpositive_threshold_is_refused_at_startup(monkeypatch):
    """A threshold of 0 would mark every task stale — a wrong answer, delivered quietly.

    Refused at settings load for the same reason the token/transport combination is:
    the failure has no symptom until someone trusts the flag.
    """
    _set_threshold(monkeypatch, "0")
    with pytest.raises(Exception, match="VIKUNJA_STALE_AFTER_DAYS"):
        config.get_settings()


# --- the unknown cases ----------------------------------------------------


async def test_zero_value_updated_reports_unknown(_patch_calls):
    """Vikunja's null timestamp must not become "739,000 days old, definitely stale"."""
    _patch_calls.return_value = fixtures.task(updated="0001-01-01T00:00:00Z")
    result = await call(server.task_get, task_id=361)
    assert result["days_since_update"] is None
    assert result["stale"] is None


async def test_absent_updated_reports_unknown(_patch_calls):
    body = fixtures.task()
    del body["updated"]
    _patch_calls.return_value = body
    result = await call(server.task_get, task_id=361)
    assert result["days_since_update"] is None
    assert result["stale"] is None


@pytest.mark.parametrize("value", ["", None, "not-a-timestamp", 12345])
async def test_unparsable_updated_reports_unknown_without_raising(_patch_calls, value):
    """A read must never fail because a timestamp was strange. Degrade to unknown."""
    _patch_calls.return_value = fixtures.task(updated=value)
    result = await call(server.task_get, task_id=361)
    assert result["days_since_update"] is None
    assert result["stale"] is None


async def test_future_updated_clamps_to_zero(_patch_calls):
    """Clock skew between forge and Vikunja must not produce a negative age."""
    future = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _patch_calls.return_value = fixtures.task(updated=future)
    result = await call(server.task_get, task_id=361)
    assert result["days_since_update"] == 0
    assert result["stale"] is False


# --- verbose parity -------------------------------------------------------


async def test_verbose_task_get_carries_staleness(_patch_calls):
    """`verbose` restores payload, not ambiguity — the same principle as the index strip."""
    _patch_calls.return_value = fixtures.task(updated=_ago(120))
    result = await call(server.task_get, task_id=361, verbose=True)
    assert result["days_since_update"] == 120
    assert result["stale"] is True


async def test_verbose_keeps_the_full_body(_patch_calls):
    """Adding two fields must not turn the verbose path into a projected one."""
    _patch_calls.return_value = fixtures.task(updated=_ago(1))
    result = await call(server.task_get, task_id=361, verbose=True)
    assert "description" in result
    assert "attachment_count" not in result


async def test_verbose_task_list_carries_staleness(_patch_calls):
    _patch_calls.return_value = fixtures.task_list(2)
    for row in await call(server.task_list, verbose=True):
        assert "days_since_update" in row


async def test_verbose_does_not_add_staleness_to_nested_related_tasks(_patch_calls):
    """Scoped to the task being read. A related-task reference is not a task being reported on."""
    _patch_calls.return_value = fixtures.task(updated=_ago(1))
    result = await call(server.task_get, task_id=361, verbose=True)
    nested = result["related_tasks"]["related"][0]
    assert "days_since_update" not in nested


# --- envelope safety ------------------------------------------------------


async def test_pagination_envelope_survives(_patch_calls):
    """The envelope is this server's own metadata — never a task, never projected."""
    _patch_calls.return_value = fixtures.paginated(fixtures.task_list(2))
    result = await call(server.task_list)
    assert result["pagination"]["truncated"] is True
    assert "days_since_update" not in result["pagination"]
    assert all("days_since_update" in row for row in result["items"])


async def test_non_task_body_is_left_alone(_patch_calls):
    """A 204's `{"ok": True}` has no `updated` to reason about and must not gain fields."""
    _patch_calls.return_value = {"ok": True}
    result = await call(server.task_get, task_id=361)
    assert result == {"ok": True}
