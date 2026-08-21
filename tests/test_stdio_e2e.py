"""End-to-end stdio: a real subprocess, real MCP framing, a stub upstream.

Why this exists as a subprocess test rather than an in-process one. The stdio transport
shipped completely broken under a green suite (vikunja#461): `test_main_stdio_transport`
asserted only that `mcp.run` was called with `transport="stdio"`, so it never noticed
that every tool call raised AuthError. An assertion about the launcher is not an
assertion about the transport.

Fixing that surfaced a second defect an in-process test also cannot see: structlog wrote
its JSON to **stdout**, the JSON-RPC channel. Only a real pipe shows what is actually on
stdout. Note the honest limit of that finding — the fastmcp client tolerates the stray
line and still completes the handshake, so this file's value is the explicit stream
assertion below, not the hope that a polluted stream would fail loudly.

The upstream Vikunja is a stub on loopback, so this needs no credential and no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# Resolve the console script next to the running interpreter rather than via PATH: pytest
# is commonly invoked as `.venv/bin/python -m pytest`, which does not put `.venv/bin` on
# PATH, and a PATH lookup would silently skip this whole file.
SCRIPT = Path(sys.executable).parent / "vikunja-mcp"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(),
    reason=f"console script not installed at {SCRIPT} (package not installed in this env)",
)

STUB_USER = {"id": 42, "username": "stub-user"}


@contextlib.asynccontextmanager
async def stub_vikunja():
    """A loopback HTTP server that answers GET /api/v1/user, capturing the auth header."""
    seen: dict[str, str] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.readuntil(b"\r\n\r\n")
        for line in data.decode("latin-1").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.lower() == "authorization":
                seen["authorization"] = value.strip()
        body = json.dumps(STUB_USER).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        await server.start_serving()
        yield f"http://127.0.0.1:{port}", seen


async def test_stdio_serves_a_tool_call_over_a_real_pipe():
    async with stub_vikunja() as (url, seen):
        env = dict(os.environ)
        env["VIKUNJA_URL"] = url
        env["VIKUNJA_TRANSPORT"] = "stdio"
        env["VIKUNJA_TOKEN"] = "stdio-e2e-token"
        env.pop("VIKUNJA_DEFAULT_PROJECT_ID", None)

        transport = StdioTransport(command=str(SCRIPT), args=[], env=env)
        async with Client(transport) as client:
            assert len(await client.list_tools()) > 0
            result = await client.call_tool("whoami", {})

    assert result.data == STUB_USER
    # The configured token must actually reach upstream, not merely satisfy a local check.
    assert seen["authorization"] == "Bearer stdio-e2e-token"


async def test_stdio_writes_no_logs_to_stdout():
    """The actual guard for stdout cleanliness.

    The end-to-end test above keeps passing if this regresses, because the client in use
    is lenient about interleaved non-protocol lines. So the invariant has to be asserted
    directly against the raw streams — otherwise nothing in the suite covers it.
    """
    env = dict(os.environ)
    env["VIKUNJA_URL"] = "http://127.0.0.1:1"
    env["VIKUNJA_TRANSPORT"] = "stdio"
    env["VIKUNJA_TOKEN"] = "unused"

    proc = await asyncio.create_subprocess_exec(
        str(SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(b""), timeout=30)
    except TimeoutError:  # pragma: no cover - defensive
        proc.kill()
        raise

    assert b"vikunja_mcp_start" not in stdout, (
        f"log line on stdout corrupts the JSON-RPC stream: {stdout[:200]!r}"
    )
    assert b"vikunja_mcp_start" in stderr, "startup log vanished entirely — check the sink"
