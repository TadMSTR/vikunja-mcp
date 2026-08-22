"""Structured metadata markers stored in a ticket's description (vikunja#465, #466).

Vikunja has no field for metadata a tool wants to keep on a ticket — an idempotency key,
a commit backlink. This module owns the one convention both features use, so neither
reimplements it.

**Where the marker lives, and why.** A visible footer in the description::

    <body>
    <hr>
    <p>vikunja-mcp: idem=abc123 ref=pr|https://github.com/o/r/pull/14</p>

Two alternatives were measured against the live API on 2026-08-22 and closed:

- An **HTML comment** never leaves this process. Every description goes through
  ``server._md_to_html``, which ends in ``nh3.clean()``, and nh3 strips comments by
  default. Enabling them means ``strip_comments=False`` on a sanitizer carrying an
  explicit ``SECURITY[control]`` (server.py) — a security decision, not a storage one.
- A **task comment** has no cheap lookup: ``filter=comments like "%...%"`` returns
  ``400 The task field 'comments' is invalid``, so finding a marker would cost one call
  per task instead of one call total.

The description survives both, and ``filter=description like "%...%"`` retrieves it
server-side in a single request (verified with a nonsense-string control returning empty,
so the filter is genuinely applied rather than silently ignored).

**Why the parser is whitespace-tolerant.** This server writes ``\\n<hr>\\n<p>``. TipTap —
Vikunja's web editor — parses stored HTML into a ProseMirror document and re-serialises it
on save *without inter-block whitespace*, so the moment a human opens a ticket and saves
it, the same marker reads ``<hr><p>``. Measured in the Phase 0 gate on 2026-08-22: a probe
task's newline count went 4 -> 0 across an edit that touched only one word of prose. Three
forms therefore exist in the corpus:

===========================  ==================================================
``\\n<hr>\\n<p>marker</p>``    written by this server
``<hr><p>marker</p>``         after any human web-UI edit
``\\n<hr><p>marker</p>``      after a web edit, then an agent re-render
===========================  ==================================================

A parser anchored on the written form matches none of a human-edited ticket's markers.
Nothing would raise: strip-on-read would leak markers into agent-visible text and
``linked_refs`` would parse back empty, both silently. Hence :data:`_MARKER_BLOCK` treats
all whitespace around the rule as optional, and the tests assert every form.
"""

from __future__ import annotations

import re

#: The namespace every marker carries. Prevents a collision with a future third use, and
#: means a foreign tool's footer is never mistaken for one of ours.
PREFIX = "vikunja-mcp:"

# A marker paragraph, with the rule that introduces it and any whitespace around both.
#
# `<p>` must *start* with the prefix (after optional whitespace): a sentence mentioning the
# format mid-prose is content, not metadata. Anchoring on a bare `<p>` is also what keeps
# quoted examples out — markdown renders a fenced block to `<pre><code>`, which never
# matches here, so a ticket documenting this feature does not parse as using it.
#
# The payload cannot contain `<`, which is what stops the match running past `</p>` into
# the rest of the body.
_MARKER_BLOCK = re.compile(
    r"(?:\s*<hr\s*/?>)?\s*<p>\s*" + re.escape(PREFIX) + r"(?P<payload>[^<]*)</p>",
    re.IGNORECASE,
)

#: A marker kind: lowercase, no separator characters that appear in the wire format.
_KIND = re.compile(r"\A[a-z][a-z0-9_]*\Z")

#: A marker value: anything that cannot forge a sibling token or escape the paragraph.
#: Whitespace is excluded because tokens are space-separated, and `<>` because the payload
#: is bounded by them.
_VALUE = re.compile(r"\A[^\s<>]+\Z")


def _validate(kind: str, value: str) -> None:
    """Reject a kind or value that could forge a second marker. Raises ``ValueError``.

    Rejected on write rather than sanitised. A key that silently changed shape between
    write and lookup would miss on exactly the retry it exists to catch — the caller needs
    to know now, not to discover a duplicate ticket later.
    """
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise ValueError(
            f"marker kind must match {_KIND.pattern!r} (lowercase, no spaces or '='); got {kind!r}"
        )
    if not isinstance(value, str) or not _VALUE.match(value):
        raise ValueError(
            f"marker value must be non-empty and free of whitespace and '<>'; got {value!r}"
        )


def parse(html: str | None) -> dict[str, list[str]]:
    """Every marker in ``html``, as ``{kind: [value, ...]}`` in written order.

    Repeats are kept: a ticket carries one ``idem`` but any number of ``ref`` backlinks,
    and collapsing those to the last one would silently drop every earlier link.

    A malformed token (no ``=``, empty value, a kind or value that would not have been
    accepted on write) is skipped rather than guessed at, so a hand-mangled footer degrades
    to "no marker" instead of to a half-parsed one.
    """
    if not html:
        return {}
    out: dict[str, list[str]] = {}
    for match in _MARKER_BLOCK.finditer(html):
        for token in match.group("payload").split():
            kind, sep, value = token.partition("=")
            if not sep:
                continue
            try:
                _validate(kind, value)
            except ValueError:
                continue
            out.setdefault(kind, []).append(value)
    return out


def strip(html: str | None) -> str | None:
    """``html`` with every marker block removed, including the rule introducing it.

    Markers are machinery, not content — an agent reading a ticket should never see them.
    The rule goes with the paragraph: leaving it behind gives the body a trailing
    horizontal rule that grows one line every time a marker is added.

    A body's *own* ``<hr>`` is untouched. Only a rule immediately preceding a marker
    paragraph is consumed, because only that one was ours to write.
    """
    if not html:
        return html
    return _MARKER_BLOCK.sub("", html)


def lookup_fragment(kind: str, value: str) -> str:
    """The substring a ``description like "%...%"`` filter should search for.

    Deliberately *not* anchored to the ``vikunja-mcp:`` prefix: a marker line holds several
    space-separated tokens and only the first sits directly after the prefix, so anchoring
    would find a key written first and miss the same key written second.

    This is a substring match, so it can in principle be satisfied by a ticket whose body
    merely *quotes* ``kind=value`` — a build report pasting a marker, say. Callers must
    therefore treat a hit as a candidate and confirm it with :func:`parse` rather than
    trusting the filter alone; ``task_create``'s idempotency path does exactly that.
    """
    _validate(kind, value)
    return f"{kind}={value}"


def append(html: str | None, kind: str, value: str) -> str:
    """``html`` with ``kind=value`` added to its marker footer.

    Additive and idempotent: the body is preserved verbatim, existing markers are carried
    across in order, and re-appending a pair that is already present returns the input
    unchanged. Appending a second ``ref`` never displaces the first — a lost backlink is
    invisible, which is what makes it worth a test rather than a comment.

    Implemented as strip-and-rewrite of the *footer*, not of the description: the body is
    whatever :func:`strip` leaves, and only the one marker paragraph is regenerated. That
    also normalises a web-edited footer back to the written form, so a ticket does not
    accumulate a different separator each time it is touched.

    Raises ``ValueError`` for a kind or value that could forge a second marker.
    """
    _validate(kind, value)

    existing = parse(html)
    if value in existing.get(kind, []):
        return html or ""

    existing.setdefault(kind, []).append(value)
    tokens = " ".join(f"{k}={v}" for k, values in existing.items() for v in values)
    footer = f"<p>{PREFIX} {tokens}</p>"

    body = strip(html) or ""
    # An empty body gets no rule — a horizontal rule above nothing reads as a mistake.
    return f"{body}\n<hr>\n{footer}" if body else footer
