# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
