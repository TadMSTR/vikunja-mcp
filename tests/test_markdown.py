"""Markdown rendering: GFM tables, leading ticket refs, idempotency, version parity."""

from __future__ import annotations

import tomllib  # stdlib since 3.11; requires-python is >=3.11
from pathlib import Path

import vikunja_mcp
from vikunja_mcp import server

# --- 6a: version parity ---------------------------------------------------


def test_dunder_version_matches_pyproject():
    """__init__.py and pyproject.toml drifted (0.2.1 vs 0.2.2) and shipped that way.

    Asserted against pyproject.toml rather than importlib.metadata deliberately: metadata
    reflects what was last *installed*, so an editable venv with stale metadata makes that
    comparison fail locally while passing in CI. This checks the repo invariant instead —
    the two files must agree — which is the drift that actually shipped.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert vikunja_mcp.__version__ == declared


# --- 6b: tables -----------------------------------------------------------


def test_markdown_table_renders_as_html_table():
    md = "| Agent | Tools |\n|-------|-------|\n| dev | 23 |"
    html = server._md_to_html(md)
    assert "<table>" in html
    assert "<th>Agent</th>" in html
    assert "<td>23</td>" in html
    assert "|" not in html  # not left as literal pipes


# --- 6b: leading ticket references ----------------------------------------


def test_leading_ticket_ref_is_not_parsed_as_heading():
    """`- #333 ...` rendered as <h1>333 ...</h1>; seen live on vikunja id 347."""
    html = server._md_to_html("- #333 the related ticket")
    assert "<h1>" not in html
    assert "#333" in html


def test_bare_leading_ticket_ref_is_escaped():
    html = server._md_to_html("#333 at the very start")
    assert "<h1>" not in html
    assert "#333" in html


def test_ordered_list_ticket_ref_is_escaped():
    html = server._md_to_html("1. #333 first")
    assert "<h1>" not in html
    assert "#333" in html


def test_real_heading_is_left_alone():
    """A `#` followed by a space is a genuine heading — do not break it."""
    html = server._md_to_html("# Context\n\nbody")
    assert "<h1>Context</h1>" in html


def test_inline_hash_is_untouched():
    html = server._md_to_html("C# and see #333 inline")
    assert "C#" in html
    assert "#333" in html
    assert "\\" not in html


def test_ticket_ref_inside_fenced_code_is_not_escaped():
    """A backslash inserted inside a fence would render literally and corrupt the block."""
    html = server._md_to_html("```bash\n#333 not a heading here\n```")
    assert "\\#333" not in html
    assert "#333" in html


def test_indented_code_block_is_not_escaped():
    html = server._md_to_html("    #333 indented code")
    assert "\\#333" not in html


# --- round trip -----------------------------------------------------------


def test_render_is_idempotent():
    """Descriptions read back from task_get are re-rendered on update — must be stable."""
    md = "## Context\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- #333 ref\n"
    once = server._md_to_html(md)
    assert server._md_to_html(once) == once
