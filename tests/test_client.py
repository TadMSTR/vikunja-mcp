"""Client behaviour: auth header forwarding, error mapping, empty-body handling."""

from __future__ import annotations

import httpx
import pytest
import respx

from vikunja_mcp import client
from vikunja_mcp.exceptions import VikunjaAPIError

BASE = "https://vikunja.test/api/v1"


@respx.mock
async def test_forwards_bearer_token_and_returns_json():
    route = respx.get(f"{BASE}/user").mock(
        return_value=httpx.Response(200, json={"id": 7, "username": "agent-developer"})
    )
    data = await client.request("GET", "/user", "tok-42")
    assert data["username"] == "agent-developer"
    assert route.calls.last.request.headers["authorization"] == "Bearer tok-42"


@respx.mock
async def test_none_query_params_are_dropped():
    route = respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=[]))
    await client.request("GET", "/tasks", "t", params={"s": None, "page": 1})
    assert "s" not in route.calls.last.request.url.params
    assert route.calls.last.request.url.params["page"] == "1"


@respx.mock
async def test_error_body_message_is_surfaced():
    respx.post(f"{BASE}/projects/9").mock(
        return_value=httpx.Response(403, json={"code": 403, "message": "forbidden: not owner"})
    )
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("POST", "/projects/9", "t", json={"title": "x"})
    assert exc.value.status_code == 403
    assert "forbidden: not owner" in exc.value.message


@respx.mock
async def test_non_json_error_falls_back_to_text():
    respx.get(f"{BASE}/tasks/1").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/tasks/1", "t")
    assert exc.value.status_code == 502
    assert "bad gateway" in exc.value.message


@respx.mock
async def test_empty_204_returns_ok_marker():
    respx.delete(f"{BASE}/tasks/5").mock(return_value=httpx.Response(204))
    assert await client.request("DELETE", "/tasks/5", "t") == {"ok": True}


@respx.mock
async def test_network_failure_becomes_api_error_status_zero():
    respx.get(f"{BASE}/user").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/user", "t")
    assert exc.value.status_code == 0
