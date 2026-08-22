"""Structured metadata markers in ticket descriptions (vikunja#465, vikunja#466).

The separator-form cases below are not hypothetical. They were measured in the Phase 0
gate on 2026-08-22 against the live instance: TipTap re-serialises a description without
any inter-block whitespace when a human edits and saves a ticket in the web UI, so the
`\\n<hr>\\n<p>` this server writes comes back as `<hr><p>`. A parser anchored on the
written form matches nothing on any ticket a human has ever opened.
"""

from __future__ import annotations

import pytest

from vikunja_mcp import markers

# The three forms observed in the wild. See docs/markers.md for how each arises.
FRESH = "<p>body</p>\n<hr>\n<p>vikunja-mcp: idem=abc123</p>"
WEB_EDITED = "<p>body</p><hr><p>vikunja-mcp: idem=abc123</p>"
RERENDERED = "<p>body</p>\n<hr><p>vikunja-mcp: idem=abc123</p>"

ALL_FORMS = pytest.mark.parametrize(
    "html",
    [
        pytest.param(FRESH, id="fresh-write"),
        pytest.param(WEB_EDITED, id="after-web-edit"),
        pytest.param(RERENDERED, id="after-rerender"),
    ],
)


# --- parsing across every stored form -------------------------------------


@ALL_FORMS
def test_parse_finds_marker_in_every_separator_form(html):
    """The Phase 0 finding, as a test. A regex tied to `\\n<hr>\\n<p>` fails two of three."""
    assert markers.parse(html) == {"idem": ["abc123"]}


@ALL_FORMS
def test_strip_removes_marker_in_every_separator_form(html):
    assert markers.strip(html) == "<p>body</p>"


@ALL_FORMS
def test_strip_leaves_no_orphan_rule(html):
    """Stripping the paragraph but leaving its `<hr>` gives the body a trailing rule."""
    assert "<hr" not in markers.strip(html)


# --- the body is not collateral -------------------------------------------


def test_body_horizontal_rule_survives_strip():
    """Only the rule introducing the marker goes. A body's own `<hr>` is content."""
    html = "<p>one</p>\n<hr>\n<p>two</p>\n<hr>\n<p>vikunja-mcp: idem=k1</p>"
    stripped = markers.strip(html)
    assert stripped == "<p>one</p>\n<hr>\n<p>two</p>"
    assert stripped.count("<hr") == 1


def test_strip_is_a_noop_without_markers():
    html = "<p>an ordinary ticket</p>\n<hr>\n<p>with a rule in it</p>"
    assert markers.strip(html) == html


def test_parse_returns_empty_without_markers():
    assert markers.parse("<p>an ordinary ticket</p>") == {}


def test_parse_tolerates_none_and_empty():
    assert markers.parse(None) == {}
    assert markers.parse("") == {}
    assert markers.strip(None) is None
    assert markers.strip("") == ""


# --- lookalike text is not a marker ---------------------------------------


def test_marker_text_quoted_in_a_code_block_is_not_parsed():
    """A ticket *about* this feature quotes the format. That is content, not metadata.

    Anchoring on a bare `<p>` is what separates the two: markdown renders a fenced block
    to `<pre><code>`, so quoted marker text never reaches the parser as a marker.
    """
    html = (
        "<p>The footer looks like this:</p>\n"
        "<pre><code>vikunja-mcp: idem=example\n</code></pre>\n"
        "<p>...which is stripped on read.</p>"
    )
    assert markers.parse(html) == {}
    assert markers.strip(html) == html


def test_marker_prefix_mid_sentence_is_not_parsed():
    """`<p>` must *start* with the prefix — a mention inside prose is not a marker."""
    html = "<p>We store a vikunja-mcp: idem=x footer on each ticket.</p>"
    assert markers.parse(html) == {}
    assert markers.strip(html) == html


# --- append: additive, idempotent, non-destructive -------------------------


def test_append_adds_a_marker_to_a_plain_body():
    out = markers.append("<p>body</p>", "idem", "abc123")
    assert markers.parse(out) == {"idem": ["abc123"]}
    assert markers.strip(out) == "<p>body</p>"


def test_append_preserves_the_body_verbatim():
    """Requirement 4: adding a marker must never rewrite unrelated description content."""
    body = "<p>one</p>\n<h2>heading</h2>\n<ul>\n<li>a</li>\n</ul>"
    out = markers.append(body, "idem", "abc123")
    assert markers.strip(out) == body


def test_append_same_kind_and_value_twice_is_idempotent():
    once = markers.append("<p>body</p>", "idem", "abc123")
    twice = markers.append(once, "idem", "abc123")
    assert once == twice
    assert markers.parse(twice) == {"idem": ["abc123"]}


def test_append_second_ref_does_not_clobber_the_first():
    """vikunja#466 requirement 4. The failure mode is a silently lost backlink."""
    out = markers.append("<p>body</p>", "ref", "pr|https://example.com/pull/14")
    out = markers.append(out, "ref", "commit|https://example.com/commit/23cea98")
    assert markers.parse(out)["ref"] == [
        "pr|https://example.com/pull/14",
        "commit|https://example.com/commit/23cea98",
    ]


def test_append_keeps_kinds_side_by_side_on_one_line():
    """The plan calls for a compact provenance line, not a data dump."""
    out = markers.append("<p>body</p>", "idem", "abc123")
    out = markers.append(out, "ref", "pr|https://example.com/pull/14")
    assert markers.parse(out) == {
        "idem": ["abc123"],
        "ref": ["pr|https://example.com/pull/14"],
    }
    assert out.count("<hr") == 1
    assert out.count("vikunja-mcp:") == 1


@ALL_FORMS
def test_append_onto_an_existing_marker_works_in_every_form(html):
    """An agent linking a commit to a ticket a human has edited must not lose the key."""
    out = markers.append(html, "ref", "pr|https://example.com/pull/14")
    assert markers.parse(out) == {
        "idem": ["abc123"],
        "ref": ["pr|https://example.com/pull/14"],
    }
    assert markers.strip(out) == "<p>body</p>"


# --- forgery: a value may not manufacture a second marker ------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x ref=https://evil.example.com/", id="space-injects-second-token"),
        pytest.param("x\tref=y", id="tab"),
        pytest.param("x\nref=y", id="newline"),
        pytest.param("<script>", id="angle-brackets"),
        pytest.param("", id="empty"),
    ],
)
def test_append_rejects_a_value_that_could_forge_another_marker(value):
    """Tokens are space-separated, so a value carrying a space forges a sibling token.

    Caught on write rather than sanitised: an idempotency key that silently changed shape
    would look up nothing on the retry it exists to catch.
    """
    with pytest.raises(ValueError):
        markers.append("<p>body</p>", "idem", value)


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("has space", id="space"),
        pytest.param("UPPER", id="uppercase"),
        pytest.param("with=equals", id="equals"),
        pytest.param("", id="empty"),
    ],
)
def test_append_rejects_a_malformed_kind(kind):
    with pytest.raises(ValueError):
        markers.append("<p>body</p>", kind, "abc123")


def test_parse_ignores_a_token_without_a_value():
    """A hand-mangled footer degrades to 'no marker', never to a half-parsed one."""
    assert markers.parse("<p>vikunja-mcp: idem= ref=</p>") == {}


# --- the lookup key the idempotency path filters on ------------------------


def test_lookup_fragment_matches_what_is_written():
    """`task_create` filters on this substring. If it drifts from `append`, every lookup
    misses and every retry double-files — the exact bug vikunja#465 exists to prevent."""
    out = markers.append("<p>body</p>", "idem", "abc123")
    assert markers.lookup_fragment("idem", "abc123") in out


# ==========================================================================
# Strip-on-read: markers are machinery, and no read path may surface them
# ==========================================================================

from unittest.mock import AsyncMock  # noqa: E402

from vikunja_mcp import hooks, server  # noqa: E402

from . import fixtures  # noqa: E402

MARKED_BODY = "<p>real ticket content</p>\n<hr>\n<p>vikunja-mcp: idem=abc123</p>"
WEB_EDITED_BODY = "<p>real ticket content</p><hr><p>vikunja-mcp: idem=abc123</p>"


@pytest.fixture(autouse=True)
def _builtins():
    """Exercise the shipped guardrails, not a bare tool — that is the thing under test.

    Also required for isolation: `test_hooks.py` and `test_contrib_audit.py` clear the
    hook registry on teardown without restoring it, so a module that merely *assumes* the
    import-time registration passes alone and fails in the full suite. Matches the
    `_builtins` fixture in `test_task_refs.py`.
    """
    hooks.clear_hooks()
    server.register_builtin_hooks()
    yield
    hooks.clear_hooks()


@pytest.fixture
def upstream(monkeypatch):
    """Replace request() so a chosen body comes back from the wire."""

    def _serve(body):
        mock = AsyncMock(return_value=body)
        monkeypatch.setattr(server, "request", mock)
        monkeypatch.setattr(server, "caller_token", lambda: "TOK")
        return mock

    return _serve


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(MARKED_BODY, id="fresh-write"),
        pytest.param(WEB_EDITED_BODY, id="after-web-edit"),
    ],
)
async def test_task_get_strips_the_marker(upstream, stored):
    upstream(fixtures.task(description=stored))
    result = await server.task_get(361)
    assert result["description"] == "<p>real ticket content</p>"
    assert "vikunja-mcp:" not in result["description"]


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(MARKED_BODY, id="fresh-write"),
        pytest.param(WEB_EDITED_BODY, id="after-web-edit"),
    ],
)
async def test_task_get_verbose_strips_the_marker_too(upstream, stored):
    """`verbose` restores payload, not machinery — the v0.5.0 `index` principle."""
    upstream(fixtures.task(description=stored))
    result = await server.task_get(361, verbose=True)
    assert "vikunja-mcp:" not in result["description"]


async def test_nested_related_task_description_is_stripped(upstream):
    """A marker on an inlined related task leaks through a top-level-only strip."""
    body = fixtures.task(
        description=MARKED_BODY,
        related_tasks={"related": [fixtures.task(id=348, description=MARKED_BODY)]},
    )
    upstream(body)
    result = await server.task_get(361, verbose=True)
    nested = result["related_tasks"]["related"][0]
    assert "vikunja-mcp:" not in nested["description"]


async def test_task_create_response_does_not_echo_the_marker(upstream):
    """The idempotency path writes a marker; the caller must not be handed it back."""
    upstream(fixtures.task(description=MARKED_BODY))
    result = await server.task_create(project_id=7, title="t")
    assert "vikunja-mcp:" not in result["description"]


async def test_task_update_response_does_not_echo_the_marker(upstream):
    """task_update returns a full task body, marker and all, unless stripped."""
    upstream(fixtures.task(description=MARKED_BODY))
    result = await server.task_update(361, priority=2)
    assert "vikunja-mcp:" not in result["description"]


async def test_task_list_rows_carry_no_marker(upstream):
    """Summary rows drop `description` entirely, but assert it rather than assume it."""
    upstream(fixtures.paginated([fixtures.task(description=MARKED_BODY)]))
    result = await server.task_list()
    assert "vikunja-mcp:" not in str(result["items"])
    assert result["pagination"]["truncated"] is True


async def test_search_verbose_rows_are_stripped(upstream):
    """`task_search(verbose=True)` returns full bodies — the widest leak surface."""
    upstream(fixtures.paginated([fixtures.task(description=MARKED_BODY)]))
    result = await server.task_search("anything", verbose=True)
    assert "vikunja-mcp:" not in str(result["items"])


async def test_an_unmarked_description_is_returned_untouched(upstream):
    """Control: the strip must not rewrite ordinary ticket bodies."""
    body = "<p>one</p>\n<hr>\n<p>two</p>"
    upstream(fixtures.task(description=body))
    result = await server.task_get(361)
    assert result["description"] == body


# ==========================================================================
# Lookup values are filter operands, so they carry a stricter charset
# ==========================================================================


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('abc"', id="double-quote"),
        pytest.param('x"||done = true||"', id="filter-breakout"),
        pytest.param("a%b", id="like-wildcard"),
        pytest.param("100%", id="trailing-wildcard"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("-leading", id="leading-punctuation"),
    ],
)
def test_lookup_value_rejects_filter_metacharacters(value):
    """A lookup value is interpolated into a Vikunja filter, so it is an injection site.

    The v0.7.0 audit found a caller filter escaping its enclosing predicates because
    Vikunja evaluates left-to-right; a key carrying `"` or `||` is the same bug reached
    through a different argument. `%` matters for a second reason — it is `like`'s
    wildcard, so `100%` would silently match keys it was never meant to.

    Enforced as a charset rather than an escaping routine, matching the reasoning in
    contrib/duplicate_check: a value that *cannot* contain the syntax needs no escaping.
    """
    with pytest.raises(ValueError):
        markers.validate_lookup_value(value)
    with pytest.raises(ValueError):
        markers.lookup_fragment("idem", value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("abc123", id="alnum"),
        pytest.param("build-2026-08-22", id="hyphens"),
        pytest.param("task_create.retry", id="underscore-and-dot"),
        pytest.param("9c07fab3-b9f4-4c0d", id="uuid-fragment"),
    ],
)
def test_lookup_value_accepts_ordinary_keys(value):
    """Control: the charset must still admit the keys callers actually generate."""
    markers.validate_lookup_value(value)
    assert markers.lookup_fragment("idem", value) == f"idem={value}"


def test_ref_values_may_still_hold_url_syntax():
    """`ref` is never a filter operand, so it keeps the permissive value charset.

    Tightening every value to the lookup charset would make it impossible to store the
    URLs vikunja#466 exists to store.
    """
    url = "pr|https://github.com/TadMSTR/vikunja-mcp/pull/14?x=1&y=2"
    out = markers.append("<p>body</p>", "ref", url)
    assert markers.parse(out)["ref"] == [url]
