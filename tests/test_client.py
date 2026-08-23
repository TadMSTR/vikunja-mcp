"""Client behaviour: auth header forwarding, error mapping, empty-body handling.

The pagination and error cases here are written against **v2 body shapes**. v1 reported
the extent of a list in ``x-pagination-*`` headers and returned a bare array, and errors
as ``{"code", "message"}``; v2 returns ``{"items", "total", "page", "per_page",
"total_pages"}`` and RFC 9457 ``application/problem+json``. Every shape asserted below was
checked against live Vikunja v2.5.0 during the port.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from vikunja_mcp import client
from vikunja_mcp.exceptions import VikunjaAPIError

BASE = "https://vikunja.test/api/v2"


def envelope(items, *, total=None, page=1, per_page=50, total_pages=1):
    """A v2 list body. ``total`` defaults to something consistent with the other fields."""
    return {
        "$schema": "https://vikunja.test/api/v2/schemas/PaginatedTask.json",
        "items": items,
        "total": len(items) if total is None else total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


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
    route = respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=envelope([])))
    await client.request("GET", "/tasks", "t", params={"q": None, "page": 1})
    assert "q" not in route.calls.last.request.url.params
    assert route.calls.last.request.url.params["page"] == "1"


@respx.mock
async def test_base_url_targets_api_v2():
    """The version is applied in the client, not configured — a v1 call must not be made."""
    route = respx.get(f"{BASE}/tasks/5").mock(return_value=httpx.Response(200, json={"id": 5}))
    await client.request("GET", "/tasks/5", "t")
    assert route.calls.last.request.url.path == "/api/v2/tasks/5"


# --- pagination envelope --------------------------------------------------


@respx.mock
async def test_truncated_list_is_wrapped_with_pagination():
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json=envelope([{"id": 1}, {"id": 2}], total=340, page=2, total_pages=7),
        )
    )
    out = await client.request("GET", "/tasks", "t", params={"page": 2, "per_page": 50})
    assert out["items"] == [{"id": 1}, {"id": 2}]
    assert out["pagination"] == {
        "page": 2,
        "total_pages": 7,
        "count": 2,
        "total": 340,
        "truncated": True,
    }


@respx.mock
async def test_total_is_the_result_set_and_count_is_this_page():
    """The two numbers mean different things; v1 could only report the second one.

    ``count`` is the rows in this response and ``total`` is the size of the whole result
    set. Asserting them together is the point — a regression that sourced ``total`` from
    ``len(items)`` would satisfy either assertion on its own.
    """
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(200, json=envelope([{"id": 1}], total=517, total_pages=259))
    )
    out = await client.request("GET", "/tasks", "t", params={"per_page": 1})
    assert out["pagination"]["count"] == 1
    assert out["pagination"]["total"] == 517


@respx.mock
async def test_single_page_list_is_returned_unwrapped():
    """The envelope must not become the default shape — it signals truncation only."""
    respx.get(f"{BASE}/labels").mock(
        return_value=httpx.Response(200, json=envelope([{"id": 1}], total=1))
    )
    out = await client.request("GET", "/labels", "t")
    assert out == [{"id": 1}]


@respx.mock
async def test_empty_list_is_returned_as_empty_list():
    """``items: null`` is what v2 sends for no matches; it must not reach a caller as None."""
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={"items": None, "total": 0, "page": 1, "per_page": 1, "total_pages": 0},
        )
    )
    assert await client.request("GET", "/tasks", "t") == []


@respx.mock
async def test_page_is_read_from_the_body_not_the_request():
    """v2 states the page it served; v1 could only be told what we asked for.

    The distinction matters when Vikunja serves something other than the requested page —
    reporting the requested one would then describe a response that was never sent.
    """
    respx.get(f"{BASE}/tasks/5/comments").mock(
        return_value=httpx.Response(200, json=envelope([{"id": 1}], page=3, total_pages=7))
    )
    out = await client.request("GET", "/tasks/5/comments", "t", params={"page": 99})
    assert out["pagination"]["page"] == 3


@respx.mock
async def test_single_resource_is_never_wrapped():
    """A task body has no total_pages, so it cannot be mistaken for a list response."""
    respx.get(f"{BASE}/tasks/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "title": "t", "items": ["not a list body"]})
    )
    out = await client.request("GET", "/tasks/5", "t")
    assert out == {"id": 5, "title": "t", "items": ["not a list body"]}


@respx.mock
async def test_bare_list_body_passes_through():
    respx.get(f"{BASE}/webhooks/events").mock(
        return_value=httpx.Response(200, json=["task.created", "task.done"])
    )
    out = await client.request("GET", "/webhooks/events", "t")
    assert out == ["task.created", "task.done"]


@respx.mock
async def test_unparsable_total_pages_degrades_to_unwrapped():
    """A malformed field must not turn a working list call into a crash."""
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1}], "total": 1, "total_pages": "not-a-number"}
        )
    )
    out = await client.request("GET", "/tasks", "t")
    assert out == [{"id": 1}]


@respx.mock
async def test_unparsable_page_falls_back_to_one():
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1}], "total": 9, "page": "abc", "total_pages": 7}
        )
    )
    out = await client.request("GET", "/tasks", "t")
    assert out["pagination"]["page"] == 1


@respx.mock
async def test_unparsable_total_falls_back_to_row_count():
    """A missing count is worth degrading over; it is not worth failing the read over."""
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1}, {"id": 2}], "total": None, "total_pages": 7}
        )
    )
    out = await client.request("GET", "/tasks", "t")
    assert out["pagination"]["total"] == 2


@respx.mock
async def test_unwrap_list_false_returns_the_envelope_untouched():
    """`_count_matching` reads `total` off a page that deliberately holds no rows.

    Reshaping would hand it the bare `items` list — empty — so the raw form is what makes
    a one-match bucket countable at all.
    """
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(200, json=envelope([], total=1, page=1_000_000, total_pages=1))
    )
    out = await client.request("GET", "/tasks", "t", unwrap_list=False)
    assert out["total"] == 1
    assert out["items"] == []
    assert "pagination" not in out
    assert "$schema" not in out


# --- $schema ---------------------------------------------------------------


@respx.mock
async def test_schema_link_is_stripped_from_a_single_resource():
    """v2 stamps every body with a link to its own JSON Schema. It is transport metadata."""
    respx.get(f"{BASE}/tasks/5").mock(
        return_value=httpx.Response(
            200,
            json={
                "$schema": "https://vikunja.test/api/v2/schemas/Task.json",
                "id": 5,
                "title": "t",
            },
        )
    )
    assert await client.request("GET", "/tasks/5", "t") == {"id": 5, "title": "t"}


@respx.mock
async def test_schema_link_on_list_rows_is_left_alone():
    """Only the top level is touched — a row is data, and nothing here rewrites rows."""
    row = {"$schema": "https://vikunja.test/api/v2/schemas/Task.json", "id": 1}
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=envelope([row])))
    assert await client.request("GET", "/tasks", "t") == [row]


# --- errors (RFC 9457 problem+json) ---------------------------------------


@respx.mock
async def test_problem_json_detail_and_code_are_surfaced():
    respx.put(f"{BASE}/projects/9").mock(
        return_value=httpx.Response(
            403,
            json={
                "title": "Forbidden",
                "status": 403,
                "detail": "forbidden: not owner",
                "code": 403,
            },
        )
    )
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("PUT", "/projects/9", "t", json={"title": "x"})
    assert exc.value.status_code == 403
    assert "forbidden: not owner" in exc.value.message
    assert "403" in exc.value.message


@respx.mock
async def test_detail_is_preferred_over_title():
    """``title`` is the generic status name; on its own it tells an agent nothing."""
    respx.get(f"{BASE}/tasks").mock(
        return_value=httpx.Response(
            400,
            json={
                "title": "Bad Request",
                "status": 400,
                "detail": "The task field 'bogusfield' is invalid.",
                "code": 4016,
            },
        )
    )
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/tasks", "t", params={"filter": "bogusfield = 1"})
    assert "bogusfield" in exc.value.message
    assert not exc.value.message.startswith("Bad Request")


@respx.mock
async def test_title_is_used_when_there_is_no_detail():
    respx.get(f"{BASE}/tasks/1").mock(
        return_value=httpx.Response(404, json={"title": "Not Found", "status": 404})
    )
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/tasks/1", "t")
    assert "Not Found" in exc.value.message


@respx.mock
async def test_v1_shaped_message_is_still_read():
    """Some middleware still answers in v1's shape — the token check is one, observed live.

    ``GET /api/v2/projects/{p}/tasks/by-index/{i}`` with a token lacking the
    ``projects → tasks_by_index`` permission returns exactly this body on v2.5.0. Reading
    only ``detail`` would reduce it to an empty reason phrase.
    """
    respx.get(f"{BASE}/projects/7/tasks/by-index/509").mock(
        return_value=httpx.Response(
            401,
            json={
                "code": 11,
                "message": "missing, malformed, expired or otherwise invalid token provided",
            },
        )
    )
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/projects/7/tasks/by-index/509", "t")
    assert "invalid token" in exc.value.message


@respx.mock
async def test_non_json_error_falls_back_to_text():
    respx.get(f"{BASE}/tasks/1").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/tasks/1", "t")
    assert exc.value.status_code == 502
    assert "bad gateway" in exc.value.message


@respx.mock
async def test_error_body_that_is_valid_json_but_not_an_object_falls_back_to_text():
    """``.get`` on a list would raise, turning a failed call into an AttributeError."""
    respx.get(f"{BASE}/tasks/1").mock(return_value=httpx.Response(500, json=["boom"]))
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/tasks/1", "t")
    assert exc.value.status_code == 500
    assert "boom" in exc.value.message


@respx.mock
async def test_error_body_with_no_usable_text_falls_back_to_the_reason_phrase():
    respx.get(f"{BASE}/tasks/1").mock(return_value=httpx.Response(500, json={"status": 500}))
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/tasks/1", "t")
    assert exc.value.message


@respx.mock
async def test_empty_204_returns_ok_marker():
    """v2's DELETE contract: 204, no body. Unchanged from v1 — do not "fix" this."""
    respx.delete(f"{BASE}/tasks/5").mock(return_value=httpx.Response(204))
    assert await client.request("DELETE", "/tasks/5", "t") == {"ok": True}


@respx.mock
async def test_network_failure_becomes_api_error_status_zero():
    respx.get(f"{BASE}/user").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(VikunjaAPIError) as exc:
        await client.request("GET", "/user", "t")
    assert exc.value.status_code == 0
