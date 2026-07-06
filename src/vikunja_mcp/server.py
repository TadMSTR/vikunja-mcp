"""vikunja-mcp — FastMCP server exposing the Vikunja REST API as scoped MCP tools.

Tools are grouped by resource (projects, tasks, labels, comments, filters, webhooks).
Every tool resolves the caller's own Vikunja token per request (see auth.py) and forwards
it upstream, so Vikunja sees the acting agent, not a shared service account.

Endpoint coverage is sourced from the live Vikunja Swagger spec (/api/v1/docs.json), not
prose docs. Vikunja's REST idiom is unusual: PUT creates, POST updates.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from fastmcp import FastMCP

from . import __version__
from .auth import caller_token
from .client import request
from .config import get_settings

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

# ---------------------------------------------------------------------------
# Optional OTLP telemetry — off unless OTEL_EXPORTER_OTLP_ENDPOINT is set
# ---------------------------------------------------------------------------

_otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
if _otel_endpoint:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _provider = TracerProvider(resource=Resource.create({"service.name": "vikunja-mcp"}))
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_otel_endpoint)))
        trace.set_tracer_provider(_provider)
        log.info("otlp_enabled", endpoint=_otel_endpoint)
    except ImportError:
        log.warning("otlp_import_failed", hint="pip install 'vikunja-mcp[telemetry]'")

mcp = FastMCP("vikunja-mcp")


def _drop_none(**fields: Any) -> dict[str, Any]:
    """Build a request body from only the fields the caller actually supplied.

    Vikunja's update endpoints merge the posted object, so omitting a field leaves it
    untouched; sending it as null would clobber it. Keep only non-None values.
    """
    return {k: v for k, v in fields.items() if v is not None}


# ===========================================================================
# Identity
# ===========================================================================


@mcp.tool()
async def whoami() -> dict:
    """Return the Vikunja user the caller's token authenticates as.

    Useful to confirm the per-agent token is wired correctly through scoped-mcp.
    """
    return await request("GET", "/user", caller_token())


# ===========================================================================
# Projects
# ===========================================================================


@mcp.tool()
async def project_list(
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    is_archived: bool = False,
) -> Any:
    """List projects the caller can access. `search` filters by title."""
    params = {"page": page, "per_page": per_page, "s": search or None, "is_archived": is_archived}
    return await request("GET", "/projects", caller_token(), params=params)


@mcp.tool()
async def project_get(project_id: int) -> dict:
    """Get a single project by ID."""
    return await request("GET", f"/projects/{project_id}", caller_token())


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def project_delete(project_id: int) -> dict:
    """Delete a project and all its tasks. Irreversible."""
    return await request("DELETE", f"/projects/{project_id}", caller_token())


# ===========================================================================
# Tasks
# ===========================================================================


@mcp.tool()
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


@mcp.tool()
async def task_search(query: str, page: int = 1, per_page: int = 50) -> Any:
    """Full-text search tasks by title/description (ParadeDB BM25 index)."""
    params = {"s": query, "page": page, "per_page": per_page}
    return await request("GET", "/tasks", caller_token(), params=params)


@mcp.tool()
async def task_get(task_id: int) -> dict:
    """Get a single task by ID, including labels, assignees, and comments."""
    return await request("GET", f"/tasks/{task_id}", caller_token())


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def task_delete(task_id: int) -> dict:
    """Delete a task. Irreversible."""
    return await request("DELETE", f"/tasks/{task_id}", caller_token())


# ===========================================================================
# Labels
# ===========================================================================


@mcp.tool()
async def label_list(page: int = 1, per_page: int = 50, search: str = "") -> Any:
    """List labels the caller can access. `search` filters by title."""
    params = {"page": page, "per_page": per_page, "s": search or None}
    return await request("GET", "/labels", caller_token(), params=params)


@mcp.tool()
async def label_get(label_id: int) -> dict:
    """Get a single label by ID."""
    return await request("GET", f"/labels/{label_id}", caller_token())


@mcp.tool()
async def label_create(title: str, description: str = "", hex_color: str = "") -> dict:
    """Create a label. `title` is required."""
    body = _drop_none(title=title, description=description or None, hex_color=hex_color or None)
    return await request("PUT", "/labels", caller_token(), json=body)


@mcp.tool()
async def label_update(
    label_id: int,
    title: str | None = None,
    description: str | None = None,
    hex_color: str | None = None,
) -> dict:
    """Update a label. Only the fields you pass change."""
    body = _drop_none(title=title, description=description, hex_color=hex_color)
    return await request("PUT", f"/labels/{label_id}", caller_token(), json=body)


@mcp.tool()
async def label_delete(label_id: int) -> dict:
    """Delete a label."""
    return await request("DELETE", f"/labels/{label_id}", caller_token())


@mcp.tool()
async def task_label_add(task_id: int, label_id: int) -> dict:
    """Attach an existing label to a task."""
    return await request(
        "PUT", f"/tasks/{task_id}/labels", caller_token(), json={"label_id": label_id}
    )


@mcp.tool()
async def task_label_remove(task_id: int, label_id: int) -> dict:
    """Detach a label from a task."""
    return await request("DELETE", f"/tasks/{task_id}/labels/{label_id}", caller_token())


# ===========================================================================
# Comments
# ===========================================================================


@mcp.tool()
async def comment_list(task_id: int) -> Any:
    """List comments on a task."""
    return await request("GET", f"/tasks/{task_id}/comments", caller_token())


@mcp.tool()
async def comment_create(task_id: int, comment: str) -> dict:
    """Add a comment to a task. `comment` may contain HTML."""
    return await request(
        "PUT", f"/tasks/{task_id}/comments", caller_token(), json={"comment": comment}
    )


@mcp.tool()
async def comment_delete(task_id: int, comment_id: int) -> dict:
    """Delete a comment from a task."""
    return await request("DELETE", f"/tasks/{task_id}/comments/{comment_id}", caller_token())


# ===========================================================================
# Saved filters
# ===========================================================================


@mcp.tool()
async def filter_get(filter_id: int) -> dict:
    """Get a saved filter by ID."""
    return await request("GET", f"/filters/{filter_id}", caller_token())


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def filter_delete(filter_id: int) -> dict:
    """Delete a saved filter."""
    return await request("DELETE", f"/filters/{filter_id}", caller_token())


# ===========================================================================
# Webhooks (project-scoped)
# ===========================================================================


@mcp.tool()
async def webhook_events() -> Any:
    """List the webhook event types Vikunja can emit (e.g. task.created, task.done)."""
    return await request("GET", "/webhooks/events", caller_token())


@mcp.tool()
async def webhook_list(project_id: int, page: int = 1, per_page: int = 50) -> Any:
    """List webhook targets configured on a project."""
    params = {"page": page, "per_page": per_page}
    return await request("GET", f"/projects/{project_id}/webhooks", caller_token(), params=params)


@mcp.tool()
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
    body = _drop_none(target_url=target_url, events=events, secret=secret or None)
    return await request("PUT", f"/projects/{project_id}/webhooks", caller_token(), json=body)


@mcp.tool()
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
