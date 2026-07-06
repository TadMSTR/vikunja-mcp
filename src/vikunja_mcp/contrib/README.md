# vikunja-mcp contrib hooks

Example [extension hooks](../hooks.py) you can register into vikunja-mcp without editing
the server. Nothing here runs unless you register it at startup.

## Handler signatures

Hooks fire around **every** tool call (the server wraps each tool in `instrument`).

| Kind | Register with | Signature | Contract |
|------|---------------|-----------|----------|
| pre  | `register_before(tool, handler)` | `async def handler(kwargs: dict) -> dict` | return the (possibly modified) kwargs |
| post | `register_after(tool, handler)`  | `async def handler(result: Any) -> Any`  | return the (possibly transformed) result |

- Handlers run in **registration order**; each receives the previous one's output.
- Handlers are **not** fire-and-forget: an exception propagates to the caller. A `before`
  exception prevents the tool from running at all.
- `tool` is the tool's function name, e.g. `"task_create"`, `"project_share_create"`.

## Registering

Do it once, at process start (e.g. from your own launcher before `server.main()`):

```python
from vikunja_mcp.hooks import register_before, register_after

async def redact_secret(kwargs: dict) -> dict:
    kwargs.pop("secret", None)   # inspect / mutate arguments
    return kwargs

register_before("webhook_create", redact_secret)

async def stamp(result):
    if isinstance(result, dict):
        result["_audited"] = True
    return result

register_after("task_get", stamp)
```

## `audit_log.py` — args-hashing audit trail

A ready-made `before` hook that logs **who / what / args-hash** for each audited tool and
never logs argument values or the bearer token in the clear:

```python
from vikunja_mcp.contrib.audit_log import register_audit_log

register_audit_log([
    "task_create", "task_update", "task_delete",
    "project_create", "project_delete",
    "team_create", "project_team_add", "project_user_add",
    "project_share_create", "webhook_create",
])
```

Each call emits a structured line:

```json
{"event": "vikunja_tool_call", "tool": "project_share_create",
 "actor": "agent:9f2b1c…", "args_hash": "4a7d…"}
```

- **actor** is a non-reversible SHA-256 prefix of the caller's token — stable per agent,
  never the credential itself.
- **args_hash** is a digest of the kwargs, so identical calls correlate without any value
  (webhook secrets, share passwords, task descriptions) reaching the log.

Pass your own `logger=` (anything with `info(event, **fields)`) to route the line into
`~/.claude/comms/artifacts/tool-audit/` instead of stdout.
