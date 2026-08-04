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


# --- pagination envelope --------------------------------------------------

_MULTI = {"x-pagination-total-pages": "7", "x-pagination-result-count": "50"}
_SINGLE = {"x-pagination-total-pages": "1", "x-pagination-result-count": "38"}


@respx.mock
async def test_truncated_list_is_wrapped_with_pagination():
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}], headers=_MULTI)
    )
    out = await client.request("GET", "/tasks", "t", params={"page": 2, "per_page": 50})
    assert out["items"] == [{"id": 1}, {"id": 2}]
    assert out["pagination"] == {
        "page": 2,
        "total_pages": 7,
        "count": 2,
        "truncated": True,
    }


@respx.mock
async def test_single_page_list_is_returned_unwrapped():
    """The envelope must not become the default shape — it signals truncation only."""
    respx.get(f"{BASE}/labels").mock(
        return_value=httpx.Response(200, json=[{"id": 1}], headers=_SINGLE)
    )
    out = await client.request("GET", "/labels", "t")
    assert out == [{"id": 1}]


@respx.mock
async def test_page_defaults_to_one_when_caller_sent_no_page():
    """Tools without a page argument (comment_list, view_list) still report a page."""
    respx.get(f"{BASE}/tasks/5/comments").mock(
        return_value=httpx.Response(200, json=[{"id": 1}], headers=_MULTI)
    )
    out = await client.request("GET", "/tasks/5/comments", "t")
    assert out["pagination"]["page"] == 1


@respx.mock
async def test_dict_body_is_never_wrapped_even_when_headers_present():
    """A single task carries the headers too; wrapping it would break every read path."""
    respx.get(f"{BASE}/tasks/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "title": "t"}, headers=_MULTI)
    )
    out = await client.request("GET", "/tasks/5", "t")
    assert out == {"id": 5, "title": "t"}


@respx.mock
async def test_list_without_pagination_headers_is_unwrapped():
    respx.get(f"{BASE}/webhooks/events").mock(
        return_value=httpx.Response(200, json=["task.created", "task.done"])
    )
    out = await client.request("GET", "/webhooks/events", "t")
    assert out == ["task.created", "task.done"]


@respx.mock
async def test_unparsable_pagination_header_degrades_to_unwrapped():
    """A malformed header must not turn a working list call into a crash."""
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1}], headers={"x-pagination-total-pages": "not-a-number"}
        )
    )
    out = await client.request("GET", "/tasks", "t")
    assert out == [{"id": 1}]


@respx.mock
async def test_non_numeric_page_param_falls_back_to_one():
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(200, json=[{"id": 1}], headers=_MULTI)
    )
    out = await client.request("GET", "/tasks", "t", params={"page": "abc"})
    assert out["pagination"]["page"] == 1


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
