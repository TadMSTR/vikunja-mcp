"""Example ``before`` hook: audit-log every call to a tool without leaking secrets.

Emits one structured log line per call recording *who* (a stable, non-reversible hash of
the caller's bearer token — never the token itself), *what* (the tool name), and a
**hash** of the arguments. Argument values are never logged in the clear, so a tool that
carries a webhook secret, a share password, or PII in a description does not spill it into
the audit trail — only a digest that lets you correlate identical calls.

Register it for the tools you want audited (typically the mutating ones)::

    from vikunja_mcp.contrib.audit_log import register_audit_log

    register_audit_log([
        "task_create", "task_update", "task_delete",
        "project_create", "project_delete",
        "team_create", "project_team_add", "project_user_add",
        "project_share_create", "webhook_create",
    ])

This satisfies the forge tool-audit directive (acting agent + tool + args-hash). To route
the line into ``~/.claude/comms/artifacts/tool-audit/`` instead of stdout, pass your own
``logger`` (any object with an ``info(event, **fields)`` method).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from typing import Any

import structlog

from ..auth import caller_token
from ..exceptions import AuthError
from ..hooks import register_before

_default_log = structlog.get_logger("vikunja_mcp.audit")


def _digest(value: Any) -> str:
    """Stable short SHA-256 digest of a JSON-serialisable value."""
    blob = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _actor_id() -> str:
    """Pseudonymous, non-reversible identifier for the calling agent.

    Derived from a hash of the bearer token so the same agent is correlatable across calls
    without the audit log ever holding the credential. Returns ``"anonymous"`` when there
    is no caller token (e.g. a direct, unauthenticated call).
    """
    try:
        token = caller_token()
    except AuthError:
        return "anonymous"
    return "agent:" + hashlib.sha256(token.encode()).hexdigest()[:16]


def audit_log_hook(tool: str, logger: Any | None = None) -> Callable[[dict], Any]:
    """Build a ``before`` handler that audit-logs calls to ``tool``.

    Args:
        tool: the tool name this handler will be registered for (logged verbatim).
        logger: optional sink with an ``info(event, **fields)`` method; defaults to a
            structlog logger. The kwargs are hashed, never logged raw.
    """
    sink = logger or _default_log

    async def handler(kwargs: dict) -> dict:
        sink.info(
            "vikunja_tool_call",
            tool=tool,
            actor=_actor_id(),
            args_hash=_digest(kwargs),
        )
        return kwargs

    return handler


def register_audit_log(tools: Iterable[str], logger: Any | None = None) -> None:
    """Register the audit-log hook for each tool name in ``tools``."""
    for tool in tools:
        register_before(tool, audit_log_hook(tool, logger=logger))
