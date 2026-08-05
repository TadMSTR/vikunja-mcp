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
| `contrib/` | Opt-in add-ons a deployment may register (e.g. `audit_log`) | Be imported by `server.py` implicitly — registration is the deployment's choice |

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
