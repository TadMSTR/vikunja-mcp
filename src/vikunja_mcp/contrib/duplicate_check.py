"""Opt-in hook pair: warn about probable duplicates when a task is created (vikunja#463).

Agents re-file findings they have already filed. The standing correction — "consolidate,
don't re-file; search first" — lives in a CLAUDE.md, which is exactly the kind of rule that
erodes under context pressure. The tracker is the right enforcement point because the
tracker is the thing that knows.

**Reports, never refuses.** The created task is returned exactly as it would have been, with
a `possible_duplicates` list attached. A false positive that blocked a legitimate filing
would lose the finding entirely and leave the agent no recourse, which is strictly worse
than the duplicate it prevented.

**It can never cost a filing.** ``hooks.py`` documents handlers as *not* fire-and-forget: an
exception in a ``before`` handler aborts the chain and the create never happens. So every
entry point here is wrapped — any failure degrades to "no duplicates reported" and the task
is created normally. See :func:`_safely`.

Register it explicitly (``server._register_duplicate_check_if_enabled`` does this when
``VIKUNJA_DUPLICATE_CHECK=1``)::

    from vikunja_mcp.contrib.duplicate_check import register_duplicate_check

    register_duplicate_check()

Two pieces of measured Vikunja behaviour this rests on, both undocumented, both covered by
``tests/test_filter_canary.py``:

1. **``like`` is case-sensitive.** Probed on live v2.3.0 (2026-08-22):
   ``title like "%containerize%"`` matches nothing while ``title like "%Containerize%"``
   matches the ticket whose title begins with that word, and ``%Docker%`` / ``%docker%``
   return *different* sets (6 and 8). Lowercasing extracted terms — the obvious thing to do
   — would therefore miss most duplicates, because titles capitalise. Every term is queried
   in several cases at once; see :func:`_case_variants`.
2. **``||`` works, ``or`` does not.** The word form returns
   ``400 The filter expression ... is invalid``.
"""

from __future__ import annotations

import contextvars
import re
from typing import Any

import structlog

from ..auth import caller_token
from ..client import request
from ..hooks import register_after, register_before

log = structlog.get_logger()

# Carries the before-hook's finding to the after-hook. A ContextVar rather than an
# attribute on anything shared, because one process serves concurrent calls from several
# agents and a module-level global would hand agent A's duplicates to agent B.
#
# It cannot ride along in the tool kwargs either: `instrument` calls `fn(**call_kwargs)`,
# so an extra key would raise TypeError on an unexpected argument and abort the create.
_pending: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "vikunja_possible_duplicates", default=None
)

# Words carrying no signal about *what* a ticket is about. Tracker verbs ("add", "fix") are
# in here with the ordinary English stopwords: nearly every ticket title has one, so
# matching on them finds everything, which is the same as finding nothing.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by can do does for from has have how in into is it its
    make more must need needs no not of off on only or our out over should so some than
    that the their then there these they this to too under up use used uses using via
    was were what when where which while who why will with without would
    add adds added allow allows also always another any back bad better both bug build
    builds change changed changes check checks clean create created creates default did
    doc docs document drop enable enabled ensure error fail fails failure fix fixed fixes
    get gets give handle handled if implement improve issue keep let like log logs long
    look made main may missing move new now old one option options patch put ran run runs
    same see set sets ship show side since still stop support take test tests thing time
    try turn update updated updates want way work works wrong
    """.split()  # noqa: SIM905 — a readable block beats a 150-item literal
)

# Below this length a token is noise, and `%ab%` is a substring of half the corpus. The
# degenerate case matters: a filter of `title like "%%"` matched all 470 tasks live, so an
# empty term would return five arbitrary tickets presented as probable duplicates.
_MIN_TERM_LENGTH = 3

# Terms extracted from the title, most distinctive first.
_MAX_TERMS = 3

# Of those, how many must *all* appear in a candidate's title. Two distinctive terms
# co-occurring is a strong duplicate signal; one is not — "vikunja-mcp" alone matches every
# ticket about this repo, which on forge is dozens.
_REQUIRED_TERMS = 2

# A single term is only accepted as a query when it is at least this long — the length at
# which a token is almost certainly a package or tool name rather than a word.
_LONE_IDENTIFIER_LENGTH = 8

_MAX_RESULTS = 5

# Tokens are matched, never split apart, on this character class. It is deliberately narrow:
# because a term can only ever contain these characters, it cannot carry a quote, a percent
# or a backslash into the filter expression that is built from it. That is a structural
# guarantee rather than an escaping routine, which is the reason there is no escaping
# routine here — see `test_terms_cannot_carry_filter_syntax`.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def extract_terms(title: str, limit: int = _MAX_TERMS) -> list[str]:
    """The most distinctive words in a title, best first.

    Distinctiveness is approximated without corpus statistics: longer tokens carry more
    signal than shorter ones, and a token holding a hyphen, underscore or digit is almost
    always an identifier (``vikunja-mcp``, ``task_create``, ``v0.7.0``) and is weighted
    accordingly. Stopwords and anything shorter than three characters are dropped first.

    Order within a score tie follows the title, so the result is deterministic.
    """
    if not isinstance(title, str):
        return []

    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []
    for position, token in enumerate(_TOKEN.findall(title)):
        if len(token) < _MIN_TERM_LENGTH or token.lower() in _STOPWORDS:
            continue
        if token.lower() in seen:
            continue
        seen.add(token.lower())
        identifier_bonus = 5 if re.search(r"[-_0-9]", token) else 0
        scored.append((len(token) + identifier_bonus, -position, token))

    scored.sort(reverse=True)
    return [token for _, _, token in scored[:limit]]


def _case_variants(term: str) -> list[str]:
    """The casings of ``term`` worth querying, deduplicated and order-stable.

    Necessary because Vikunja's ``like`` is case-sensitive (measured — see the module
    docstring). Three forms cover what actually appears in titles: all-lower for identifiers
    (``searxng-mcp``), capitalised for the ordinary prose case including the first word of
    a title, and all-upper for the acronyms this kind of corpus is full of (``MCP``, ``CI``,
    ``DLQ``, ``HTTP``).
    """
    variants = [term.lower(), term.capitalize(), term.upper()]
    return list(dict.fromkeys(variants))


def build_filter(project_id: int | None, terms: list[str]) -> str:
    """A Vikunja filter matching titles that contain **every** term, in any casing.

    Shaped as an AND over terms, each term an OR over its casings::

        project = 7 && (title like "%foo%" || title like "%Foo%") && (title like "%bar%" ...)

    The AND is what makes this a duplicate signal rather than a topic search: OR-ing the
    terms would match every ticket mentioning any one of them, which on a corpus where 40
    tickets share a `repo:` prefix is close to useless.
    """
    clauses = []
    if project_id is not None:
        clauses.append(f"project = {project_id}")
    for term in terms:
        alternatives = " || ".join(f'title like "%{variant}%"' for variant in _case_variants(term))
        clauses.append(f"({alternatives})")
    return " && ".join(clauses)


def _score(title: str, terms: list[str]) -> int:
    """How many of ``terms`` appear in ``title``, compared case-insensitively.

    Done here rather than upstream because this side can afford a proper case-insensitive
    comparison, and because the count is what orders the results.
    """
    lowered = (title or "").lower()
    return sum(1 for term in terms if term.lower() in lowered)


async def find_possible_duplicates(
    project_id: int | None,
    title: str,
    limit: int = _MAX_RESULTS,
) -> list[dict[str, Any]]:
    """Tickets whose titles look like they already cover ``title``. One upstream call.

    Returns ``[]`` — never raises, never guesses — when the title yields too few distinctive
    terms to say anything. That case is common and deliberately silent: a one-word title
    genuinely carries no duplicate signal, and reporting the whole project as candidates
    would be worse than reporting nothing.

    Done tasks are **included**. A ticket re-filed because the original was closed and
    forgotten is precisely the case this exists to catch, so `done` is reported per result
    and the judgement is left to the reader.
    """
    terms = extract_terms(title)
    # One distinctive term is a topic, not a duplicate: "vikunja-mcp" alone matches every
    # ticket about this repo. The single exception is a lone long identifier, where that one
    # term is genuinely all the title had to offer and is specific enough to mean something.
    lone_identifier = len(terms) == 1 and len(terms[0]) >= _LONE_IDENTIFIER_LENGTH
    if len(terms) < _REQUIRED_TERMS and not lone_identifier:
        return []

    result = await request(
        "GET",
        "/tasks",
        caller_token(),
        params={"filter": build_filter(project_id, terms), "per_page": limit, "page": 1},
    )
    rows = result["items"] if isinstance(result, dict) and "items" in result else result
    if not isinstance(rows, list):
        return []

    from ..server import _task_url

    candidates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        candidates.append(
            {
                "id": row.get("id"),
                "identifier": row.get("identifier"),
                "title": row.get("title"),
                "done": row.get("done"),
                "url": _task_url(row.get("id")),
                "matched_terms": _score(row.get("title", ""), terms),
            }
        )
    candidates.sort(key=lambda c: c["matched_terms"], reverse=True)
    return candidates[:limit]


async def before_task_create(kwargs: dict) -> dict:
    """Look for duplicates before the task exists, and stash the finding.

    Searching *before* the create is what keeps the new task out of its own results without
    needing to filter on an id that does not exist yet.

    ``kwargs`` is returned unmodified — `instrument` passes it straight to ``task_create``,
    so an added key would raise TypeError and abort the create.

    The **entire** body is guarded, not just the upstream call. Hook handlers are not
    fire-and-forget: anything escaping here aborts the chain and the task is never created.
    Losing a filing to a convenience feature is the one outcome this module has to make
    impossible, so the bookkeeping around the search is inside the guard too — that is a
    narrower mistake than it sounds, and the test suite caught exactly it.

    Bare ``Exception`` is deliberate. Enumerating what an upstream search can raise —
    timeouts, 4xx, 5xx, a malformed body, a filter Vikunja stopped accepting after an
    upgrade — is precisely the list that goes stale without anyone noticing.
    """
    try:
        # Cleared first, so a create that raised upstream on a previous call in this same
        # context cannot leave a stale finding for the next task to inherit.
        _pending.set(None)
        found = await find_possible_duplicates(kwargs.get("project_id"), kwargs.get("title", ""))
        _pending.set(found or None)
    except Exception as exc:  # broad by design — see docstring; this must not propagate
        log.warning("vikunja_duplicate_check_failed", stage="search", error=str(exc))
    return kwargs


async def after_task_create(result: Any) -> Any:
    """Attach ``possible_duplicates`` to the created task, if the before-hook found any.

    The key is omitted entirely when nothing was found, rather than set to ``[]``: an empty
    list would read as "checked, and it is definitely novel", which is a stronger claim than
    a lexical title match can support — and it is indistinguishable from the degraded case
    where the search failed.

    Guarded for a different reason than the before-hook. This runs *after* the upstream
    write, so an exception here reports failure for a task that already exists — and an
    agent's natural response to a failed create is to file it again, which manufactures the
    duplicate this module exists to prevent.
    """
    try:
        found = _pending.get()
        _pending.set(None)
        if found and isinstance(result, dict):
            result["possible_duplicates"] = found
    except Exception as exc:  # broad by design — see docstring; this must not propagate
        log.warning("vikunja_duplicate_check_failed", stage="attach", error=str(exc))
    return result


# Tagged so the server can detect an already-registered pair without depending on closure
# identity, matching audit_log.py's `is_audit_log_hook` convention.
before_task_create.is_duplicate_check_hook = True  # type: ignore[attr-defined]
after_task_create.is_duplicate_check_hook = True  # type: ignore[attr-defined]


def register_duplicate_check() -> None:
    """Wire the hook pair onto ``task_create``."""
    register_before("task_create", before_task_create)
    register_after("task_create", after_task_create)
