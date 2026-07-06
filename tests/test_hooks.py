"""Extension-hook registry + the server wiring that fires hooks around every tool."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from vikunja_mcp import hooks, server


@pytest.fixture(autouse=True)
def _clean():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


@pytest.fixture
def _patch_calls(monkeypatch):
    mock = AsyncMock(return_value={"id": 1, "title": "orig"})
    monkeypatch.setattr(server, "request", mock)
    monkeypatch.setattr(server, "caller_token", lambda: "TOK")
    return mock


# --- registry unit behaviour ----------------------------------------------


async def test_before_hooks_run_in_registration_order():
    order = []

    async def first(kwargs):
        order.append("first")
        return kwargs

    async def second(kwargs):
        order.append("second")
        return kwargs

    hooks.register_before("task_get", first)
    hooks.register_before("task_get", second)
    await hooks.run_before_hooks("task_get", {})
    assert order == ["first", "second"]


async def test_before_hook_can_mutate_kwargs():
    async def bump(kwargs):
        kwargs["task_id"] = kwargs["task_id"] + 1
        return kwargs

    hooks.register_before("task_get", bump)
    out = await hooks.run_before_hooks("task_get", {"task_id": 4})
    assert out["task_id"] == 5


async def test_after_hook_can_transform_result():
    async def wrap(result):
        return {"wrapped": result}

    hooks.register_after("task_get", wrap)
    out = await hooks.run_after_hooks("task_get", {"id": 1})
    assert out == {"wrapped": {"id": 1}}


async def test_hook_exception_propagates_not_swallowed():
    async def boom(kwargs):
        raise ValueError("nope")

    hooks.register_before("task_get", boom)
    with pytest.raises(ValueError, match="nope"):
        await hooks.run_before_hooks("task_get", {})


async def test_no_hooks_returns_input_unchanged():
    assert await hooks.run_before_hooks("unhooked", {"a": 1}) == {"a": 1}
    assert await hooks.run_after_hooks("unhooked", "x") == "x"


# --- wiring: hooks fire around a real instrumented tool --------------------


async def test_before_hook_fires_and_mutates_the_actual_tool_call(_patch_calls):
    async def redirect(kwargs):
        kwargs["task_id"] = 999
        return kwargs

    hooks.register_before("task_get", redirect)
    await _run(server.task_get, task_id=1)
    # the tool hit the upstream path with the *mutated* id
    assert _patch_calls.call_args.args[:2] == ("GET", "/tasks/999")


async def test_after_hook_fires_and_transforms_tool_result(_patch_calls):
    async def tag(result):
        result["seen"] = True
        return result

    hooks.register_after("task_get", tag)
    out = await _run(server.task_get, task_id=1)
    assert out["seen"] is True


async def test_before_hook_exception_prevents_upstream_call(_patch_calls):
    async def veto(kwargs):
        raise PermissionError("blocked")

    hooks.register_before("task_delete", veto)
    with pytest.raises(PermissionError, match="blocked"):
        await _run(server.task_delete, task_id=1)
    _patch_calls.assert_not_called()


def _fn(tool):
    return tool if callable(tool) and not hasattr(tool, "fn") else tool.fn


async def _run(tool, **kwargs):
    return await _fn(tool)(**kwargs)
