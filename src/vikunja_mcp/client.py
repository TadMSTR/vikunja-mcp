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
    return f"{cfg.url.rstrip('/')}/api/v2"


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


def _is_list_envelope(data: Any) -> bool:
    """True if ``data`` is one of v2's paginated list bodies.

    v2 answers every list endpoint with ``{"items": [...], "total": n, "page": n,
    "per_page": n, "total_pages": n}`` — the envelope *is* the body, where v1 put the
    extent in ``x-pagination-*`` headers and returned a bare array. Both ``items`` and
    ``total_pages`` are required here, so a single resource that merely happens to carry
    an ``items`` field cannot be mistaken for a list response.
    """
    return isinstance(data, dict) and "items" in data and "total_pages" in data


def _int_or(value: Any, fallback: int) -> int:
    """``int(value)``, or ``fallback`` when it is missing or unparsable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        log.info("vikunja_pagination_field_unparsable", value=value)
        return fallback


def _unwrap_list(data: dict[str, Any]) -> Any:
    """Turn a v2 list envelope into what the tools expect: bare list, or truncation envelope.

    Vikunja paginates every list endpoint (default 50 per page). Returning only the rows
    therefore hands an agent a silently truncated answer: "find every ticket about X" reads
    at most one page and looks complete. That is a correctness problem, not an ergonomics
    one, so a truncated result is re-shaped into ``{"items": [...], "pagination": {...}}``
    while a complete one is returned as the bare list callers already handle.

    The outward shape is deliberately unchanged from the v1 client — every list-consuming
    tool in ``server.py`` branches on ``"items" in result`` — with one addition:

    ``total`` is now reported. v1 exposed no total-item count (``x-pagination-result-count``
    counted the rows in *this* response, not the result set), so the v1 client reported
    none rather than inferring one. v2's body carries a real ``total``, so that limitation
    is gone and the number is passed through.

    ``count`` still means "rows in this response" and is still sourced from ``len(items)``.
    It is deliberately not named ``result_count``: reading it as the size of the whole
    result set is precisely the misreading the envelope exists to prevent. ``total`` now
    sits beside it, so the two are distinguishable rather than merely warned about.
    """
    items = data.get("items") or []
    total_pages = _int_or(data.get("total_pages"), 1)
    if total_pages <= 1:
        return items

    return {
        "items": items,
        "pagination": {
            "page": _int_or(data.get("page"), 1),
            "total_pages": total_pages,
            "count": len(items),
            "total": _int_or(data.get("total"), len(items)),
            "truncated": True,
        },
    }


def _extract_error(resp: httpx.Response) -> str:
    """Pull Vikunja's error message out of the body, falling back to raw text.

    v2 answers errors as RFC 9457 ``application/problem+json``:
    ``{"title": "Bad Request", "status": 400, "detail": "...", "code": 4016}``. The
    human-readable text that v1 put in ``message`` now lives in ``detail``; ``title`` is
    the generic status name and is only a fallback, since "Bad Request" alone tells an
    agent nothing. ``code`` is Vikunja's own numeric error code, unchanged from v1 and
    documented at https://vikunja.io/docs/errors/ — appended when present because it is
    the one part of the body that is stable enough to match on.

    ``message`` is still read as a last resort before the raw text: a handful of routes
    (link-share auth, the token middleware) answer from Echo rather than the v2 handler
    stack and still emit the v1 ``{"code", "message"}`` shape. Observed live on v2.5.0 —
    ``GET /api/v2/projects/{p}/tasks/by-index/{i}`` with a token lacking the
    ``projects → tasks_by_index`` permission returns exactly that. Dropping the fallback
    would turn those into an empty reason phrase.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip() or resp.reason_phrase
    if not isinstance(body, dict):
        return resp.text.strip() or resp.reason_phrase

    detail = body.get("detail") or body.get("message") or body.get("title")
    if not detail:
        return resp.text.strip() or resp.reason_phrase

    code = body.get("code")
    return f"{detail} (Vikunja code {code})" if code else str(detail)


async def request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    files: Any = None,
    unwrap_list: bool = True,
) -> Any:
    """Make one authenticated request to Vikunja and return the decoded JSON.

    Two reshapes, both of them narrowing v2's envelope back to what the tools expect:

    * A **list** body arrives as ``{"items": [...], "total": n, ...}``. It is returned as
      the bare row list, unless it spans more than one page, in which case it is wrapped
      as ``{"items": [...], "pagination": {...}}`` so the caller can tell a first page
      from a complete answer (see :func:`_unwrap_list`).
    * ``$schema`` is dropped from a single-resource body. v2 stamps every response with a
      link to its own JSON Schema; it is transport metadata, identical on every row of a
      kind, and it would otherwise be repeated into an agent's context on every read.
      Only the top level is touched — list rows do not carry it.

    Args:
        method: HTTP verb.
        path: API path relative to /api/v2 (leading slash optional).
        token: the caller's Vikunja bearer token (see auth.caller_token).
        params: query string parameters.
        json: request body, serialized as JSON.
        files: multipart file payload (attachment upload). Mutually exclusive with ``json``;
            when set, httpx encodes a ``multipart/form-data`` body instead of JSON.
        unwrap_list: when False, a list response is returned as the raw v2 envelope
            instead of being reshaped. Exists for callers that want the envelope's own
            fields rather than its rows — ``server._count_matching`` reads ``total`` and
            never looks at ``items``, and the reshape would hide it behind a row count
            that is wrong for any query whose page holds no rows.

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

    # v2's DELETE contract is 204 with an empty body, which is what this already returns.
    # Correct as written — do not "fix" it to parse a body that is not sent.
    if resp.status_code == 204 or not resp.content:
        return {"ok": True}

    data = resp.json()
    if _is_list_envelope(data):
        if not unwrap_list:
            data.pop("$schema", None)
            return data
        unwrapped = _unwrap_list(data)
        if isinstance(unwrapped, dict):
            log.info(
                "vikunja_result_truncated",
                method=method,
                path=path,
                page=unwrapped["pagination"]["page"],
                total_pages=unwrapped["pagination"]["total_pages"],
                total=unwrapped["pagination"]["total"],
            )
        return unwrapped
    if isinstance(data, dict):
        data.pop("$schema", None)
    return data
