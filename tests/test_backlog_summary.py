"""`backlog_summary` — counts, not rows (vikunja#467, id 486).

The point of this tool is that orienting in a backlog stops costing a pagination sweep, so
most of these tests assert the **number of upstream calls**, not just the output shape. A
version that quietly fetched every task and counted client-side would satisfy an
output-only test perfectly while destroying the only reason the tool exists.

The counting itself reads `total` off v2's list envelope — the size of the whole result
set, reported by the API. On v1 there was no such number and the count was inferred from
`x-pagination-total-pages` at `per_page=1`, where one row per page makes pages and rows
the same thing; that inference is gone, and with it the reason it had to be probed.

Three response regimes still have to be covered, because `client.request` hands back a
different *shape* for each and only the third is obvious: 0 matches (bare `[]`), exactly 1
(bare one-item list), and N (the envelope). Verified against live v2.5.0 on 2026-08-23.
"""

from __future__ import annotations

import pytest

from vikunja_mcp import config, server


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def call(tool, **kwargs):
    return await _fn(tool)(**kwargs)


def _labels(*names: str) -> list[dict]:
    return [{"id": 30 + i, "title": name} for i, name in enumerate(names)]


class Upstream:
    """Fake upstream that answers a count query with a chosen number of matches.

    Reproduces all three response regimes `client.request` can hand back for a
    `per_page=1` count, keyed off the number of matches the test wants for a given filter.
    """

    def __init__(self, counts: dict[str, int], labels: list[dict] | None = None):
        self.counts = counts
        self.labels = labels if labels is not None else []
        self.calls: list[dict] = []

    async def __call__(
        self, method, path, token, *, params=None, json=None, files=None, unwrap_list=True
    ):
        self.calls.append({"path": path, "params": dict(params or {}), "unwrap_list": unwrap_list})
        if path == "/labels":
            # Paginated honestly, because vikunja#625 *was* a pagination bug: the fetch
            # asked for one hardcoded page of 50 and the truncation flag was computed
            # against what arrived. A fake that ignores `per_page` and returns every
            # label cannot reproduce that, and would have let the fix ship untested.
            per_page = int((params or {}).get("per_page", 50))
            page = int((params or {}).get("page", 1))
            rows = self.labels[(page - 1) * per_page : page * per_page]
            total = len(self.labels)
            return {
                "items": rows,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": max(1, -(-total // per_page)),
            }
        n = self.counts.get((params or {}).get("filter"), 0)
        # The count query asks for a page past the end, so `items` is empty at every count
        # — including the ones that hold matches. That is the shape being reproduced here:
        # a fake that returned a row would let a row-counting regression pass.
        return {"items": [], "total": n, "page": params["page"], "per_page": 1, "total_pages": n}

    @property
    def task_calls(self) -> list[dict]:
        return [c for c in self.calls if c["path"] == "/tasks"]


@pytest.fixture
def upstream(monkeypatch):
    def _install(counts, labels=None):
        fake = Upstream(counts, labels)
        monkeypatch.setattr(server, "request", fake)
        monkeypatch.setattr(server, "caller_token", lambda: "TOK")
        return fake

    return _install


# --- the counting primitive across all three regimes ----------------------


@pytest.mark.parametrize("matches", [0, 1, 2, 49, 206])
async def test_count_is_exact_for_any_number_of_matches(upstream, matches):
    """1 is the case worth naming: it is the bucket size a row-counter reports as 0."""
    fake = upstream({"project = 7": matches})
    assert await server._count_matching("project = 7") == matches
    assert fake.task_calls[0]["params"]["per_page"] == 1


async def test_count_query_asks_for_a_page_past_the_end(upstream):
    """No rows are wanted — only the envelope's `total`, which every page reports."""
    fake = upstream({"done = false": 5})
    await server._count_matching("done = false")
    assert fake.task_calls[0]["params"] == {
        "filter": "done = false",
        "per_page": 1,
        "page": server._COUNT_PAGE,
    }
    assert server._COUNT_PAGE > 1


async def test_count_reads_the_raw_envelope(upstream):
    """`unwrap_list=False` is load-bearing, not a style choice.

    A one-match bucket is a single page, so the reshaped form is the bare row list — and
    the count query deliberately lands on a page holding no rows, so that list is empty.
    Reading through the reshape would report 0 for every bucket holding exactly one task,
    which is the v1 bug class this tool was careful to avoid in the first place.
    """
    fake = upstream({"done = false": 1})
    assert await server._count_matching("done = false") == 1
    assert fake.task_calls[0]["unwrap_list"] is False


async def test_count_reads_total_not_total_pages(monkeypatch):
    """The two are equal at `per_page=1`, so every other test here would pass either way.

    That equality is exactly what made the v1 inference work, and it is why the swap to
    `total` needs one case where the two numbers *disagree* — otherwise the assertion is a
    constant match and a regression back to page-counting would go unnoticed.
    """

    async def fake(method, path, token, *, params=None, json=None, files=None, unwrap_list=True):
        return {"items": [], "total": 206, "page": 1, "per_page": 1, "total_pages": 3}

    monkeypatch.setattr(server, "request", fake)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    assert await server._count_matching("project = 7") == 206


async def test_count_degrades_to_zero_on_an_unusable_total(monkeypatch):
    """A malformed count must not take the whole summary down with it."""

    async def fake(method, path, token, *, params=None, json=None, files=None, unwrap_list=True):
        return {"items": [], "total": "many"}

    monkeypatch.setattr(server, "request", fake)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    assert await server._count_matching("project = 7") == 0


# --- it counts rather than fetching ---------------------------------------


async def test_returns_counts_not_rows(upstream):
    upstream({}, labels=_labels("type:bug"))
    result = await call(server.backlog_summary, project_id=7)
    assert "items" not in result
    assert "tasks" not in result
    assert isinstance(result["total"], int)


async def test_never_fetches_more_than_one_row_per_call(upstream):
    """The whole claim of this tool. A client-side count would fail here, not in the output."""
    fake = upstream({}, labels=_labels("type:bug", "type:chore"))
    await call(server.backlog_summary, project_id=7)
    assert all(c["params"]["per_page"] == 1 for c in fake.task_calls)


async def test_call_count_is_bounded_and_reported(upstream):
    """`calls` is in the response so the cost is visible to whoever pays it.

    It counts *every* upstream request, the label listing included — a number that
    excluded the one call the caller did not ask for would be the flattering one, not the
    true one.
    """
    fake = upstream({}, labels=_labels("a", "b", "c"))
    result = await call(server.backlog_summary, project_id=7)
    assert result["calls"] == len(fake.calls)
    # 1 label list + 1 total + 2 done/not-done + 6 priorities + 3 labels + 2 staleness
    assert result["calls"] == 15


# --- scope is stated, never implied ---------------------------------------


async def test_scope_reports_the_exact_filter_counted(upstream):
    """Each predicate is its own group — see `server._compose`, where that is a security
    control rather than formatting. The reported scope is the expression actually sent."""
    upstream({}, labels=[])
    result = await call(server.backlog_summary, project_id=7)
    assert result["scope"]["filter"] == "(project = 7) && (done = false)"
    assert result["scope"]["project_id"] == 7


async def test_caller_filter_is_composed_into_every_bucket(upstream):
    fake = upstream({}, labels=_labels("type:bug"))
    await call(server.backlog_summary, project_id=7, filter="priority >= 3")
    assert all("priority >= 3" in c["params"]["filter"] for c in fake.task_calls)


# --- scope containment (security audit 2026-08-22, MEDIUM) ----------------


async def test_a_caller_filter_containing_or_cannot_widen_the_scope(upstream):
    """A caller `filter` with a top-level `||` must not escape the scoping predicates.

    Vikunja evaluates filters strictly left to right, so an ungrouped composition of
    `project = 7 && <caller filter>` where the caller supplies `a || b` parses as
    `((project = 7 && a) || b)` — and `b` is scoped by nothing. Reproduced live before
    fixing: `done = false && id = 999999 || done = true` returns 264 *done* tasks.

    The fix is grouping, so this asserts the shape that makes the escape impossible rather
    than trying to re-derive the parser's behaviour from a mock.
    """
    fake = upstream({}, labels=_labels("type:bug"))
    await call(server.backlog_summary, project_id=7, filter="id = 999999 || done = true")

    for c in fake.task_calls:
        composed = c["params"]["filter"]
        # The caller's expression is confined to its own group...
        assert "(id = 999999 || done = true)" in composed
        # ...and the scope predicate is never left bare beside a top-level ||.
        assert "project = 7 && id = 999999" not in composed
        assert "(project = 7)" in composed


async def test_every_predicate_is_grouped(upstream):
    """Grouping is unconditional — a predicate that looks safe today is still wrapped."""
    fake = upstream({}, labels=[])
    await call(server.backlog_summary, project_id=7)
    for c in fake.task_calls:
        composed = c["params"]["filter"]
        if composed:
            # Every `&&` joins two parenthesised groups; no bare predicate at top level.
            for part in composed.split(" && "):
                assert part.startswith("(") and part.endswith(")"), part


async def test_exclusions_stay_applied_alongside_a_crafted_filter(upstream):
    """The `id != N` exclusion must survive a caller filter that tries to reintroduce it."""
    import os

    os.environ["VIKUNJA_SUMMARY_EXCLUDE_IDS"] = "180"
    config.reset_settings()
    try:
        fake = upstream({}, labels=[])
        await call(server.backlog_summary, project_id=7, filter="id = 180 || done = true")
        for c in fake.task_calls:
            assert "(id != 180)" in c["params"]["filter"]
            assert "(id = 180 || done = true)" in c["params"]["filter"]
    finally:
        os.environ.pop("VIKUNJA_SUMMARY_EXCLUDE_IDS", None)
        config.reset_settings()


async def test_unscoped_summary_omits_the_project_predicate(upstream):
    """An unscoped total sends no filter at all, which reaches the wire as no param."""
    fake = upstream({}, labels=[])
    result = await call(server.backlog_summary)
    assert result["scope"]["project_id"] is None
    assert all("project =" not in (c["params"]["filter"] or "") for c in fake.task_calls)
    assert any(c["params"]["filter"] is None for c in fake.task_calls)


async def test_include_done_widens_the_bucket_scope(upstream):
    fake = upstream({}, labels=_labels("type:bug"))
    result = await call(server.backlog_summary, project_id=7, include_done=True)
    assert "done = false" not in result["scope"]["filter"]
    label_calls = [c for c in fake.task_calls if "labels in" in c["params"]["filter"]]
    assert all("done = false" not in c["params"]["filter"] for c in label_calls)


# --- the buckets ----------------------------------------------------------


async def test_done_and_not_done_are_counted_separately(upstream):
    upstream(
        {
            "(project = 7)": 470,
            "(project = 7) && (done = true)": 264,
            "(project = 7) && (done = false)": 206,
        }
    )
    result = await call(server.backlog_summary, project_id=7)
    assert (result["total"], result["done"], result["not_done"]) == (470, 264, 206)


async def test_priority_buckets_cover_the_whole_vikunja_range(upstream):
    fake = upstream({}, labels=[])
    result = await call(server.backlog_summary, project_id=7)
    assert set(result["by_priority"]) == {"0", "1", "2", "3", "4", "5"}
    assert sum("priority =" in c["params"]["filter"] for c in fake.task_calls) == 6


async def test_label_buckets_are_keyed_by_title_and_counted_by_id(upstream):
    fake = upstream(
        {"(project = 7) && (done = false) && (labels in 30)": 49},
        labels=_labels("type:bug", "agent"),
    )
    result = await call(server.backlog_summary, project_id=7)
    assert result["by_label"]["type:bug"] == 49
    assert any("labels in 30" in c["params"]["filter"] for c in fake.task_calls)


async def test_staleness_buckets_use_a_date_comparison_not_a_fetch(upstream):
    """`updated < <cutoff>` is the server-side form. Verified live: it partitions exactly."""
    fake = upstream({}, labels=[])
    result = await call(server.backlog_summary, project_id=7)
    assert set(result["by_staleness"]) == {"stale", "fresh"}
    stale_calls = [c for c in fake.task_calls if "updated <" in c["params"]["filter"]]
    assert len(stale_calls) == 1
    assert result["scope"]["stale_after_days"] == 90


async def test_staleness_cutoff_follows_the_configured_threshold(upstream, monkeypatch):
    monkeypatch.setenv("VIKUNJA_STALE_AFTER_DAYS", "30")
    config.reset_settings()
    upstream({}, labels=[])
    result = await call(server.backlog_summary, project_id=7)
    assert result["scope"]["stale_after_days"] == 30


# --- the anchor-task exclusion (vikunja#467's off-by-one) -----------------


async def test_excluded_ids_are_applied_to_every_bucket(upstream, monkeypatch):
    """A task carrying every label inflates every label bucket by exactly one."""
    monkeypatch.setenv("VIKUNJA_SUMMARY_EXCLUDE_IDS", "180")
    config.reset_settings()
    fake = upstream({}, labels=_labels("type:bug"))
    result = await call(server.backlog_summary, project_id=7)
    assert all("id != 180" in c["params"]["filter"] for c in fake.task_calls)
    assert result["scope"]["excluded_task_ids"] == [180]


async def test_no_exclusion_configured_means_no_predicate(upstream, monkeypatch):
    """Default is empty — a task id baked into a public repo would be an SC-01 repeat."""
    monkeypatch.delenv("VIKUNJA_SUMMARY_EXCLUDE_IDS", raising=False)
    config.reset_settings()
    fake = upstream({}, labels=[])
    result = await call(server.backlog_summary, project_id=7)
    assert all("id !=" not in c["params"]["filter"] for c in fake.task_calls)
    assert result["scope"]["excluded_task_ids"] == []


async def test_multiple_excluded_ids_are_all_applied(upstream, monkeypatch):
    monkeypatch.setenv("VIKUNJA_SUMMARY_EXCLUDE_IDS", "180, 42")
    config.reset_settings()
    fake = upstream({}, labels=[])
    await call(server.backlog_summary, project_id=7)
    first = fake.task_calls[0]["params"]["filter"]
    assert "id != 180" in first and "id != 42" in first


async def test_malformed_exclude_ids_are_refused_at_startup(monkeypatch):
    monkeypatch.setenv("VIKUNJA_SUMMARY_EXCLUDE_IDS", "180,not-an-id")
    config.reset_settings()
    with pytest.raises(Exception, match="VIKUNJA_SUMMARY_EXCLUDE_IDS"):
        config.get_settings()


# --- bucket cap: truncation is reported, never silent ---------------------


async def test_label_buckets_are_capped(upstream):
    fake = upstream({}, labels=_labels(*[f"label-{i}" for i in range(40)]))
    result = await call(server.backlog_summary, project_id=7, max_label_buckets=5)
    assert len(result["by_label"]) == 5
    assert sum("labels in" in c["params"]["filter"] for c in fake.task_calls) == 5


async def test_truncated_label_buckets_say_so(upstream):
    """Silent truncation reads as 'that is the whole picture'. It is not."""
    upstream({}, labels=_labels(*[f"label-{i}" for i in range(40)]))
    result = await call(server.backlog_summary, project_id=7, max_label_buckets=5)
    assert result["labels_truncated"] is True
    note = " ".join(result["notes"])
    assert "35" in note  # the number dropped, not just the fact of dropping


async def test_untruncated_label_buckets_report_false(upstream):
    upstream({}, labels=_labels("a", "b"))
    result = await call(server.backlog_summary, project_id=7)
    assert result["labels_truncated"] is False
    assert result["notes"] == []


# --- upstream shapes that must not break it -------------------------------


async def test_paginated_label_list_is_followed_to_the_end(upstream, monkeypatch):
    """A label list spanning pages is summarised in full, not from page one (vikunja#625).

    This test previously asserted the opposite — that reading the first page of a
    multi-page label list and stopping was correct behaviour. It was the bug, written
    down as an invariant: the summary reported `labels_truncated: false` over a label
    set it had only partly seen.
    """
    monkeypatch.setattr(server, "_LABEL_PAGE_SIZE", 2)
    upstream({}, labels=_labels("type:bug", "type:chore", "type:docs", "agent-filed"))
    result = await call(server.backlog_summary, project_id=7)
    assert set(result["by_label"]) == {"type:bug", "type:chore", "type:docs", "agent-filed"}
    assert result["labels_truncated"] is False
    assert result["notes"] == []


async def test_no_labels_at_all_still_returns_a_summary(upstream):
    upstream({}, labels=[])
    result = await call(server.backlog_summary, project_id=7)
    assert result["by_label"] == {}
    assert isinstance(result["total"], int)


# --- vikunja#625: the truncation flag counts labels that EXIST ------------


async def test_label_fetch_is_not_capped_at_one_page(upstream, monkeypatch):
    """Every label is counted however many pages the listing spans.

    The page size is pinned small on purpose. The original defect was a hardcoded
    `per_page=50`, and raising that number to 100 would make a test written against 51
    labels pass without the fetch ever learning to paginate — a green test measuring a
    constant instead of the behaviour, which is the failure mode this whole build is
    about. Pinning it means the test fails unless the fetch actually follows pages.
    """
    monkeypatch.setattr(server, "_LABEL_PAGE_SIZE", 10)
    upstream({}, labels=_labels(*[f"label-{i}" for i in range(51)]))
    result = await call(server.backlog_summary, project_id=7, max_label_buckets=100)
    assert len(result["by_label"]) == 51
    assert result["labels_truncated"] is False
    assert result["notes"] == []


async def test_truncation_is_measured_against_the_labels_that_exist(upstream, monkeypatch):
    """`dropped` was `len(labels) - len(labels[:cap])` — both sides post-fetch.

    Truncation caused by the fetch itself therefore cancelled out of its own flag. With
    60 labels upstream and a cap of 55, the honest answer is "5 dropped"; the old code
    fetched 50, kept 50, and reported none.
    """
    monkeypatch.setattr(server, "_LABEL_PAGE_SIZE", 10)
    upstream({}, labels=_labels(*[f"label-{i}" for i in range(60)]))
    result = await call(server.backlog_summary, project_id=7, max_label_buckets=55)
    assert result["labels_truncated"] is True
    assert len(result["by_label"]) == 55
    assert "5 of 60" in " ".join(result["notes"])


async def test_fetch_truncation_is_reported_separately_from_cap_truncation(upstream, monkeypatch):
    """A caller who raises the cap and still sees truncation must learn why.

    The two causes need different actions: raising `max_label_buckets` fixes one and
    does nothing at all for the other. One undifferentiated note sends the caller to
    the parameter that cannot help them.
    """
    monkeypatch.setattr(server, "_LABEL_PAGE_SIZE", 10)
    monkeypatch.setattr(server, "_LABEL_PAGE_LIMIT", 1)
    upstream({}, labels=_labels(*[f"label-{i}" for i in range(25)]))
    result = await call(server.backlog_summary, project_id=7, max_label_buckets=100)
    assert result["labels_truncated"] is True
    note = " ".join(result["notes"])
    assert "15 of 25" in note
    assert "max_label_buckets" in note and "will not help" in note
