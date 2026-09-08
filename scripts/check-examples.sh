#!/usr/bin/env bash
#
# Validate everything under examples/.
#
# WHY THIS EXISTS: Flagship requires examples be runnable and exercised by CI, and the
# reason is specific rather than aspirational — compose examples rot precisely because
# nothing runs them, so they stay syntactically present and functionally wrong.
#
# Two halves, because the two kinds of example fail differently:
#
#   compose  can be stood up, so it is stood up and probed.
#   skills   are markdown and cannot be executed. The check with real value is that every
#            tool name a skill references still EXISTS in the server's tool list — that is
#            what catches a skill going stale against a renamed or removed tool, which is
#            how these actually break.
#
# TWO THINGS THIS SCRIPT LEARNED THE HARD WAY, both on its first run:
#
#   1. IT COPIES THE EXAMPLES BEFORE TOUCHING THEM. The first version ran `sed -i` on the
#      repo's own compose files to point them at the CI image, and left the working tree
#      modified. A script that edits the thing it is checking is a script that can quietly
#      commit its own scaffolding.
#   2. IT PICKS A FREE PORT AND PROVES THE ANSWER CAME FROM ITS OWN CONTAINER. The first
#      version hardcoded 8501, which was already bound on the dev host by an unrelated
#      vikunja-mcp. `docker compose up` failed, and the /health probe then answered 200
#      "status":"ok" — FROM THE OTHER CONTAINER. The check passed while testing nothing.
#      A port in use makes a smoke test describe somebody else's service.
#
# Usage: scripts/check-examples.sh <image-ref>

set -euo pipefail

IMAGE="${1:?usage: check-examples.sh <image-ref>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"

cleanup() {
  for d in "$WORKDIR"/compose/*/; do
    [ -d "$d" ] || continue
    (cd "$d" && COMPOSE_PROJECT_NAME="vkmcp-ci-$(basename "$d")" VIKUNJA_URL=https://vikunja.invalid \
       docker compose down -v --remove-orphans >/dev/null 2>&1) || true
  done
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

fail() { echo "::error::$*"; exit 1; }

# A port nothing else holds. Asking the kernel for one beats picking a number and hoping.
free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

# Work on a COPY. Never mutate the repo's own examples.
cp -a "$ROOT/examples" "$WORKDIR/"
mv "$WORKDIR/examples/compose" "$WORKDIR/compose"

# =============================================================================================
# 1. Compose examples actually stand up
# =============================================================================================
echo "[1/3] compose examples stand up and answer /health"

found=0
for dir in "$WORKDIR"/compose/*/; do
  name=$(basename "$dir")
  found=$((found + 1))
  port=$(free_port)
  echo "  --- $name (host port $port)"
  (
    cd "$dir"
    export COMPOSE_PROJECT_NAME="vkmcp-ci-$name"
    export VIKUNJA_URL=https://vikunja.invalid
    export VIKUNJA_DEFAULT_PROJECT_ID=
    export VIKUNJA_SUMMARY_EXCLUDE_IDS=

    # Validate the file AS WRITTEN before overriding anything.
    docker compose config -q || { echo "::error::$name: compose config is invalid"; exit 1; }

    # Override rather than edit: the example must be tested as it ships, with only the
    # image (must be THIS commit's, or the gate says nothing about the change under
    # review) and the host port (must not collide) redirected.
    # `!override`, not a plain list: compose MERGES sequences from an override file by
    # appending, so a plain `ports:` would ADD the CI port and keep the example's 8501 —
    # which then collides with anything already on 8501 and fails the whole run for a
    # reason unrelated to the example. Measured: that is exactly what happened first time.
    cat > docker-compose.override.yml <<EOF
services:
  vikunja-mcp:
    image: ${IMAGE}
    ports: !override ["127.0.0.1:${port}:8501"]
EOF

    # uid/gid 1000 inside the container; the full example mounts an audit-log directory
    # and the server fails closed if it cannot write there.
    [ -d audit-log ] && chmod 777 audit-log

    docker compose up -d --quiet-pull \
      || { docker compose logs; echo "::error::$name: compose up failed"; exit 1; }

    # CONTROL: prove the thing we are about to probe is OUR container. Without this, a
    # port collision turns this whole job into a test of whatever else is listening.
    cid=$(docker compose ps -q vikunja-mcp)
    [ -n "$cid" ] || { echo "::error::$name: no container id — compose up did not start it"; exit 1; }
    mapped=$(docker port "$cid" 8501/tcp | head -1)
    case "$mapped" in
      *:"$port") ;;
      *) echo "::error::$name: container maps 8501 to '$mapped', not the probed port $port"; exit 1 ;;
    esac

    for _ in $(seq 1 40); do
      curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1 && break
      sleep 2
    done
    body=$(curl -fsS "http://127.0.0.1:${port}/health") \
      || { docker compose logs; echo "::error::$name never answered /health"; exit 1; }
    echo "$body" | grep -q '"status":"ok"' \
      || { echo "::error::$name /health did not report ok: $body"; exit 1; }
    echo "      /health -> $body"

    # The full example CLAIMS the audit-log directory is writable by the container. That
    # claim is why the volume and the chown note are in the file, so assert it rather than
    # trusting it — a read-only root plus a wrong-owner mount fails closed at the first
    # write, long after startup, which is exactly what an example gets wrong silently.
    if [ -d audit-log ]; then
      docker compose exec -T vikunja-mcp \
        sh -c 'touch /var/log/vikunja-mcp/.ci-probe && rm /var/log/vikunja-mcp/.ci-probe' \
        || { echo "::error::$name: audit-log volume is not writable by the container's uid"; exit 1; }
      echo "      audit-log volume writable by uid 1000"
    fi

    docker compose down -v --remove-orphans >/dev/null 2>&1
  ) || fail "compose example '$name' failed"
done

# Control: a glob that matched nothing would make the loop above vacuously green.
[ "$found" -ge 2 ] || fail "expected at least 2 compose examples, found $found"
echo "  ok — $found compose example(s) stood up and answered"

# =============================================================================================
# 2. Skill frontmatter and links
# =============================================================================================
echo "[2/3] skill frontmatter and links"
python3 - "$ROOT" <<'PY'
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
skills = sorted((root / "examples/skills").glob("*.md"))
if len(skills) < 1:
    print("::error::no skills found under examples/skills/"); sys.exit(1)
bad = 0
for s in skills:
    text = s.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        print(f"::error::{s.name}: no YAML frontmatter"); bad += 1; continue
    fm = m.group(1)
    for field in ("name", "description", "tools"):
        if not re.search(rf"^{field}:", fm, re.M):
            print(f"::error::{s.name}: frontmatter missing '{field}'"); bad += 1
    name = re.search(r"^name:\s*(\S+)", fm, re.M)
    if name and name.group(1) != s.stem:
        print(f"::error::{s.name}: frontmatter name '{name.group(1)}' != filename '{s.stem}'"); bad += 1
    for link in re.findall(r'\[[^\]]*\]\(([^)#][^)]*)\)', text):
        t = link.split('#')[0].strip()
        if not t or t.startswith(("http://", "https://", "mailto:")):
            continue
        if not (s.parent / t).exists():
            print(f"::error::{s.name}: dead link {t}"); bad += 1
if bad:
    sys.exit(1)
print(f"  ok — {len(skills)} skill(s), frontmatter and links valid")
PY

# =============================================================================================
# 3. Every tool a skill names still exists
#
# This is the check with real value. A skill referencing a renamed or removed tool is broken
# in a way no markdown linting detects, and it fails at use time in somebody else's session.
#
# The tool list is read from the RUNNING IMAGE over a real MCP handshake, not from a grep of
# the source — a decorator can be present and the tool still not registered.
# =============================================================================================
echo "[3/3] every tool named in a skill exists in the server's tool list"

# The trailing `sleep` is load-bearing, not padding. Closing stdin ends the stdio session,
# and the server can shut down before it has flushed the tools/list response — measured at
# 1 failure in 5 without it, 0 in 8 with it. An intermittently red gate is worse than no
# gate: it trains people to re-run instead of read.
{ printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check-examples","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'; sleep 5; } \
| docker run -i --rm \
    -e VIKUNJA_URL=https://vikunja.invalid \
    -e VIKUNJA_TRANSPORT=stdio \
    -e VIKUNJA_TOKEN=check-examples-not-a-real-token \
    "$IMAGE" > "$WORKDIR/mcp.jsonl" 2>/dev/null || true

python3 - "$ROOT" "$WORKDIR/mcp.jsonl" <<'PY'
import json, pathlib, re, sys
root, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

tools = None
for line in out.read_text().splitlines():
    line = line.strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line.startswith("{"):
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("id") == 2 and "result" in d:
        tools = {t["name"] for t in d["result"]["tools"]}

# CONTROL: a failed handshake must not fall through to a vacuous pass. An empty tool set
# would make "is this tool known" answer the same way for every name.
if not tools:
    print("::error::could not read tools/list from the image — the check below would be vacuous")
    sys.exit(1)
if len(tools) < 50:
    print(f"::error::only {len(tools)} tools returned, expected ~73 — tools/list looks truncated")
    sys.exit(1)
print(f"  server advertises {len(tools)} tools")

# The declared `tools:` frontmatter list is authoritative and every entry must exist. Names
# scraped from the prose are checked too, but only where they look like a tool call — the
# body also contains plain English and Python builtins, and flagging those would make this
# gate red for a reason unrelated to what it is for.
KNOWN_PROSE = {
    "backlog_summary", "label_list", "label_create", "task_label_add", "task_label_remove",
    "task_list", "task_get", "task_search", "comment_list", "comment_create",
    "tasks_bulk_update", "project_list", "project_get", "whoami", "task_create",
    "task_update", "task_assignee_list", "attachment_list", "attachment_upload",
    "view_list", "view_get", "bucket_list", "task_bucket_move", "task_relation_add",
    "task_relation_remove", "label_get",
}
bad = 0
for s in sorted((root / "examples/skills").glob("*.md")):
    text = s.read_text()
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    declared = re.search(r"^tools:\s*\[(.*?)\]", fm, re.M | re.S)
    named = {t.strip() for t in declared.group(1).split(",") if t.strip()} if declared else set()
    named |= {n for n in re.findall(r'\b([a-z][a-z0-9_]{3,})\(', text) if n in KNOWN_PROSE}
    unknown = sorted(n for n in named if n not in tools)
    if unknown:
        print(f"::error::{s.name}: references tool(s) that do not exist: {', '.join(unknown)}")
        bad += 1
    else:
        print(f"  ok — {s.stem}: {len(named)} tool reference(s), all present")
sys.exit(1 if bad else 0)
PY

echo
echo "examples check passed"
