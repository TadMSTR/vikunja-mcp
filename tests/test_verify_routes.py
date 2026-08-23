"""Guards for scripts/verify-routes.py's route extraction.

The sweep's value depends entirely on discover_routes actually finding the routes. If
server.py's request() call shape changed and extraction silently returned nothing, the CI
job would pass while probing zero endpoints — the failure mode this file exists to catch.
No network is touched here; only the AST walk is exercised.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify-routes.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_routes", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def vr():
    return _load()


def test_discovers_a_realistic_number_of_routes(vr):
    routes = vr.discover_routes(vr.SERVER_PY)
    # 69 at the time of writing; a floor rather than an equality so adding a tool does not
    # fail this, while extraction collapsing to nothing does.
    assert len(routes) >= 60


def test_every_discovered_route_is_concrete(vr):
    """No unsubstituted f-string braces may survive into a probe URL."""
    for method, path in vr.discover_routes(vr.SERVER_PY):
        assert method.isupper()
        assert "{" not in path and "}" not in path, (method, path)
        assert path.startswith("/")


def test_known_routes_are_present(vr):
    """Spot-check the endpoint whose verb swagger got wrong on v1, and a nested path.

    ``/labels/{id}`` is the route that shipped the v0.2.1 bug: v1 swagger documented PUT
    and the router accepted only POST. v2 routes it as PUT (update), which is what the
    live ``Allow:`` header reports — the reason this file exists is that the header, not
    the spec, is what settles it.
    """
    routes = set(vr.discover_routes(vr.SERVER_PY))
    assert ("PUT", f"/labels/{vr.PROBE_ID}") in routes
    assert ("GET", "/user") in routes
    assert (
        "PUT",
        f"/projects/{vr.PROBE_ID}/views/{vr.PROBE_ID}/buckets/{vr.PROBE_ID}",
    ) in routes


def test_path_rendering_substitutes_every_interpolation(vr):
    node = ast.parse('f"/tasks/{task_id}/relations/{kind}/{other}"', mode="eval").body
    assert vr._path_from_node(node) == (
        f"/tasks/{vr.PROBE_ID}/relations/{vr.PROBE_ID}/{vr.PROBE_ID}"
    )


def test_plain_string_path_is_returned_verbatim(vr):
    node = ast.parse('"/tasks/bulk"', mode="eval").body
    assert vr._path_from_node(node) == "/tasks/bulk"


def test_probe_verb_is_not_a_real_http_method(vr):
    """The sweep is only non-destructive because nothing routes this verb."""
    assert vr.PROBE_VERB not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
