#!/usr/bin/env python3
"""Verify every implemented (method, path) against Vikunja's live router.

Why this exists
---------------
`tests/test_server.py` pins each tool's verb by asserting the code sends what the code
sends — it has no reference to the actual API, so it cannot catch a wrong verb. Swagger
cannot serve as that reference either: live Vikunja v2.3.0 `docs.json` documents
`PUT /labels/{id}` and no `POST /labels/{id}`, while reality is the exact opposite
(`PUT` -> 405, `POST` -> 404 "label does not exist"). That divergence is why the v0.2.1
label bug reached production.

Vikunja's Echo router answers an unknown verb with `405` and an `Allow:` header listing
the methods a path really accepts. That is a non-destructive ground truth: the probe verb
matches no handler, so nothing is ever executed. This script sends that probe for every
route `server.py` implements and asserts the implemented method appears in `Allow`.

Usage
-----
    VIKUNJA_URL=https://vikunja.example python scripts/verify-routes.py

Exits 0 if every route agrees with the router, 1 on any mismatch, and 0 with a skip notice
when no target URL is set (so an opt-in CI job stays green rather than failing closed).

**No credential is needed.** Echo answers the route-mismatch 405 before auth middleware
runs, verified across all 69 routes with no Authorization header. Do not configure a
VIKUNJA_TOKEN secret for this — an unnecessary CI secret is only blast radius. If one is
present in the environment it is forwarded (so this still works should a future Vikunja
authenticate before routing), and it is never logged, echoed, or included in output.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

import httpx

# A verb no handler is registered for. Echo replies 405 + Allow: rather than routing it.
PROBE_VERB = "FROBNICATE"

# Substituted for every interpolated path segment. Never resolves to a real object, and
# is irrelevant anyway — routing is matched on the path pattern, not on existence.
PROBE_ID = "999999"

SERVER_PY = Path(__file__).resolve().parent.parent / "src" / "vikunja_mcp" / "server.py"


def _path_from_node(node: ast.expr) -> str | None:
    """Render a request() path argument, replacing interpolations with the probe id."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                out.append(PROBE_ID)
            else:  # pragma: no cover - defensive
                return None
        return "".join(out)
    return None


def discover_routes(source: Path) -> list[tuple[str, str]]:
    """Extract every (METHOD, path) pair passed to client.request() in server.py.

    Derived from the AST rather than a hand-maintained list, so a tool added without a
    corresponding entry here cannot silently escape the sweep.
    """
    tree = ast.parse(source.read_text(), filename=str(source))
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.id if isinstance(node.func, ast.Name) else None
        if fname != "request" or len(node.args) < 2:
            continue
        method_node, path_node = node.args[0], node.args[1]
        if not (isinstance(method_node, ast.Constant) and isinstance(method_node.value, str)):
            continue
        path = _path_from_node(path_node)
        if path:
            routes.add((method_node.value.upper(), path))
    return sorted(routes)


def check(client: httpx.Client, base: str, method: str, path: str) -> tuple[bool, str]:
    """Probe one route. Returns (ok, detail)."""
    url = f"{base.rstrip('/')}/api/v2/{path.lstrip('/')}"
    try:
        resp = client.request(PROBE_VERB, url)
    except httpx.RequestError as exc:
        return False, f"request failed: {exc.__class__.__name__}"

    allow = resp.headers.get("allow")
    if not allow:
        # No Allow header means Echo did not match the path to any route at all.
        return False, f"no Allow header (status {resp.status_code}) — path may not exist"

    accepted = {m.strip().upper() for m in allow.split(",") if m.strip()}
    if method in accepted:
        return True, allow
    return False, f"router accepts {sorted(accepted)}, code sends {method}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=os.environ.get("VIKUNJA_URL"), help="Vikunja base URL")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    if not args.url:
        # Skip, do not fail: this is opt-in and not every environment configures a target.
        print("SKIP: VIKUNJA_URL is not set; nothing probed.")
        return 0

    # No credential is required. Echo matches the route and answers 405 + Allow: before any
    # auth middleware runs — verified 2026-08-04 across all 69 routes with no Authorization
    # header at all, and again with a deliberately invalid one. The sweep therefore needs no
    # live Vikunja token, and none should be configured for it: a CI secret that is never
    # needed is pure blast radius. A token is still forwarded when one happens to be set, so
    # this keeps working if a future Vikunja authenticates before routing.
    headers = {}
    token = os.environ.get("VIKUNJA_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    routes = discover_routes(SERVER_PY)
    if not routes:
        print("FAIL: no routes discovered — did server.py's request() call shape change?")
        return 1

    failures: list[str] = []
    with httpx.Client(timeout=args.timeout, headers=headers) as client:
        for method, path in routes:
            ok, detail = check(client, args.url, method, path)
            status = "ok  " if ok else "FAIL"
            print(f"{status} {method:6} {path:55} {detail}")
            if not ok:
                failures.append(f"{method} {path}: {detail}")

    print()
    print(f"{len(routes)} routes checked, {len(failures)} mismatched")
    if failures:
        print("\nMismatches:")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nA mismatch means the code's verb disagrees with the live router. Trust the "
            "router: swagger is known wrong for /labels/{id}."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
