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
) -> Any:
    """Make one authenticated request to Vikunja and return the decoded JSON.

    Args:
        method: HTTP verb.
        path: API path relative to /api/v1 (leading slash optional).
        token: the caller's Vikunja bearer token (see auth.caller_token).
        params: query string parameters.
        json: request body, serialized as JSON.

    Raises:
        VikunjaAPIError: on a network failure (status 0) or any 4xx/5xx response.
    """
    client = get_client()
    headers = {"Authorization": f"Bearer {token}"}
    # Strip None query params so optional tool arguments don't leak literal "None".
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = await client.request(
            method, path.lstrip("/"), headers=headers, params=clean_params or None, json=json
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
    return resp.json()
