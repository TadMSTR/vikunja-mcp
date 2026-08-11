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
    kwargs.pop("secret", None)  # inspect / mutate arguments
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
never logs argument values or the bearer token in the clear.

**`server.py` already wires this** — it is not purely an example. Set two env vars and
restart:

```
VIKUNJA_AUDIT_LOG=1
VIKUNJA_AUDIT_LOG_DIR=/path/to/audit/directory
```

`register_builtin_hooks()` then registers it for `server._AUDITED_TOOLS` (the mutating
tools: `task_create`, `task_update`, `tasks_bulk_update`, `task_delete`, `project_create`,
`project_delete`, `team_create`, `project_team_add`, `project_user_add`,
`project_share_create`, `webhook_create`), logging through `FileAuditLogger`, which appends
to `<VIKUNJA_AUDIT_LOG_DIR>/YYYY-MM-DD.md` — never stdout, so it doesn't mix into process
logs. **Unset `VIKUNJA_AUDIT_LOG` to turn it back off.** Setting `VIKUNJA_AUDIT_LOG=1` with
no directory configured raises `ConfigError` at startup rather than silently falling back to
stdout.

To register it for a different tool set, or route it elsewhere, call it directly instead of
relying on the env gate:

```python
from vikunja_mcp.contrib.audit_log import FileAuditLogger, register_audit_log

register_audit_log(
    ["task_create", "task_update", "task_delete", "webhook_create"],
    logger=FileAuditLogger("/path/to/audit/directory"),
)
```

Each call emits a structured line:

```json
{"event": "vikunja_tool_call", "tool": "project_share_create",
 "actor": "agent:9f2b1c…", "args_hash": "4a7d…"}
```

- **actor** is a non-reversible SHA-256 prefix of the caller's token — stable per agent,
  never the credential itself. There is currently no hash→agent mapping, so the trail
  answers "did agent X call this" only if you already know X's token hash, not "which agent
  did this" from the line alone.
- **args_hash** is a digest of the kwargs, so identical calls correlate without any value
  (webhook secrets, share passwords, task descriptions) reaching the log.

Pass any object with an `info(event, **fields)` method as `logger=` to route elsewhere
instead of `FileAuditLogger` — a structlog logger, for instance, if stdout/PM2 log mixing is
acceptable for your deployment.
