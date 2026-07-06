# Forge deployment

How `vikunja-mcp` is deployed on forge and wired into scoped-mcp. This is the reference for
build-plan **Phase 9 (manifest)** and **Phase 11 (PM2 + wiring)** of
`vikunja-migration-2026-07`.

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
set -a
source /opt/appdata/vikunja-mcp/env   # VIKUNJA_URL etc. — NO token here
set +a
exec /opt/venvs/vikunja-mcp/bin/vikunja-mcp
```

```bash
python -m venv /opt/venvs/vikunja-mcp
/opt/venvs/vikunja-mcp/bin/pip install /home/ted/repos/personal/vikunja-mcp
pm2 start ecosystem.config.js && pm2 save
curl -s http://127.0.0.1:8501/health   # or the FastMCP health path
```

The `env` file holds only non-secret config (`VIKUNJA_URL`, `VIKUNJA_PORT`, `LOG_LEVEL`).
No Vikunja token is ever written here — that is the point of the passthrough model.

## scoped-mcp manifest (Phase 9)

Add this module to **each agent's** manifest. The per-agent Vikunja token lives at
`secret/data/vikunja/agent-<role>` (KV v2, key `token`) and is pulled by scoped-mcp's own
Vault credential source, then substituted into the header as `${token}`.

```yaml
# credentials block is manifest-scoped — reconcile with the agent's existing source.
# The vault path interpolates {agent_type}: developer -> agent-developer, etc.
credentials:
  source: vault
  vault:
    addr: https://vault.helmforge.me
    auth: approle
    path: "secret/data/vikunja/agent-{agent_type}"
    kv_version: 2

modules:
  vikunja-mcp:
    type: mcp_proxy
    config:
      url: http://127.0.0.1:8501/mcp/     # verify path against nextcloud-mcp's live manifest
      headers:
        Authorization: "Bearer ${token}"  # ${token} = key from the Vault secret above
      tool_allowlist: [ ... see grant matrix below ... ]
```

### Grant matrix

Per-agent `tool_allowlist` (replaces the plan's placeholder tool names with the ones this
server actually exposes):

| Agent | Tools |
|-------|-------|
| sysadmin | *(all — omit `tool_allowlist`)* |
| developer | `project_list, project_get, project_create, task_list, task_search, task_get, task_create, task_update, label_list, label_create, task_label_add, comment_list, comment_create, whoami` |
| research | `project_list, task_list, task_search, task_get, task_create, whoami` |
| writer | `project_list, task_list, task_get, task_create, task_update, comment_create, whoami` |
| security | `project_list, task_list, task_search, task_get, whoami` |

Reload scoped-mcp after editing manifests so grants take effect.

> **Integration note.** `credentials` is a single manifest-level source. If an agent's
> manifest already reads other secrets from a different Vault path, the Vikunja `token` key
> must be reachable from the same resolved credential set (co-locate the keys or split by a
> mechanism scoped-mcp supports). Confirm against the agent's current manifest before merging
> — do not blindly overwrite an existing `credentials` block.

## Webhooks (Phase 10 — separate `vikunja-webhook-listener`)

Not part of this server. When that listener registers Vikunja webhooks via `webhook_create`,
the `target_url` **must** be a public SWAG hostname — Vikunja refuses delivery to RFC1918
addresses (SSRF guard). Set the webhook `secret` so the listener can verify
`X-Vikunja-Signature`.
