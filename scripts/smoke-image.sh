#!/usr/bin/env bash
#
# Smoke test for the container image.
#
# ONE DEFINITION, TWO CALL SITES: ci.yml runs this on every PR, release.yml runs it BEFORE
# pushing to GHCR. The PR path and the publish path must not be able to drift apart, because
# the one that matters is the one nobody watches. release.yml used to build and push with no
# smoke test at all — a test that runs after the push tells you what you have already shipped.
#
# Usage: scripts/smoke-image.sh <image-ref>
#
# Every check below asserts a property this service actually has. That constraint is the point:
# a gate that goes red for a reason unrelated to what it is for cannot distinguish what it is
# for. In particular, see the CONTRACT section — the obvious "401 unauthenticated / 200 with a
# credential" shape is WRONG for this server and asserting it would have been a gate that
# passes only by accident.

set -euo pipefail

IMAGE="${1:?usage: smoke-image.sh <image-ref>}"
CONTAINER="vikunja-mcp-smoke-$$"
PORT="${SMOKE_PORT:-8599}"
WORKDIR="$(mktemp -d)"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

fail() { echo "::error::$*"; exit 1; }
ok()   { echo "  ok — $*"; }

# ---------------------------------------------------------------------------------------------
# 1. Fail-closed startup
#
# Building proves the image assembles; it does not prove it runs. get_settings() raises when
# VIKUNJA_URL is unset and that loud failure is the point — a baked default would let a
# misconfigured deployment silently point at someone else's Vikunja instance.
# ---------------------------------------------------------------------------------------------
echo "[1/6] refuses to start with no VIKUNJA_URL"
set +e
out=$(docker run --rm "$IMAGE" 2>&1); status=$?
set -e
[ $status -ne 0 ] || fail "server started without VIKUNJA_URL — the fail-closed default is gone"
echo "$out" | grep -q "VIKUNJA_URL is not set" \
  || fail "exited non-zero but not for the expected reason: $out"
ok "exits $status with the expected message"

# ---------------------------------------------------------------------------------------------
# 2. Non-root
# ---------------------------------------------------------------------------------------------
echo "[2/6] runs as non-root"
uid=$(docker run --rm --entrypoint id "$IMAGE" -u)
[ "$uid" != "0" ] || fail "image runs as root"
ok "uid $uid"

# ---------------------------------------------------------------------------------------------
# 3. Artefact contents
#
# Asserted against the exported filesystem rather than trusting .dockerignore. The two-stage
# build is what keeps the source tree out; this is what proves it still does. `usr/bin/test` is
# coreutils and is excluded explicitly — a pattern loose enough to match it would go red for a
# reason unrelated to what this check is for.
# ---------------------------------------------------------------------------------------------
echo "[3/6] shipped layer carries no source, tests, config or secrets"
cid=$(docker create "$IMAGE")
docker export "$cid" > "$WORKDIR/img.tar"
docker rm -f "$cid" >/dev/null
tar -tf "$WORKDIR/img.tar" > "$WORKDIR/manifest.txt"

# Control: the manifest must be populated, or every grep below is vacuously clean.
entries=$(wc -l < "$WORKDIR/manifest.txt")
[ "$entries" -gt 1000 ] || fail "export produced only $entries entries — the checks below would be vacuous"

leaked=$(grep -Ev '(^|/)site-packages/' "$WORKDIR/manifest.txt" \
  | grep -E '(^|/)(\.env|\.git|pyproject\.toml|conftest\.py|\.pytest_cache|tests?)(/|$)' \
  | grep -Ev '^usr/bin/test$' || true)
[ -z "$leaked" ] || fail "shipped layer contains build context or tests:"$'\n'"$leaked"

grep -q '^src/' "$WORKDIR/manifest.txt" && fail "the build stage's /src survived into the runtime image"
ok "$entries entries, no source tree, no build context, no test files"

# ---------------------------------------------------------------------------------------------
# 4. Dependency audit of what is actually INSTALLED
#
# EVERY site-packages tree in the image, not just the venv. The base image ships its own pip in
# /usr/local/lib/python3.*/site-packages, which a venv-only audit never looks at — an
# unaudited tree in a shipped artefact is exactly the gap this gate exists to close.
#
# THE POPULATION ASSERTION IS THE GATE, NOT PADDING. `pip-audit --strict --path <empty dir>`
# exits 0 printing "No known vulnerabilities found" (measured, pip-audit 2.10.1). So every way
# extraction can silently yield nothing — a moved venv, a Python minor bump renaming
# python3.13, a changed WORKDIR — turns this green while auditing thin air.
#
# vikunja_mcp's own dist-info is removed before auditing: the package is deliberately
# unpublished (the name is namesquatted on PyPI — see release.yml), so --strict aborts with
# "Dependency not found on PyPI" and would take every other distribution down with it.
# ---------------------------------------------------------------------------------------------
echo "[4/6] audits every site-packages tree in the image"
trees=$(docker run --rm --entrypoint find "$IMAGE" / -name site-packages -type d 2>/dev/null || true)
[ -n "$trees" ] || fail "found no site-packages tree in the image"

found_project=0
total=0
i=0
while IFS= read -r tree; do
  [ -n "$tree" ] || continue
  i=$((i + 1))
  dest="$WORKDIR/sp-$i"
  mkdir -p "$dest"
  cid=$(docker create "$IMAGE")
  docker cp "$cid:$tree/." "$dest/" >/dev/null 2>&1 || true
  docker rm -f "$cid" >/dev/null
  if ls -d "$dest"/vikunja_mcp-*.dist-info >/dev/null 2>&1; then
    found_project=1
    rm -rf "$dest"/vikunja_mcp-*.dist-info
  fi
  count=$(find "$dest" -maxdepth 1 -name '*.dist-info' | wc -l)
  total=$((total + count))
  echo "    $tree — $count distributions"
  [ "$count" -gt 0 ] || continue
  uv tool run pip-audit --strict --path "$dest"
done <<< "$trees"

# Control 1 — did we extract THIS project's venv, or an empty/foreign tree?
[ "$found_project" -eq 1 ] \
  || fail "no vikunja_mcp dist-info in any tree — pip-audit --path would report a green audit of nothing"
# Control 2 — is the result populated? ~91 across both trees at the time of writing.
[ "$total" -ge 50 ] \
  || fail "only $total distributions audited across all trees, expected ~91 — the audit was vacuous"
ok "$total distributions audited across $i tree(s)"

# ---------------------------------------------------------------------------------------------
# 5. /health is unauthenticated and echoes no config
# ---------------------------------------------------------------------------------------------
echo "[5/6] /health serves unauthenticated and leaks no config"
docker run -d --name "$CONTAINER" -e VIKUNJA_URL=https://vikunja.invalid \
  -p "127.0.0.1:$PORT:8501" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER")" = "healthy" ] && break
  sleep 2
done
[ "$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER")" = "healthy" ] || {
  docker logs "$CONTAINER"; fail "container never became healthy"; }

body=$(curl -fsS "http://127.0.0.1:$PORT/health")
echo "$body" | grep -q '"status":"ok"' || fail "/health did not report ok: $body"
# `if grep` rather than `grep && exit 1`: the latter returns non-zero from the compound when
# grep finds nothing, failing the step on the success path.
if echo "$body" | grep -qi "vikunja.invalid\|token"; then
  fail "/health echoed config — it is unauthenticated by design"
fi
ok "$body"

# ---------------------------------------------------------------------------------------------
# 6. THE SERVICE CONTRACT
#
# READ THIS BEFORE "FIXING" IT TO A 401/200 SHAPE. vikunja-mcp holds no Vikunja credentials: it
# lifts the caller's bearer token off the Authorization header and forwards it upstream
# unchanged (the token-passthrough model, src/vikunja_mcp/auth.py). Consequences, both measured
# against a running container rather than assumed:
#
#   - There is NO transport-level auth. POST /mcp `initialize` with no Authorization header
#     returns HTTP 200. Asserting 401 there would assert something this server has never done.
#   - There is no "correct credential" it can validate offline. Any well-formed bearer is
#     accepted by THIS process and adjudicated by Vikunja.
#
# So the contract is at the tool-call layer, and it is a pair of DISTINGUISHABLE failures:
#
#   no Authorization header  -> AuthError naming the passthrough model   (fails closed)
#   a bearer token present   -> upstream connection error, NOT AuthError (passthrough engaged)
#
# The second case is the control the first one needs. Without it, a server that failed every
# call for any reason would pass the first assertion — which is why the negative assertion on
# the auth string is not redundant. It is also the "something only this service could have
# produced" requirement: another service holding the port can return 200, but it cannot produce
# this specific pair.
# ---------------------------------------------------------------------------------------------
echo "[6/6] tool-call contract: fails closed without a token, engages passthrough with one"
U="http://127.0.0.1:$PORT/mcp"
HDRS=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')

SID=$(curl -si -X POST "$U" "${HDRS[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  | grep -i '^mcp-session-id:' | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] || fail "no mcp-session-id returned from initialize — the MCP endpoint is not answering"

curl -fsS -o /dev/null -X POST "$U" "${HDRS[@]}" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

call_tool() {
  curl -fsS -X POST "$U" "${HDRS[@]}" -H "mcp-session-id: $SID" "$@" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"project_list","arguments":{}}}'
}

AUTH_ERR="No Authorization header on request"

noauth=$(call_tool)
echo "$noauth" | grep -q "$AUTH_ERR" \
  || fail "a tool call with no Authorization header did not fail closed: $noauth"
ok "no token -> fails closed with the passthrough AuthError"

withauth=$(call_tool -H "Authorization: Bearer smoke-test-not-a-real-token")
if echo "$withauth" | grep -q "$AUTH_ERR"; then
  fail "a tool call WITH a bearer token still raised the no-header AuthError — passthrough never engaged: $withauth"
fi
echo "$withauth" | grep -q "request to Vikunja failed" \
  || fail "expected an upstream failure against vikunja.invalid, got: $withauth"
ok "token present -> passthrough engaged, reaches upstream and fails there"

echo
echo "smoke test passed for $IMAGE"
