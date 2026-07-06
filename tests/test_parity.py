"""Verb + path + body mapping for the Phase B API-parity tools.

Same capture strategy as ``test_server.py``: ``request`` is replaced with a mock so the
exact (method, path) — and body where it matters — is pinned without touching the network.
Vikunja's PUT-creates / POST-updates idiom is easy to get wrong, so every write tool is
asserted here and reinforced on the wire in ``test_parity_wire.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import server


@pytest.fixture(autouse=True)
def _patch_calls(monkeypatch):
    mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    return mock


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def call(tool, **kwargs):
    return await _fn(tool)(**kwargs)


# --- teams ----------------------------------------------------------------


async def test_team_create_uses_put(_patch_calls):
    await call(server.team_create, name="Platform")
    assert _patch_calls.call_args.args[:2] == ("PUT", "/teams")
    assert _patch_calls.call_args.kwargs["json"] == {"name": "Platform"}


async def test_team_update_uses_post(_patch_calls):
    await call(server.team_update, team_id=3, name="Renamed")
    assert _patch_calls.call_args.args[:2] == ("POST", "/teams/3")
    assert _patch_calls.call_args.kwargs["json"] == {"name": "Renamed"}


async def test_team_member_add_sends_username_and_admin(_patch_calls):
    await call(server.team_member_add, team_id=3, username="agent-writer", admin=True)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/teams/3/members")
    assert _patch_calls.call_args.kwargs["json"] == {"username": "agent-writer", "admin": True}


async def test_team_member_remove_uses_username_path(_patch_calls):
    await call(server.team_member_remove, team_id=3, username="agent-writer")
    assert _patch_calls.call_args.args[:2] == ("DELETE", "/teams/3/members/agent-writer")


# --- project sharing ------------------------------------------------------


async def test_project_team_add_uses_put_with_permission(_patch_calls):
    await call(server.project_team_add, project_id=5, team_id=2, permission=1)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/5/teams")
    assert _patch_calls.call_args.kwargs["json"] == {"team_id": 2, "permission": 1}


async def test_project_team_update_uses_post(_patch_calls):
    await call(server.project_team_update, project_id=5, team_id=2, permission=2)
    assert _patch_calls.call_args.args[:2] == ("POST", "/projects/5/teams/2")
    assert _patch_calls.call_args.kwargs["json"] == {"permission": 2}


async def test_project_user_add_uses_put(_patch_calls):
    await call(server.project_user_add, project_id=5, username="agent-research", permission=0)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/5/users")
    assert _patch_calls.call_args.kwargs["json"] == {"username": "agent-research", "permission": 0}


async def test_project_share_create_uses_put_and_drops_empty(_patch_calls):
    await call(server.project_share_create, project_id=5, permission=1)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/5/shares")
    # password/name omitted → not in body; permission + sharing_type present.
    assert _patch_calls.call_args.kwargs["json"] == {"permission": 1, "sharing_type": 0}


# --- buckets / kanban -----------------------------------------------------


async def test_bucket_create_uses_put(_patch_calls):
    await call(server.bucket_create, project_id=1, view_id=4, title="In Progress")
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/1/views/4/buckets")
    assert _patch_calls.call_args.kwargs["json"] == {"title": "In Progress"}


async def test_bucket_update_uses_post(_patch_calls):
    await call(server.bucket_update, project_id=1, view_id=4, bucket_id=9, limit=5)
    assert _patch_calls.call_args.args[:2] == ("POST", "/projects/1/views/4/buckets/9")
    assert _patch_calls.call_args.kwargs["json"] == {"limit": 5}


async def test_task_bucket_move_posts_task_and_bucket(_patch_calls):
    await call(server.task_bucket_move, project_id=1, view_id=4, bucket_id=9, task_id=42)
    assert _patch_calls.call_args.args[:2] == ("POST", "/projects/1/views/4/buckets/9/tasks")
    assert _patch_calls.call_args.kwargs["json"] == {"task_id": 42, "bucket_id": 9}


# --- views ----------------------------------------------------------------


async def test_view_create_uses_put(_patch_calls):
    await call(server.view_create, project_id=1, title="Board", view_kind="kanban")
    assert _patch_calls.call_args.args[:2] == ("PUT", "/projects/1/views")
    assert _patch_calls.call_args.kwargs["json"] == {"title": "Board", "view_kind": "kanban"}


async def test_view_update_uses_post_with_done_bucket(_patch_calls):
    await call(server.view_update, project_id=1, view_id=4, done_bucket_id=9)
    assert _patch_calls.call_args.args[:2] == ("POST", "/projects/1/views/4")
    assert _patch_calls.call_args.kwargs["json"] == {"done_bucket_id": 9}


# --- assignees ------------------------------------------------------------


async def test_task_assignee_add_uses_put(_patch_calls):
    await call(server.task_assignee_add, task_id=7, user_id=3)
    assert _patch_calls.call_args.args[:2] == ("PUT", "/tasks/7/assignees")
    assert _patch_calls.call_args.kwargs["json"] == {"user_id": 3}


async def test_task_assignees_bulk_wraps_ids(_patch_calls):
    await call(server.task_assignees_add_bulk, task_id=7, user_ids=[3, 4])
    assert _patch_calls.call_args.args[:2] == ("POST", "/tasks/7/assignees/bulk")
    assert _patch_calls.call_args.kwargs["json"] == {"assignees": [{"id": 3}, {"id": 4}]}


async def test_task_assignee_remove_uses_userid_path(_patch_calls):
    await call(server.task_assignee_remove, task_id=7, user_id=3)
    assert _patch_calls.call_args.args[:2] == ("DELETE", "/tasks/7/assignees/3")


# --- relations ------------------------------------------------------------


async def test_task_relation_add_uses_put(_patch_calls):
    await call(server.task_relation_add, task_id=7, other_task_id=8, relation_kind="subtask")
    assert _patch_calls.call_args.args[:2] == ("PUT", "/tasks/7/relations")
    assert _patch_calls.call_args.kwargs["json"] == {
        "other_task_id": 8,
        "relation_kind": "subtask",
    }


async def test_task_relation_remove_encodes_kind_and_other(_patch_calls):
    await call(server.task_relation_remove, task_id=7, relation_kind="blocking", other_task_id=8)
    assert _patch_calls.call_args.args[:2] == ("DELETE", "/tasks/7/relations/blocking/8")


# --- reminders ------------------------------------------------------------


async def test_task_reminders_set_wraps_timestamps(_patch_calls):
    await call(server.task_reminders_set, task_id=7, reminders=["2026-07-10T09:00:00Z"])
    assert _patch_calls.call_args.args[:2] == ("POST", "/tasks/7")
    assert _patch_calls.call_args.kwargs["json"] == {
        "reminders": [{"reminder": "2026-07-10T09:00:00Z"}]
    }


# --- attachments ----------------------------------------------------------


async def test_attachment_upload_decodes_and_sends_multipart(_patch_calls):
    import base64

    await call(
        server.attachment_upload,
        task_id=7,
        filename="log.txt",
        content_base64=base64.b64encode(b"hello").decode(),
    )
    assert _patch_calls.call_args.args[:2] == ("PUT", "/tasks/7/attachments")
    files = _patch_calls.call_args.kwargs["files"]
    assert files["files"][0] == "log.txt"
    assert files["files"][1] == b"hello"


# --- bulk -----------------------------------------------------------------


async def test_tasks_bulk_update_sends_ids_and_values(_patch_calls):
    await call(server.tasks_bulk_update, task_ids=[1, 2, 3], values={"done": True})
    assert _patch_calls.call_args.args[:2] == ("POST", "/tasks/bulk")
    assert _patch_calls.call_args.kwargs["json"] == {
        "task_ids": [1, 2, 3],
        "values": {"done": True},
    }


# --- read/delete regression pins ------------------------------------------


@pytest.mark.parametrize(
    ("tool", "kwargs", "expected"),
    [
        (server.team_list, {}, ("GET", "/teams")),
        (server.team_get, {"team_id": 3}, ("GET", "/teams/3")),
        (server.team_delete, {"team_id": 3}, ("DELETE", "/teams/3")),
        (
            server.team_member_toggle_admin,
            {"team_id": 3, "user_id": 5},
            ("POST", "/teams/3/members/5/admin"),
        ),
        (server.project_team_list, {"project_id": 1}, ("GET", "/projects/1/teams")),
        (
            server.project_team_remove,
            {"project_id": 1, "team_id": 2},
            ("DELETE", "/projects/1/teams/2"),
        ),
        (server.project_user_list, {"project_id": 1}, ("GET", "/projects/1/users")),
        (
            server.project_user_update,
            {"project_id": 1, "user_id": 2, "permission": 1},
            ("POST", "/projects/1/users/2"),
        ),
        (
            server.project_user_remove,
            {"project_id": 1, "user_id": 2},
            ("DELETE", "/projects/1/users/2"),
        ),
        (server.project_share_list, {"project_id": 1}, ("GET", "/projects/1/shares")),
        (
            server.project_share_get,
            {"project_id": 1, "share_id": 4},
            ("GET", "/projects/1/shares/4"),
        ),
        (
            server.project_share_delete,
            {"project_id": 1, "share_id": 4},
            ("DELETE", "/projects/1/shares/4"),
        ),
        (server.view_list, {"project_id": 1}, ("GET", "/projects/1/views")),
        (server.view_get, {"project_id": 1, "view_id": 4}, ("GET", "/projects/1/views/4")),
        (server.view_delete, {"project_id": 1, "view_id": 4}, ("DELETE", "/projects/1/views/4")),
        (
            server.bucket_list,
            {"project_id": 1, "view_id": 4},
            ("GET", "/projects/1/views/4/buckets"),
        ),
        (
            server.bucket_delete,
            {"project_id": 1, "view_id": 4, "bucket_id": 9},
            ("DELETE", "/projects/1/views/4/buckets/9"),
        ),
        (server.task_assignee_list, {"task_id": 7}, ("GET", "/tasks/7/assignees")),
        (server.attachment_list, {"task_id": 7}, ("GET", "/tasks/7/attachments")),
        (
            server.attachment_delete,
            {"task_id": 7, "attachment_id": 2},
            ("DELETE", "/tasks/7/attachments/2"),
        ),
    ],
)
async def test_verb_path_mappings(_patch_calls, tool, kwargs, expected):
    await call(tool, **kwargs)
    assert _patch_calls.call_args.args[:2] == expected
