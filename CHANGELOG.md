# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
