# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
