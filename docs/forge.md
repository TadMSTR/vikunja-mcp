# Forge deployment

How `vikunja-mcp` is deployed on forge and wired into scoped-mcp.

Originally written against `vikunja-migration-2026-07` (its Phases 9 and 11). The topology
and grant matrix below were re-verified against the live deployment on 2026-08-04; where
this doc and the deployment disagreed, the deployment won and this doc was corrected.

## Topology

```mermaid
flowchart LR
    subgraph agent["each agent's scoped-mcp process (AGENT_ID fixed)"]
      SM[scoped-mcp<br/>mcp_proxy module]
    end
    VLT[(Vault<br/>secret/data/vikunja/agent-*)] -.->|approle, {agent_type}| SM
    SM -->|Authorization: Bearer token| MCP[vikunja-mcp<br/>127.0.0.1:8501]
    MCP -->|same token, verbatim| API[(Vikunja /api/v1)]
```

There is **one** `vikunja-mcp` process. Every agent's own scoped-mcp instance proxies to it,
injecting that agent's Vikunja token as the `Authorization` header. `vikunja-mcp` forwards
the token untouched — it stores nothing.

## PM2

`ecosystem.config.js` points at `/opt/appdata/vikunja-mcp/run.sh`:

```bash
#!/bin/bash
set -euo pipefail                     # fail closed if the env file is missing/unreadable
set -a
source /opt/appdata/vikunja-mcp/env   # VIKUNJA_URL etc. — NO token here
set +a
exec /opt/venvs/vikunja-mcp/bin/vikunja-mcp
```

```bash
python -m venv /opt/venvs/vikunja-mcp
/opt/venvs/vikunja-mcp/bin/pip install /home/ted/repos/personal/vikunja-mcp
pm2 start ecosystem.config.js && pm2 save
```

**Verifying it came up.** There is no `/health` route — `curl http://127.0.0.1:8501/health`
returns 404, and an earlier revision of this doc suggested it as the check. Use one of:

```bash
pm2 describe vikunja-mcp | grep -E 'status|uptime'
pm2 logs vikunja-mcp --lines 20 --nostream | grep vikunja_mcp_start   # reports version
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8501/mcp    # 406, i.e. listening
```

`/mcp` answering 406 is a healthy result: the endpoint is up and refusing a request that
lacks the MCP content-type. A connection refused or a 502 is the failure signal. Note the
path has no trailing slash — `/mcp/` answers 307 and redirects.

The `env` file holds only non-secret config (`VIKUNJA_URL`, `VIKUNJA_PORT`, `LOG_LEVEL`).
No Vikunja token is ever written here — that is the point of the passthrough model.

## scoped-mcp manifest (Phase 9)

Add this module to **each agent's** manifest. The per-agent Vikunja token lives at
`secret/data/vikunja/agent-<role>` (KV v2, key `token`) and is pulled by scoped-mcp's own
Vault credential source, then substituted into the header as `${token}`.

The block below is transcribed from the **live** manifests in `/etc/forge/manifests/`
(read 2026-08-04), not from the original build plan. An earlier revision of this doc
documented a `secret/data/vikunja/agent-{agent_type}` Vault path and a `${token}` header
variable; neither matches what is deployed.

```yaml
# credentials block is manifest-scoped — reconcile with the agent's existing source.
credentials:
  source: vault
  vault:
    addr: ${VAULT_ADDR}
    auth: approle
    path: "agents/{agent_type}"
    kv_version: 2
    role_id_env: VAULT_ROLE_ID
    secret_id_env: VAULT_SECRET_ID

modules:
  vikunja-mcp:
    type: mcp_proxy
    config:
      url: http://127.0.0.1:8501/mcp             # no trailing slash — /mcp/ 307-redirects
      headers:
        Authorization: "Bearer ${VIKUNJA_TOKEN}"
      tool_allowlist: [ ... see grant matrix below ... ]
```

### Grant matrix

**State as deployed**, read from `/etc/forge/manifests/*-agent.yml` on 2026-08-04. Every
agent's live grant was wider than what this doc previously claimed — in every case wider,
never narrower:

| Agent | Previously documented | Deployed 2026-08-04 | Target |
|-------|----------------------|---------------------|--------|
| sysadmin | all (no allowlist) | all (no allowlist) | unchanged |
| developer | 14 tools | **71 — the entire surface** | **23** (below) |
| research | 6 tools | 22 | out of scope, see id 349 |
| writer | 7 tools | 8 | out of scope, see id 349 |
| security | 5 tools | 16 (read-only shape held) | out of scope, see id 349 |

The old 14-tool developer row was never deployed. It also predates the ratified flat
taxonomy and granted `project_create` and `label_create`, which now work *against* that
taxonomy. It is recorded above as history, not as a target.

The cross-agent reconciliation for research/writer/security is tracked separately as
vikunja id 349 (`#335`) and is deliberately not attempted here.

#### developer — 23 tools

Built from what the role's workflow actually needs (file a ticket, label it, link it,
comment on it, move it across the board, close it), with every destructive and
administrative tool removed.

| Group | Tools | Why |
|-------|-------|-----|
| Identity | `whoami` | verify token wiring |
| Projects (read) | `project_list`, `project_get` | flat taxonomy — read only, never create |
| Tasks | `task_list`, `task_search`, `task_get`, `task_create`, `task_update` | core filing + status |
| Labels | `label_list`, `label_get`, `task_label_add`, `task_label_remove` | attach/detach from the ratified vocabulary; never mutate the vocabulary itself |
| Comments | `comment_list`, `comment_create` | build notes on a ticket |
| Relations | `task_relation_add`, `task_relation_remove` | multi-repo build grouping |
| Assignees (read) | `task_assignee_list` | read only |
| Attachments | `attachment_list`, `attachment_upload` | attach a failing test log or diff |
| Kanban | `view_list`, `view_get`, `bucket_list`, `task_bucket_move` | read the board and move a ticket across it |
| Backlinks | `task_link_commit` | record the commit/PR that closed a ticket (v0.8.0) |

**New in v0.8.0 and not yet granted anywhere.** `task_link_commit` is the one tool added
since this table was ratified on 2026-08-04. It writes only to the description footer of a
ticket the agent could already `task_update`, so it grants no reach the role does not have
— it is a narrower way to do something already permitted. It is listed below, but the
manifests are root-owned and unchanged by this repo; granting it is a steward proposal.

```yaml
tool_allowlist:
  - whoami
  - project_list
  - project_get
  - task_list
  - task_search
  - task_get
  - task_create
  - task_update
  - label_list
  - label_get
  - task_label_add
  - task_label_remove
  - comment_list
  - comment_create
  - task_relation_add
  - task_relation_remove
  - task_assignee_list
  - attachment_list
  - attachment_upload
  - view_list
  - view_get
  - bucket_list
  - task_bucket_move
  - task_link_commit   # v0.8.0 — not yet in the live manifest
```

#### developer — the 48 removed, and why

| Removed | Reason |
|---------|--------|
| `project_delete` | deletes a project **and all its tasks** — catastrophic, never needed |
| `task_delete` | agents close tickets, they do not delete them |
| `tasks_bulk_update` | mutates N tickets in one call; a migration tool, not a developer tool. Do not re-grant even now that the field-wipe bug is fixed |
| `label_create`, `label_update`, `label_delete` | the label vocabulary is ratified; `label_delete` strips the label from every task carrying it |
| `project_create`, `project_update` | the taxonomy is deliberately flat — one project, id 7 |
| `team_*` (8 tools) | team administration is not a developer function |
| `project_team_*`, `project_user_*`, `project_share_*` (11 tools) | access control; `project_share_create` can mint a public link share |
| `webhook_create`, `webhook_delete`, `webhook_events`, `webhook_list` | webhook registration belongs to `vikunja-webhook-listener`; `webhook_create` is the SSRF-relevant surface |
| `filter_*` (4 tools) | saved filters are shared UI objects |
| `view_create/update/delete`, `bucket_create/update/delete` | board structure, not per-ticket work |
| `comment_delete`, `attachment_delete`, `task_assignee_add/remove`, `task_assignees_add_bulk`, `task_reminders_set` | destructive or unused |

**Verified against a real filing cycle.** The full CLAUDE.md ticket-filing path was exercised
end to end while writing this doc — `task_create` → 2× `task_label_add` → `task_relation_add`
→ `comment_create` → `task_update` → `task_get`, for tickets id 356 and id 357. Every tool it
touched is in the 23-tool set, so the trim does not break the workflow it is scoped around.

One consequence to note before applying it: `tasks_bulk_update` is on the removed list, and
it is the tool whose data-loss fix this release ships. Its live verification must be run
*before* the trim lands, because afterwards developer cannot call it.

Reload scoped-mcp after editing manifests so grants take effect. An allowlist that has not
been observed rejecting anything has not been observed working — confirm by calling a
removed tool and getting a refusal.

> **Integration note.** `credentials` is a single manifest-level source. If an agent's
> manifest already reads other secrets from a different Vault path, the Vikunja `token` key
> must be reachable from the same resolved credential set (co-locate the keys or split by a
> mechanism scoped-mcp supports). Confirm against the agent's current manifest before merging
> — do not blindly overwrite an existing `credentials` block.

## Webhooks (Phase 10 — separate `vikunja-webhook-listener`)

Not part of this server. If that listener were to register a Vikunja webhook via
`webhook_create`, `target_url` would need to be genuinely external to forge — **not** a
SWAG hostname. Split-horizon DNS resolves every `*.helmforge.me` name to its LAN address, so
`vikunja-mcp`'s SSRF guard classifies SWAG-fronted vhosts as internal and refuses them, the
same as any other private address. Set the webhook `secret` so the listener can verify
`X-Vikunja-Signature`, once a valid external target exists.

As of this writing there is no valid target on this deployment:
`vikunja-webhook-listener` binds the Docker bridge gateway (`172.20.1.1:8502`,
container-reachable only), has no SWAG proxy-conf, and no agent is granted `webhook_create`.
This is a gap to close by adding a real external target, not by weakening the guard.

The SSRF check that enforces this runs **in `vikunja-mcp`**, not upstream: forge's Vikunja
container sets `VIKUNJA_OUTGOINGREQUESTS_ALLOWNONROUTABLEIPS=true`, which disables
Vikunja's own filter. See `SECURITY.md` for what the MCP-side guard does and does not
cover.
