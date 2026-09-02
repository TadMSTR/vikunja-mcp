"""Upgrade canary: every undocumented Vikunja filter behaviour this server relies on.

**Why this file exists.** v0.5.0, v0.6.0 and v0.7.0 each took a dependency on filter
behaviour that Vikunja does not document, and in places documents wrongly — `index`
filtering works and appears nowhere in the docs while `position` 500s; `like` is
case-sensitive; `not like` is rejected outright while `not in` is fine. Forge runs Vikunja
from a floating `:latest` tag. A `compose pull` can therefore move the API underneath
`task_get`'s `#N` resolution, `backlog_summary`'s counts and duplicate detection with **no
test failure at all** — the symptom would be a tool quietly returning wrong or empty
results, which is the vikunja#331 incident class.

Every assertion below is a behaviour some shipped code path would be wrong without. If one
fails after a Vikunja upgrade, that is the point: fix the code or the assumption before the
upgrade reaches production, rather than discovering it from a wrong answer weeks later.

**Negative controls are not optional here.** A filter silently becoming a no-op is the
dangerous direction and it looks identical to success — a query that returns everything
looks like a query that matched everything. So each positive assertion is paired with a
nonsense value that must return *empty*, and with `bogusfield = 1` which must still 400.

**Requires a live Vikunja and skips without one.** It builds its own project, tasks and
labels, asserts against them, and deletes everything in a finally. It never reads the
ambient corpus, so it is safe to point at any instance and asserts nothing about forge::

    VIKUNJA_CANARY_URL=https://vikunja.example \\
    VIKUNJA_CANARY_TOKEN=tk_... \\
    pytest tests/test_filter_canary.py -v
"""

from __future__ import annotations

import os

import httpx
import pytest

_URL = os.environ.get("VIKUNJA_CANARY_URL", "").rstrip("/")
_TOKEN = os.environ.get("VIKUNJA_CANARY_TOKEN", "")

pytestmark = [
    pytest.mark.canary,
    pytest.mark.skipif(
        not (_URL and _TOKEN),
        reason="live canary: set VIKUNJA_CANARY_URL and VIKUNJA_CANARY_TOKEN to run",
    ),
]

# Deliberately odd strings. They must not collide with anything in a real tracker, because
# a collision would turn a control ("this matches nothing") into a false pass.
_MARK = "zqcanary"
_BODY_MARK = "zqcanarybody"
_NONSENSE = "zzzznotarealstringanywhere"

# Seven tasks: a prime, so `per_page` divisors cannot accidentally make a ceiling look like
# an exact count. Titles vary in case on purpose — that is what pins case-sensitivity.
_TITLES = [
    f"Alpha {_MARK} widget",
    f"alpha {_MARK} lowercase",
    f"BETA {_MARK} SHOUTED",
    f"Beta {_MARK} gadget",
    f"Gamma {_MARK} gizmo",
    f"Delta {_MARK} doohickey",
    f"Epsilon {_MARK} thing",
]


class Api:
    def __init__(self, client: httpx.Client) -> None:
        self._c = client

    def get(self, path: str, **params):
        return self._c.get(path, params=params)

    def count(self, filter_expr: str) -> int:
        """Match count from the list envelope's `total`, as `_count_matching` reads it."""
        resp = self.get("/tasks", filter=filter_expr, per_page=1)
        resp.raise_for_status()
        return int(resp.json()["total"])

    def rows(self, path: str, **params) -> list:
        """The rows of a list response. v2 always wraps them; v1 returned a bare array."""
        resp = self.get(path, **params)
        resp.raise_for_status()
        return resp.json()["items"] or []

    def status(self, filter_expr: str) -> int:
        return self.get("/tasks", filter=filter_expr, per_page=1).status_code


@pytest.fixture(scope="module")
def canary():
    """A disposable project holding known tasks and a label. Torn down unconditionally."""
    client = httpx.Client(
        base_url=f"{_URL}/api/v2",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        timeout=30.0,
    )
    project_id = None
    label_id = None
    try:
        project = client.post("/projects", json={"title": f"vikunja-mcp {_MARK} canary"})
        project.raise_for_status()
        project_id = project.json()["id"]

        label = client.post("/labels", json={"title": f"{_MARK}-label"})
        label.raise_for_status()
        label_id = label.json()["id"]

        tasks = []
        for position, title in enumerate(_TITLES):
            body = {"title": title}
            # Exactly one task carries the description marker, so `description like` has an
            # unambiguous expected count of 1.
            if position == 0:
                body["description"] = f"<p>{_BODY_MARK}</p>"
            created = client.post(f"/projects/{project_id}/tasks", json=body)
            created.raise_for_status()
            tasks.append(created.json())

        # Exactly one task carries the label, so `labels in` is 1 and `not in` is the rest.
        client.post(f"/tasks/{tasks[0]['id']}/labels", json={"label_id": label_id})

        yield (
            Api(client),
            {
                "project_id": project_id,
                "label_id": label_id,
                "tasks": tasks,
                "scope": f"project = {project_id}",
            },
        )
    finally:
        if project_id is not None:
            client.delete(f"/projects/{project_id}")  # cascades to its tasks
        if label_id is not None:
            client.delete(f"/labels/{label_id}")
        client.close()


# --- the counting trick backlog_summary is built on ----------------------


def test_envelope_total_is_the_exact_match_count(canary):
    """`backlog_summary` reports every bucket as this number. If it becomes a ceiling or an
    estimate, every count this server produces is silently wrong."""
    api, fx = canary
    assert api.count(fx["scope"]) == len(_TITLES)


def test_total_counts_rows_not_pages(canary):
    """The distinction v1 could not make, and the one the port depends on.

    Seven items at `per_page=7` is one page. A `total` that reported pages would be 1 here
    and would be indistinguishable from the row count at `per_page=1`, which is exactly how
    v1's inference worked — so this is the case that proves the number changed meaning.
    """
    api, fx = canary
    body = api.get("/tasks", filter=fx["scope"], per_page=len(_TITLES)).json()
    assert body["total"] == len(_TITLES)
    assert body["total_pages"] == 1
    assert len(body["items"]) == len(_TITLES)


def test_a_page_past_the_end_still_reports_the_total(canary):
    """**What makes `backlog_summary` cheap.** `_count_matching` asks for `_COUNT_PAGE`.

    A page beyond the result set must come back empty *and* still carry the real `total`.
    That is what turns each of the 37 bucket queries from a ~4 KB task row into a ~150
    byte envelope. If Vikunja ever clamped the page to the last one, this goes red and the
    cost — not the correctness — is what changed: `total` is read either way.
    """
    api, fx = canary
    body = api.get("/tasks", filter=fx["scope"], per_page=1, page=1_000_000).json()
    assert body["total"] == len(_TITLES)
    assert not body["items"]


def test_zero_matches_reports_zero(canary):
    """The 0 regime: `client.request` returns a bare `[]` here, never the envelope, so a
    counter reading only `pagination` would report 0 for every single-item bucket too."""
    api, _ = canary
    body = api.get("/tasks", filter=f'title like "%{_NONSENSE}%"', per_page=1).json()
    assert body["total"] == 0
    assert not body["items"]


def test_exactly_one_match_reports_one(canary):
    api, fx = canary
    assert api.count(f'{fx["scope"]} && description like "%{_BODY_MARK}%"') == 1


# --- title matching, and its case-sensitivity ----------------------------


def test_title_like_matches_titles(canary):
    api, fx = canary
    assert api.count(f'{fx["scope"]} && title like "%{_MARK}%"') == len(_TITLES)


def test_title_like_is_scoped_to_titles_not_descriptions(canary):
    """Duplicate detection uses `title like` precisely because it does not reach bodies."""
    api, fx = canary
    assert api.count(f'{fx["scope"]} && title like "%{_BODY_MARK}%"') == 0


def test_case_variant_query_finds_a_title_in_any_casing(canary):
    """**The property `contrib/duplicate_check.py` actually depends on.**

    It queries every term as ``lower || Capital || UPPER`` because `like` case-sensitivity
    turns out to be a property of Vikunja's **database backend**, not of Vikunja: measured
    2026-08-22 on two v2.3.0 instances, `like` is case-*sensitive* on Postgres and
    case-*insensitive* on SQLite. A portable server cannot assume either, so it asserts the
    invariant that holds under both — the variant query finds the title whatever its case.

    This is the assertion that would go red if the workaround stopped working. The regime
    itself is pinned separately below, where a *change* is informative but not a breakage.
    """
    api, fx = canary
    for probe in ("alpha", "beta", "gamma"):
        variants = " || ".join(
            f'title like "%{v}%"' for v in dict.fromkeys([probe, probe.capitalize(), probe.upper()])
        )
        assert api.count(f"{fx['scope']} && ({variants})") >= 1, probe


def test_like_case_regime_is_one_of_the_two_known_ones(canary):
    """Records which regime this instance is in, and fails on a third.

    One match means case-sensitive (Postgres): only ``Alpha zqcanary widget`` matches
    ``%Alpha%``. Two means case-insensitive (SQLite/MySQL): ``alpha zqcanary lowercase``
    matches too. Anything else means the semantics changed in a way neither the code nor
    this file anticipates, which is worth stopping for.
    """
    api, fx = canary
    matches = api.count(f'{fx["scope"]} && title like "%Alpha%"')
    assert matches in (1, 2), (
        f'`title like "%Alpha%"` matched {matches} of two candidate titles differing only '
        "in case; expected 1 (case-sensitive, Postgres) or 2 (case-insensitive, SQLite)."
    )


def test_title_like_control_matches_nothing(canary):
    """The control. Without it, "the filter works" is indistinguishable from "the filter is
    ignored and everything matched"."""
    api, fx = canary
    assert api.count(f'{fx["scope"]} && title like "%{_NONSENSE}%"') == 0


# --- description matching -------------------------------------------------


def test_description_like_matches_bodies(canary):
    api, fx = canary
    assert api.count(f'{fx["scope"]} && description like "%{_BODY_MARK}%"') == 1


def test_description_like_control_matches_nothing(canary):
    api, fx = canary
    assert api.count(f'{fx["scope"]} && description like "%{_NONSENSE}%"') == 0


# --- negation: `not in` works, `not like` does not -----------------------


def test_labels_in_matches(canary):
    api, fx = canary
    assert api.count(f"{fx['scope']} && labels in {fx['label_id']}") == 1


def test_labels_not_in_is_supported(canary):
    """`labels not in` is the robust form of the anchor exclusion documented in
    `VIKUNJA_SUMMARY_EXCLUDE_IDS` — pinned so the option stays available."""
    api, fx = canary
    assert api.count(f"{fx['scope']} && labels not in {fx['label_id']}") == len(_TITLES) - 1


def test_not_like_is_still_rejected(canary):
    """Pins the *absence* of a feature. `title not like` is a 400 (`expected a sign
    operator, got "not"`), which is why exclusion is by id and not by title prefix. If this
    ever starts working, the simpler exclusion becomes available."""
    api, fx = canary
    assert api.status(f'{fx["scope"]} && title not like "%{_MARK}%"') == 400


def test_id_not_equal_excludes_exactly_one(canary):
    """The form `backlog_summary` uses for VIKUNJA_SUMMARY_EXCLUDE_IDS."""
    api, fx = canary
    excluded = fx["tasks"][0]["id"]
    assert api.count(f"{fx['scope']} && id != {excluded}") == len(_TITLES) - 1


# --- boolean composition --------------------------------------------------


def test_double_pipe_is_or(canary):
    """Uses the trailing nouns, not the greek prefixes: `widget`/`gizmo` are unique across
    the fixture regardless of case, so this counts the same under either case regime."""
    api, fx = canary
    both = f'{fx["scope"]} && (title like "%widget%" || title like "%gizmo%")'
    assert api.count(both) == 2


def test_the_word_or_is_rejected(canary):
    """`||` is the only OR spelling. The word form is a 400, so a "readability" rewrite
    would take duplicate detection out entirely — caught here rather than in production."""
    api, fx = canary
    assert api.status(f'{fx["scope"]} && (title like "%widget%" or title like "%gizmo%")') == 400


def test_or_control_matches_nothing(canary):
    api, fx = canary
    control = f'{fx["scope"]} && (title like "%{_NONSENSE}%" || title like "%{_NONSENSE}2%")'
    assert api.count(control) == 0


def test_parentheses_contain_an_or(canary):
    """**The grouping `server._compose` depends on as a security control.**

    Without it a caller-supplied `||` inside `backlog_summary(filter=...)` escapes the
    tool's own scoping predicates. Pinned here because the fix is only a fix for as long as
    Vikunja keeps honouring the parentheses — if grouping ever stopped being respected, the
    scope escape would come back silently and every other test would still pass.
    """
    api, fx = canary
    scope = fx["scope"]
    # Ungrouped: the `|| widget` escapes the impossible first conjunct and matches anyway.
    escaped = api.count(f'{scope} && title like "%{_NONSENSE}%" || title like "%widget%"')
    # Grouped: the impossible conjunct holds, so nothing matches.
    contained = api.count(
        f'{scope} && (title like "%{_NONSENSE}%" || title like "%widget%") '
        f'&& title like "%{_NONSENSE}%"'
    )
    assert escaped >= 1, "expected the ungrouped form to escape its scope"
    assert contained == 0, "parenthesised grouping is no longer being honoured"


def test_evaluation_is_left_to_right_not_and_precedence(canary):
    """Records *which* rule Vikunja uses, because the two differ in what they leak.

    `a || b && c` is 0 under strict left-to-right (`(a || b) && c`) and non-zero under the
    AND-binds-tighter precedence most languages use (`a || (b && c)`). Measured 2026-08-22:
    left-to-right. The practical consequence is that a *trailing* predicate — the `id != N`
    exclusions — constrains the whole accumulated expression and was never escapable, while
    predicates composed *before* the caller's were. `_compose` groups everything regardless,
    so a change here is informative rather than a breakage; it is worth knowing about.
    """
    api, fx = canary
    scope = fx["scope"]
    result = api.count(f'{scope} && title like "%widget%" || title like "%gizmo%" && id = 0')
    assert result == 0, (
        f"expected 0 under left-to-right evaluation, got {result}. Vikunja may have moved "
        "to AND-binds-tighter precedence — re-check server._compose's reasoning."
    )


# --- project scoping, date comparison, index -----------------------------


def test_project_predicate_actually_scopes(canary):
    """Needs its own control: a summary of one project that silently counted every project
    would look perfectly healthy."""
    api, fx = canary
    assert api.count(fx["scope"]) == len(_TITLES)
    assert api.count("project = 0") == 0


def test_updated_accepts_a_date_comparison(canary):
    """`backlog_summary`'s staleness buckets are a date comparison on `updated`."""
    api, fx = canary
    scope = fx["scope"]
    assert api.count(f'{scope} && updated > "1990-01-01"') == len(_TITLES)
    assert api.count(f'{scope} && updated < "1990-01-01"') == 0


def test_index_is_filterable(canary):
    """Undocumented, and load-bearing: `#N` ticket resolution in `task_get` is this filter.
    Misreading `index` for `id` is vikunja#331, which mutated three unrelated tickets."""
    api, fx = canary
    index = fx["tasks"][0]["index"]
    assert api.count(f"{fx['scope']} && index = {index}") == 1


def test_by_index_route_is_unauthorised_for_a_production_shaped_token(canary):
    """Records *why* the filter above is still in use, rather than v2's documented route.

    `GET /projects/{project}/tasks/by-index/{index}` does the same lookup and is
    documented, but it is collected for API-token permissions as group `projects`,
    permission `tasks_by_index`, which forge's agent tokens do not carry. Measured on
    v2.5.0: 401 for every forge agent token, while every other route the server uses
    returns 200.

    **This assertion used to accept 200 or 401, and that is vikunja#515.** The canary
    authenticated with a login JWT, which carries the user's whole permission set rather
    than a token's, so this route answered 200 in CI and 401 in production — and the test
    was written to tolerate both, because with a JWT there was no other honest option. A
    route unreachable by every real caller therefore read healthy, indefinitely: "Filter
    canary — success" twice in the ten days before this was fixed.

    The workflow now mints a scoped API token deliberately *without*
    `projects.tasks_by_index`, so the credential has production's shape and 401 is the
    only correct answer. Measured on v2.6.0, same instance and same moment: JWT 200,
    scoped token 401, every other route 200 for both.

    A failure here is informative, not noise: it means upstream stopped gating by-index
    behind that permission, and vikunja#514 (switch `_resolve_index` to the route) is
    worth revisiting.
    """
    api, fx = canary
    index = fx["tasks"][0]["index"]
    status = api.get(f"/projects/{fx['project_id']}/tasks/by-index/{index}").status_code
    assert status == 401, (
        f"by-index returned {status}, expected 401. If this is 200, either the canary "
        "token was minted with projects.tasks_by_index (check the workflow — a token "
        "holding it detects nothing, which is vikunja#515) or upstream no longer gates "
        "the route behind it, which unblocks vikunja#514."
    )


# --- the parser is genuinely parsing -------------------------------------


def test_an_unknown_field_is_still_a_400(canary):
    """The broadest control in the file. If Vikunja ever starts ignoring unknown fields
    instead of rejecting them, every filter above could silently degrade to a no-op and
    every other test here would still pass."""
    api, _ = canary
    assert api.status("bogusfield = 1") == 400
