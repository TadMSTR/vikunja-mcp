"""Async httpx client for the Vikunja REST API.

One long-lived ``AsyncClient`` is reused across calls (connection pooling); the caller's
bearer token is applied per request, never stored on the client, because different agents
share this process but must reach Vikunja as themselves.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from .config import get_settings
from .exceptions import VikunjaAPIError

log = structlog.get_logger()

_client: httpx.AsyncClient | None = None


def _api_base() -> str:
    cfg = get_settings()
    return f"{cfg.url.rstrip('/')}/api/v1"


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        cfg = get_settings()
        _client = httpx.AsyncClient(base_url=_api_base(), timeout=cfg.request_timeout)
    return _client


async def aclose() -> None:
    """Close the shared client (shutdown/test cleanup)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _pagination_envelope(
    resp: httpx.Response, data: list[Any], params: dict[str, Any]
) -> dict[str, Any] | None:
    """Wrap a truncated list response so the caller can see it is incomplete.

    Vikunja paginates every list endpoint (default 50 per page) and reports the extent
    only in headers. Returning the bare list therefore hands an agent a silently truncated
    answer: "find every ticket about X" reads at most one page and looks complete. That is
    a correctness problem, not an ergonomics one, so a truncated result is re-shaped into
    ``{"items": [...], "pagination": {...}}`` while a complete one is returned untouched.

    Returns None when the response is not a truncated list, meaning "return ``data`` as-is".

    On the two headers: ``x-pagination-total-pages`` is the page count, and
    ``x-pagination-result-count`` is the number of items in *this* response, not the size
    of the whole result set — probed at per_page 1/5/50, where it came back 1/5/50 against
    340/68/7 total pages. It is surfaced as ``count`` for that reason; calling it
    ``result_count`` invites exactly the misreading the envelope exists to prevent.
    Vikunja exposes no total-item count, so none is reported rather than inferred.
    """
    raw_total = resp.headers.get("x-pagination-total-pages")
    if not raw_total:
        return None
    try:
        total_pages = int(raw_total)
    except ValueError:
        log.info("vikunja_pagination_header_unparsable", value=raw_total)
        return None
    if total_pages <= 1:
        return None

    # The requested page is taken from the params we actually sent — explicit, never
    # inferred from the response. Vikunja defaults to page 1 when the caller omits it.
    try:
        page = int(params.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    return {
        "items": data,
        "pagination": {
            "page": page,
            "total_pages": total_pages,
            "count": len(data),
            "truncated": True,
        },
    }


def _extract_error(resp: httpx.Response) -> str:
    """Pull Vikunja's error message out of the body, falling back to raw text.

    Vikunja error bodies look like ``{"code": 403, "message": "..."}``. We surface the
    message so the agent sees *why* a call failed without a debugger.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip() or resp.reason_phrase
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return resp.text.strip() or resp.reason_phrase


async def request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    files: Any = None,
) -> Any:
    """Make one authenticated request to Vikunja and return the decoded JSON.

    Returns the decoded body unchanged, with one exception: a **list** body that Vikunja
    reports as spanning more than one page is wrapped as
    ``{"items": [...], "pagination": {...}}`` so the caller can tell a first page from a
    complete answer (see :func:`_pagination_envelope`). Single-page lists and all non-list
    bodies pass through untouched.

    Args:
        method: HTTP verb.
        path: API path relative to /api/v1 (leading slash optional).
        token: the caller's Vikunja bearer token (see auth.caller_token).
        params: query string parameters.
        json: request body, serialized as JSON.
        files: multipart file payload (attachment upload). Mutually exclusive with ``json``;
            when set, httpx encodes a ``multipart/form-data`` body instead of JSON.

    Raises:
        VikunjaAPIError: on a network failure (status 0) or any 4xx/5xx response.
    """
    client = get_client()
    headers = {"Authorization": f"Bearer {token}"}
    # Strip None query params so optional tool arguments don't leak literal "None".
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = await client.request(
            method,
            path.lstrip("/"),
            headers=headers,
            params=clean_params or None,
            json=json,
            files=files,
        )
    except httpx.RequestError as exc:
        log.warning("vikunja_request_failed", method=method, path=path, error=str(exc))
        raise VikunjaAPIError(0, f"request to Vikunja failed: {exc}") from exc

    if resp.status_code >= 400:
        detail = _extract_error(resp)
        log.info("vikunja_api_error", method=method, path=path, status=resp.status_code)
        raise VikunjaAPIError(resp.status_code, detail)

    if resp.status_code == 204 or not resp.content:
        return {"ok": True}

    data = resp.json()
    if isinstance(data, list):
        envelope = _pagination_envelope(resp, data, clean_params)
        if envelope is not None:
            log.info(
                "vikunja_result_truncated",
                method=method,
                path=path,
                page=envelope["pagination"]["page"],
                total_pages=envelope["pagination"]["total_pages"],
            )
            return envelope
    return data
