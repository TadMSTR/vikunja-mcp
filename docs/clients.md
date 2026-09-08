# Connecting a client

Configuration for the MCP clients people actually use. Every block below is copy-paste and
was run against `ghcr.io/tadmstr/vikunja-mcp` before being written down.

**Pick your transport first — it decides where the credential lives, and the two options are
not interchangeable.**

| | `stdio` | `http` |
|---|---|---|
| Who launches the server | your MCP client, as a subprocess | you, as a long-running service |
| Callers | exactly one, by construction | many |
| Where the token comes from | `VIKUNJA_TOKEN` on the server | each caller's `Authorization` header |
| Who Vikunja sees | one identity | the identity of whoever called |
| Use it when | a single person is using one client | several agents or people share one server |

The rule that catches people out: **`VIKUNJA_TOKEN` is required for `stdio` and refused for
`http`.** Both are startup failures, not warnings, and both are deliberate — a static token
on a shared port makes every caller reach Vikunja as one identity and silently destroys
per-agent attribution. See [SECURITY.md](../SECURITY.md).

If a client reports the server failed to start, run the config validator — it names the
problem without you having to read a traceback:

```bash
docker run --rm --entrypoint vikunja-mcp ghcr.io/tadmstr/vikunja-mcp:latest --check
```

---

## stdio — single user

The ordinary MCP setup: your client launches the server as a subprocess and talks to it over
stdin/stdout. Nothing listens on a port.

You need a Vikunja API token: **Settings → General → API Tokens** in the Vikunja web UI.
Give it the scopes for what you want the agent to do; read-only is a fine place to start.

### Claude Code

```bash
claude mcp add vikunja \
  --env VIKUNJA_URL=https://vikunja.example.com \
  --env VIKUNJA_TRANSPORT=stdio \
  --env VIKUNJA_TOKEN=your-vikunja-api-token \
  -- docker run -i --rm \
       -e VIKUNJA_URL -e VIKUNJA_TRANSPORT -e VIKUNJA_TOKEN \
       ghcr.io/tadmstr/vikunja-mcp:latest
```

Then `claude mcp list` should show `vikunja` connected, and `/mcp` inside Claude Code lists
its tools.

`-i` is required — without it the container gets no stdin and the client sees a server that
starts and immediately goes quiet. `--rm` keeps a container from accumulating per launch.

### Claude Desktop

Edit `claude_desktop_config.json`:

- macOS — `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows — `%APPDATA%\Claude\claude_desktop_config.json`
- Linux — `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "vikunja": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "VIKUNJA_URL",
        "-e", "VIKUNJA_TRANSPORT",
        "-e", "VIKUNJA_TOKEN",
        "ghcr.io/tadmstr/vikunja-mcp:latest"
      ],
      "env": {
        "VIKUNJA_URL": "https://vikunja.example.com",
        "VIKUNJA_TRANSPORT": "stdio",
        "VIKUNJA_TOKEN": "your-vikunja-api-token"
      }
    }
  }
}
```

Restart Claude Desktop after editing. The file is only read at startup.

### Any client that speaks `.mcp.json`

Most MCP clients — including Claude Code's project-scoped config — accept this shape. Drop
it at the root of your project as `.mcp.json`:

```json
{
  "mcpServers": {
    "vikunja": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "VIKUNJA_URL=https://vikunja.example.com",
        "-e", "VIKUNJA_TRANSPORT=stdio",
        "-e", "VIKUNJA_TOKEN=your-vikunja-api-token",
        "ghcr.io/tadmstr/vikunja-mcp:latest"
      ]
    }
  }
}
```

> **Do not commit a `.mcp.json` with a real token in it.** Prefer the `"env"` form above and
> keep the value in your environment, or use your client's secret handling. A token in a
> committed file is a token in your git history.

### Without Docker

There is **no PyPI package** — the name `vikunja-mcp` is squatted on public PyPI by an
unrelated project, so `pip install vikunja-mcp` installs someone else's code. Install from a
checkout instead:

```bash
git clone https://github.com/TadMSTR/vikunja-mcp && cd vikunja-mcp
pip install .
```

Then use `"command": "vikunja-mcp"` with no `args`, keeping the same `env` block.

---

## http — shared or multi-agent

Run the server once; every caller presents its own Vikunja token. This is the setup the
token-passthrough model exists for, and the reason it is worth the extra moving part is
attribution: each call reaches Vikunja *as the agent that made it*, so task authorship,
comments and audit trails are per-agent for free.

Start it:

```bash
docker run -d --name vikunja-mcp \
  -e VIKUNJA_URL=https://vikunja.example.com \
  -p 127.0.0.1:8501:8501 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --read-only --tmpfs /tmp \
  ghcr.io/tadmstr/vikunja-mcp:latest

curl -fsS http://127.0.0.1:8501/health
```

Note **no `VIKUNJA_TOKEN`** — setting one here is refused at startup.

`-p 127.0.0.1:8501:8501` publishes to loopback only, and that is the actual access control.
Any process that can reach this port can forward any Vikunja token it holds, so treat
reaching the port as equivalent to being able to call Vikunja. Binding `0.0.0.0` *inside*
the container is not an exposure decision — the publish is. See [docker.md](docker.md).

### Claude Code

```bash
claude mcp add --transport http vikunja http://127.0.0.1:8501/mcp \
  --header "Authorization: Bearer your-vikunja-api-token"
```

### `.mcp.json`

```json
{
  "mcpServers": {
    "vikunja": {
      "type": "http",
      "url": "http://127.0.0.1:8501/mcp",
      "headers": {
        "Authorization": "Bearer your-vikunja-api-token"
      }
    }
  }
}
```

### Behind a proxy that injects the token

The intended multi-agent shape: each agent's proxy holds that agent's own token and injects
it, so no client config carries a credential and no two agents share one. See
[deployment.md](deployment.md) for the general pattern.

---

## Verifying it works

```bash
# 1. Is the configuration coherent? (does not contact Vikunja)
docker run --rm -e VIKUNJA_URL=https://vikunja.example.com \
  --entrypoint vikunja-mcp ghcr.io/tadmstr/vikunja-mcp:latest --check

# 2. Is an http server alive? (unauthenticated by design, returns no config)
curl -fsS http://127.0.0.1:8501/health

# 3. Does the client see the tools?
claude mcp list
```

A green `--check` means the *configuration* is coherent. It deliberately does not contact
Vikunja, so it says nothing about whether the instance is reachable or the token is valid —
step 3 is what tells you that.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `VIKUNJA_URL is not set` | No default exists on purpose, so a misconfigured deployment cannot silently point at someone else's instance. Set it. |
| `VIKUNJA_TRANSPORT=stdio requires VIKUNJA_TOKEN` | Under stdio there is no HTTP request to carry a header, so passthrough has nothing to pass through. Set the token. |
| `VIKUNJA_TOKEN is set but VIKUNJA_TRANSPORT is 'http'` | Refused deliberately — see the transport table above. Unset the token and let each caller send its own header. |
| Server starts, every tool call returns `No Authorization header on request` | Running `http` and the client is not sending a bearer. Add the header, or switch to `stdio`. |
| Client shows the server as connected but no tools | Almost always a `stdio` launch without `-i`. |
| `Vikunja API error 401` | The configuration is fine and the token reached Vikunja, which rejected it. Regenerate the token. |
| `Vikunja API error 404` on every call | `VIKUNJA_URL` probably includes `/api/v2`. Give the base URL only; the client appends the suffix. |

Related: [docker.md](docker.md) for the container security model,
[deployment.md](deployment.md) for running behind a proxy,
[../SECURITY.md](../SECURITY.md) for the credential rules in full.
