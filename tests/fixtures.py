"""Task bodies whose **shape** was captured from a live Vikunja v2.3.0 API response.

Re-checked against ``/api/v2`` on Vikunja v2.5.0 on 2026-08-23: the task body itself is
unchanged between API versions — same field names, same ``snake_case``, same
null-vs-empty conventions, including the ``related_tasks`` dict-of-lists below. What
changed is the *list* wrapper around it, which is why :func:`paginated` gained ``total``
and the raw list bodies did not move.

Why this module exists rather than a hand-written dict per test: the Phase 1 assertion in
v0.5.0 is "no read path returns a bare ``index``". A hand-written mock that simply forgets
to include ``index`` cannot fail that assertion, so the suite would go green while the bug
shipped. The regression is only real if the fixture carries every field the live API
actually returns — including the ones being stripped.

**Shape is real; content is synthetic.** The key sets, the nesting, and the null-vs-empty
conventions below were transcribed from an actual ``GET /tasks?filter=...`` response on
2026-08-21. The titles, descriptions and usernames were replaced, because this is a public
repository and the live corpus is a private homelab tracker.

Three shape details that a hand-written fixture reliably gets wrong, and that the tests
depend on:

1. ``related_tasks`` is a **dict keyed by relation kind** (``{"related": [...]}``), not a
   flat list. A recursive strip has to walk dict -> list -> dict to reach the nested task.
2. A task nested under ``related_tasks`` carries its **own** ``index``. That is the
   deep-nesting case; stripping only the top level would leave it behind.
3. A nested task's ``identifier`` can be the **empty string** while its ``index`` is
   populated. So ``identifier`` is not a guaranteed stand-in for the ticket number on
   nested entries — ``id`` is the only field that is always meaningful there.
"""

from __future__ import annotations

import copy
from typing import Any


def _user(uid: int, username: str) -> dict[str, Any]:
    return {
        "id": uid,
        "name": "",
        "username": username,
        "created": "2026-01-01T12:00:00Z",
        "updated": "2026-01-02T12:00:00Z",
    }


def _label(lid: int, title: str, color: str) -> dict[str, Any]:
    return {
        "id": lid,
        "title": title,
        "description": "",
        "hex_color": color,
        "created_by": _user(4, "agent-two"),
        "created": "2026-01-03T16:55:21Z",
        "updated": "2026-01-03T16:55:21Z",
    }


# The nested task, as Vikunja inlines it under related_tasks. Note the populated `index`
# beside an *empty* `identifier` — both are live-observed, not an oversight here.
_NESTED_TASK: dict[str, Any] = {
    "id": 348,
    "title": "Umbrella tracker",
    "description": "<p>Parent of the task that links to it.</p>",
    "done": True,
    "done_at": "2026-02-11T21:08:09Z",
    "due_date": "0001-01-01T00:00:00Z",
    "reminders": None,
    "project_id": 7,
    "repeat_after": 0,
    "repeat_mode": 0,
    "priority": 3,
    "start_date": "0001-01-01T00:00:00Z",
    "end_date": "0001-01-01T00:00:00Z",
    "assignees": None,
    "labels": None,
    "hex_color": "",
    "percent_done": 0,
    "identifier": "",
    "index": 334,
    "related_tasks": None,
    "attachments": None,
    "cover_image_attachment_id": 0,
    "is_favorite": False,
    "created": "2026-02-03T11:13:47Z",
    "updated": "2026-02-11T21:08:09Z",
    "bucket_id": 0,
    "position": 0,
    "reactions": None,
    "created_by": None,
}

_TASK: dict[str, Any] = {
    "id": 361,
    "title": "Wire the audit log so mutating tools leave a trail",
    "description": "<h2>Context</h2>\n<p>A description long enough to be worth dropping.</p>",
    "done": True,
    "done_at": "2026-02-11T21:07:26Z",
    "due_date": "0001-01-01T00:00:00Z",
    "reminders": None,
    "project_id": 7,
    "repeat_after": 0,
    "repeat_mode": 0,
    "priority": 2,
    "start_date": "0001-01-01T00:00:00Z",
    "end_date": "0001-01-01T00:00:00Z",
    "assignees": None,
    "labels": [_label(35, "type:security", "b23c17"), _label(36, "agent-filed", "f4b400")],
    "hex_color": "",
    "percent_done": 0,
    "identifier": "#342",
    "index": 342,
    "related_tasks": {"related": [_NESTED_TASK]},
    "attachments": None,
    "cover_image_attachment_id": 0,
    "is_favorite": False,
    "created": "2026-02-05T00:17:04Z",
    "updated": "2026-02-11T21:07:35Z",
    "bucket_id": 0,
    "position": 0,
    "reactions": None,
    "created_by": _user(3, "agent-one"),
}


def task(**overrides: Any) -> dict[str, Any]:
    """A single task body, deep-copied so a mutating hook cannot poison other tests."""
    body = copy.deepcopy(_TASK)
    body.update(overrides)
    return body


def task_list(count: int = 3) -> list[dict[str, Any]]:
    """A bare list body, as Vikunja returns when the result fits on one page."""
    out = []
    for offset in range(count):
        out.append(task(id=361 + offset, index=342 + offset, identifier=f"#{342 + offset}"))
    return out


def paginated(
    items: list[dict[str, Any]],
    page: int = 1,
    total_pages: int = 4,
    total: int | None = None,
) -> dict[str, Any]:
    """The envelope ``client.request`` wraps a multi-page list body in.

    ``pagination`` is this server's own metadata, not a task. Every projection and strip
    has to leave it intact, or a caller loses the ability to tell page 1 from a complete
    answer.

    ``total`` is the size of the whole result set and is new in the v2 port — v1 exposed
    no such number. It defaults to a value consistent with ``total_pages`` rather than to
    ``len(items)``, so a fixture built from this helper cannot accidentally make the two
    interchangeable.
    """
    return {
        "items": items,
        "pagination": {
            "page": page,
            "total_pages": total_pages,
            "count": len(items),
            "total": len(items) * total_pages if total is None else total,
            "truncated": True,
        },
    }
