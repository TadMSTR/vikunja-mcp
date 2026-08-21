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

    Measured, not assumed: an unadorned `GET /mcp` returns **406**, because the transport
    rejects on content negotiation before it ever looks at credentials. Asserting a
    specific 401 here would encode a failure mode this server does not have; what matters
    is that the MCP surface does not serve a bare GET.
    """
    async with http_client() as client:
        resp = await client.get("/mcp")
    assert resp.status_code != 200
    assert resp.status_code == 406
