# Security

> Every release is audited before merge by a reviewer independent of the agent that wrote
> it. See [docs/security-audit.md](docs/security-audit.md) for what those audits found —
> one High finding in the project's history, fixed before it shipped.

## Model: token passthrough, no stored credentials

On every network transport, `vikunja-mcp` holds no Vikunja API tokens. Each request must
carry the caller's own Vikunja bearer token in the `Authorization` header; the server
forwards it upstream unchanged and Vikunja itself validates it. Consequences:

- **No ambient authority.** A request with no `Authorization` header is rejected fail-closed
  (`AuthError`) — there is no default or service token to fall back to.
- **Blast radius.** Compromising this process exposes at most the token on an in-flight
  request, never a stored set of agent credentials.
- **Attribution.** Every upstream call is made as the acting agent, so Vikunja's own
  authorization and audit trail apply per agent.

### The stdio exception

`VIKUNJA_TRANSPORT=stdio` is the one mode where the server does hold a token, because it
is the one mode where passthrough is impossible: there is no HTTP request, so there is no
header to lift. Before this was addressed the server started cleanly, registered every
tool, and then failed 100% of tool calls (vikunja#461).

Under stdio, `VIKUNJA_TOKEN` supplies the credential. The exception is kept narrow by two
independent guards, both of which are **hard startup errors, not warnings**:

| Combination | Result |
|---|---|
| `VIKUNJA_TOKEN` set, transport is anything but `stdio` | `ConfigError` — refuses to start |
| transport `stdio`, no `VIKUNJA_TOKEN` | `ConfigError` — refuses to start |

The first is the one that matters. A static token on a shared port would make every caller
reach Vikunja as one identity — silently, with no symptom until someone asks who changed a
ticket. That is precisely what passthrough exists to prevent, so it is refused rather than
documented as a footgun. The check is written against `stdio` (the safe case) rather than
against `http` (one unsafe case), so a future network transport is covered by default.

At runtime the header remains strictly preferred: the fallback is consulted only when
there is no `Authorization` header **and** no HTTP request in scope at all. An HTTP request
that merely forgot its header still fails closed — the two look identical from the header
dict alone, which is why the request-in-scope check exists rather than a bare emptiness
test.

Note the exception does not weaken attribution, because stdio has exactly one caller by
construction: a single subprocess owned by a single client. There is no per-agent identity
to collapse.

## Trust boundaries

- The server binds to `127.0.0.1` only. In production it sits behind each agent's scoped-mcp
  instance, which injects the token from Vault. Tool-level access is enforced by scoped-mcp
  grants, not by this server.
- A local process that already holds a valid Vikunja token could call the port directly; it
  would gain nothing it could not already do by calling Vikunja directly with that token.
- Webhook registration (`webhook_create`) validates `target_url` **in this server**, before
  it reaches Vikunja. `_validate_webhook_target` requires an `http(s)` scheme and refuses a
  host that is loopback, private, link-local, reserved, multicast, unspecified, or carries
  an internal suffix (`.local`, `.internal`, `.lan`, `.home`, `.corp`); hostnames are
  resolved and every returned address is checked.

  This guard can be load-bearing rather than defence in depth, depending on how Vikunja is
  configured. Vikunja has its own outgoing-request SSRF filter, but it can be switched off
  with `VIKUNJA_OUTGOINGREQUESTS_ALLOWNONROUTABLEIPS=true` — and a deployment that has done
  so leaves the MCP-side check as the only thing standing between a webhook registration
  and an internal address. Check your own instance before assuming upstream will catch it,
  and do not weaken this check on that assumption.

  **A public-looking hostname is not automatically a valid target.** The guard judges the
  address a name RESOLVES to, not how the name looks. Under split-horizon DNS — common
  wherever a reverse proxy fronts internal services on a public domain — a hostname that
  looks external resolves to a private address, and the guard refuses it. This bites most
  often on the obvious candidate: the webhook receiver you just deployed behind your own
  reverse proxy. A valid `target_url` must resolve to a genuinely external address. Fix
  the target, not the check.

  **The guard fails closed.** A host that cannot be resolved is refused rather than waved
  through. It was once allowed, on the reasoning that Vikunja re-resolves at delivery — but
  that reasoning is void wherever the upstream filter is disabled, because then the
  delivery-time resolution is itself unguarded. The practical cost is that registering a
  webhook against a host this server cannot currently resolve will be rejected; that is the
  cheaper failure for a rare, deliberate operation.

  Residual limit: validation happens at registration, and Vikunja performs the actual
  delivery in its own process. A name that resolves to a public address when registered and
  is re-pointed to an internal one before delivery is still a TOCTOU window that a
  registration-time check cannot close. Narrowing it further would require either a control
  inside Vikunja (its own filter, currently disabled) or network isolation of the container.

## Reporting

This is a personal homelab project. Report issues via the repository's issue tracker.
