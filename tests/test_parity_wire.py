"""On-the-wire verb assertions for the new write tools.

``test_parity.py`` pins the verb at the ``request()`` boundary with a mock; this file goes
one layer deeper and asserts the real HTTP method/path that leaves httpx, via respx. This
is the acceptance-criterion check for Vikunja's PUT-creates / POST-updates idiom — a tool
that silently used the wrong verb would pass the mock test but fail here.

Only ``caller_token`` is patched (there is no real HTTP request context in a unit test);
``request`` runs for real so respx sees the traffic.
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from vikunja_mcp import server

BASE = "https://vikunja.test/api/v1"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


@respx.mock
async def test_team_create_puts_on_the_wire():
    route = respx.put(f"{BASE}/teams").mock(return_value=httpx.Response(200, json={"id": 1}))
    await _fn(server.team_create)(name="Platform")
    assert route.called
    assert route.calls.last.request.method == "PUT"
    assert route.calls.last.request.headers["authorization"] == "Bearer TOK"


@respx.mock
async def test_team_update_posts_on_the_wire():
    route = respx.post(f"{BASE}/teams/3").mock(return_value=httpx.Response(200, json={"id": 3}))
    await _fn(server.team_update)(team_id=3, name="x")
    assert route.calls.last.request.method == "POST"


@respx.mock
async def test_project_share_create_puts_on_the_wire():
    route = respx.put(f"{BASE}/projects/5/shares").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    await _fn(server.project_share_create)(project_id=5, permission=1)
    assert route.calls.last.request.method == "PUT"


@respx.mock
async def test_project_team_add_puts_and_update_posts():
    add = respx.put(f"{BASE}/projects/5/teams").mock(return_value=httpx.Response(200, json={}))
    upd = respx.post(f"{BASE}/projects/5/teams/2").mock(return_value=httpx.Response(200, json={}))
    await _fn(server.project_team_add)(project_id=5, team_id=2, permission=1)
    await _fn(server.project_team_update)(project_id=5, team_id=2, permission=2)
    assert add.calls.last.request.method == "PUT"
    assert upd.calls.last.request.method == "POST"


@respx.mock
async def test_bucket_create_puts_and_move_posts():
    create = respx.put(f"{BASE}/projects/1/views/4/buckets").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )
    move = respx.post(f"{BASE}/projects/1/views/4/buckets/9/tasks").mock(
        return_value=httpx.Response(200, json={})
    )
    await _fn(server.bucket_create)(project_id=1, view_id=4, title="Doing")
    await _fn(server.task_bucket_move)(project_id=1, view_id=4, bucket_id=9, task_id=42)
    assert create.calls.last.request.method == "PUT"
    assert move.calls.last.request.method == "POST"


@respx.mock
async def test_task_relation_add_puts_on_the_wire():
    route = respx.put(f"{BASE}/tasks/7/relations").mock(return_value=httpx.Response(200, json={}))
    await _fn(server.task_relation_add)(task_id=7, other_task_id=8, relation_kind="subtask")
    assert route.calls.last.request.method == "PUT"


@respx.mock
async def test_task_assignee_add_puts_and_bulk_posts():
    add = respx.put(f"{BASE}/tasks/7/assignees").mock(return_value=httpx.Response(200, json={}))
    bulk = respx.post(f"{BASE}/tasks/7/assignees/bulk").mock(
        return_value=httpx.Response(200, json={})
    )
    await _fn(server.task_assignee_add)(task_id=7, user_id=3)
    await _fn(server.task_assignees_add_bulk)(task_id=7, user_ids=[3, 4])
    assert add.calls.last.request.method == "PUT"
    assert bulk.calls.last.request.method == "POST"


@respx.mock
async def test_tasks_bulk_update_posts_on_the_wire():
    route = respx.post(f"{BASE}/tasks/bulk").mock(return_value=httpx.Response(200, json={}))
    await _fn(server.tasks_bulk_update)(task_ids=[1, 2], values={"done": True})
    assert route.calls.last.request.method == "POST"


@respx.mock
async def test_attachment_upload_puts_multipart_on_the_wire():
    route = respx.put(f"{BASE}/tasks/7/attachments").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    await _fn(server.attachment_upload)(
        task_id=7, filename="a.txt", content_base64=base64.b64encode(b"hi").decode()
    )
    req = route.calls.last.request
    assert req.method == "PUT"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b"hi" in req.content
