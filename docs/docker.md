# Running vikunja-mcp in Docker

The published image is the recommended way to run this server if you are not already
managing Python venvs on a Debian host.

```bash
docker run -d --name vikunja-mcp \
  -e VIKUNJA_URL=https://vikunja.example.com \
  -p 127.0.0.1:8501:8501 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --read-only --tmpfs /tmp \
  ghcr.io/tadmstr/vikunja-mcp:latest
```

Then check it is up — `/health` needs no credentials:

```bash
curl -fsS http://127.0.0.1:8501/health
# {"status":"ok","version":"0.6.0"}
```

A `docker-compose.yml` is included at the repo root as a starting point. Two lines in it
are decisions you have to make rather than defaults you can accept: `VIKUNJA_URL` and the
`ports` publish.

> **Do not `pip install vikunja-mcp`.** The name is squatted on public PyPI by an unrelated
> package. This project is not published there and does not intend to be; the container
> image and the git repo are the only distribution channels.

## Configuration

| Variable | Default in image | Notes |
|---|---|---|
| `VIKUNJA_URL` | *(none — refuses to start)* | Base URL of your Vikunja, **without** the `/api/v1` suffix. |
| `VIKUNJA_HOST` | `0.0.0.0` | Overridden from the repo default of `127.0.0.1`. See below. |
| `VIKUNJA_PORT` | `8501` | Change this and the `ports` mapping together. |
| `VIKUNJA_TRANSPORT` | `http` | Leave it. A container is only useful over HTTP. |
| `VIKUNJA_TOKEN` | *(unset)* | **Refused on this transport.** stdio only — see [Tokens](#tokens-and-why-the-container-holds-none). |
| `VIKUNJA_DEFAULT_PROJECT_ID` | *(unset)* | Scopes what a bare `#12` reference resolves against. Unset means an ambiguous reference raises rather than guessing. |
| `VIKUNJA_REQUEST_TIMEOUT` | `30` | Upstream timeout, seconds. |
| `LOG_LEVEL` | `INFO` | |
| `VIKUNJA_AUDIT_LOG` / `_DIR` | *(unset)* | Opt-in. Needs a writable mount — see [Audit log](#audit-log). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | The `[telemetry]` extra **is** installed in the image, so this works out of the box. Use gRPC (`:4317`). |

`VIKUNJA_URL` has no default on purpose. An unset value is a hard startup error rather than
a fallback, so a half-configured deployment cannot quietly end up pointing at the wrong
Vikunja instance.

### About `VIKUNJA_HOST=0.0.0.0`

The image overrides the repo's loopback default because a container that binds `127.0.0.1`
binds *its own* loopback and is unreachable from anywhere else, including the host.

This is not an exposure decision, and treating it as one leads people to "harden" it back
to a broken state. A bind address is a no-op as a security control inside a network
namespace — the wildcard there is not the host's wildcard. **The `ports` publish is the
actual control.** `-p 127.0.0.1:8501:8501` reaches the host loopback only; `-p 8501:8501`
reaches your network. Pick deliberately.

## Tokens, and why the container holds none

This server does not store Vikunja credentials. Every tool call carries the caller's own
Vikunja API token in the `Authorization` header and the server forwards it upstream
unchanged, so Vikunja sees whoever actually made the call. A request without a token is
rejected — there is no ambient fallback.

That has a useful consequence for a container: **there is no secret in the image, no secret
in the environment, and nothing to leak from a compromise beyond a single in-flight
request's token.**

`VIKUNJA_TOKEN` exists but is **refused at startup on any network transport**, including
the `http` the container runs. It is not an oversight and not a footgun to work around: a
single static token on a shared port would make every caller reach Vikunja as one identity,
destroying the per-caller attribution the passthrough model exists to provide. If you want
a single-user setup, that is what stdio is for — run the package directly, not the
container, and see the README.

Point your MCP client at `http://<host>:8501/mcp` and have it send your token.

## Health

`GET /health` is unauthenticated and returns `{"status": "ok", "version": "..."}` and
nothing else. Treat its output as public — that is why it carries no configuration.

It is a **liveness** check and deliberately does not probe upstream Vikunja. A Vikunja
restart therefore does not mark this container unhealthy: the server is stateless and
recovers on its own, so coupling the two would only buy needless restarts. If you want a
readiness signal that reflects upstream, that belongs at a separate `/ready`.

The image ships a `HEALTHCHECK` against it, so `docker ps` and compose report health with
no extra configuration. Note that `/mcp` answers **406** to a bare `GET` (it wants an SSE
`Accept` header and an MCP handshake), so do not point a healthcheck at it and do not read
that 406 as "authenticated".

## Audit log

The container runs with a read-only root filesystem, which is safe by default because the
optional audit log is the only path this server ever writes to. To enable it, give it
somewhere to write:

```yaml
    environment:
      VIKUNJA_AUDIT_LOG: "1"
      VIKUNJA_AUDIT_LOG_DIR: /var/log/vikunja-mcp
    volumes:
      - ./audit-log:/var/log/vikunja-mcp
```

The container runs as uid/gid **1000**, so `chown 1000:1000 ./audit-log` on the host.

## Webhooks: a caveat worth reading before you file a bug

`webhook_create` validates `target_url` **in this server**, before it reaches Vikunja, and
refuses loopback, private, link-local, reserved and multicast addresses along with the
`.local`, `.internal`, `.lan`, `.home` and `.corp` suffixes. Hostnames are resolved and
every returned address is checked.

If your Vikunja and your webhook receiver are both containers on one bridge network, your
receiver is on a private IP and **registration will be refused**. That is the guard working
as designed, not a bug.

Escape hatches, in preference order:

1. Give the receiver a publicly resolvable address and register that.
2. Register the webhook through Vikunja's own UI or API directly, bypassing this server.
   The guard is this server's, not Vikunja's.
3. If you are certain and control the whole network, patch `_host_is_blocked` in a fork.
   There is intentionally no environment variable to disable it — an SSRF guard that can be
   turned off by config is one that will be turned off by config.

**`VIKUNJA_URL` is not subject to this guard.** The two look like the same problem and are
not: pointing the server at `http://vikunja:3456` on a compose network works fine. Only
webhook *targets* are filtered.

## Building it yourself

```bash
docker build -t vikunja-mcp:local .
```

The base image is pinned by digest and both stages are wheel-only — no compiler ends up in
the runtime image. `python:3.13-slim` rather than alpine is deliberate: `nh3` is a Rust
extension and the `[telemetry]` extra pulls grpc wheels, and musl wheel coverage for those
is the kind of gap that turns into a build toolchain in your runtime image.
