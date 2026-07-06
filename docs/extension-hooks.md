# Extension hooks

vikunja-mcp exposes a **pre/post hook** system so third parties can intercept tool calls
without editing the server — the same pattern as the other forge MCP servers, extended here
to post-hooks. It follows the `scoped-mcp/hooks.py` convention but, because this is a
single-server process, keys on the **tool name alone** rather than `(server, tool)`.

## Where hooks fire

Every tool is registered through `server.tool`, which wraps it in `server.instrument`:

```
call → run_before_hooks(tool, kwargs) → [telemetry span] → tool(**kwargs)
     → run_after_hooks(tool, result) → return
```

Because the wrapper is applied uniformly, a hook registered for a tool is guaranteed to
fire around every invocation of it — including calls that arrive over MCP.

## API

| Function | Purpose |
|----------|---------|
| `register_before(tool, handler)` | `async def handler(kwargs: dict) -> dict` — inspect/mutate args |
| `register_after(tool, handler)`  | `async def handler(result) -> result` — inspect/transform result |
| `run_before_hooks(tool, kwargs)` | fire the before-chain (called by `instrument`) |
| `run_after_hooks(tool, result)`  | fire the after-chain (called by `instrument`) |
| `clear_hooks()` | drop all registrations (tests only) |

## Contract

- Handlers run in **registration order**; each receives the previous handler's output.
- Handlers are **not** fire-and-forget. An exception propagates to the caller; a `before`
  exception aborts the chain and prevents the tool (and the upstream Vikunja call) running.
- `tool` is the tool's Python function name (`"task_create"`, `"project_share_create"`, …).

## Example

```python
from vikunja_mcp.hooks import register_before

async def enforce_public_webhook(kwargs: dict) -> dict:
    url = kwargs.get("target_url", "")
    if url.startswith("http://10.") or "localhost" in url:
        raise PermissionError("webhook target must be a public SWAG hostname")
    return kwargs

register_before("webhook_create", enforce_public_webhook)
```

See [`../src/vikunja_mcp/contrib/README.md`](../src/vikunja_mcp/contrib/README.md) for a
ready-made args-hashing **audit-log** hook and how to register it for the mutating tools.
