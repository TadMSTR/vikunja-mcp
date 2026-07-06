"""Pre/post extension-hook registry for vikunja-mcp.

Third parties can register handlers that fire **before** a tool runs (to inspect or
mutate its arguments) and **after** it returns (to inspect or transform its result),
without editing the server. This mirrors the ``scoped-mcp/hooks.py`` convention but is
extended to post-hooks and, because this is a single-server process, keys on the tool
name alone (scoped-mcp keys on ``(server, tool)``).

API::

    from vikunja_mcp.hooks import register_before, register_after

    async def redact(kwargs: dict) -> dict:
        kwargs.pop("secret", None)
        return kwargs

    register_before("webhook_create", redact)

    async def tag(result):
        return {"wrapped": result}

    register_after("task_get", tag)

The server wires ``run_before_hooks``/``run_after_hooks`` around every tool (see
``server.instrument``), so a registered handler is guaranteed to fire for its tool.

Handler contract:
- ``before`` handler: ``async def handler(kwargs: dict) -> dict`` — return the (possibly
  modified) kwargs. Each handler receives the output of the previous one.
- ``after`` handler: ``async def handler(result: Any) -> Any`` — return the (possibly
  transformed) result. Each handler receives the output of the previous one.
- Handlers run in **registration order**. They are **not** fire-and-forget: an exception
  raised inside a handler propagates to the caller and aborts the chain. A ``before``
  exception prevents the tool from running; an ``after`` exception surfaces after the
  upstream call already happened.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# tool name -> ordered list of async handlers.
_before: dict[str, list[Callable[..., Any]]] = {}
_after: dict[str, list[Callable[..., Any]]] = {}


def register_before(tool: str, handler: Callable[..., Any]) -> None:
    """Register an async pre-call hook for a tool.

    Args:
        tool: the tool's name (its Python function name, e.g. ``"task_create"``).
        handler: ``async def handler(kwargs: dict) -> dict``.
    """
    _before.setdefault(tool, []).append(handler)


def register_after(tool: str, handler: Callable[..., Any]) -> None:
    """Register an async post-call hook for a tool.

    Args:
        tool: the tool's name (its Python function name, e.g. ``"task_get"``).
        handler: ``async def handler(result: Any) -> Any``.
    """
    _after.setdefault(tool, []).append(handler)


async def run_before_hooks(tool: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Run all registered pre-call hooks for ``tool`` in registration order.

    Returns the final kwargs (possibly modified). Unchanged if no hooks are registered.
    """
    for handler in _before.get(tool, []):
        kwargs = await handler(kwargs)
    return kwargs


async def run_after_hooks(tool: str, result: Any) -> Any:
    """Run all registered post-call hooks for ``tool`` in registration order.

    Returns the final result (possibly transformed). Unchanged if no hooks are registered.
    """
    for handler in _after.get(tool, []):
        result = await handler(result)
    return result


def clear_hooks() -> None:
    """Remove all registered hooks. Intended for tests only."""
    _before.clear()
    _after.clear()
