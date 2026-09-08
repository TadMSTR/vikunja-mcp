# Documentation

Start with the [README](../README.md) quickstart if you just want a working client.

## Setting up

| Doc | Read it when |
|---|---|
| [clients.md](clients.md) | Connecting Claude Code, Claude Desktop, or any `.mcp.json` client. Copy-paste config for both transports, plus a troubleshooting table |
| [docker.md](docker.md) | Running the container — env vars, the security model, the audit-log mount, the webhook caveat |
| [deployment.md](deployment.md) | One server, several agents, each reaching Vikunja as itself. Token wiring and the per-agent grant model |

## Using it

| Doc | Read it when |
|---|---|
| [markers.md](markers.md) | Embedding structured markers in task descriptions and comments — the format, and what a marker value may not contain |
| [vikunja-structure.md](vikunja-structure.md) | Conventions for laying out projects and labels |
| [extension-hooks.md](extension-hooks.md) | Changing behaviour without editing the server — the pre/post hook registry, with worked examples |
| [telemetry.md](telemetry.md) | Turning on metrics or tracing. The full backend matrix (OTLP, InfluxDB 3, NATS) and the two-step trap |

## Understanding it

| Doc | Read it when |
|---|---|
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | Module boundaries, the request lifecycle, and the six invariants a change should not break |
| [../SECURITY.md](../SECURITY.md) | The credential rules, the SSRF guard, and how to report a vulnerability |
| [../AGENTS.md](../AGENTS.md) | Working on this repo with a coding agent — module boundaries and the traps that have caused real bugs |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Opening a PR: setup, what CI checks, house style |

## Reference

- [../CHANGELOG.md](../CHANGELOG.md) — what changed, and why
- [`examples/`](../examples/) — runnable compose files and sample agent skills, exercised by CI
- [`scripts/smoke-image.sh`](../scripts/smoke-image.sh) — what the image is actually asserted to do

---

**The two things most people trip over first:**

1. `VIKUNJA_TOKEN` is **required** for `stdio` and **refused** for `http`. Both are startup
   failures. [clients.md](clients.md) has the reasoning.
2. `#454` is a ticket index, unique only *per project*. `454` is a global task id. They are
   different address spaces and the server never guesses between them.
