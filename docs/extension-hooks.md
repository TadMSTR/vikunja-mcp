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

> **Catch inside your handler if the tool matters more than the hook.** The contract above
> is not a footnote: a `before` handler that raises turns a convenience feature into a lost
> write, and an `after` handler that raises reports failure for an operation that already
> succeeded — whose natural retry duplicates it. `contrib/duplicate_check.py` wraps the
> *entire* body of both its handlers for exactly this reason, and it is worth reading as the
> worked example: the first draft guarded only the upstream call and left the bookkeeping
> outside, which the test suite caught.

## Shipped hooks

| Module | Fires on | Default | Purpose |
|--------|----------|---------|---------|
| `contrib/audit_log.py` | mutating tools (`before`) | off — `VIKUNJA_AUDIT_LOG=1` | Actor/tool/args-hash trail; never logs raw arguments or the bearer token. |
| `contrib/duplicate_check.py` | `task_create` (`before` + `after`) | **on** — `VIKUNJA_DUPLICATE_CHECK=0` disables | Attaches `possible_duplicates` to the created task. Reports, never refuses. |

Both are wired from `server.register_builtin_hooks` rather than from an entry point in a
deployment, so the wiring stays visible to this repo's tests and code review.

## Example

```python
from vikunja_mcp.hooks import register_before


async def enforce_public_webhook(kwargs: dict) -> dict:
    url = kwargs.get("target_url", "")
    if url.startswith("http://10.") or "localhost" in url:
        raise PermissionError("webhook target must be genuinely external to forge")
    return kwargs


register_before("webhook_create", enforce_public_webhook)
```

See [`../src/vikunja_mcp/contrib/README.md`](../src/vikunja_mcp/contrib/README.md) for a
ready-made args-hashing **audit-log** hook and how to register it for the mutating tools.
