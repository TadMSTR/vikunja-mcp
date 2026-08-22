"""vikunja-mcp — FastMCP server exposing the Vikunja REST API as scoped MCP tools.

Tools are grouped by resource (projects, tasks, labels, comments, filters, webhooks,
teams, sharing, buckets/kanban, views, assignees, relations, reminders, attachments,
bulk). Every tool resolves the caller's own Vikunja token per request (see auth.py) and
forwards it upstream, so Vikunja sees the acting agent, not a shared service account.

Endpoint coverage is *derived* from the live Vikunja Swagger spec (/api/v1/docs.json) but
*verified* against the live router, because swagger is not trustworthy for verbs on this
API. Vikunja's REST idiom is unusual to begin with: **PUT creates, POST updates**.

Known divergence — do not "correct" it back. Swagger documents ``PUT /labels/{id}`` and no
``POST /labels/{id}``; the router accepts ``DELETE, GET, POST`` and rejects ``PUT``. Taking
swagger at its word is what shipped the v0.2.1 ``label_update`` bug. ``scripts/verify-routes.py``
is the ground truth: it sends an unroutable verb to every implemented path and asserts the
method appears in Echo's ``Allow:`` header. Run it after changing any verb or path.

Every tool is wrapped by :func:`instrument`, which fires the pre/post extension hooks
(see ``hooks.py``) and records telemetry (see ``telemetry.py``) around the call.

Permission integers used by the sharing tools follow Vikunja's ``Right``:
``0`` = read-only, ``1`` = read/write, ``2`` = admin.
"""

from __future__ import annotations

import base64
import binascii
import functools
import inspect
import ipaddress
import os
import re
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

import markdown as _markdown_lib
import nh3
import structlog
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, telemetry
from .auth import caller_token
from .client import request
from .config import get_settings
from .exceptions import ConfigError, VikunjaAPIError
from .hooks import (
    after_handlers,
    before_handlers,
    register_after,
    register_before,
    run_after_hooks,
    run_before_hooks,
)

# ---------------------------------------------------------------------------
# Logging — JSON structlog, on by default (forge MCP convention)
# ---------------------------------------------------------------------------


def _log_stream():
    """Pick the log sink: stderr under stdio, stdout otherwise.

    MCP's stdio transport uses **stdout as the JSON-RPC channel**, so it must carry
    protocol frames and nothing else. Before this change `vikunja_mcp_start` was written
    there — verified by capturing the subprocess's streams separately.

    Scope of the impact, stated precisely because it is easy to overclaim: the fastmcp
    client tolerates the stray line and completes the handshake anyway (measured, by
    reverting this and re-running the end-to-end test). It is fixed because emitting
    non-protocol bytes on the protocol channel is a violation a stricter client is
    entitled to reject, not because a specific client was observed failing.

    Read from the environment rather than ``get_settings()`` because logging is configured
    at import time, before config validation has run — and a log line emitted during
    import would corrupt the stream just as effectively as one emitted later.

    Under any network transport the sink stays stdout, so PM2's existing out/error log
    split on forge is unchanged.
    """
    return sys.stderr if os.environ.get("VIKUNJA_TRANSPORT") == "stdio" else sys.stdout


structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(file=_log_stream()),
)
log = structlog.get_logger()

# Optional telemetry (OTLP spans+metrics, InfluxDB3, NATS) — no-op unless env-configured.
telemetry.init()

mcp = FastMCP("vikunja-mcp")


def instrument(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool coroutine with the pre/post hook chain and telemetry.

    Around every tool call this: runs the registered *before* hooks (which may mutate the
    kwargs), opens a telemetry span + records call/error/latency, runs the tool, then runs
    the registered *after* hooks (which may transform the result). Hook exceptions
    propagate — hooks are not fire-and-forget.

    The wrapped callable keeps ``fn``'s signature (via ``__signature__``) so FastMCP still
    derives the correct tool schema.
    """
    tool_name = fn.__name__
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        call_kwargs = dict(bound.arguments)
        call_kwargs = await run_before_hooks(tool_name, call_kwargs)
        async with telemetry.record_tool_call(tool_name):
            result = await fn(**call_kwargs)
        return await run_after_hooks(tool_name, result)

    wrapper.__signature__ = sig  # type: ignore[attr-defined]
    return wrapper


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register ``fn`` as an instrumented MCP tool. Use as ``@tool`` (no parentheses)."""
    return mcp.tool()(instrument(fn))


def _drop_none(**fields: Any) -> dict[str, Any]:
    """Build a request body from only the fields the caller actually supplied.

    Vikunja's *generic* update endpoints (projects, labels, teams, filters, views,
    buckets) only write the fields present in the posted object, so omitting a field
    leaves it untouched. NOTE: the **task** update endpoint (`POST /tasks/{id}`) is the
    exception — it is a full replace. Task writes must not rely on this helper alone;
    see ``_apply_task_update``.
    """
    return {k: v for k, v in fields.items() if v is not None}


# A line that begins with a ticket reference — `#333 ...` or `- #333 ...` — is parsed by
# markdown as an ATX heading, so a "Related" list renders as a run of <h1>s. Seen live on
# vikunja id 347. Matches an optional list marker and at most three leading spaces: four or
# more would be an indented code block, where inserting a backslash would corrupt the text.
_LEADING_TICKET_REF = re.compile(r"^( {0,3}(?:[-*+]\s+|\d+\.\s+)?)(#\d)")
_FENCE = re.compile(r"^\s*(?:```|~~~)")


def _escape_leading_ticket_refs(text: str) -> str:
    """Escape a `#` that starts a line and is followed by a digit, outside code fences.

    Fixes the reference, rather than requiring every agent to learn to write `` `#333` ``.
    A `#` followed by a space (`# Real Heading`) is a genuine heading and is left alone, as
    is any `#` that is not at the start of a line (`C#`, `see #333`).
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            out.append(_LEADING_TICKET_REF.sub(r"\1\\\2", line))
    return "\n".join(out)


def _md_to_html(text: str | None) -> str | None:
    """Render markdown to the HTML Vikunja's rich-text fields expect.

    Vikunja's task/project description and comment fields are TipTap rich text (HTML),
    not markdown — a raw markdown string stored verbatim renders as literal `##`/`-`
    characters with collapsed whitespace (no <p>/<h2>/<br> tags). Agents author these
    fields in plain markdown, so convert on the way in.

    `tables` is enabled: GFM tables are written constantly in ticket descriptions and
    otherwise render as literal pipe characters. nh3's default allowlist already permits
    every table tag, so the rendered table survives sanitisation byte-identical.

    The conversion is idempotent — re-rendering HTML read back from `task_get` is safe.

    # SECURITY[control]: Python-Markdown passes embedded raw HTML through unmodified (no
    # safe_mode since 3.0) — `<script>`, `onerror=`, etc. would otherwise be stored
    # verbatim and execute in whoever's browser next opens the task in Vikunja's TipTap
    # UI. `nh3.clean()` (allowlist-based, Rust `ammonia` bindings) strips disallowed
    # tags/attributes (script, event handlers, javascript: URLs) while preserving the
    # structural HTML markdown legitimately produces. Audit: 2026-07-19/
    # vikunja-mcp-markdown-html-render-2026-07.
    """
    if not text:
        return text
    html = _markdown_lib.markdown(
        _escape_leading_ticket_refs(text),
        extensions=["fenced_code", "nl2br", "tables"],
    )
    return nh3.clean(html)


# Collections Vikunja returns on a task but stores in their own tables, not as task
# columns. They are not affected by the full-replace behaviour of POST /tasks/{id}, so
# read-merge-write does not need to echo them back — and echoing `related_tasks` is
# actively expensive, because Vikunja inlines the *entire* body of every related task
# (one task_search over a well-linked ticket returned 155k characters). Dropped from the
# merged body before re-posting; verified by probe that relations, labels and assignees
# all survive their omission.
_READ_ONLY_TASK_COLLECTIONS = ("related_tasks", "attachments", "reactions")


async def _apply_task_update(task_id: int, token: str, changes: dict[str, Any]) -> dict:
    """Merge ``changes`` over the current task and POST the full object.

    Vikunja's ``POST /tasks/{id}`` is a **full replace**: any column the body omits is
    reset to its zero value. Unlike the generic project/label/etc. update endpoints, a
    partial POST here silently wipes untouched fields (ticket #173 / task 183). To honor
    the partial-update contract, fetch the task, overlay the caller's changed fields, and
    re-post the whole object so untouched columns survive.
    """
    # SECURITY[accepted]: GET-then-POST TOCTOU window — a concurrent writer's change to
    # this task between our GET and re-POST is silently overwritten by our stale re-post.
    # Accepted given forge's low-concurrency, agent-driven, per-agent-token write pattern;
    # no locking/ETag/version check added. Revisit if forge moves to higher-concurrency
    # multi-agent writes on shared tasks, or Vikunja exposes cheap If-Match support.
    # Audit: 2026-07-19/vikunja-mcp-task-update-full-replace-2026-07.
    if not changes:
        return await request("GET", f"/tasks/{task_id}", token)
    current = await request("GET", f"/tasks/{task_id}", token)
    if not isinstance(current, dict):
        return await request("POST", f"/tasks/{task_id}", token, json=changes)
    for heavy in _READ_ONLY_TASK_COLLECTIONS:
        current.pop(heavy, None)
    current.update(changes)
    return await request("POST", f"/tasks/{task_id}", token, json=current)


# Base64 attachment size ceiling — reject before decoding a huge blob into memory (F-04).
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Hostname suffixes that only ever name internal resources — refuse webhook delivery there.
_INTERNAL_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home", ".corp")


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True if an address is non-routable / internal and unsafe as a webhook target.

    ``is_global`` is the primary test rather than an enumeration of private-ish flags.
    Since CPython 3.12.4, ``100.64.0.0/10`` (CGNAT — also Tailscale's range) reports
    ``is_private=False``, so an is_private-based guard let it through despite it being
    plainly not a public destination. Anything not globally routable is refused.

    The named checks are kept after it: they document intent, and ``is_global`` is True
    for parts of multicast/reserved space that are still not valid webhook targets.
    """
    return (
        not getattr(ip, "is_global", False)
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _host_is_blocked(host: str) -> bool:
    """True if a webhook host is loopback/private/link-local/internal (SSRF guard, F-02)."""
    h = host.strip().rstrip(".").lower().strip("[]")
    if not h or h == "localhost" or h.endswith(_INTERNAL_HOST_SUFFIXES):
        return True
    try:
        return _ip_is_blocked(ipaddress.ip_address(h))
    except ValueError:
        pass  # not an IP literal — it's a hostname; resolve it best-effort below
    try:
        infos = socket.getaddrinfo(h, None)
    except OSError:
        # Unresolvable — refuse. This previously allowed the host, reasoning that Vikunja
        # re-resolves at delivery; that reasoning was void, because forge disables Vikunja's
        # own outgoing-request filter (ALLOWNONROUTABLEIPS=true), so the delivery-time
        # resolution is unguarded. A name that does not resolve now but resolves to an
        # internal address at delivery is the DNS-rebinding case, and this MCP-side check is
        # the only control standing in front of it. Failing closed costs a rejected
        # registration when a legitimate host is momentarily unresolvable, which is the
        # cheaper error for a rare, deliberate operation. (audit 2026-08-04, MEDIUM)
        return True
    for info in infos:
        addr = info[4][0].split("%")[0]  # strip any zone id
        try:
            if _ip_is_blocked(ipaddress.ip_address(addr)):
                return True
        except ValueError:
            continue
    return False


def _validate_webhook_target(url: str) -> None:
    """Reject a webhook target_url that points at an internal address (SSRF guard, F-02).

    The MCP enforces this independently of Vikunja's own outgoing-request filter, which is
    disabled in the forge deployment (`OUTGOINGREQUESTS_ALLOWNONROUTABLEIPS=true`).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise VikunjaAPIError(
            0, f"webhook target_url must be http(s); got scheme {parsed.scheme!r}"
        )
    host = parsed.hostname
    if not host or _host_is_blocked(host):
        raise VikunjaAPIError(
            0,
            f"webhook target_url host {host!r} is loopback/private/link-local/internal "
            "(or resolves there) and is refused (SSRF guard). Note: on forge, split-horizon "
            "DNS resolves *.helmforge.me — including SWAG-fronted vhosts — to the LAN, so "
            "those are blocked too. A valid target must be genuinely external to forge; "
            "see SECURITY.md.",
        )


# ===========================================================================
# Identity
# ===========================================================================


@tool
async def whoami() -> dict:
    """Return the Vikunja user the caller's token authenticates as.

    Useful to confirm the per-agent token is wired correctly through scoped-mcp.
    """
    return await request("GET", "/user", caller_token())


# ===========================================================================
# Projects
# ===========================================================================


@tool
async def project_list(
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    is_archived: bool = False,
) -> Any:
    """List projects the caller can access. `search` filters by title.

    Saved filters appear here as pseudo-projects with negative IDs — Vikunja has no
    separate "list filters" endpoint.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page, "s": search or None, "is_archived": is_archived}
    return await request("GET", "/projects", caller_token(), params=params)


@tool
async def project_get(project_id: int) -> dict:
    """Get a single project by ID."""
    return await request("GET", f"/projects/{project_id}", caller_token())


@tool
async def project_create(
    title: str,
    description: str = "",
    parent_project_id: int | None = None,
    hex_color: str = "",
) -> dict:
    """Create a new project. `title` is required."""
    body = _drop_none(
        title=title,
        description=_md_to_html(description) or None,
        parent_project_id=parent_project_id,
        hex_color=hex_color or None,
    )
    return await request("PUT", "/projects", caller_token(), json=body)


@tool
async def project_update(
    project_id: int,
    title: str | None = None,
    description: str | None = None,
    hex_color: str | None = None,
    is_archived: bool | None = None,
) -> dict:
    """Update a project. Only the fields you pass are changed."""
    body = _drop_none(
        title=title,
        description=_md_to_html(description),
        hex_color=hex_color,
        is_archived=is_archived,
    )
    return await request("POST", f"/projects/{project_id}", caller_token(), json=body)


@tool
async def project_delete(project_id: int) -> dict:
    """Delete a project and all its tasks. Irreversible."""
    return await request("DELETE", f"/projects/{project_id}", caller_token())


# ===========================================================================
# Tasks
# ===========================================================================

# Fields a summary row carries through from the upstream task. Chosen to answer the
# questions a *list* is actually asked — what is this, is it done, how urgent, when is it
# due — without the fields that make the answer expensive.
_SUMMARY_FIELDS = (
    "id",
    "identifier",
    "title",
    "done",
    "project_id",
    "priority",
    "due_date",
    "updated",
)

# Collections reduced to a count rather than returned in full, on every projected path,
# mapped to the key the count is reported under. Spelled out rather than derived from the
# field name so nothing depends on stripping a trailing "s".
_COUNTED_COLLECTIONS = {"attachments": "attachment_count", "reactions": "reaction_count"}


def _task_url(task_id: Any) -> str:
    """The browser URL for a task.

    Always built from ``id``, never from ``index``/``identifier``. Constructing
    ``/tasks/454`` from the ticket number lands on a different, unrelated ticket — that is
    the same off-by-a-drifting-offset confusion as vikunja#331, and handing the agent the
    finished URL is what removes the opportunity to get it wrong.
    """
    return f"{get_settings().url.rstrip('/')}/tasks/{task_id}"


def _count(value: Any) -> int:
    """Length of a collection Vikunja may return as null, a list, or a keyed dict."""
    if value is None:
        return 0
    if isinstance(value, dict):
        return sum(_count(v) for v in value.values())
    if isinstance(value, list):
        return len(value)
    return 0


# Vikunja spells a null timestamp `0001-01-01T00:00:00Z`, which parses perfectly well and
# is ~740,000 days ago. Anything at or before year 1 is the sentinel, not a date.
_ZERO_TIMESTAMP_YEAR = 1


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an upstream RFC3339 timestamp, or return None if it is not one.

    Returns None for absent, empty, non-string, unparsable, and Vikunja's zero-value
    sentinel. Never raises: a read must not fail because a timestamp was strange, and a
    derived convenience field is the last thing that should be able to break a ticket
    fetch.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.year <= _ZERO_TIMESTAMP_YEAR:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _staleness(updated: Any) -> dict[str, Any]:
    """Derive ``days_since_update`` and ``stale`` from a task's ``updated`` timestamp.

    **What this signal actually means, stated plainly because it is easy to overread:**
    ``updated`` moves whenever *anything* on the task changes — adding a label, closing a
    subtask, an assignee change. So ``stale: false`` means "recently touched", **not**
    "verified current": a ticket whose body describes a system that no longer exists reads
    as fresh the moment someone relabels it. The converse is the stronger direction —
    ``stale: true`` does mean nobody has touched it in a while.

    It ships anyway, because the alternative on offer is not a better signal, it is no
    signal: the raw ISO string is already in the payload and nothing reads it. A weak
    signal in the place the decision is made beats a strong one nobody consults.

    The sharpest illustration of the weakness, measured on the corpus this was built for:
    a bulk migration rewrites ``updated`` on every task at once. Forge's tracker was
    imported from Plane on 2026-07-19, so on 2026-08-22 its oldest open ticket read as 33
    days old — including tickets whose text predated the import by months. Nothing was
    fresh; every timestamp was. Set ``VIKUNJA_STALE_AFTER_DAYS`` with that in mind after
    any import, and do not read a corpus-wide ``stale: false`` as a clean bill of health.

    Both fields are ``None`` when the age is unknown — see :func:`_parse_timestamp`.
    ``None`` rather than ``false`` because "not known to be stale" and "known to be fresh"
    are different claims, and only one of them is true here.
    """
    parsed = _parse_timestamp(updated)
    if parsed is None:
        return {"days_since_update": None, "stale": None}
    # Clamped at zero: a task `updated` in the future is clock skew between forge and
    # Vikunja, and a negative age reads as a bug in this server rather than in the clocks.
    days = max((datetime.now(UTC) - parsed).days, 0)
    return {"days_since_update": days, "stale": days >= get_settings().stale_after_days}


def _with_staleness(task: dict[str, Any]) -> dict[str, Any]:
    """The verbose path's projection: the untouched body plus the two derived fields.

    ``verbose`` restores *payload*, not ambiguity — the principle v0.5.0 established when
    the ``index`` strip was applied to the verbose path too. A caller asking for the full
    body is not asking to be handed a raw timestamp to diff by hand.

    Only the top-level task is annotated. A task inlined under ``related_tasks`` is a
    reference to something the caller did not ask about, and dating it invites reading a
    staleness verdict on a ticket that was never fetched.
    """
    return {**task, **_staleness(task.get("updated"))}


def _task_ref(task: dict[str, Any]) -> dict[str, Any]:
    """The smallest honest reference to a task: enough to identify it and fetch it.

    ``identifier`` is included but can be the empty string on a task Vikunja inlines under
    ``related_tasks`` (live-observed) — ``id`` is the field that is always meaningful.
    """
    return {
        "id": task.get("id"),
        "identifier": task.get("identifier"),
        "title": task.get("title"),
        "done": task.get("done"),
    }


def _thin_related(related: Any) -> Any:
    """Reduce ``related_tasks`` from inlined full task bodies to bare references.

    Vikunja inlines the *entire* body of every related task, description included — one
    ``task_search`` over a well-linked ticket returned 155,000 characters. ``related_tasks``
    is a dict keyed by relation kind (``{"related": [...], "subtask": [...]}``), so the
    reduction has to walk the values, not the top level.
    """
    if not isinstance(related, dict):
        return related
    return {
        kind: [_task_ref(t) if isinstance(t, dict) else t for t in (entries or [])]
        for kind, entries in related.items()
    }


def _summarise_task(task: dict[str, Any]) -> dict[str, Any]:
    """A list row: identity, status, urgency, and a link. No body.

    ``description`` is the field that dominates a list response — 132 KB of a measured
    182 KB page of 50 — and it is almost never what a list was asked for. Call ``task_get``
    on the row you care about, or pass ``verbose=True`` to get the old shape back.
    """
    row: dict[str, Any] = {k: task.get(k) for k in _SUMMARY_FIELDS}
    row["url"] = _task_url(task.get("id"))
    row["labels"] = [
        {"id": label.get("id"), "title": label.get("title")}
        for label in (task.get("labels") or [])
        if isinstance(label, dict)
    ]
    row["assignee_count"] = _count(task.get("assignees"))
    for name, count_key in _COUNTED_COLLECTIONS.items():
        row[count_key] = _count(task.get(name))
    row.update(_staleness(task.get("updated")))
    return row


def _compact_task(task: dict[str, Any]) -> dict[str, Any]:
    """A single task, minus the collections that make it expensive.

    The opposite trade to :func:`_summarise_task`: ``description`` is kept, because reading
    one ticket's body is the point of ``task_get``. What goes is the inlined bodies of
    related tasks, and the attachment/reaction payloads — replaced by counts, so their
    existence stays discoverable and a caller knows to go look.

    Unknown fields pass through untouched, so a Vikunja upgrade that adds a column does not
    silently lose it here.
    """
    out = {k: v for k, v in task.items() if k not in _COUNTED_COLLECTIONS}
    out["url"] = _task_url(task.get("id"))
    if "related_tasks" in out:
        out["related_tasks"] = _thin_related(out["related_tasks"])
    for name, count_key in _COUNTED_COLLECTIONS.items():
        out[count_key] = _count(task.get(name))
    out.update(_staleness(task.get("updated")))
    return out


def _project(result: Any, projection: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
    """Apply ``projection`` to every task in a response, whatever shape it arrived in.

    Handles the three bodies ``client.request`` can hand back: a single task dict, a bare
    list (single-page result), and the ``{"items": [...], "pagination": {...}}`` envelope it
    wraps a multi-page list in. ``pagination`` is left strictly alone — projecting a list
    while dropping the metadata that says it is only page 1 would trade one silent-truncation
    bug for another.

    Anything without an ``id`` is passed through unprojected. That covers the ``{"ok": True}``
    body a 204 produces, which would otherwise pick up a ``url`` of ``/tasks/None`` — a
    plausible-looking link to nothing is worse than no link.
    """

    def apply(item: Any) -> Any:
        return projection(item) if isinstance(item, dict) and "id" in item else item

    if isinstance(result, dict) and "items" in result and "pagination" in result:
        result["items"] = [apply(item) for item in result["items"]]
        return result
    if isinstance(result, list):
        return [apply(item) for item in result]
    return apply(result)


@tool
async def task_list(
    page: int = 1,
    per_page: int = 50,
    filter: str = "",
    sort_by: str = "",
    order_by: str = "",
    verbose: bool = False,
) -> Any:
    """List tasks across all projects the caller can access.

    `filter` accepts Vikunja's filter syntax, e.g. `done = false && priority >= 3`.
    `index` is filterable too (`project = 7 && index = 454`) though Vikunja does not
    document it; `sort_by` does **not** accept `index`.

    Returns **summary rows** — id, identifier, title, done, project_id, priority,
    due_date, updated, url, labels, and counts for assignees/attachments/reactions. There
    is no `description`: it is 132 KB of a measured 182 KB page and is rarely what a list
    was asked for. Call `task_get` on the row you want, or pass `verbose=true` for the
    full bodies.

    Each row also carries `days_since_update` and `stale` (see `task_get` for exactly what
    those two do and do not tell you — `stale: false` is **not** "verified current").

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    Do not treat one page as the whole answer.
    """
    params = {
        "page": page,
        "per_page": per_page,
        "filter": filter or None,
        "sort_by": sort_by or None,
        "order_by": order_by or None,
    }
    result = await request("GET", "/tasks", caller_token(), params=params)
    return _project(result, _with_staleness if verbose else _summarise_task)


@tool
async def task_search(query: str, page: int = 1, per_page: int = 50, verbose: bool = False) -> Any:
    """Full-text search tasks by title/description (ParadeDB BM25 index).

    Returns **summary rows** — see `task_list` for the field list (including the staleness
    fields) and the reasoning. Pass `verbose=true` for the full task bodies.

    This searches **descriptions as well as titles**. That is the right default for "find
    anything about X", but it means a hit is not evidence of a duplicate — on a corpus
    where tickets quote each other, most hits are tickets that merely *discuss* the term.
    Use `task_list(filter='title like "%term%"')` when you need title-scoped matching.

    Note this searches with Vikunja's `s` parameter, which cannot be combined with
    `filter`. Use `task_list` when you need a filter.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    A "find every ticket about X" question is not answered by page 1 alone.
    """
    params = {"s": query, "page": page, "per_page": per_page}
    result = await request("GET", "/tasks", caller_token(), params=params)
    return _project(result, _with_staleness if verbose else _summarise_task)


@tool
async def task_get(task_id: int | str, verbose: bool = False) -> dict:
    """Get a single task, including its description, labels and assignees.

    `task_id` accepts the global id (`473`), the ticket number (`"#454"`), or the
    `"#456 (id 475)"` form forge tickets are written in. A **bare** integer or digit
    string is always a global id — `"454"` without the `#` is never read as a ticket
    number, because that guess is what vikunja#331 was.

    `description` is kept — reading one ticket's body is the point. What is dropped is the
    inlined body of every related task (reduced to `{id, identifier, title, done}`) and
    the attachment/reaction payloads (reduced to `attachment_count`/`reaction_count`).
    Pass `verbose=true` for the untouched upstream body plus the staleness fields below;
    note that the convenience `url` field is only added on the projected path.

    **Staleness.** `days_since_update` is the whole-day age of `updated`, and `stale` is
    true once that reaches `VIKUNJA_STALE_AFTER_DAYS` (default 90). Read them honestly:
    `updated` moves on *any* change, including a label edit, so `stale: false` means
    "recently touched", **not** "the text below is still true". The useful direction is
    the other one — `stale: true` means nobody has touched this in months, so treat its
    description as a claim about the past and verify before acting on it. Both fields are
    `null` when `updated` is missing or is Vikunja's `0001-01-01` zero value; `null` means
    unknown, not fresh.
    """
    result = await request("GET", f"/tasks/{task_id}", caller_token())
    return _project(result, _with_staleness if verbose else _compact_task)


@tool
async def task_create(
    project_id: int,
    title: str,
    description: str = "",
    priority: int | None = None,
    due_date: str = "",
) -> dict:
    """Create a task in a project. `title` is required. `due_date` is RFC3339 (or omit)."""
    body = _drop_none(
        title=title,
        description=_md_to_html(description) or None,
        priority=priority,
        due_date=due_date or None,
    )
    return await request("PUT", f"/projects/{project_id}/tasks", caller_token(), json=body)


def _strip_index_in_place(node: Any) -> None:
    """Recursively drop every ``index`` key from a decoded response body.

    Walks dicts and lists so a task nested inside ``related_tasks.*`` is stripped along
    with the top-level one. The pagination envelope's ``pagination`` key is skipped: it is
    this server's own metadata, never a task, and must survive untouched (a caller has to
    be able to tell page 1 from a complete answer).

    The walk is deliberately untyped — it does not try to recognise "a task" — because
    every nested object Vikunja returns on a task (``labels``, ``assignees``,
    ``created_by``, ``attachments``, ``reactions``, ``reminders``) is index-free, and the
    one that is not (``related_tasks``) holds real tasks that need stripping. Erring
    towards stripping costs a field nobody should be reading; erring the other way is
    vikunja#331.
    """
    if isinstance(node, dict):
        node.pop("index", None)
        for key, value in node.items():
            if key != "pagination":
                _strip_index_in_place(value)
    elif isinstance(node, list):
        for item in node:
            _strip_index_in_place(item)


async def _strip_task_index(result: Any) -> Any:
    """Drop the bare ``index`` from every task in a create or read response.

    Vikunja returns three identifiers on a task: ``id`` (the global int every other tool
    takes), ``index`` (a per-project int) and ``identifier`` (the display string
    ``"#N"``). ``index`` is indistinguishable from a task id at a glance, and misreading
    it is what caused vikunja#331 (id 342): an agent passed it to task_label_add and
    silently mutated three unrelated tickets, briefly closing an open security ticket
    among them.

    Until v0.5.0 this was create-only, so ``task_get``/``task_list``/``task_search`` kept
    returning ``"index": 454`` directly beside ``"id": 473`` — the ambiguity was closed on
    the one path agents rarely read and left open on the three they read constantly. It
    now covers the read paths too, at any nesting depth and inside the pagination
    envelope.

    ``identifier`` is deliberately kept. It is a string, so it cannot be passed where an
    int id is expected without an obvious type error, and five forge consumers display it
    (the ``[TRACKER] task #N (id M)`` line in four agents' CLAUDE.md, plus
    research-plan-create). Stripping it would break every agent's ticket-filing output.

    Nothing returns a bare ``index`` any more. Use ``identifier`` for the ticket number,
    and pass it straight back as ``task_id`` — ``_resolve_task_ref`` accepts it.
    """
    _strip_index_in_place(result)
    return result


# ---------------------------------------------------------------------------
# Ticket-reference resolution — accepting "#454" where a task id is taken
# ---------------------------------------------------------------------------

# The Vikunja release the `index` filter below was verified against. Named in the error
# message when a resolve fails upstream, because that filter is *undocumented* (see
# `_resolve_index`) and an upgrade dropping it is the most likely cause.
_VERIFIED_VIKUNJA_VERSION = "v2.3.0"

# The "#456 (id 475)" form that forge tickets, CLAUDE.md files and Matrix messages are
# written in. When the global id is spelled out, take it and skip the API call entirely.
#
# Only consulted on a string that *starts* with a ticket reference, and only when it holds
# exactly one `id N`. A bare `findall` over arbitrary prose would accept "see id 999
# somewhere" and, worse, silently take the first of several — "#456 (id 475) blocks #331
# (id 342)" would resolve to 475 on nothing but position. Every other ambiguous case in this
# module raises and names the candidates; this one must not be the exception.
_EMBEDDED_ID = re.compile(r"\bid\s+(\d+)\b", re.IGNORECASE)

# A string that opens with a ticket reference, whatever follows it.
_STARTS_WITH_TICKET_REF = re.compile(r"^#(\d+)\b")

# A bare ticket reference: "#454", and nothing else.
_TICKET_REF = re.compile(r"^#(\d+)$")

_REF_FORMS = (
    'a global task id (473, or the string "473"), '
    'a ticket reference ("#454"), '
    'or the combined form forge writes ("#456 (id 475)")'
)


async def _resolve_index(index: int, token: str) -> int:
    """Resolve a per-project ticket number to the global task id, server-side.

    One filtered call, no cache. ``index`` is a first-class filterable field — probed live
    with a negative control (``bogusfield = 1`` returns 400, so Vikunja rejects unknown
    filter fields rather than ignoring them, which proves the filter is genuinely applied).

    **This filter is undocumented.** Vikunja's published filter-field list names twelve
    fields and ``index`` is not among them; it works anyway on the version recorded in
    ``_VERIFIED_VIKUNJA_VERSION``, and the server's accepted set matches the docs in neither
    direction (``bucket_id`` works, ``position`` 500s). That is an accepted dependency, but
    it must fail loudly — hence the re-raise below naming the verified version, and the live
    guard test in ``tests/test_task_refs.py``.

    Raises:
        ValueError: no task carries that index, or more than one does.
        VikunjaAPIError: the resolve call itself failed. Never swallowed, and **never**
            fallen back to treating the ticket number as a global id — that fallback is
            vikunja#331 reintroduced as an error path.
    """
    scope = get_settings().default_project_id
    expr = f"index = {index}" if scope is None else f"project = {scope} && index = {index}"
    try:
        result = await request("GET", "/tasks", token, params={"filter": expr})
    except VikunjaAPIError as exc:
        raise VikunjaAPIError(
            exc.status_code,
            f"could not resolve ticket reference #{index}: the upstream filter "
            f"{expr!r} failed ({exc}). Vikunja does not document `index` as a filterable "
            f"field; it was verified working on Vikunja {_VERIFIED_VIKUNJA_VERSION}, so an "
            "upgrade may have removed it. Pass the global task id instead until this is "
            "fixed — do NOT assume the ticket number is the id.",
        ) from exc

    matches = result.get("items", []) if isinstance(result, dict) else result
    matches = [t for t in (matches or []) if isinstance(t, dict)]

    if not matches:
        where = "" if scope is None else f" in project {scope}"
        raise ValueError(
            f"no task with ticket number #{index}{where}. Check the project — ticket "
            "numbers restart per project and are not global task ids."
        )
    if len(matches) > 1:
        candidates = ", ".join(f"id {t.get('id')} (project {t.get('project_id')})" for t in matches)
        raise ValueError(
            f"ticket number #{index} is ambiguous — it matches {len(matches)} tasks: "
            f"{candidates}. Ticket numbers are only unique within a project. Pass the "
            "global task id you meant, or set VIKUNJA_DEFAULT_PROJECT_ID to scope "
            "resolution to one project."
        )
    return int(matches[0]["id"])


async def _resolve_task_ref(ref: int | str, token: str | None = None) -> int:
    """Turn whatever a caller passed as ``task_id`` into a global task id.

    Accepted, in the order they are tried:

    ==========================  ===============================================
    ``473`` / ``"473"``         a global id, returned as-is. No API call.
    ``"#454"``                  a ticket number — one filtered lookup.
    ``"#456 (id 475)"``         the id is spelled out — take it. No API call.
    ==========================  ===============================================

    The third form is only honoured on a string that *opens* with a ticket reference and
    names exactly one ``id N``. Prose that merely mentions an id is refused, and a string
    naming several raises rather than taking the first — position is not evidence.

    A **bare** number is always a global id, never a ticket number. That asymmetry is the
    whole safety property: the ``#`` is what makes a ticket reference recognisable, and
    without it there is no way to tell ``454`` meaning "task 454" from ``454`` meaning
    "ticket #454, which is task 473". Guessing is precisely what vikunja#331 did.

    ``token`` is resolved from the caller's request only when a lookup is actually needed,
    so the no-API-call paths stay usable outside an HTTP request context.

    Raises:
        ValueError: on any other form, naming what is accepted.
    """
    # bool is an int subclass, and `task_id=True` reaching /tasks/1 is never intended.
    if isinstance(ref, bool):
        raise ValueError(f"task_id must be {_REF_FORMS}; got the boolean {ref!r}")
    if isinstance(ref, int):
        return ref
    if not isinstance(ref, str):
        raise ValueError(f"task_id must be {_REF_FORMS}; got {type(ref).__name__} {ref!r}")

    text = ref.strip()

    # `isascii()` matters: `str.isdigit()` is also true for superscripts like "²", where
    # `int()` then raises "invalid literal for int()" — a confusing message for what is
    # simply an unaccepted form. Unicode decimal digits ("٤٧٣") would convert cleanly, but
    # a task id arriving in Arabic-Indic numerals is far more likely to be a mistake than
    # an intention, so the rule is plain ASCII digits and a clear error otherwise.
    if text.isascii() and text.isdigit():
        return int(text)

    ticket = _TICKET_REF.match(text)
    if ticket:
        return await _resolve_index(int(ticket.group(1)), token or caller_token())

    if _STARTS_WITH_TICKET_REF.match(text):
        ids = set(_EMBEDDED_ID.findall(text))
        if len(ids) == 1:
            return int(ids.pop())
        if len(ids) > 1:
            raise ValueError(
                f"task_id {ref!r} names {len(ids)} different task ids "
                f"({', '.join(sorted(ids, key=int))}). Pass the one you mean — picking the "
                "first would be a guess, and guessing which task an ambiguous reference "
                "points at is the whole failure this accepts references to prevent."
            )

    raise ValueError(f"task_id must be {_REF_FORMS}; got {ref!r}")


async def _resolve_task_ref_kwarg(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Before-hook: rewrite ``task_id`` in place to a global id.

    Registered on every tool that takes a ``task_id``, so ``"#454"`` works uniformly rather
    than on whichever tools someone remembered. A tool call with no ``task_id`` passes
    through untouched.
    """
    if "task_id" in kwargs:
        kwargs["task_id"] = await _resolve_task_ref(kwargs["task_id"])
    return kwargs


# Every tool that takes a `task_id`. All of them accept a ticket reference, resolved by
# `_resolve_task_ref_kwarg` before the tool body runs.
#
# Registered from one list rather than decorated tool-by-tool so the set cannot drift: a
# new task_id-taking tool that is not added here silently rejects "#454" while its
# neighbours accept it, and "works on some tools" is worse than "works on none".
#
# Deliberately NOT resolved:
#   - `tasks_bulk_update.task_ids` — a list, mutating N tasks per call, and the tool behind
#     vikunja#333. Widening it deserves its own review (plan Phase 3 step 15).
#   - `task_relation_add`/`task_relation_remove`'s `other_task_id` — still `int`, so a
#     "#452" there is refused at schema validation. Loud and safe, but inconsistent;
#     tracked separately rather than half-widened here.
_TASK_REF_TOOLS = (
    "task_get",
    "task_update",
    "task_delete",
    "task_label_add",
    "task_label_remove",
    "comment_list",
    "comment_create",
    "comment_delete",
    "task_assignee_list",
    "task_assignee_add",
    "task_assignees_add_bulk",
    "task_assignee_remove",
    "task_relation_add",
    "task_relation_remove",
    "task_reminders_set",
    "attachment_list",
    "attachment_upload",
    "attachment_delete",
    "task_bucket_move",
)

# Every tool whose response body is a task, or a list of tasks. All of them get the
# `index` strip.
#
# The plan for v0.5.0 named only the three read tools, on the reasoning that they are what
# agents actually read. But `task_update`, `task_delete`, `task_reminders_set`,
# `tasks_bulk_update` and `task_bucket_move` all return a full task body too, `index` and
# all — so naming only the read paths would have left the same `"index": 454` next to
# `"id": 473` on five other tools, which is the trap this hook exists to remove. The strip
# is a no-op on a body that has no `index`, so over-including a tool costs nothing and
# under-including one is vikunja#331 again.
_INDEX_STRIPPED_TOOLS = (
    "task_create",
    "task_get",
    "task_list",
    "task_search",
    "task_update",
    "task_delete",
    "task_reminders_set",
    "tasks_bulk_update",
    "task_bucket_move",
)

# Mutating tools audited when VIKUNJA_AUDIT_LOG=1 — the set contrib/audit_log.py's own
# docstring names, plus tasks_bulk_update, which mutates N tasks per call and was missing
# from that list (vikunja#342, id 361). Flagged as an omission, not folded in silently.
_AUDITED_TOOLS = (
    "task_create",
    "task_update",
    "tasks_bulk_update",
    "task_delete",
    "project_create",
    "project_delete",
    "team_create",
    "project_team_add",
    "project_user_add",
    "project_share_create",
    "webhook_create",
)


def _register_audit_log_if_enabled() -> None:
    """Env-gated wiring for contrib/audit_log.py (vikunja#342, id 361).

    ``contrib/`` is deliberately not imported by default — see AGENTS.md's module-boundary
    table. This is the one exception: an explicit opt-in via ``VIKUNJA_AUDIT_LOG=1``, chosen
    over a deployment-side entry point so the wiring is visible to this repo's own tests and
    code review rather than living in ``/opt/appdata``.
    """
    if os.environ.get("VIKUNJA_AUDIT_LOG", "").strip().lower() not in ("1", "true", "yes"):
        return
    if any(getattr(h, "is_audit_log_hook", False) for h in before_handlers("task_create")):
        return  # already wired — register_builtin_hooks() can be called more than once

    audit_dir = os.environ.get("VIKUNJA_AUDIT_LOG_DIR", "").strip()
    if not audit_dir:
        raise ConfigError(
            "VIKUNJA_AUDIT_LOG=1 but VIKUNJA_AUDIT_LOG_DIR is unset. Set it to the directory "
            "the audit trail should be written to (one file per day, never stdout), or unset "
            "VIKUNJA_AUDIT_LOG to leave the audit trail off."
        )

    from .contrib.audit_log import FileAuditLogger, register_audit_log

    register_audit_log(_AUDITED_TOOLS, logger=FileAuditLogger(audit_dir))


def register_builtin_hooks() -> None:
    """Register the hooks this server ships with. Idempotent.

    Called at import so the guardrails are on by default in every deployment. Kept as a
    callable (rather than a bare ``register_after`` at module scope) because
    ``hooks.clear_hooks()`` wipes built-ins along with test-registered handlers — a test
    that clears hooks calls this to restore them instead of depending on import order.
    """
    for name in _INDEX_STRIPPED_TOOLS:
        if _strip_task_index not in after_handlers(name):
            register_after(name, _strip_task_index)
    for name in _TASK_REF_TOOLS:
        if _resolve_task_ref_kwarg not in before_handlers(name):
            register_before(name, _resolve_task_ref_kwarg)
    _register_audit_log_if_enabled()


register_builtin_hooks()


@tool
async def task_update(
    task_id: int | str,
    title: str | None = None,
    description: str | None = None,
    done: bool | None = None,
    priority: int | None = None,
    due_date: str | None = None,
    percent_done: float | None = None,
) -> dict:
    """Update a task. Only the fields you pass change. Set `done=true` to complete it.

    Implemented as read-merge-write: Vikunja's task endpoint is a full replace, so we
    fetch the task and overlay your fields before posting (see ``_apply_task_update``).
    """
    changes = _drop_none(
        title=title,
        description=_md_to_html(description),
        done=done,
        priority=priority,
        due_date=due_date,
        percent_done=percent_done,
    )
    return await _apply_task_update(task_id, caller_token(), changes)


@tool
async def task_delete(task_id: int | str) -> dict:
    """Delete a task. Irreversible."""
    return await request("DELETE", f"/tasks/{task_id}", caller_token())


@tool
async def tasks_bulk_update(task_ids: list[int], values: dict) -> dict:
    """Apply the same field changes to many tasks in one call (migration throughput).

    `values` is a partial task object, e.g. `{"done": true}` or `{"priority": 4}`; it is
    applied to every task in `task_ids` as a **targeted field write** — naming a key in
    `values` is what makes that column eligible to be written, and columns you do not name
    are left alone.

    This is the bulk counterpart to ``_apply_task_update``: both exist because Vikunja's
    task writes are full replaces by default. The single-task path solves it with
    read-merge-write; the bulk path solves it with the ``fields`` array below, which does
    the same job server-side without N GETs and N TOCTOU windows.
    """
    # Vikunja's POST /tasks/bulk is a full replace *per task*: without `fields`, every
    # column absent from `values` is reset to its zero value on every task in the list
    # (ticket #333 / task 347 — same root cause as #173, verified destroying description,
    # priority and percent_done on a live probe). `models.BulkTask.fields` restricts the
    # write to the named columns. It is real but undocumented in swagger, so the probe
    # recorded in the #333 ticket is its specification.
    body = {"task_ids": task_ids, "fields": list(values.keys()), "values": values}
    return await request("POST", "/tasks/bulk", caller_token(), json=body)


# ===========================================================================
# Labels
# ===========================================================================


@tool
async def label_list(page: int = 1, per_page: int = 50, search: str = "") -> Any:
    """List labels the caller can access. `search` filters by title.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page, "s": search or None}
    return await request("GET", "/labels", caller_token(), params=params)


@tool
async def label_get(label_id: int) -> dict:
    """Get a single label by ID."""
    return await request("GET", f"/labels/{label_id}", caller_token())


@tool
async def label_create(title: str, description: str = "", hex_color: str = "") -> dict:
    """Create a label. `title` is required."""
    body = _drop_none(title=title, description=description or None, hex_color=hex_color or None)
    return await request("PUT", "/labels", caller_token(), json=body)


@tool
async def label_update(
    label_id: int,
    title: str | None = None,
    description: str | None = None,
    hex_color: str | None = None,
) -> dict:
    """Update a label. Only the fields you pass change."""
    body = _drop_none(title=title, description=description, hex_color=hex_color)
    return await request("POST", f"/labels/{label_id}", caller_token(), json=body)


@tool
async def label_delete(label_id: int) -> dict:
    """Delete a label."""
    return await request("DELETE", f"/labels/{label_id}", caller_token())


@tool
async def task_label_add(task_id: int | str, label_id: int) -> dict:
    """Attach an existing label to a task."""
    return await request(
        "PUT", f"/tasks/{task_id}/labels", caller_token(), json={"label_id": label_id}
    )


@tool
async def task_label_remove(task_id: int | str, label_id: int) -> dict:
    """Detach a label from a task."""
    return await request("DELETE", f"/tasks/{task_id}/labels/{label_id}", caller_token())


# ===========================================================================
# Comments
# ===========================================================================


@tool
async def comment_list(task_id: int | str, page: int = 1, per_page: int = 50) -> Any:
    """List comments on a task.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/tasks/{task_id}/comments", caller_token(), params=params)


@tool
async def comment_create(task_id: int | str, comment: str) -> dict:
    """Add a comment to a task. `comment` may contain markdown or HTML."""
    return await request(
        "PUT",
        f"/tasks/{task_id}/comments",
        caller_token(),
        json={"comment": _md_to_html(comment)},
    )


@tool
async def comment_delete(task_id: int | str, comment_id: int) -> dict:
    """Delete a comment from a task."""
    return await request("DELETE", f"/tasks/{task_id}/comments/{comment_id}", caller_token())


# ===========================================================================
# Assignees
# ===========================================================================


@tool
async def task_assignee_list(task_id: int | str, page: int = 1, per_page: int = 50) -> Any:
    """List the users assigned to a task.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/tasks/{task_id}/assignees", caller_token(), params=params)


@tool
async def task_assignee_add(task_id: int | str, user_id: int) -> dict:
    """Assign a user to a task."""
    return await request(
        "PUT", f"/tasks/{task_id}/assignees", caller_token(), json={"user_id": user_id}
    )


@tool
async def task_assignees_add_bulk(task_id: int | str, user_ids: list[int]) -> dict:
    """Assign several users to a task in one call (carries Plane assignees on migration)."""
    body = {"assignees": [{"id": uid} for uid in user_ids]}
    return await request("POST", f"/tasks/{task_id}/assignees/bulk", caller_token(), json=body)


@tool
async def task_assignee_remove(task_id: int | str, user_id: int) -> dict:
    """Remove a user's assignment from a task."""
    return await request("DELETE", f"/tasks/{task_id}/assignees/{user_id}", caller_token())


# ===========================================================================
# Relations / subtasks
# ===========================================================================

# Vikunja relation kinds: subtask, parenttask, related, duplicateof, duplicates,
# blocking, blocked, precedes, follows, copiedfrom, copiedto.


@tool
async def task_relation_add(task_id: int | str, other_task_id: int, relation_kind: str) -> dict:
    """Relate two tasks. `relation_kind` is e.g. `subtask`, `related`, `blocking`, `precedes`."""
    body = {"other_task_id": other_task_id, "relation_kind": relation_kind}
    return await request("PUT", f"/tasks/{task_id}/relations", caller_token(), json=body)


@tool
async def task_relation_remove(task_id: int | str, relation_kind: str, other_task_id: int) -> dict:
    """Remove a relation between two tasks. `relation_kind` must match the existing relation."""
    # relation_kind is a free-text path segment — percent-encode it so a value like
    # "../.." cannot traverse to a different API path (IV-01).
    return await request(
        "DELETE",
        f"/tasks/{task_id}/relations/{quote(relation_kind, safe='')}/{other_task_id}",
        caller_token(),
    )


# ===========================================================================
# Reminders
# ===========================================================================


@tool
async def task_reminders_set(task_id: int | str, reminders: list[str]) -> dict:
    """Set a task's reminders. `reminders` is a list of RFC3339 timestamps.

    Replaces the task's reminder set (Vikunja stores reminders on the task object). Pass
    an empty list to clear all reminders. Read-merge-write so the task's other fields are
    preserved — the task endpoint is a full replace (see ``_apply_task_update``).
    """
    changes = {"reminders": [{"reminder": r} for r in reminders]}
    return await _apply_task_update(task_id, caller_token(), changes)


# ===========================================================================
# Attachments
# ===========================================================================


@tool
async def attachment_list(task_id: int | str, page: int = 1, per_page: int = 50) -> Any:
    """List attachments on a task.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/tasks/{task_id}/attachments", caller_token(), params=params)


@tool
async def attachment_upload(task_id: int | str, filename: str, content_base64: str) -> dict:
    """Upload a file attachment to a task.

    `content_base64` is the file's bytes, base64-encoded (keeps the transport JSON-safe).
    """
    # Reject before decoding so a huge payload can't be inflated into memory (F-04).
    if len(content_base64) > _MAX_ATTACHMENT_BYTES // 3 * 4 + 4:
        raise VikunjaAPIError(0, "attachment exceeds the 25 MiB size limit")
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VikunjaAPIError(0, f"attachment content is not valid base64: {exc}") from exc
    files = {"files": (filename, raw)}
    return await request("PUT", f"/tasks/{task_id}/attachments", caller_token(), files=files)


@tool
async def attachment_delete(task_id: int | str, attachment_id: int) -> dict:
    """Delete an attachment from a task."""
    return await request("DELETE", f"/tasks/{task_id}/attachments/{attachment_id}", caller_token())


# ===========================================================================
# Saved filters
# ===========================================================================


@tool
async def filter_get(filter_id: int) -> dict:
    """Get a saved filter by ID."""
    return await request("GET", f"/filters/{filter_id}", caller_token())


@tool
async def filter_create(title: str, filter_query: str, description: str = "") -> dict:
    """Create a saved filter.

    `filter_query` uses Vikunja's filter syntax, e.g. `done = false && due_date < now`.
    """
    body = _drop_none(
        title=title,
        description=description or None,
        filters={"filter": filter_query},
    )
    return await request("PUT", "/filters", caller_token(), json=body)


@tool
async def filter_update(
    filter_id: int,
    title: str | None = None,
    filter_query: str | None = None,
    description: str | None = None,
) -> dict:
    """Update a saved filter. Only the fields you pass change."""
    body = _drop_none(
        title=title,
        description=description,
        filters={"filter": filter_query} if filter_query is not None else None,
    )
    return await request("POST", f"/filters/{filter_id}", caller_token(), json=body)


@tool
async def filter_delete(filter_id: int) -> dict:
    """Delete a saved filter."""
    return await request("DELETE", f"/filters/{filter_id}", caller_token())


# ===========================================================================
# Teams
# ===========================================================================


@tool
async def team_list(page: int = 1, per_page: int = 50, search: str = "") -> Any:
    """List teams the caller belongs to. `search` filters by name.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page, "s": search or None}
    return await request("GET", "/teams", caller_token(), params=params)


@tool
async def team_get(team_id: int) -> dict:
    """Get a single team by ID, including its members."""
    return await request("GET", f"/teams/{team_id}", caller_token())


@tool
async def team_create(name: str, description: str = "") -> dict:
    """Create a team. `name` is required."""
    body = _drop_none(name=name, description=description or None)
    return await request("PUT", "/teams", caller_token(), json=body)


@tool
async def team_update(
    team_id: int,
    name: str | None = None,
    description: str | None = None,
    is_public: bool | None = None,
) -> dict:
    """Update a team. Only the fields you pass change."""
    body = _drop_none(name=name, description=description, is_public=is_public)
    return await request("POST", f"/teams/{team_id}", caller_token(), json=body)


@tool
async def team_delete(team_id: int) -> dict:
    """Delete a team. Irreversible."""
    return await request("DELETE", f"/teams/{team_id}", caller_token())


@tool
async def team_member_add(team_id: int, username: str, admin: bool = False) -> dict:
    """Add a user to a team. Set `admin=true` to make them a team admin."""
    body = {"username": username, "admin": admin}
    return await request("PUT", f"/teams/{team_id}/members", caller_token(), json=body)


@tool
async def team_member_remove(team_id: int, username: str) -> dict:
    """Remove a user from a team."""
    # username is a free-text path segment — percent-encode it (IV-01).
    return await request(
        "DELETE", f"/teams/{team_id}/members/{quote(username, safe='')}", caller_token()
    )


@tool
async def team_member_toggle_admin(team_id: int, user_id: int) -> dict:
    """Toggle a team member's admin status."""
    return await request("POST", f"/teams/{team_id}/members/{user_id}/admin", caller_token())


# ===========================================================================
# Project sharing — teams, users, link shares
# ===========================================================================
# Permission integers (Vikunja Right): 0 = read-only, 1 = read/write, 2 = admin.


@tool
async def project_team_list(project_id: int, page: int = 1, per_page: int = 50) -> Any:
    """List teams a project is shared with.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/projects/{project_id}/teams", caller_token(), params=params)


@tool
async def project_team_add(project_id: int, team_id: int, permission: int = 0) -> dict:
    """Share a project with a team. `permission`: 0=read, 1=write, 2=admin."""
    body = {"team_id": team_id, "permission": permission}
    return await request("PUT", f"/projects/{project_id}/teams", caller_token(), json=body)


@tool
async def project_team_update(project_id: int, team_id: int, permission: int) -> dict:
    """Change a team's permission on a shared project. `permission`: 0=read, 1=write, 2=admin."""
    return await request(
        "POST",
        f"/projects/{project_id}/teams/{team_id}",
        caller_token(),
        json={"permission": permission},
    )


@tool
async def project_team_remove(project_id: int, team_id: int) -> dict:
    """Stop sharing a project with a team."""
    return await request("DELETE", f"/projects/{project_id}/teams/{team_id}", caller_token())


@tool
async def project_user_list(project_id: int, page: int = 1, per_page: int = 50) -> Any:
    """List users a project is shared with directly.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/projects/{project_id}/users", caller_token(), params=params)


@tool
async def project_user_add(project_id: int, username: str, permission: int = 0) -> dict:
    """Share a project with a user. `permission`: 0=read, 1=write, 2=admin."""
    body = {"username": username, "permission": permission}
    return await request("PUT", f"/projects/{project_id}/users", caller_token(), json=body)


@tool
async def project_user_update(project_id: int, user_id: int, permission: int) -> dict:
    """Change a user's permission on a shared project. `permission`: 0=read, 1=write, 2=admin."""
    return await request(
        "POST",
        f"/projects/{project_id}/users/{user_id}",
        caller_token(),
        json={"permission": permission},
    )


@tool
async def project_user_remove(project_id: int, user_id: int) -> dict:
    """Stop sharing a project with a user."""
    return await request("DELETE", f"/projects/{project_id}/users/{user_id}", caller_token())


@tool
async def project_share_list(project_id: int, page: int = 1, per_page: int = 50) -> Any:
    """List link shares configured on a project.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/projects/{project_id}/shares", caller_token(), params=params)


@tool
async def project_share_get(project_id: int, share_id: int) -> dict:
    """Get a single link share for a project."""
    return await request("GET", f"/projects/{project_id}/shares/{share_id}", caller_token())


@tool
async def project_share_create(
    project_id: int,
    permission: int = 0,
    password: str = "",
    name: str = "",
    sharing_type: int = 0,
) -> dict:
    """Create a link share for a project.

    `permission`: 0=read, 1=write, 2=admin. `sharing_type`: 0=without-password,
    1=with-password, 2=authenticated. Set `password` when `sharing_type=1`.
    """
    # Couple password and sharing_type so a share can't end up less protected than intended
    # (F-05): with-password requires a password; a password with any other type is a mistake.
    if sharing_type == 1 and not password:
        raise VikunjaAPIError(0, "sharing_type=1 (with-password) requires a non-empty password")
    if password and sharing_type != 1:
        raise VikunjaAPIError(
            0, "a password is only meaningful with sharing_type=1 (with-password)"
        )
    body = _drop_none(
        permission=permission,
        password=password or None,
        name=name or None,
        sharing_type=sharing_type,
    )
    return await request("PUT", f"/projects/{project_id}/shares", caller_token(), json=body)


@tool
async def project_share_delete(project_id: int, share_id: int) -> dict:
    """Remove a link share from a project."""
    return await request("DELETE", f"/projects/{project_id}/shares/{share_id}", caller_token())


# ===========================================================================
# Views (list / gantt / table / kanban)
# ===========================================================================


@tool
async def view_list(project_id: int, page: int = 1, per_page: int = 50) -> Any:
    """List the views configured on a project (the 4 auto-created ones by default).

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/projects/{project_id}/views", caller_token(), params=params)


@tool
async def view_get(project_id: int, view_id: int) -> dict:
    """Get a single project view, including its bucket/filter configuration."""
    return await request("GET", f"/projects/{project_id}/views/{view_id}", caller_token())


@tool
async def view_create(project_id: int, title: str, view_kind: str) -> dict:
    """Create a project view. `view_kind` is one of `list`, `gantt`, `table`, `kanban`."""
    body = {"title": title, "view_kind": view_kind}
    return await request("PUT", f"/projects/{project_id}/views", caller_token(), json=body)


@tool
async def view_update(
    project_id: int,
    view_id: int,
    title: str | None = None,
    view_kind: str | None = None,
    default_bucket_id: int | None = None,
    done_bucket_id: int | None = None,
) -> dict:
    """Update a project view. Only the fields you pass change.

    `done_bucket_id` sets which kanban bucket marks a task done when a task is dropped in
    it; `default_bucket_id` is where new tasks land.
    """
    body = _drop_none(
        title=title,
        view_kind=view_kind,
        default_bucket_id=default_bucket_id,
        done_bucket_id=done_bucket_id,
    )
    return await request(
        "POST", f"/projects/{project_id}/views/{view_id}", caller_token(), json=body
    )


@tool
async def view_delete(project_id: int, view_id: int) -> dict:
    """Delete a project view."""
    return await request("DELETE", f"/projects/{project_id}/views/{view_id}", caller_token())


# ===========================================================================
# Buckets / Kanban
# ===========================================================================


@tool
async def bucket_list(project_id: int, view_id: int, page: int = 1, per_page: int = 50) -> Any:
    """List the kanban buckets (columns) of a project view.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request(
        "GET",
        f"/projects/{project_id}/views/{view_id}/buckets",
        caller_token(),
        params=params,
    )


@tool
async def bucket_create(
    project_id: int, view_id: int, title: str, limit: int | None = None
) -> dict:
    """Create a kanban bucket (column) in a view. `limit` caps tasks (0 = no limit)."""
    body = _drop_none(title=title, limit=limit)
    return await request(
        "PUT", f"/projects/{project_id}/views/{view_id}/buckets", caller_token(), json=body
    )


@tool
async def bucket_update(
    project_id: int,
    view_id: int,
    bucket_id: int,
    title: str | None = None,
    limit: int | None = None,
) -> dict:
    """Update a kanban bucket. Only the fields you pass change."""
    body = _drop_none(title=title, limit=limit)
    return await request(
        "POST",
        f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}",
        caller_token(),
        json=body,
    )


@tool
async def bucket_delete(project_id: int, view_id: int, bucket_id: int) -> dict:
    """Delete a kanban bucket."""
    return await request(
        "DELETE",
        f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}",
        caller_token(),
    )


@tool
async def task_bucket_move(
    project_id: int, view_id: int, bucket_id: int, task_id: int | str
) -> dict:
    """Move a task into a kanban bucket (column) — drives status changes on migration."""
    body = {"task_id": task_id, "bucket_id": bucket_id}
    return await request(
        "POST",
        f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
        caller_token(),
        json=body,
    )


# ===========================================================================
# Webhooks (project-scoped)
# ===========================================================================


@tool
async def webhook_events() -> Any:
    """List the webhook event types Vikunja can emit (e.g. task.created, task.done)."""
    return await request("GET", "/webhooks/events", caller_token())


@tool
async def webhook_list(project_id: int, page: int = 1, per_page: int = 50) -> Any:
    """List webhook targets configured on a project.

    Capped at `per_page` (default 50). When more pages exist the result becomes
    `{"items": [...], "pagination": {"truncated": true, ...}}` — raise `page` to read on.
    """
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/projects/{project_id}/webhooks", caller_token(), params=params)


@tool
async def webhook_create(
    project_id: int,
    target_url: str,
    events: list[str],
    secret: str = "",
) -> dict:
    """Register a webhook target on a project.

    SECURITY / SSRF: `target_url` must be genuinely external to forge. This server's SSRF
    guard resolves the hostname and refuses any address that is loopback, private,
    link-local, reserved, multicast, or unspecified — and on forge that includes
    `*.helmforge.me` (split-horizon DNS resolves it to the LAN), so a SWAG-fronted hostname
    is refused just like a raw internal IP. See SECURITY.md. `secret` is the HMAC key
    Vikunja signs deliveries with (X-Vikunja-Signature) — set it so the listener can verify
    authenticity.
    """
    _validate_webhook_target(target_url)
    body = _drop_none(target_url=target_url, events=events, secret=secret or None)
    return await request("PUT", f"/projects/{project_id}/webhooks", caller_token(), json=body)


@tool
async def webhook_delete(project_id: int, webhook_id: int) -> dict:
    """Delete a webhook target from a project."""
    return await request("DELETE", f"/projects/{project_id}/webhooks/{webhook_id}", caller_token())


# ===========================================================================
# Entry point
# ===========================================================================


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe. Unauthenticated by design — so it must stay free of config.

    Everything this returns is public. Do not add ``VIKUNJA_URL``, the transport binding,
    upstream identity, or any other config echo: this is the one route on the server that
    answers without a bearer token, and `/mcp` answering 406 rather than 401 makes it easy
    to misread the surface as authenticated when it is not.

    It deliberately does **not** probe upstream Vikunja. This server is stateless and
    recovers on its own, so a Vikunja restart marking the container unhealthy would cost a
    needless restart loop and buy nothing. If readiness signal is ever wanted, add a
    separate ``/ready`` rather than overloading liveness with a dependency check.
    """
    return JSONResponse({"status": "ok", "version": __version__})


def main() -> None:
    cfg = get_settings()
    log.info(
        "vikunja_mcp_start",
        version=__version__,
        url=cfg.url,
        transport=cfg.transport,
        host=cfg.host,
        port=cfg.port,
    )
    if cfg.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=cfg.transport, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
