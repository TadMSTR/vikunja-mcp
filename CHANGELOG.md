# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.10.0] — 2026-09-02

Four defects with one root cause: a signal that reported success whether or not the thing it
measured had happened. Each was found by a check that had already passed.

### Fixed
- **`labels_truncated` now means what its name says** (vikunja#625). `dropped` was
  `len(labels) - len(labels[:cap])`, where `labels` came from a hardcoded `per_page=50` fetch
  of page one — both sides post-fetch, so truncation caused by the fetch cancelled out of the
  flag whose only job was to report it. Measured on a 67-label tracker: 50 buckets at
  `max_label_buckets=70` *and* at 200, with `labels_truncated: false` and `notes: []`. No
  caller-side parameter fixed it. The label listing is now followed to the end, truncation is
  measured against the labels that exist, and the two causes (a cap that bit, a listing that
  fell short) are reported separately because they call for different actions.
- **A bucket that was not counted no longer reads as `0`** (vikunja#624). `by_label` is keyed
  by label title, and titles are not unique — one tracker carried `source:github` as id 1 and
  again as id 38 — so one bucket per id collided in the output dict and the surviving number
  depended on where the cap fell: 0 at cap 25, 9 at cap 45, same backlog. Labels sharing a
  title are now a single bucket counted over all of their ids. `_count_matching` no longer
  returns 0 for a response it cannot read a `total` off; label buckets tolerate that per
  bucket, reporting it as absent-and-named rather than as a number nobody measured.
- **The CI filter canary can now detect what it exists to detect** (vikunja#515). It
  authenticated with a login JWT, which carries the user's whole permission set where
  production forwards a scoped API token, so a route unreachable by every real caller read
  healthy — with a green weekly run to say so. The workflow now mints a scoped API token
  that deliberately excludes `projects.tasks_by_index`, and the by-index assertion is `401`
  outright rather than "200 or 401". Measured on v2.6.0, same instance and moment: JWT 200,
  scoped token 401.
- **The test suite no longer leaks a wiped hook registry between modules** (vikunja#473).
  Two modules cleared the registry on teardown without restoring the built-ins, so the next
  module added passed alone and failed in the suite, with a failure that read as a bug in the
  code under test. Clear-and-restore now lives in one autouse fixture in `tests/conftest.py`;
  five per-module workarounds are gone.

### Security
- **The CI filter canary's API token is narrowed to a measured minimum.** Following the
  2026-09-02 audit, each candidate permission set was minted against a throwaway v2.6.0 and
  the whole canary run against it: `projects.read_all`, `projects.read_one`,
  `labels.read_all` and `tasks.read_one` are unnecessary and are dropped. `projects.delete`
  and `labels.delete` are kept despite stripping them also passing — the fixture teardown
  calls both without `raise_for_status()`, so a 401 there is discarded and the run stays
  green while cleanup silently does nothing. That green is the absence of a check, not
  evidence the calls are unused.
- **Label ids are coerced to `int` before reaching a filter expression.** They arrive from an
  upstream response and were interpolated directly. `_compose`'s per-predicate grouping
  already contained any `||`, so this is defence in depth rather than a fix for a scope
  escape — but coercing *before* insertion also closes a fabricated zero: coercing during
  `append` leaves `setdefault`'s empty list behind, and an empty id list renders as
  `labels in `, whose count comes back 0.

### Changed — BREAKING
- **`backlog_summary` gained `labels_not_counted`**, listing by name the buckets that were
  skipped. "How many were dropped" does not tell a consumer whether the label it cares about
  was one of them, which is what a "this bucket reads 0" negative control needs to know.
- **`max_label_buckets` now defaults to every label in scope**, rather than 25, with a
  200-bucket ceiling that reports itself when it bites. A fixed default of 25 was already
  wrong against 67 labels; any fixed default is a rot clock. Truncation is now opted into.
  Callers passing an explicit value are unaffected.

## [0.9.0] — 2026-08-23

Ported from Vikunja's `/api/v1` to `/api/v2`. v1 is frozen upstream at 2.4.0 — every route
Vikunja adds from there lands on v2 only — deprecated at 3.0 and removed at 4.0, so this is
the version that stops the server being frozen against a frozen API.

Every one of the 78 call sites was checked against the live OpenAPI spec and then against
the live router before and after the change; both sweeps are green with 0 mismatches across
69 distinct routes.

### Changed — BREAKING
- **Minimum Vikunja is now 2.4.0.** `client._api_base()` targets `/api/v2`. There is no
  dual-version mode and version selection is deliberately not configurable — supporting
  both would mean an abstraction over the verb, pagination and error differences below, for
  a fallback to an API that is being removed.
- **Verbs are inverted** relative to v1, which used PUT to create and POST to update. v2 is
  conventional: POST creates (201), PUT replaces, PATCH merges, DELETE returns 204 empty.
  One route does not follow it — `POST /teams/{team}/members/{user}/admin` is a toggle and
  stays POST on both versions.
- **Search parameter `s` → `q`** on `task_search`, `task_list`, `project_list`,
  `label_list` and `team_list`. Worth calling out because v2 does not reject `s`, it
  *ignores* it: a missed rename returns the unfiltered result set rather than an error.
- **Errors are RFC 9457 `application/problem+json`.** The human-readable text moved from
  `message` to `detail`; `code` still carries Vikunja's numeric error code and is now
  appended to the surfaced message. `message` is still read as a fallback — some middleware
  (the API-token check among them) still answers in v1's shape.
- **Pagination is a body envelope, not headers.** The outward shape tool callers see is
  unchanged (`{"items": [...], "pagination": {...}}` when truncated, a bare list otherwise),
  populated from `{items, total, page, per_page, total_pages}` rather than
  `x-pagination-*`. `page` is now read from the response rather than echoed from the
  request.
- `$schema` is stripped from single-resource bodies. v2 stamps every response with a link
  to its own JSON Schema; it is transport metadata and would otherwise be repeated into an
  agent's context on every read.

### Added
- **`pagination.total`** — the size of the whole result set, which v1 could not report at
  all (its `x-pagination-result-count` counted the rows in one response). `count` keeps its
  meaning of "rows in this response", so the two are now distinguishable rather than merely
  warned about.
- `client.request(..., unwrap_list=False)` returns the raw v2 list envelope for callers
  that want its fields rather than its rows.

### Removed
- **The read-merge-write path behind `task_update`.** v1's `POST /tasks/{id}` was a full
  replace, so a partial update had to fetch the task, overlay the caller's fields and
  re-post the whole object (#173). v2's `PATCH` does it in one request. That deletes a
  request per update, the list of heavy read-only collections that had to be stripped back
  out of the merged body, and the `SECURITY[accepted]` TOCTOU window in which a concurrent
  writer's change was overwritten by a stale re-post — the window is gone because there is
  no longer a read to go stale, not because it was narrowed.

### Fixed
- `backlog_summary` no longer infers its counts. Each bucket reads `total` off the response
  envelope instead of treating `x-pagination-total-pages` at `per_page=1` as a match count,
  and asks for a page past the end of the result set so no task row is transferred at all.
  Re-measured against a 514-task tracker: 37 requests, ~125 ms, **15.5 KB** of upstream
  traffic (down from 109 KB when the count query returns a row), 1.2 KB of tool response.

### Notes
- **`tasks_bulk_update` keeps its `fields` array and moves POST → PUT.** v2 routes only
  `PUT` on `/tasks/bulk`, so there is no bulk `PATCH` to move to. What did change is that
  `fields` is documented upstream now — v1 swagger never mentioned it, and the probe on
  ticket #333 was its only specification.
- **`#N` ticket resolution still uses the undocumented `filter=index = N`.** v2 ships
  `GET /projects/{project}/tasks/by-index/{index}`, which is the same lookup, documented —
  and it returns 401 for any API token lacking the `projects → tasks_by_index` permission,
  which no token created before v2 carries. This server forwards the caller's token, so it
  cannot grant itself one; adopting the route would break `#N` refs for every existing
  deployment until each token was re-issued. No fallback between the two was added: a
  fallback fires exactly when a loud error is wanted. The filter and its negative control
  were re-verified against v2 on 2.5.0.
- **Markdown conversion is still done here, not delegated.** v2 can convert server-side via
  `?format=markdown`, but `nh3.clean()` can only sanitize HTML this process produced —
  delegating the conversion would remove the sanitizer from the path rather than move it.
  `format` is not set on any call, so every write sends HTML and nothing round-trips a
  description through Markdown's lossy conversion.
- ETag / `If-Match` conditional requests are a real v2 affordance and are deliberately out
  of scope here; adding them during a port would make the diff hard to review.

## [0.8.0] — 2026-08-22

Structured metadata that Vikunja has no field for, under one convention used twice.
Tickets #465 (id 484) and #466 (id 485). Unlike 0.7.0, this **writes persistent,
human-visible data into ticket bodies** — which is why the marker format was gated on a
round-trip probe before any of it was built.

### Added
- **Marker convention** (`vikunja_mcp.markers`) — a visible provenance footer in the
  description: one `<hr>`, one paragraph, space-separated `kind=value` tokens under a
  `vikunja-mcp:` namespace. Stripped from every task-returning tool's response at any
  nesting depth and inside the pagination envelope, in `verbose` too. Full rationale in
  `docs/markers.md`.
- **`idempotency_key` on `task_create`** (#465) — a caller-supplied key makes a retried
  create safe. On a match the existing task is returned with `idempotent_hit: true`
  instead of a second being filed. Scoped to the project; never derived from the title,
  which would collapse two legitimately-similar tickets into one (the vikunja#331 failure
  mode). If the lookup itself fails the task is still created, with
  `idempotency_degraded: true` on the response — losing a filing to a convenience feature
  is worse than failing to deduplicate, but silently claiming a guarantee that did not
  hold is worse than both.
- **`task_link_commit(task_id, ref_type, ref_url)`** (#466) — records a commit/PR/branch
  backlink, read back as a structured `linked_refs` field on `task_get`. Accepts `#463`
  and `#463 (id 482)` task refs. Links accumulate: a second never displaces the first, and
  re-linking the same pair is a no-op. Explicitly **not** in scope: transitioning ticket
  state from VCS events.

### Security
- **Markers are not authenticated, and the consequences are now constrained.** A marker is
  ordinary text in a field every `task_create`/`task_update` caller can write, so a
  description of `vikunja-mcp: ref=commit|javascript:alert(1)` parsed as a genuine backlink
  and bypassed `_validate_ref_url` entirely (security audit 2026-08-22, HIGH). Two fixes:
  the footer must now be the **trailing** block, and `linked_refs` **re-validates every URL
  on read** with the same guard the write path uses, dropping and logging what fails.
  Requiring the introducing `<hr>` was measured and rejected — typing `---` renders a
  byte-identical one, so it gates nothing. What deliberately remains is documented in
  `docs/markers.md`: a trailing forged `ref` to a well-formed https URL grants nothing a
  caller could not do via `task_link_commit`, and `idem` is trust-on-write.
- **Fixed: a marker-lookalike paragraph mid-body was silently deleted on read.** Same root
  cause; the trailing constraint fixes it. A ticket documenting this format previously lost
  that paragraph from every read projection while storage still held it.
- **Fixed: ReDoS on every read path.** `_MARKER_BLOCK` runs over every description, and
  unbounded `\s*` runs made a long whitespace run quadratic — 100k spaces took 24s, so one
  ticket could hang `task_list` for every caller. Each gap is bounded at `\s{0,16}`; the
  same input is now 0.045s.
- **Fixed: `ref_url` host spoofing.** `https://github.com@evil.example.com/x` reads as a
  GitHub link and navigates elsewhere. Userinfo and backslashes are refused; `@` later in
  the path still works.
- **Idempotency keys are filter operands, so they carry a charset, not an escape.** A key
  is interpolated into a Vikunja filter expression — the surface the 0.7.0 audit found
  `_compose` escaping. Vikunja evaluates filters left to right, so a key of
  `x" || done = true || "` turns a project-scoped lookup into an unscoped one, and `%`
  (`like`'s wildcard) would silently over-match. Both are refused structurally, matching
  `contrib/duplicate_check._TOKEN`'s reasoning.
- **A filter hit is confirmed, not trusted.** `like` is a substring match, so it also
  returns tickets that merely quote a key and tickets whose key is a prefix of it. Every
  candidate is re-parsed before being accepted, because returning the wrong one suppresses
  a real filing while looking like success.
- **`ref_url` is guarded as stored-link injection.** https with a dotted hostname only;
  `javascript:`, `data:`, plain `http` and `https://localhost` are refused before any
  upstream call. Not the `webhook_create` SSRF case — nothing fetches the URL — so the
  internal-host guard is deliberately *not* reused, since a backlink to an internal Gitea
  is the normal case on forge.
- Marker values cannot forge a sibling token: whitespace and `<>` are refused on write
  rather than sanitised, because a key that silently changed shape would miss on exactly
  the retry it exists to catch.
- `nh3`'s `strip_comments` is **unchanged**. The HTML-comment marker format was rejected
  partly to avoid touching that audited `SECURITY[control]`.

### Notes
- **TipTap strips inter-block whitespace.** Measured against the live instance: when a
  human edits a ticket in the web UI, `\n<hr>\n<p>` is re-serialised as `<hr><p>`. Three
  separator forms therefore exist in the corpus and the parser tolerates all three. A
  parser anchored on the written form would silently leak markers on read and return empty
  backlinks for every ticket a human had ever opened, while key lookup kept working — see
  `docs/markers.md`.
- **Duplicate detection is unaffected.** vikunja#463 searches titles only, asserted on the
  generated filter and at the hook seam, so markers cannot produce phantom matches.
- `task_link_commit` is a new tool and is **not** in any agent's scoped-mcp allowlist yet.

## [0.7.0] — 2026-08-22

Three signals an agent reads at the moment it decides to act, plus a canary over the
undocumented Vikunja behaviour they rest on. Tickets #463 (id 482), #464 (id 483),
#467 (id 486) and #470 (id 489). All read-path or report-only — nothing here stores new
state, and the only write path touched is `task_create`, which gains a read beside it and a
field on its response.

### Added
- **Staleness on every task read** — `days_since_update` and `stale` on `task_get`,
  `task_list` and `task_search`, in `verbose` too. Threshold is `VIKUNJA_STALE_AFTER_DAYS`
  (default 90); `0` or less is refused at startup rather than marking the whole backlog
  stale. Both fields are `null`, never `false`, when the age is unknown — including for
  Vikunja's `0001-01-01` zero value, which parses fine and would otherwise report ~740,000
  days and `stale: true`. The field docs state plainly that `stale: false` means "recently
  touched", not "still true".
- **`backlog_summary`** — counts rather than rows: totals, done/open, and breakdowns by
  priority, label and staleness. Each bucket is one request asking for a single row, using
  `per_page=1` so `total_pages` *is* the match count. Measured live: 37 calls, ~150 ms,
  ~1.2 KB for a 470-task tracker, with the cost reported in the response as `calls`.
  `VIKUNJA_SUMMARY_EXCLUDE_IDS` drops an all-labels "anchor" task that would otherwise
  inflate every label count by exactly one.
- **Duplicate detection on `task_create`** (`contrib/duplicate_check.py`), **on by
  default** — `VIKUNJA_DUPLICATE_CHECK=0` disables it. Attaches `possible_duplicates` to
  the created task. Reports, never refuses, and never costs a filing: both hook handlers
  wrap their entire body, so any failure degrades to no warning rather than aborting the
  create. Default-on was decided on measurement — over all 470 titles in forge's tracker,
  19 warned (4.0%), 0 errored, and ~12 of the 19 were genuine.
- **Filter upgrade canary** (`tests/test_filter_canary.py`) — 22 assertions over the
  undocumented filter behaviour this server depends on, with negative controls throughout,
  since a filter silently becoming a no-op looks identical to success. Self-contained
  (builds and deletes its own fixtures), skipped unless `VIKUNJA_CANARY_URL` and
  `VIKUNJA_CANARY_TOKEN` are set, and run weekly in CI against `vikunja/vikunja:latest`.

### Security
- **`backlog_summary`'s caller-supplied `filter` can no longer escape the tool's own scope.**
  Every predicate is now composed into its own parenthesised group. Previously they were
  joined with a bare `&&`, and because Vikunja evaluates filter expressions strictly
  left to right, a caller `filter` containing a top-level `||` broke out of the predicates
  composed before it — `done = false && id = 999999 || done = true` returns 264 *done*
  tasks, having escaped the `done = false` it opens with. Not privilege escalation (the
  caller already reaches whatever its own token permits), but the tool would report counts
  for one scope while `scope.filter` claimed another. Found by the build audit; the
  reproduction, the fix, and the left-to-right evaluation order are all pinned by tests.

### Notes
- **`like` case-sensitivity depends on the database backend, not on Vikunja.** Measured on
  two instances both running v2.3.0: case-sensitive on Postgres, case-insensitive on
  SQLite. Duplicate detection therefore queries every term in lower/Capital/UPPER at once,
  which is correct under either. Found by the canary before release, not after.
- `task_search`'s docstring now says it searches descriptions as well as titles, so a hit
  is not evidence of a duplicate.

## [0.6.0] — 2026-08-21

Makes the server deployable by someone who is not running PM2 on a Debian host. Three
things blocked that: there was no container image, no endpoint a container could be health
checked against, and the stdio transport was completely non-functional. Tickets #462
(id 481) and #461 (id 480).

### Added
- **Container image**, published to `ghcr.io/tadmstr/vikunja-mcp` on every `v*` tag, for
  `linux/amd64` and `linux/arm64`. Multi-stage, `python:3.13-slim` pinned by digest,
  non-root (uid 1000), no pip or build metadata in the runtime layer. The `[telemetry]`
  extra is installed, so `OTEL_EXPORTER_OTLP_ENDPOINT` works without the silent
  `otlp_import_failed` state that has bitten this project's siblings before.
- **`GET /health`** — unauthenticated, returns `{"status", "version"}` and deliberately
  nothing else, since the response is public by construction. It is a liveness check and
  does **not** probe upstream Vikunja: this server is stateless, so failing health on a
  Vikunja restart would cost restarts and buy nothing. A `HEALTHCHECK` in the image uses it.
- **Reference `docker-compose.yml`** at the repo root — `cap_drop: ALL`,
  `no-new-privileges`, `read_only`, loopback publish — plus [`docs/docker.md`](docs/docker.md)
  covering the env table, the security model, the audit-log mount and the webhook caveat.
- **CI builds and *runs* the image** on every PR: a build proves the image assembles, not
  that the entry point works. Smoke tests cover the fail-closed `VIKUNJA_URL`, an
  unauthenticated `/health` that echoes no config, and non-root.

### Fixed
- **stdio is usable at all.** It previously started cleanly, logged, registered all 71
  tools, and then failed **100% of tool calls** with `AuthError`: `caller_token()` read
  HTTP headers, and under stdio there is no request to read. `VIKUNJA_TOKEN` now supplies
  the credential in that mode. (#461)
- **stdio no longer writes logs to stdout**, which is the JSON-RPC channel. Logs go to
  stderr under `transport=stdio` only, leaving the log split unchanged everywhere else.
- **`test_main_stdio_transport` now invokes a tool** instead of only asserting `mcp.run`
  was called with `transport="stdio"`. Asserting the launcher rather than the behaviour is
  precisely how a wholly broken transport shipped under a green suite; a subprocess
  end-to-end test over real MCP framing was added alongside it.

### Security
- **`VIKUNJA_TOKEN` with a network transport is refused at startup**, not warned about. A
  static token on a shared port collapses every caller into one Vikunja identity, silently,
  with no symptom until someone asks who changed a ticket — the exact failure the
  token-passthrough model exists to prevent. The check is written against `stdio` (the one
  safe case) rather than against `http`, so `sse` and any future network transport are
  covered by default. `stdio` without a token is likewise refused at startup instead of
  failing at the first tool call. A blank `VIKUNJA_TOKEN` counts as unset, so an empty
  compose assignment cannot satisfy the check and reintroduce the deferred failure.
- `SECURITY.md` and the README no longer state that the server never holds a token — true
  only on network transports after this release, and both now say so plainly rather than
  being quietly falsified.

### Notes
- **No PyPI publish.** The name `vikunja-mcp` is namesquatted on public PyPI, which makes
  `pip install vikunja-mcp` actively unsafe to document. The image is the safe adoption
  path, not merely a parallel one.
- Forge still runs the PM2 process. Cutover to the container is a separate, deliberate
  sysadmin change with its own rollback; nothing in this release touches the live service.

## [0.5.0] — 2026-08-21

Two halves of the same problem: agents kept confusing the per-project ticket number
(`#454`) with the global task id (`473`), and every read shipped far more payload than the
question needed. Ticket #456 (id 475).

### Added
- **`task_id` accepts a ticket reference on all 19 tools that take one.** `"#454"` resolves
  server-side with one filtered lookup (`filter=index = 454`); `"#456 (id 475)"` — the form
  forge tickets are written in — is parsed directly with no API call at all. A bare number,
  int or string, is always a global id: `"454"` without the `#` is never read as a ticket
  number, because guessing there is what caused #331. The spelled-out form is accepted only
  on a string that opens with a ticket reference and names exactly one `id N`: prose
  mentioning an id is refused, and a string naming several
  (`"#456 (id 475) blocks #331 (id 342)"`) raises rather than taking the first. Non-ASCII
  digits are refused too — `str.isdigit()` is also true for `"²"`, where `int()` would raise
  a confusing message instead of a clear one.
- **`VIKUNJA_DEFAULT_PROJECT_ID`** scopes ticket-reference resolution to one project.
  Unset by default, in which case resolution runs unscoped and **raises** if more than one
  task matches, naming every candidate rather than picking one. Ticket numbers are only
  unique within a project (`index = 1` matches id 9 in project 7 *and* id 344 in project 2).
- **`url` on every projected task**, built from `id`. Constructing `/tasks/454` from a
  ticket number lands on an unrelated task; this removes the opportunity.
- **`verbose` on `task_get`, `task_list` and `task_search`** — returns the upstream body
  untouched, for the cases that genuinely need everything.

### Changed
- **BREAKING: no read path returns a bare `index` any more.** `_strip_ambiguous_task_index`
  was create-only by its own docstring, so `task_get`/`task_list`/`task_search` still
  returned `"index": 454` sitting beside `"id": 473` — the exact ambiguity behind #331 (id
  342), where an agent passed one for the other, silently mutated three unrelated tickets
  and briefly closed an open security ticket. It is now `_strip_task_index`, applies at any
  nesting depth (including the tasks Vikunja inlines under `related_tasks`, which carry
  their own `index`), and covers all nine tools that return a task body — not just the
  three read tools, since `task_update` and friends returned one too. `identifier` is
  unchanged: it is a string, so it cannot be passed where an int id is expected without an
  obvious type error. **The strip applies in `verbose` mode as well** — `verbose` restores
  the payload, not the ambiguity.
- **BREAKING: `task_get`, `task_list` and `task_search` are compact by default.** The
  expensive field differs per tool, so the projection does too. `task_list`/`task_search`
  return summary rows and drop `description` — 132 KB of a measured 182 KB page of 50, or
  roughly 45k tokens for one call. `task_get` **keeps** `description` (that is the point of
  reading one ticket) and instead reduces the inlined bodies of related tasks to
  `{id, identifier, title, done}`. Dropped collections become counts
  (`attachment_count`, `reaction_count`, `assignee_count`) so their existence stays
  discoverable. Measured: 182,108 B → ~13 KB for a 50-row list, 9,523 B → ~4.6 KB for
  `task_get`. Pass `verbose=true` for the old shape.
  - `pagination` is never projected — a truncated list still reports `truncated: true` and
    `total_pages`, so one page is not mistaken for a whole answer.
  - Surveyed before merging: no forge consumer reads `description` out of a list result.
    Every `task_list`/`task_search` reference across `agent-platform-agents`,
    `.claude/skills` and `host-forge-scripts` is either a manifest allowlist entry or prose
    telling an agent to search for a ticket by title. No call site needed `verbose=true`.

### Notes
- Resolution depends on `filter=index = N`, which **Vikunja does not document** — its
  published filter-field list omits `index`, and the server's accepted set matches the docs
  in neither direction (`bucket_id` works, `position` 500s). Verified working on Vikunja
  **v2.3.0** with a negative control (`bogusfield = 1` → 400, proving unknown filter fields
  are rejected rather than silently ignored). A failed resolve raises with a message naming
  that caveat and the verified version; it **never** falls back to treating `"#454"` as id
  454, which would be #331 reintroduced as an error path.
  `tests/test_task_refs.py::test_live_index_filter_still_resolves` is an opt-in live canary
  for a future upgrade removing it.
- Deliberately not resolved: `tasks_bulk_update`'s `task_ids` (mutates N tasks per call —
  the tool behind #333, deserves its own review) and `other_task_id` on the relation tools
  (still `int`, so a `"#454"` there is refused at schema validation — loud and safe).
- No cache. `index` is filterable server-side, so there is no N+1 lookup to cache away, and
  a stale `index → id` map's failure symptom is commenting on the wrong ticket — precisely
  what this release exists to prevent.

## [0.4.0] — 2026-08-11

### Added
- **`comment_list`, `attachment_list`, `task_assignee_list`, and `view_list` now take
  `page`/`per_page`.** These four list tools were the only ones without a way to reach
  results past the first 50 — a caller could see `pagination.truncated: true` since v0.3.0
  but had no way to fetch the rest. (ticket #341 / id 357)
- **Opt-in audit trail for mutating tools.** Set `VIKUNJA_AUDIT_LOG=1` and
  `VIKUNJA_AUDIT_LOG_DIR=<path>` to have `contrib/audit_log.py` register for `task_create`,
  `task_update`, `tasks_bulk_update`, `task_delete`, `project_create`, `project_delete`,
  `team_create`, `project_team_add`, `project_user_add`, `project_share_create`, and
  `webhook_create`, writing one line per call to `<dir>/YYYY-MM-DD.md`. Off by default;
  `VIKUNJA_AUDIT_LOG=1` with no directory set fails closed rather than falling back to
  stdout. The trail is pseudonymous (a hash of the caller's token, not the token or a
  reversible agent identity) and records that a call happened, not what changed. (ticket
  #342 / id 361)

### Changed
- **`config.url` no longer defaults to a real hostname.** It now defaults to `""` and the
  server refuses to start with a clear `ConfigError` if `VIKUNJA_URL` is unset, instead of
  silently falling back to Ted's forge instance. (ticket #344 / id 363, SC-01)

### Security
- **Webhook documentation and the SSRF guard's refusal message no longer recommend a
  target that doesn't work.** `SECURITY.md`, `docs/forge.md`, and the `webhook_create`
  docstring told callers to point `target_url` at a public SWAG hostname; on forge,
  split-horizon DNS resolves every `*.helmforge.me` name to the LAN, so the guard refused
  exactly the target the docs recommended. Docs now state a valid target must be genuinely
  external to forge, and that none currently exists on this deployment. The guard itself
  (`_host_is_blocked`) is unchanged — it was already correct. (ticket #343 / id 362)

## [0.3.0] — 2026-08-04

### Fixed
- **`tasks_bulk_update` no longer wipes untouched columns on every task it touches.**
  `POST /tasks/bulk` is a full replace *per task*, so posting a bare `values` object reset
  every column absent from it — `values={"done": true}` erased description, priority and
  percent_done across the whole list. Same root cause as #173, which was fixed in v0.2.2
  for the two single-task tools and never applied to the bulk one. The request now carries
  `models.BulkTask`'s `fields` array, restricting the write to the columns named in
  `values`. Verified live against Vikunja 2.3.0. (ticket #333 / id 347)
- **Version drift between `__init__.py` (0.2.1) and `pyproject.toml` (0.2.2)**, which meant
  the deployed process reported the wrong version at startup. A test now pins the two
  together.
- **Markdown tables render as tables.** `tables` was missing from the extension list, so a
  GFM table in a ticket description came out as literal pipe characters.
- **A line starting with a ticket reference is no longer parsed as a heading.** `- #333 …`
  rendered as `<h1>333 …</h1>`, which mangled the "Related" list of at least one ticket. A
  leading `#` followed by a digit is now escaped, outside code fences. Real headings
  (`# Context`) and inline references (`C#`, `see #333`) are untouched.

### Added
- **List results report truncation instead of hiding it.** Vikunja paginates every list
  endpoint at 50 per page and signals the extent only in headers, which were discarded — so
  "find every ticket about X" returned at most one page and looked complete. A multi-page
  list is now returned as `{"items": [...], "pagination": {page, total_pages, count,
  truncated}}`; single-page lists and all non-list bodies are unchanged.
- **`task_create` no longer returns the ambiguous `index` field.** Vikunja returns `id`
  (global), `index` (per-project) and `identifier` (`"#N"`). `index` is indistinguishable
  from a task id at a glance and misreading it caused #331 — an agent passed it to
  `task_label_add` and silently mutated three unrelated tickets. `identifier` is kept; it
  is a string and five consumers display it. Read `index` back via `task_get` if needed.
  (ticket #331 / id 342, closed as wrong root cause — the `id` mapping was always correct)
- **`scripts/verify-routes.py`** — probes every implemented route against Vikunja's live
  Echo router using an unroutable verb and asserts the code's method appears in the
  `Allow:` header. Swagger is wrong about `/labels/{id}` (it documents `PUT`; the router
  takes `DELETE, GET, POST`), which is how the v0.2.1 `label_update` bug shipped. Wired as
  an opt-in `workflow_dispatch` + nightly job, deliberately not on the PR path.
- `hooks.after_handlers()`, so a handler can be registered idempotently without touching
  the registry's private state.

### Changed
- `_apply_task_update` no longer echoes `related_tasks`/`attachments`/`reactions` back in
  the re-posted body. Vikunja inlines the full body of every related task — one
  `task_search` returned 155k characters. These live in their own tables; verified by probe
  that relations, labels and assignees survive their omission.
- `docs/vikunja-structure.md` is now a pointer to the ratified knowledge-base copy rather
  than a stale 83-line fork missing the label-ID table every agent's CLAUDE.md relies on.

### Security
- **Webhook SSRF guard now refuses any non-globally-routable address.** It enumerated
  `is_private`/`is_loopback`/etc, and since CPython 3.12.4 `100.64.0.0/10` (CGNAT, also
  Tailscale's range) reports `is_private=False` — so those targets passed. Latent rather
  than exploitable on forge (no CGNAT interface, route, or Tailscale install), but this
  guard is load-bearing here because forge disables Vikunja's own filter.
- **The webhook SSRF guard now fails closed on an unresolvable host.** It previously
  allowed one, reasoning that Vikunja re-resolves at delivery — but forge disables
  Vikunja's outgoing-request filter, so that second resolution is unguarded, leaving a DNS
  rebinding path into `forge-net`. A host that cannot be classified is now refused. Cost:
  registering a webhook against a momentarily unresolvable legitimate host is rejected,
  which is the cheaper failure for a rare, deliberate operation. (audit 2026-08-04, MEDIUM)
- **`scripts/verify-routes.py` no longer requires a Vikunja token,** and the CI job no
  longer takes one. Echo answers the route-mismatch 405 before auth middleware runs —
  verified across all 69 routes with no `Authorization` header — so the sweep never needed
  a credential. Removing it means no live Vikunja token exists as a GitHub secret at all,
  rather than merely being scoped down. (audit 2026-08-04, INFO)

### Documentation
- `SECURITY.md` no longer credits Vikunja's upstream SSRF filter, which forge disables via
  `VIKUNJA_OUTGOINGREQUESTS_ALLOWNONROUTABLEIPS=true`. It now states that the MCP-side
  guard is the only control in this deployment, and records the DNS-rebinding limit.
- `AGENTS.md` invariant 5 claimed partial updates never clobber unspecified fields — true
  for projects/labels/teams/filters/views/buckets, false for tasks since v0.2.2. Replaced
  with a table of all three behaviours so nobody "simplifies" `_apply_task_update` back
  into #173. `hooks.py`, `telemetry.py` and `contrib/` added to module boundaries.
- `docs/forge.md` grant matrix rewritten against the live manifests: developer's documented
  14-tool grant was never deployed (the manifest grants all 71) and the target is a
  23-tool set. Also corrected the Vault path, header variable, module URL, and the
  verification step — there is no `/health` route.
- README documents that enabling telemetry needs *both* the `[telemetry]` extra and the
  endpoint env var, and that `otlp_enabled` — not the presence of the variable — is the
  acceptance check.
- `contrib/audit_log.py` no longer claims to satisfy the forge tool-audit directive on its
  own; nothing registers it, so this server currently emits no audit trail.

## [0.2.2] — 2026-07-19

### Fixed
- **Task/project descriptions and comments now render correctly.** Vikunja's description
  and comment fields are HTML (TipTap), not markdown — agent-authored markdown (`##`
  headers, `- ` lists, blank-line paragraphs) was being stored and displayed verbatim,
  producing an unreadable wall of text. `task_create`/`task_update`/`comment_create`/
  `project_create`/`project_update` now convert markdown to HTML before writing.
- **task_update / task_reminders_set no longer wipe untouched fields.** Vikunja's
  `POST /tasks/{id}` is a full replace, not a merge-patch; a partial call silently reset
  description/priority/due_date/percent_done (and title) to their zero values. Both tools
  now read-merge-write: fetch the task, overlay the changed fields, re-post the full
  object. (ticket #173 / task 183)

### Security
- **Sanitize markdown-converted HTML before writing to Vikunja.** Python-Markdown passes
  embedded raw HTML through unmodified (no `safe_mode` since 3.0), so `_md_to_html()`
  could be used to store `<script>`/event-handler HTML that executes in whoever's browser
  next opens the task. Output is now run through `nh3.clean()` (allowlist-based) before
  being written.
- Bumped `markdown` dependency floor to `>=3.8.1` — versions up to 3.8 are affected by
  CVE-2025-69534 (GHSA-5wmx-573v-2qwq), an unauthenticated DoS via malformed HTML-like
  markdown input.

## [0.2.1]

### Fixed
- `label_update()` sent `PUT /labels/{id}`, which Vikunja 2.3.0 rejects with 405 at the
  router (before auth). Changed to `POST`, matching every other update-by-id route.
  `label_create` (`PUT /labels`) was unaffected. (#4)

## [0.2.0]

### Added
- **Full Vikunja API parity.** New tools covering every remaining resource, all sourced
  from the live Swagger spec and each pinned to the correct verb by a respx wire test:
  - Teams: `team_list/get/create/update/delete`, `team_member_add/remove/toggle_admin`.
  - Project sharing: `project_team_list/add/update/remove`,
    `project_user_list/add/update/remove`, `project_share_list/get/create/delete`
    (permission ints: 0=read, 1=write, 2=admin).
  - Kanban: `bucket_list/create/update/delete`, `task_bucket_move`.
  - Views: `view_list/get/create/update/delete` (list/gantt/table/kanban; done-bucket).
  - Assignees: `task_assignee_list/add/remove`, `task_assignees_add_bulk`.
  - Relations/subtasks: `task_relation_add/remove`.
  - Reminders: `task_reminders_set`.
  - Attachments: `attachment_list/upload/delete` (base64 upload, multipart on the wire).
  - Bulk: `tasks_bulk_update` for migration throughput.
- **Pre/post extension hooks** (`hooks.py`): `register_before`/`register_after`,
  `run_before_hooks`/`run_after_hooks`, `clear_hooks`. Every tool is wrapped by
  `instrument`, so a registered handler is guaranteed to fire around its tool. Handlers run
  in registration order and propagate exceptions (not fire-and-forget).
- **Contrib hooks** (`contrib/`): an args-hashing `audit_log` example that records
  actor/tool/args-hash without ever logging raw arguments or the bearer token, plus a
  README documenting the handler signatures for third parties.
- **Telemetry** (`telemetry.py`): every tool now emits OTLP spans **and** metrics
  (call count, error count, upstream latency). Added optional InfluxDB 3
  (`influxdb3-python`) and NATS (`nats-py`) sinks. All backends are env-gated and off by
  default; credentials are read from the environment only, and the sinks are best-effort
  (a telemetry outage never breaks a tool call).
- `.pre-commit-config.yaml` (ruff check/format + hygiene) mirroring scoped-mcp.
- `docs/extension-hooks.md`, `docs/telemetry.md`, and `docs/vikunja-structure.md` (a
  proposed project-taxonomy contract for the MCP and the future CloudCLI plugin).

### Changed
- `client.request` gained a `files` parameter for multipart attachment uploads.

### Security
Remediations from the pre-merge security audit (0C/0H/1M/1L/3Info):
- **webhook_create** now enforces an MCP-side SSRF guard — rejects `target_url` hosts that
  are loopback/RFC1918/link-local/reserved or `.local`/`.internal` (resolving hostnames
  best-effort), independent of Vikunja's own outgoing-request filter (F-02, Medium).
- Telemetry: the blocking InfluxDB write is offloaded to a worker thread so a hung endpoint
  can't stall the event loop; fire-and-forget sink tasks are retained to avoid GC dropping
  them (F-01/F-03).
- **attachment_upload** validates base64 (`binascii.Error` → `VikunjaAPIError`) and caps
  decoded size at 25 MiB (F-04).
- **project_share_create** couples `password`↔`sharing_type` so a share can't be created
  weaker than intended (F-05).
- Path-segment encoding for `relation_kind`/`username` (IV-01, from the pre-audit baseline).

### Notes
- Vikunja exposes **no** `GET /filters` list endpoint; saved filters appear as pseudo-
  projects (negative IDs) via `project_list`, so no `filter_list` tool was added.

## [0.1.0]

### Added
- Initial `vikunja-mcp` FastMCP server.
- Token-passthrough auth model: the caller's Vikunja bearer token is read per request and
  forwarded upstream; the server holds no credentials and fails closed on a missing token.
- Tools for projects, tasks (incl. BM25 search), labels + task-label attach/detach,
  comments, saved filters, and project webhooks. Endpoint coverage sourced from the live
  Vikunja Swagger spec (`/api/v1/docs.json`).
- `whoami` for verifying per-agent token wiring through scoped-mcp.
- CI (lint + matrix tests on 3.11–3.13, coverage floor 80%), action versions pinned to SHAs.
