# AGENTS.md — vikunja-mcp

Operating contract for Claude sessions working in this repo.

## What this server does

Exposes the Vikunja REST API (`/api/v2`) as MCP tools for projects, tasks, labels,
comments, saved filters, and webhooks. It is a thin, stateless translator — no business
logic, no caching, no persistence.

**Minimum Vikunja is 2.4.0**, the release that shipped `/api/v2`. The version is applied in
`client._api_base()`, not configured — dual-version support was considered and rejected.

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
4. **POST creates, PUT replaces, PATCH merges** on v2. Keep `*_create` on POST and
   `*_update` on PUT. `tests/test_server.py` pins every mapping and
   `scripts/verify-routes.py` checks it against the live router.

   Two things to know before touching a verb. First, this **inverts** v1's idiom
   (PUT-creates / POST-updates), so a global find-and-replace in either direction corrupts
   the set it is not aimed at — work call sites individually. Second, one route does not
   follow the pattern: `POST /teams/{team}/members/{user}/admin` is a toggle, not an
   update, and is POST on both versions.

5. **Partial updates are honored by two different mechanisms — know which one applies.**

   | Resource | Endpoint behaviour | Mechanism | Do not |
   |----------|-------------------|-----------|--------|
   | projects, labels, teams, filters, views, buckets | writes only the fields present in the body | `_drop_none` | send nulls for unspecified fields |
   | a single task (`PATCH /tasks/{id}`) | JSON Merge Patch — omitted fields untouched | `_apply_task_update`, one request | reintroduce a read-merge-write, or route this through `PUT /tasks/{id}` — that is ticket #173 |
   | many tasks (`PUT /tasks/bulk`) | **full replace per task** | `fields` array naming the columns to write | send a bare `values` object — that is ticket #333 |

   The two task paths were a pair on v1: both existed because task writes were full
   replaces, and the single-task path paid for it with a GET, a merged body and a TOCTOU
   window. v2's `PATCH` deletes that half of the problem. The bulk half remains, because
   v2 routes only `PUT` on `/tasks/bulk` — no PATCH exists there. What changed for it is
   documentation: `fields` was undocumented in v1 swagger and the probe on ticket #333 was
   its only specification; v2's spec now states it outright.

   **`related_tasks` is populated on v2 list rows**, with each related task's full body
   inlined (measured: 3.7–10 KB per entry). Nothing echoes it back any more — there is no
   merged body to echo it into — but any new code that reads a list must keep projecting
   it away, which is invariant 6's job.

6. **List results may be truncated, and the envelope says so.** v2 answers every list with
   `{"items", "total", "page", "per_page", "total_pages"}`; `client.request` reshapes that
   into `{"items": [...], "pagination": {...}}` when it spans pages and returns single-page
   lists bare. Do not make the envelope unconditional (callers would all need updating for
   no gain) and do not remove it (a silently truncated list is a wrong answer, not a short
   one).

   `pagination.total` is the size of the result set and `pagination.count` is the rows in
   this response. They are not interchangeable, and at `per_page=1` they happen to be
   equal — which is what makes a page-counting regression easy to miss. Pass
   `unwrap_list=False` when you want the raw envelope; `_count_matching` does, because it
   reads `total` off a page that deliberately holds no rows.

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
   | `index` is filterable | `filter=index = 454` → one row, id 473 | `filter=bogusfield = 1` → **400** `The task field 'bogusfield' is invalid` | v2.3.0 2026-08-21; re-run on `/api/v2` @ v2.5.0, 2026-08-23 |
   | `index` filter is project-scopable | `filter=project = 7 && index = 454` → `[473]` | `filter=project = 2 && index = 454` → `[]` | same |
   | `index` is **not** sortable | — | `sort_by` accepts `id, title, done, done_at, due_date, start_date, end_date, priority, percent_done, created, updated, position` | v2.3.0, 2026-08-21 |
   | `q` cannot be combined with `filter` | — | — | Vikunja filter docs; this is why `task_search` and `task_list` stay separate tools |
   | a page past the end reports the real `total` with no rows | `per_page=1&page=1000000` → `total=237`, `items=[]`, 145 bytes | `page=1` → same `total`, one row, 4122 bytes | `/api/v2` @ v2.5.0, 2026-08-23 |
   | v2's documented `by-index` route is **unusable** with an API token | `GET /projects/7/tasks/by-index/509` → **401**, incl. on a task the same token had just created | every other route the server uses → 200 with that token | same |

   That last row is why `#N` resolution still goes through the undocumented `index` filter
   rather than the documented route the port was expected to adopt. The route is collected
   for token permissions as group `projects`, permission `tasks_by_index` (upstream
   `pkg/models/api_routes.go`), which no token minted before v2 carries — and this server
   forwards the caller's token, so it cannot grant itself one. Tracked in vikunja#514.

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
