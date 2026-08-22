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

**A marker is not authenticated.** It is ordinary text in a field every
``task_create``/``task_update`` caller can write, so a well-formed footer at the end of a
description is indistinguishable from one this module wrote. Two constraints bound the
consequences rather than the forgery — the footer must be the *trailing* block, and
``server._linked_refs`` re-validates every ``ref`` URL on read. See :data:`_MARKER_BLOCK`
for what is accepted and why, and ``docs/markers.md`` for the whole picture.
"""

from __future__ import annotations

import re

#: The namespace every marker carries. Prevents a collision with a future third use, and
#: means a foreign tool's footer is never mistaken for one of ours.
PREFIX = "vikunja-mcp:"

#: Whitespace between the rule and the marker paragraph. **Bounded on purpose.**
#:
#: A description is attacker-influenced text that every read path runs this pattern over,
#: so its cost is a denial-of-service surface reachable by anyone who can file a ticket.
#: With unbounded `\s*` runs it was one: `(?:\s*<hr\s*/?>)?\s*<p>` puts two `\s*` adjacent,
#: giving a run of N spaces N+1 ways to split, and even after separating them the leading
#: `\s*` alone is re-explored at every one of N starting offsets. Measured on the unbounded
#: form: 20k spaces took 1.0s, 100k took 24s, and `task_list` would have hung for every
#: caller on a single such ticket.
#:
#: A real marker carries a single newline here, or nothing at all — the three forms in the
#: module docstring are the whole vocabulary. A generous ceiling therefore costs nothing
#: and makes the match cost linear in the body length. Covered by
#: `test_the_marker_regex_does_not_backtrack_catastrophically`.
_GAP = r"\s{0,16}"

#: The footer must be the **trailing** block of the description, and that is a security
#: constraint rather than tidiness.
#:
#: A marker is ordinary text in a field every `task_create`/`task_update` caller can write,
#: so nothing distinguishes a footer this module wrote from a paragraph that happens to
#: start with the same eleven characters. Security audit 2026-08-22 (HIGH) demonstrated
#: both halves of the consequence: a plain description of
#: ``vikunja-mcp: ref=commit|javascript:alert(1)`` parsed as a genuine backlink, bypassing
#: ``_validate_ref_url`` entirely; and ``vikunja-mcp: idem=<key>`` on any ticket hijacked
#: every future idempotent create for that key.
#:
#: Requiring the introducing ``<hr>`` was considered and measured to be worthless — a
#: caller typing ``---`` in markdown renders a byte-identical ``<hr>``, so it gates
#: nothing. Position is the only structural signal left, and it is the one that separates
#: the collision that actually happens: a ticket *documenting* this format (docs/markers.md
#: pasted into a body) versus a ticket *using* it. All three stored forms put the footer
#: last, so this costs nothing real.
#:
#: SECURITY[accepted]: a caller who writes a well-formed footer at the *end* of a
#: description gets a real marker, so an `idem=` key can be planted to suppress a future
#: idempotent create in that project and return an unrelated ticket. Accepted by Ted
#: 2026-08-22 on the audit's HIGH finding. Bounded by the trust boundary that already
#: exists — forging it requires `task_create`/`task_update` on the target project, which
#: the caller has anyway — and not closable without a server-side secret to sign with.
#: This server is deliberately stateless with per-caller tokens, so a shared secret would
#: not bind identity, and signing would defeat the point of a footer a human can read and
#: delete. The `ref` half of the same finding IS closed: `server._linked_refs` re-validates
#: every URL on read. Revisit if a marker ever carries a capability rather than a hint.
#: Audit: 2026-08-22/vikunja-mcp-ticket-metadata-2026-08.
#:
#: Two further properties the pattern carries, both load-bearing: ``<p>`` must *start* with
#: the prefix, so a sentence mentioning the format mid-prose stays content rather than
#: metadata; and the payload cannot contain ``<``, which is what stops a match running past
#: ``</p>`` into the rest of the body. Anchoring on a bare ``<p>`` is also what keeps quoted
#: examples out — markdown renders a fenced block to ``<pre><code>``, which never matches.
_MARKER_BLOCK = re.compile(
    _GAP
    + r"(?:<hr"
    + _GAP
    + r"/?>"
    + _GAP
    + r")?<p>"
    + _GAP
    + re.escape(PREFIX)
    + r"(?P<payload>[^<]*)</p>\s*\Z",
    re.IGNORECASE,
)

#: A marker kind: lowercase, no separator characters that appear in the wire format.
_KIND = re.compile(r"\A[a-z][a-z0-9_]*\Z")

#: A marker value: anything that cannot forge a sibling token or escape the paragraph.
#: Whitespace is excluded because tokens are space-separated, and `<>` because the payload
#: is bounded by them. Deliberately permissive otherwise — a `ref` value is a URL, and
#: tightening this would make the backlinks vikunja#466 exists to store unstorable.
_VALUE = re.compile(r"\A[^\s<>]+\Z")

#: A value that will be interpolated into a Vikunja **filter expression**, which is a
#: narrower thing than a value that is merely stored.
#:
#: Two distinct hazards, neither of which escaping would address as well as a charset:
#:
#: 1. **Filter break-out.** Vikunja evaluates a filter strictly left to right, so a value
#:    carrying `"` and `||` escapes its enclosing predicates — the vulnerability the
#:    v0.7.0 audit found in ``_compose`` (server.py), reached through a different
#:    argument. A key of ``x" || done = true || "`` would turn a scoped lookup into an
#:    unscoped one.
#: 2. **Silent over-matching.** `%` is `like`'s wildcard. A key of ``100%`` would match
#:    every key beginning ``100``, so a create would be suppressed as a duplicate of an
#:    unrelated ticket — a wrong answer that raises nothing.
#:
#: Same reasoning as ``contrib/duplicate_check._TOKEN``: a value that cannot *contain* the
#: syntax needs no escaping routine, and a structural guarantee does not rot the way an
#: escaping routine does. Must start alphanumeric so a value cannot lead with punctuation
#: that reads as an operator.
_LOOKUP_VALUE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


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


def value_is_storable(value: str) -> bool:
    """Whether ``value`` can be stored in a marker without forging a sibling token.

    Exposed so a caller can produce its own domain-specific error — ``task_link_commit``
    explains the whitespace rule in terms of a URL — rather than raising this module's
    generic one at a caller who never mentioned markers.
    """
    return isinstance(value, str) and bool(_VALUE.match(value))


def linked_refs(html: str | None) -> list[dict[str, str]]:
    """Every backlink on a ticket, decoded, in the order it was linked.

    Malformed entries are dropped rather than guessed at, so a hand-edited footer degrades
    to a shorter list instead of to a link with a plausible-looking half.
    """
    out = []
    for value in parse(html).get("ref", []):
        decoded = decode_ref(value)
        if decoded is not None:
            out.append(decoded)
    return out


#: Separates a backlink's type from its URL inside a single ``ref`` value.
#:
#: A marker line is space-separated, so the two halves cannot be stored as two tokens
#: without losing which URL belongs to which type once a ticket carries several. A pipe is
#: not valid in a URL without percent-encoding, which makes "contains a delimiter" a
#: reliable signal that the input is malformed rather than a legitimate address — and
#: :func:`encode_ref` refuses one rather than encoding it, so a round-trip can never
#: silently split a URL into a bogus second link.
REF_DELIMITER = "|"


def encode_ref(ref_type: str, ref_url: str) -> str:
    """The stored value for a backlink. Raises ``ValueError`` on anything ambiguous."""
    if not isinstance(ref_type, str) or not _KIND.match(ref_type):
        raise ValueError(
            f"ref_type must match {_KIND.pattern!r} (lowercase, no spaces); got {ref_type!r}"
        )
    if not isinstance(ref_url, str) or REF_DELIMITER in ref_url:
        raise ValueError(f"ref_url may not contain {REF_DELIMITER!r}; got {ref_url!r}")
    return f"{ref_type}{REF_DELIMITER}{ref_url}"


def decode_ref(value: str) -> dict[str, str] | None:
    """Split a stored ``ref`` value back into its parts, or ``None`` if malformed.

    ``None`` rather than a partial dict: a hand-edited footer should degrade to "no link",
    never to a link with a plausible-looking half. Splits once, so a URL that somehow
    carries a delimiter keeps the remainder in the URL instead of dropping it.
    """
    ref_type, sep, ref_url = value.partition(REF_DELIMITER)
    if not sep or not ref_type or not ref_url:
        return None
    return {"ref_type": ref_type, "ref_url": ref_url}


def validate_lookup_value(value: str) -> None:
    """Reject a value that cannot safely be interpolated into a filter. ``ValueError``.

    Call this on any value that will later be looked up — an idempotency key — at the
    point the caller supplies it, not only inside :func:`lookup_fragment`. Validating in
    both places is deliberate: writing a marker whose value can never be looked up is a
    silent dead end, so the write path has to refuse it too.
    """
    if not isinstance(value, str) or not _LOOKUP_VALUE.match(value):
        raise ValueError(
            f"lookup value must match {_LOOKUP_VALUE.pattern!r} — letters, digits, "
            f"'.', '_' and '-', starting alphanumeric. Quotes, '%' and whitespace are "
            f"refused because the value is interpolated into a Vikunja filter "
            f"expression. Got {value!r}"
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
    validate_lookup_value(value)
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
