# Ticket metadata markers

Vikunja has no field for metadata a tool wants to keep on a ticket. This server stores it
as a **visible footer** in the description, under one namespaced convention shared by every
feature that needs it — currently idempotency keys (`vikunja#465`) and commit backlinks
(`vikunja#466`).

```
<body the human wrote>

---

vikunja-mcp: idem=abc123 ref=pr|https://github.com/o/r/pull/14
```

One `<hr>`, one paragraph, space-separated `kind=value` tokens. It is visible in the web UI
on purpose: it reads as a compact provenance line rather than hidden state, and a human
who does not like it can delete it without breaking anything except the guarantee it
carried.

Markers are **stripped from every read path**, at any nesting depth and inside the
pagination envelope, so an agent reading a ticket never sees them.

---

## Why the description, and not somewhere tidier

Both alternatives were measured against the live API on 2026-08-22 and closed on evidence.

| Option | Outcome |
|---|---|
| **HTML comment** | Never leaves this process. Every description goes through `_md_to_html`, which ends in `nh3.clean()`, and nh3 strips comments by default. Enabling them means `strip_comments=False` on a sanitizer carrying an explicit `SECURITY[control]` — a security decision, not a storage one. |
| **Task comment** | No cheap lookup. `filter=comments like "%...%"` returns `400 The task field 'comments' is invalid`, so finding a marker would cost one call per task instead of one call total. |
| **Label per key** | Vikunja's model is flat, so labels are global. A per-key label is an unbounded global namespace. |
| **Description footer** ✅ | Writable, and retrievable server-side in one call via `filter=description like "%...%"` — verified with a nonsense-string control returning empty, so the filter is genuinely applied rather than silently ignored. |

---

## The separator problem

**This is the thing to know before touching the parser.**

This server writes `\n<hr>\n<p>`. Vikunja's web editor is TipTap, which parses stored HTML
into a ProseMirror document and re-serialises it on save **without inter-block
whitespace**. Measured in the Phase 0 gate: a probe ticket's newline count went 4 → 0
across an edit that touched one word of prose.

So three forms exist in the corpus, and all three are real:

| Stored form | Arises from |
|---|---|
| `\n<hr>\n<p>marker</p>` | a fresh write from this server |
| `<hr><p>marker</p>` | after **any** human web-UI edit |
| `\n<hr><p>marker</p>` | after a web edit, then an agent re-render through `_md_to_html` |

A parser anchored on the written form matches nothing on a ticket a human has opened.
Nothing raises:

- strip-on-read leaks markers into agent-visible description text
- `linked_refs` parses back empty, so backlinks look lost
- key *lookup* keeps working, because that is a substring `LIKE` on the key rather than on
  the block — which is exactly what makes the other two hard to notice

`markers._MARKER_BLOCK` therefore treats whitespace around the rule as optional, and
`tests/test_markers.py` asserts every form. Mutating the regex back to the written-form
anchor fails six tests.

---

## Two different value charsets, on purpose

| Rule | Applies to | Why |
|---|---|---|
| `_VALUE` — no whitespace, no `<>` | every stored value | Tokens are space-separated, so a space forges a sibling token; `<>` would escape the paragraph. Permissive otherwise, because a `ref` value is a URL. |
| `_LOOKUP_VALUE` — `[A-Za-z0-9][A-Za-z0-9._-]*` | anything **looked up** (idempotency keys) | The value is interpolated into a Vikunja **filter expression**. Vikunja evaluates filters left to right, so a value carrying `"` and `\|\|` escapes its enclosing predicates — the vulnerability the v0.7.0 audit found in `_compose`, reached through a different argument. `%` is a second hazard: it is `like`'s wildcard, so `100%` would match every key starting `100`. |

Both are charsets rather than escaping routines, matching the reasoning already recorded in
`contrib/duplicate_check._TOKEN`: a value that cannot *contain* the syntax needs no
escaping, and a structural guarantee does not rot the way an escaping routine does.

---

## A filter hit is a candidate, not an answer

`description like "%idem=<key>%"` is a **substring** match. It also returns:

- a ticket whose body merely *quotes* `idem=<key>` — a build report pasting a footer
- a ticket whose key only *starts* with this one (`k1` matches a stored `k12`)

Returning either would suppress a legitimate filing and hand back an unrelated ticket as
though the caller had created it, with nothing raised. So every candidate from the filter
is confirmed by re-parsing its markers, which is an exact match on a real marker paragraph
rather than a substring of arbitrary text.

---

## Markers are not authenticated

**A marker is ordinary text in a field every `task_create`/`task_update` caller can write.**
Nothing distinguishes a footer this module wrote from a paragraph that starts with the same
eleven characters. Security audit 2026-08-22 (HIGH) demonstrated both halves:

```
description = "vikunja-mcp: ref=commit|javascript:alert(1)"
  -> parses as a genuine backlink, never touching _validate_ref_url

description = "vikunja-mcp: idem=<key>"
  -> makes that ticket the "existing" hit for every future create with <key>
```

Requiring the introducing `<hr>` does **not** help and was measured rather than assumed: a
caller typing `---` in markdown renders a byte-identical `<hr>`, so it gates nothing.

Two constraints apply instead. Neither is authentication, and neither should be described
as such.

**1. The footer must be the trailing block.** Position is the only structural signal
available, and it separates the collision that actually happens — a ticket *documenting*
this format versus one *using* it. All three stored forms put the footer last, so it costs
nothing real. This also fixes a content-loss bug: before it, a ticket with a
marker-lookalike paragraph mid-body had that paragraph silently deleted from every read
projection.

**2. `ref` URLs are re-validated on read.** `server._linked_refs` runs every decoded URL
through the same `_validate_ref_url` the write path uses and drops what fails, logging
`vikunja_forged_ref_dropped`. `linked_refs` is presented as a structured, machine-parsed
field, so a consumer that trusts it *because it looks validated* needs that to be true.
The read guard is deliberately the **same** predicate as the write guard, not a stricter
one — a stricter read would silently eat links this server itself wrote, and that reads as
data loss rather than as a control.

### What remains, deliberately

A caller who puts a well-formed footer at the **end** of a description still gets a real
marker:

- **`ref`** — they can forge a link to a well-formed https URL. This grants nothing: it is
  exactly what they could have written by calling `task_link_commit`. What they can no
  longer do is smuggle a scheme the write path refuses.
- **`idem`** — trust-on-write. A planted key can suppress a future create in that project
  and return an unrelated ticket. Bounded by the same trust boundary that already lets the
  caller write any description at all, and unfixable without a server-side secret — which
  would defeat the point of a footer a human can read and delete.

`append()` preserves markers it finds, so linking a commit to a ticket carrying a forged
footer adopts that footer into the rewritten line. That is the intended additive
semantics; read-time validation is what protects the consumer.

## Interaction with duplicate detection

`VIKUNJA_DUPLICATE_CHECK` (vikunja#463) searches **titles only** — `build_filter` emits
nothing but `title like`. Markers live in descriptions, so they cannot produce phantom
duplicate matches. This is asserted on the generated filter rather than on a result set,
so it holds regardless of corpus, and separately at the hook seam: `extract_terms` *will*
tokenise a marker key if handed one, so the guarantee is that the hook only ever passes
`title`.

Note that `task_search` **does** reach description text (ParadeDB BM25 over title and
description), so a distinctive key is findable by search. That is harmless and occasionally
useful, but it means marker content is ordinary searchable text — not hidden state.

---

## Adding a third kind

1. Pick a `kind` matching `[a-z][a-z0-9_]*`. It is namespaced by the `vikunja-mcp:` prefix
   already, so it only has to be unique among ours.
2. If it will be **looked up**, its values must satisfy `validate_lookup_value`.
3. Write with `markers.append`, which is additive and idempotent and never rewrites the
   body.
4. If it should surface as a structured read field, derive it in a projection
   (`_compact_task` / `_with_staleness`) — those run **before** the strip hook, so the
   description still carries the footer. Reverse the order and the parse finds nothing.
