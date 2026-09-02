"""`/health` — the one unauthenticated route on the server.

These tests drive the real ASGI app from ``mcp.http_app()`` rather than calling the
handler directly, because the thing being asserted is a property of the *route table*:
that `/health` answers without an ``Authorization`` header and nothing else does. Calling
`health()` as a plain function would pass even if the route were never registered.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from vikunja_mcp import __version__
from vikunja_mcp.server import mcp


@asynccontextmanager
async def http_client():
    """An ASGI client over the real app, with lifespan run (the session manager needs it).

    A context manager entered inside each test rather than a pytest fixture: as an async
    generator fixture, anyio tears the lifespan's cancel scope down in a different task
    than it was entered in and every test errors during teardown while still reporting a
    pass. Entering it in the test body keeps setup and teardown on one task.
    """
    app = mcp.http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def test_health_needs_no_authorization_header():
    async with http_client() as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


async def test_health_body_echoes_no_config():
    """The response must carry status and version and nothing else.

    An allowlist, not a denylist: asserting `"vikunja.test" not in body` would keep
    passing if someone later added `default_project_id` or the bind host, which are just
    as much config as the URL is.
    """
    async with http_client() as client:
        resp = await client.get("/health")
    assert set(resp.json()) == {"status", "version"}

    body = resp.text.lower()
    for leak in ("vikunja.test", "8501", "127.0.0.1", "token", "authorization", "transport"):
        assert leak not in body, f"/health leaked {leak!r} — this endpoint is public"


async def test_mcp_endpoint_is_not_open():
    """`/health` being open must not mean the app is open.

    Asserts the *property* rather than the status code. The code is a transport detail and
    has already moved once: fastmcp 3 refused a bare GET at content negotiation (406,
    "Client must accept text/event-stream"), fastmcp 4 falls through to session validation
    (400, "Missing session ID"). This test pinned `== 406` and so went red on the
    fastmcp 3 -> 4 bump for a reason with nothing to do with the server being open — while
    its own docstring argued against pinning a specific code. Measured under both majors.

    The `tools/list` probe is the load-bearing half. A bare GET could plausibly be refused
    by a transport that would still answer a well-formed unauthenticated call, so refusing
    the GET alone does not establish that the surface is closed.
    """
    async with http_client() as client:
        bare_get = await client.get("/mcp")
        unauthenticated_call = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    for label, resp in (
        ("bare GET", bare_get),
        ("unauthenticated tools/list", unauthenticated_call),
    ):
        assert 400 <= resp.status_code < 500, (
            f"{label} got {resp.status_code}; expected a 4xx refusal. A 2xx means the MCP "
            "surface is open, a 5xx means it broke rather than refused."
        )
        assert '"result"' not in resp.text, f"{label} returned a JSON-RPC result"
        assert "backlog_summary" not in resp.text, f"{label} leaked the tool list"
