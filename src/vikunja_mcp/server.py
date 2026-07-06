"""vikunja-mcp — FastMCP server exposing the Vikunja REST API as scoped MCP tools.

Tools are grouped by resource (projects, tasks, labels, comments, filters, webhooks,
teams, sharing, buckets/kanban, views, assignees, relations, reminders, attachments,
bulk). Every tool resolves the caller's own Vikunja token per request (see auth.py) and
forwards it upstream, so Vikunja sees the acting agent, not a shared service account.

Endpoint coverage is sourced from the live Vikunja Swagger spec (/api/v1/docs.json), not
prose docs. Vikunja's REST idiom is unusual: **PUT creates, POST updates**.

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
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlparse

import structlog
from fastmcp import FastMCP

from . import __version__, telemetry
from .auth import caller_token
from .client import request
from .config import get_settings
from .exceptions import VikunjaAPIError
from .hooks import run_after_hooks, run_before_hooks

# ---------------------------------------------------------------------------
# Logging — JSON structlog, on by default (forge MCP convention)
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
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

    Vikunja's update endpoints merge the posted object, so omitting a field leaves it
    untouched; sending it as null would clobber it. Keep only non-None values.
    """
    return {k: v for k, v in fields.items() if v is not None}


# Base64 attachment size ceiling — reject before decoding a huge blob into memory (F-04).
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Hostname suffixes that only ever name internal resources — refuse webhook delivery there.
_INTERNAL_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home", ".corp")


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True if an address is non-routable / internal and unsafe as a webhook target."""
    return (
        ip.is_private
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
        # Unresolvable here — can't classify; Vikunja re-resolves at delivery. Allow.
        return False
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
            f"webhook target_url host {host!r} is loopback/private/link-local/internal and "
            "is refused (SSRF guard). Use a public SWAG hostname.",
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
        description=description or None,
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
        title=title, description=description, hex_color=hex_color, is_archived=is_archived
    )
    return await request("POST", f"/projects/{project_id}", caller_token(), json=body)


@tool
async def project_delete(project_id: int) -> dict:
    """Delete a project and all its tasks. Irreversible."""
    return await request("DELETE", f"/projects/{project_id}", caller_token())


# ===========================================================================
# Tasks
# ===========================================================================


@tool
async def task_list(
    page: int = 1,
    per_page: int = 50,
    filter: str = "",
    sort_by: str = "",
    order_by: str = "",
) -> Any:
    """List tasks across all projects the caller can access.

    `filter` accepts Vikunja's filter syntax, e.g. `done = false && priority >= 3`.
    """
    params = {
        "page": page,
        "per_page": per_page,
        "filter": filter or None,
        "sort_by": sort_by or None,
        "order_by": order_by or None,
    }
    return await request("GET", "/tasks", caller_token(), params=params)


@tool
async def task_search(query: str, page: int = 1, per_page: int = 50) -> Any:
    """Full-text search tasks by title/description (ParadeDB BM25 index)."""
    params = {"s": query, "page": page, "per_page": per_page}
    return await request("GET", "/tasks", caller_token(), params=params)


@tool
async def task_get(task_id: int) -> dict:
    """Get a single task by ID, including labels, assignees, and comments."""
    return await request("GET", f"/tasks/{task_id}", caller_token())


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
        description=description or None,
        priority=priority,
        due_date=due_date or None,
    )
    return await request("PUT", f"/projects/{project_id}/tasks", caller_token(), json=body)


@tool
async def task_update(
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    done: bool | None = None,
    priority: int | None = None,
    due_date: str | None = None,
    percent_done: float | None = None,
) -> dict:
    """Update a task. Only the fields you pass change. Set `done=true` to complete it."""
    body = _drop_none(
        title=title,
        description=description,
        done=done,
        priority=priority,
        due_date=due_date,
        percent_done=percent_done,
    )
    return await request("POST", f"/tasks/{task_id}", caller_token(), json=body)


@tool
async def task_delete(task_id: int) -> dict:
    """Delete a task. Irreversible."""
    return await request("DELETE", f"/tasks/{task_id}", caller_token())


@tool
async def tasks_bulk_update(task_ids: list[int], values: dict) -> dict:
    """Apply the same field changes to many tasks in one call (migration throughput).

    `values` is a partial task object, e.g. `{"done": true}` or `{"priority": 4}`; it is
    applied to every task in `task_ids`.
    """
    body = {"task_ids": task_ids, "values": values}
    return await request("POST", "/tasks/bulk", caller_token(), json=body)


# ===========================================================================
# Labels
# ===========================================================================


@tool
async def label_list(page: int = 1, per_page: int = 50, search: str = "") -> Any:
    """List labels the caller can access. `search` filters by title."""
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
    return await request("PUT", f"/labels/{label_id}", caller_token(), json=body)


@tool
async def label_delete(label_id: int) -> dict:
    """Delete a label."""
    return await request("DELETE", f"/labels/{label_id}", caller_token())


@tool
async def task_label_add(task_id: int, label_id: int) -> dict:
    """Attach an existing label to a task."""
    return await request(
        "PUT", f"/tasks/{task_id}/labels", caller_token(), json={"label_id": label_id}
    )


@tool
async def task_label_remove(task_id: int, label_id: int) -> dict:
    """Detach a label from a task."""
    return await request("DELETE", f"/tasks/{task_id}/labels/{label_id}", caller_token())


# ===========================================================================
# Comments
# ===========================================================================


@tool
async def comment_list(task_id: int) -> Any:
    """List comments on a task."""
    return await request("GET", f"/tasks/{task_id}/comments", caller_token())


@tool
async def comment_create(task_id: int, comment: str) -> dict:
    """Add a comment to a task. `comment` may contain HTML."""
    return await request(
        "PUT", f"/tasks/{task_id}/comments", caller_token(), json={"comment": comment}
    )


@tool
async def comment_delete(task_id: int, comment_id: int) -> dict:
    """Delete a comment from a task."""
    return await request("DELETE", f"/tasks/{task_id}/comments/{comment_id}", caller_token())


# ===========================================================================
# Assignees
# ===========================================================================


@tool
async def task_assignee_list(task_id: int) -> Any:
    """List the users assigned to a task."""
    return await request("GET", f"/tasks/{task_id}/assignees", caller_token())


@tool
async def task_assignee_add(task_id: int, user_id: int) -> dict:
    """Assign a user to a task."""
    return await request(
        "PUT", f"/tasks/{task_id}/assignees", caller_token(), json={"user_id": user_id}
    )


@tool
async def task_assignees_add_bulk(task_id: int, user_ids: list[int]) -> dict:
    """Assign several users to a task in one call (carries Plane assignees on migration)."""
    body = {"assignees": [{"id": uid} for uid in user_ids]}
    return await request("POST", f"/tasks/{task_id}/assignees/bulk", caller_token(), json=body)


@tool
async def task_assignee_remove(task_id: int, user_id: int) -> dict:
    """Remove a user's assignment from a task."""
    return await request("DELETE", f"/tasks/{task_id}/assignees/{user_id}", caller_token())


# ===========================================================================
# Relations / subtasks
# ===========================================================================

# Vikunja relation kinds: subtask, parenttask, related, duplicateof, duplicates,
# blocking, blocked, precedes, follows, copiedfrom, copiedto.


@tool
async def task_relation_add(task_id: int, other_task_id: int, relation_kind: str) -> dict:
    """Relate two tasks. `relation_kind` is e.g. `subtask`, `related`, `blocking`, `precedes`."""
    body = {"other_task_id": other_task_id, "relation_kind": relation_kind}
    return await request("PUT", f"/tasks/{task_id}/relations", caller_token(), json=body)


@tool
async def task_relation_remove(task_id: int, relation_kind: str, other_task_id: int) -> dict:
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
async def task_reminders_set(task_id: int, reminders: list[str]) -> dict:
    """Set a task's reminders. `reminders` is a list of RFC3339 timestamps.

    This replaces the task's reminder set (Vikunja stores reminders on the task object).
    Pass an empty list to clear all reminders.
    """
    body = {"reminders": [{"reminder": r} for r in reminders]}
    return await request("POST", f"/tasks/{task_id}", caller_token(), json=body)


# ===========================================================================
# Attachments
# ===========================================================================


@tool
async def attachment_list(task_id: int) -> Any:
    """List attachments on a task."""
    return await request("GET", f"/tasks/{task_id}/attachments", caller_token())


@tool
async def attachment_upload(task_id: int, filename: str, content_base64: str) -> dict:
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
async def attachment_delete(task_id: int, attachment_id: int) -> dict:
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
    """List teams the caller belongs to. `search` filters by name."""
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
    """List teams a project is shared with."""
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
    """List users a project is shared with directly."""
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
    """List link shares configured on a project."""
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
async def view_list(project_id: int) -> Any:
    """List the views configured on a project (the 4 auto-created ones by default)."""
    return await request("GET", f"/projects/{project_id}/views", caller_token())


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
    """List the kanban buckets (columns) of a project view."""
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
async def task_bucket_move(project_id: int, view_id: int, bucket_id: int, task_id: int) -> dict:
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
    """List webhook targets configured on a project."""
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

    SECURITY / SSRF: Vikunja refuses to deliver webhooks to private/RFC1918 addresses by
    default and will reject a target_url that resolves to one. Always point `target_url`
    at a public hostname routed through SWAG (e.g. the vikunja-webhook-listener vhost),
    never a raw internal IP. `secret` is the HMAC key Vikunja signs deliveries with
    (X-Vikunja-Signature) — set it so the listener can verify authenticity.
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
