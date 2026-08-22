"""`task_link_commit` — structured commit/PR backlinks on a ticket (vikunja#466).

The loop from ticket -> branch -> commit -> PR -> merged is manual on forge: the
connection survives only in prose someone remembered to write. githost-mcp knows the
commit and vikunja-mcp knows the ticket, and nothing joins them.

Explicitly **not** here: auto-transitioning ticket state from VCS events. That needs a
webhook listener and a state machine, and guessing wrong closes tickets that are not done.
Record the link; let a human or an explicit action close the ticket.
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import hooks, markers, server

from . import fixtures

PR_URL = "https://github.com/TadMSTR/vikunja-mcp/pull/14"
COMMIT_URL = "https://github.com/TadMSTR/vikunja-mcp/commit/23cea98"


@pytest.fixture(autouse=True)
def _builtins():
    hooks.clear_hooks()
    server.register_builtin_hooks()
    yield
    hooks.clear_hooks()


@pytest.fixture
def wire(monkeypatch):
    """Serve a task on GET and snapshot the body each POST actually sent.

    The snapshot is the point. A mock records a *reference* to the json it was called
    with, `_apply_task_update` returns that same dict as the tool's result, and the
    marker-strip after-hook then rewrites it in place — so reading `await_args_list`
    directly shows a "request" that the response hook has already edited, and every write
    assertion here silently checks post-strip state instead of what went upstream.
    """

    def _install(stored_description="<p>body</p>"):
        task = fixtures.task(id=361, description=stored_description)
        sent: list[tuple[str, dict]] = []

        async def _dispatch(method, path, *args, **kwargs):
            sent.append((method, copy.deepcopy(kwargs.get("json") or {})))
            if method == "GET":
                # `/tasks` is the collection the "#334" resolver filters over and it
                # expects a list; `/tasks/{id}` is the single body. Serving a dict for
                # both makes ticket-number resolution look broken when it is not.
                return [dict(task)] if path.rstrip("/").endswith("/tasks") else dict(task)
            return kwargs.get("json", {})

        mock = AsyncMock(side_effect=_dispatch)
        mock.sent = sent
        monkeypatch.setattr(server, "request", mock)
        monkeypatch.setattr(server, "caller_token", lambda: "TOK")
        return mock

    return _install


def _written(mock):
    """The description the last POST actually sent upstream."""
    return [body for method, body in mock.sent if method == "POST"][-1]["description"]


# --- recording a link -----------------------------------------------------


async def test_a_link_is_recorded_as_a_marker(wire):
    mock = wire()
    await server.task_link_commit(361, "pr", PR_URL)
    assert markers.parse(_written(mock))["ref"] == [f"pr{markers.REF_DELIMITER}{PR_URL}"]


async def test_the_ticket_body_is_not_rewritten(wire):
    """Requirement 4: appending metadata must not touch the content a human wrote."""
    body = "<p>one</p>\n<h2>heading</h2>\n<p>two</p>"
    mock = wire(body)
    await server.task_link_commit(361, "pr", PR_URL)
    assert markers.strip(_written(mock)) == body


async def test_a_second_link_does_not_clobber_the_first(wire):
    """The whole point of vikunja#466 requirement 4 — and a lost backlink is silent."""
    stored = markers.append("<p>body</p>", "ref", f"pr{markers.REF_DELIMITER}{PR_URL}")
    mock = wire(stored)
    await server.task_link_commit(361, "commit", COMMIT_URL)
    assert markers.parse(_written(mock))["ref"] == [
        f"pr{markers.REF_DELIMITER}{PR_URL}",
        f"commit{markers.REF_DELIMITER}{COMMIT_URL}",
    ]


async def test_linking_the_same_ref_twice_is_idempotent(wire):
    stored = markers.append("<p>body</p>", "ref", f"pr{markers.REF_DELIMITER}{PR_URL}")
    mock = wire(stored)
    await server.task_link_commit(361, "pr", PR_URL)
    assert markers.parse(_written(mock))["ref"] == [f"pr{markers.REF_DELIMITER}{PR_URL}"]


async def test_a_link_survives_alongside_an_idempotency_key(wire):
    """Both features share one footer; neither may evict the other."""
    stored = markers.append("<p>body</p>", "idem", "k1")
    mock = wire(stored)
    await server.task_link_commit(361, "pr", PR_URL)
    parsed = markers.parse(_written(mock))
    assert parsed["idem"] == ["k1"]
    assert parsed["ref"] == [f"pr{markers.REF_DELIMITER}{PR_URL}"]


async def test_a_link_can_be_added_to_a_web_edited_ticket(wire):
    """The Phase 0 case. A written-form-only parser drops the existing key here."""
    mock = wire("<p>body</p><hr><p>vikunja-mcp: idem=k1</p>")
    await server.task_link_commit(361, "pr", PR_URL)
    parsed = markers.parse(_written(mock))
    assert parsed["idem"] == ["k1"]
    assert parsed["ref"] == [f"pr{markers.REF_DELIMITER}{PR_URL}"]


async def test_a_ticket_number_is_accepted_as_the_task_id(wire):
    """An agent linking a commit has the ticket number in hand, not the global id."""
    mock = wire()
    await server.task_link_commit("#334", "pr", PR_URL)
    # The ref hook resolves "#334" upstream before any write lands.
    assert [m for m, _ in mock.sent if m == "POST"]


# --- ref_url is agent-supplied and a human will click it ------------------


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("javascript:alert(1)", id="javascript"),
        pytest.param("data:text/html;base64,PHNjcmlwdD4=", id="data"),
        pytest.param("http://example.com/x", id="plain-http"),
        pytest.param("ftp://example.com/x", id="ftp"),
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("https://", id="no-host"),
        pytest.param("https://localhost/x", id="dotless-host"),
        pytest.param("not a url", id="not-a-url"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_ref_url_that_is_not_a_plausible_https_link_is_refused(wire, url):
    """Stored-link injection, not SSRF — nothing here fetches the URL, but a human clicks
    it. Refused before any read or write, so a bad link costs nothing."""
    mock = wire()
    with pytest.raises(ValueError):
        await server.task_link_commit(361, "pr", url)
    assert mock.sent == []


async def test_a_ref_url_containing_the_delimiter_is_refused(wire):
    """The stored form is `type|url`, so a `|` in the URL would split into a bogus ref."""
    wire()
    with pytest.raises(ValueError):
        await server.task_link_commit(361, "pr", f"https://example.com/a{markers.REF_DELIMITER}b")


async def test_a_ref_url_with_whitespace_is_refused(wire):
    """A space would forge a sibling token in the space-separated marker line."""
    wire()
    with pytest.raises(ValueError):
        await server.task_link_commit(361, "pr", "https://example.com/a b")


@pytest.mark.parametrize(
    "ref_type",
    [
        pytest.param("has space", id="space"),
        pytest.param("UPPER", id="uppercase"),
        pytest.param(f"a{markers.REF_DELIMITER}b", id="delimiter"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_malformed_ref_type_is_refused(wire, ref_type):
    wire()
    with pytest.raises(ValueError):
        await server.task_link_commit(361, ref_type, PR_URL)


async def test_an_ordinary_https_url_is_accepted(wire):
    """Control: the guard must not reject the links this exists to store."""
    mock = wire()
    # A self-hosted Gitea, which is the case most likely to be over-rejected by a guard
    # written against github.com. Host is deliberately generic — this repo is public.
    await server.task_link_commit(361, "pr", "https://gitea.example.com/org/repo/pulls/3")
    assert markers.parse(_written(mock))["ref"]


# --- reading them back ----------------------------------------------------


@pytest.fixture
def serve(monkeypatch):
    def _install(body):
        mock = AsyncMock(return_value=body)
        monkeypatch.setattr(server, "request", mock)
        monkeypatch.setattr(server, "caller_token", lambda: "TOK")
        return mock

    return _install


async def test_task_get_exposes_linked_refs_structurally(serve):
    """An agent should get `linked_refs`, not raw description text to regex itself."""
    stored = markers.append("<p>body</p>", "ref", f"pr{markers.REF_DELIMITER}{PR_URL}")
    stored = markers.append(stored, "ref", f"commit{markers.REF_DELIMITER}{COMMIT_URL}")
    serve(fixtures.task(id=361, description=stored))
    result = await server.task_get(361)
    assert result["linked_refs"] == [
        {"ref_type": "pr", "ref_url": PR_URL},
        {"ref_type": "commit", "ref_url": COMMIT_URL},
    ]


async def test_linked_refs_come_with_a_clean_description(serve):
    stored = markers.append("<p>body</p>", "ref", f"pr{markers.REF_DELIMITER}{PR_URL}")
    serve(fixtures.task(id=361, description=stored))
    result = await server.task_get(361)
    assert result["description"] == "<p>body</p>"
    assert "vikunja-mcp:" not in result["description"]


async def test_verbose_exposes_linked_refs_too(serve):
    stored = markers.append("<p>body</p>", "ref", f"pr{markers.REF_DELIMITER}{PR_URL}")
    serve(fixtures.task(id=361, description=stored))
    result = await server.task_get(361, verbose=True)
    assert result["linked_refs"] == [{"ref_type": "pr", "ref_url": PR_URL}]


async def test_linked_refs_is_absent_when_there_are_none(serve):
    """Omitted rather than `[]`: an empty list reads as 'checked, and there are none',
    which is a stronger claim than 'this ticket has no marker'."""
    serve(fixtures.task(id=361, description="<p>body</p>"))
    result = await server.task_get(361)
    assert "linked_refs" not in result


async def test_an_idempotency_key_alone_produces_no_linked_refs(serve):
    serve(fixtures.task(id=361, description=markers.append("<p>b</p>", "idem", "k1")))
    result = await server.task_get(361)
    assert "linked_refs" not in result


async def test_a_malformed_ref_value_is_skipped_not_guessed_at(serve):
    """A hand-edited footer degrades to 'no link', never to half a link."""
    serve(fixtures.task(id=361, description="<p>b</p>\n<hr>\n<p>vikunja-mcp: ref=nodelimiter</p>"))
    result = await server.task_get(361)
    assert "linked_refs" not in result
