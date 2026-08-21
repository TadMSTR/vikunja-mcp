# AGENTS.md — vikunja-mcp

Operating contract for Claude sessions working in this repo.

## What this server does

Exposes the Vikunja REST API (`/api/v1`) as MCP tools for projects, tasks, labels,
comments, saved filters, and webhooks. It is a thin, stateless translator — no business
logic, no caching, no persistence.

## Module boundaries

| Module | Responsibility | Must NOT |
|--------|----------------|----------|
| `config.py` | Env-var settings (upstream URL, transport binding) | Hold any Vikunja token |
| `auth.py` | Extract the caller's bearer token from the incoming request | Fall back to any ambient/default credential |
| `client.py` | One pooled httpx client; per-request auth; error mapping; the pagination envelope | Store a token on the client instance |
| `server.py` | Tool definitions + verb/path mapping to Vikunja | Contain HTTP or credential logic inline |
| `exceptions.py` | Typed error hierarchy | — |
| `hooks.py` | Pre/post extension-hook registry, keyed on tool name | Swallow handler exceptions — hooks are not fire-and-forget |
| `telemetry.py` | Optional OTLP spans/metrics, InfluxDB3, NATS — inert unless env-configured | Become a hard import dependency; a missing extra must degrade, not crash |
| `contrib/` | Opt-in add-ons a deployment may register (e.g. `audit_log`) | Be imported unconditionally — the one exception is `audit_log`, gated behind `VIKUNJA_AUDIT_LOG=1` in `register_builtin_hooks` (vikunja#342, id 361); everything else in `contrib/` still needs its own explicit registration |

## Invariants (do not break)

1. **No stored credentials.** This process never reads a Vikunja token from Vault, env, or
   disk. The token arrives per request in the `Authorization` header and is forwarded
   verbatim. This is the whole security model — see `SECURITY.md`.
2. **Fail closed.** A missing or empty `Authorization` header raises `AuthError`; it is
   never treated as anonymous or defaulted.
3. **`authorization` must be explicitly opted into** `get_http_headers(include={...})` — the
   library strips it by default. `tests/test_auth.py::test_authorization_header_is_explicitly_requested`
   guards this; do not remove it.
4. **PUT creates, POST updates** in Vikunja. Keep `*_create` on PUT and `*_update` on POST.
   `tests/test_server.py` pins every mapping.
5. **Partial updates are honored by two different mechanisms — know which one applies.**
   Vikunja is not consistent here, and conflating the two reintroduces a data-loss bug.

   | Resource | Endpoint behaviour | Mechanism | Do not |
   |----------|-------------------|-----------|--------|
   | projects, labels, teams, filters, views, buckets | writes only the fields present in the body | `_drop_none` | send nulls for unspecified fields |
   | a single task (`POST /tasks/{id}`) | **full replace** — omitted columns reset to zero | `_apply_task_update` read-merge-write | "simplify" it to a partial POST — that is ticket #173 |
   | many tasks (`POST /tasks/bulk`) | **full replace per task** | `fields` array naming the columns to write | send a bare `values` object — that is ticket #333 |

   The two task paths are a pair: both exist because task writes are full replaces. The
   single-task path solves it client-side because it must; the bulk path solves it
   server-side via `fields` because N read-merge-writes would mean N GETs and N TOCTOU
   windows. `models.BulkTask.fields` is real but undocumented in swagger — the probe
   recorded on ticket #333 is its specification.

6. **List results may be truncated, and the envelope says so.** `client.request` wraps a
   multi-page list as `{"items": [...], "pagination": {...}}` and returns single-page
   lists bare. Do not make the envelope unconditional (callers would all need updating for
   no gain) and do not remove it (a silently truncated list is a wrong answer, not a short
   one). `_apply_task_update` also drops `related_tasks`/`attachments`/`reactions` from the
   re-posted body — they live in their own tables and echoing them back is what made one
   `task_search` return 155k characters.

7. **`index` and `id` are different numbers, and only `id` may leave this server.**
   `index` is per-project; `id` is global. Their difference is not constant (offsets of 8,
   11 and 19 in one live corpus). `_strip_task_index` removes every bare `index` from every
   response listed in `server._INDEX_STRIPPED_TOOLS`, at any nesting depth — including the
   tasks Vikunja inlines under `related_tasks`, which carry their own. Keep `identifier`:
   it is a string, so it cannot be passed where an int id is expected without an obvious
   type error, and five forge consumers render it. This is vikunja#331 (id 342), where an
   agent passed `index` to `task_label_add` and silently mutated three unrelated tickets.

   `_resolve_task_ref` is the inverse: it accepts `"#454"` on every tool in
   `server._TASK_REF_TOOLS`. Three rules that are load-bearing, not stylistic:

   - **A bare number is always a global id.** `"454"` without a `#` is never a ticket
     number. The `#` is the only thing that distinguishes the two, so guessing without it
     is #331 with extra steps.
   - **A failed resolve raises.** It must never fall back to treating `"#454"` as id 454.
   - **An ambiguous resolve raises and names every candidate.** Never pick one. Ticket
     numbers are unique per project, not globally (`index = 1` matches id 9 in project 7
     and id 344 in project 2, live).

   The annotation `task_id: int | str` is the enabling change, not cosmetic — FastMCP
   derives the schema from `wrapper.__signature__`, so `task_id: int` rejects `"#454"`
   before any before-hook can run. `tests/test_task_refs.py` asserts this against the
   schema FastMCP actually publishes, and asserts the tool list is exactly the set of tools
   whose signature takes a `task_id`, so it cannot drift.

8. **Endpoint semantics verified by probe, not by documentation.** Swagger and the docs are
   both unreliable for this API; each row below is a live probe with a negative control.

   | Behaviour | Probe | Control | Verified on |
   |---|---|---|---|
   | `index` is filterable | `filter=index = 454` → one row, id 473 | `filter=bogusfield = 1` → **400** `The task field 'bogusfield' is invalid` | Vikunja v2.3.0, 2026-08-21 |
   | `index` filter is project-scopable | `filter=project = 7 && index = 454` → `[473]` | `filter=project = 2 && index = 454` → `[]` | same |
   | `index` is **not** sortable | — | `sort_by` accepts `id, title, done, done_at, due_date, start_date, end_date, priority, percent_done, created, updated, position` | same |
   | `s` cannot be combined with `filter` | — | — | Vikunja filter docs; this is why `task_search` and `task_list` stay separate tools |

   The control matters more than the probe: Vikunja *rejects* unknown filter fields rather
   than ignoring them, which is what proves the `index` filter is genuinely applied
   server-side rather than silently dropped. Note that `index` is **not** in Vikunja's
   documented filter-field list — the accepted set matches the docs in neither direction
   (`bucket_id` works, `position` 500s). Ticket-reference resolution therefore depends on
   undocumented behaviour, deliberately and with a live canary test guarding it.

9. **The audit trail is opt-in via env, not always-on.** Set `VIKUNJA_AUDIT_LOG=1` and
   `VIKUNJA_AUDIT_LOG_DIR=<path>` to have `register_builtin_hooks` wire
   `contrib/audit_log.py` for the mutating tool set (`server._AUDITED_TOOLS`), writing one
   line per call to `<dir>/YYYY-MM-DD.md`. Unset `VIKUNJA_AUDIT_LOG` to turn it back off.
   `VIKUNJA_AUDIT_LOG=1` with no dir set raises `ConfigError` at startup rather than
   defaulting to stdout, where it would mix into PM2 logs. The trail records that a call
   happened and a pseudonymous actor hash — not what changed, and not a reversible agent
   identity (vikunja#342, id 361).

## Test expectations

- `pytest --cov=vikunja_mcp` — 80% floor (enforced in `pyproject.toml`).
- Security-critical negative tests live in `tests/test_auth.py`: missing/blank/prefix-only
  tokens must all be rejected. Never delete these to make coverage easier.
- `test_server.py` asserts verb + path + body for each tool without touching the network;
  add a case there when you add a tool.

## scoped-mcp manifest (forge)

Fronted by scoped-mcp on port 8501 via the `mcp_proxy` module. The per-agent Vikunja token
is injected as the `Authorization` header by the manifest's `headers` block (resolved from
Vault). See `docs/forge.md` for the full manifest and grant matrix.

<!-- SECURITY[control]: This server intentionally has no internal credential store. Auth is
the caller-supplied Vikunja token, validated upstream by Vikunja itself. Tool-level access is
enforced by scoped-mcp grants. Do not add a static service token as a fallback — that would
re-introduce the multi-token blast radius this design exists to avoid. -->
