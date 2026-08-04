# Security

## Model: token passthrough, no stored credentials

`vikunja-mcp` holds no Vikunja API tokens. Each request must carry the caller's own Vikunja
bearer token in the `Authorization` header; the server forwards it upstream unchanged and
Vikunja itself validates it. Consequences:

- **No ambient authority.** A request with no `Authorization` header is rejected fail-closed
  (`AuthError`) — there is no default or service token to fall back to.
- **Blast radius.** Compromising this process exposes at most the token on an in-flight
  request, never a stored set of agent credentials.
- **Attribution.** Every upstream call is made as the acting agent, so Vikunja's own
  authorization and audit trail apply per agent.

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

  This guard is load-bearing rather than defence in depth. Vikunja has its own
  outgoing-request SSRF filter, but forge **disables** it — the Vikunja container sets
  `VIKUNJA_OUTGOINGREQUESTS_ALLOWNONROUTABLEIPS=true` (verified 2026-08-04). In this
  deployment the MCP-side check is the only thing standing between a webhook registration
  and an internal address, so do not weaken it on the assumption that upstream will catch
  it. Always target a public SWAG hostname.

  Known limit: a host that does not resolve at registration time is allowed through, since
  it cannot be classified. Vikunja re-resolves at delivery, and with the upstream filter
  off that re-resolution is unguarded — so a DNS entry that is public at registration and
  internal at delivery (DNS rebinding) is not covered here.

## Reporting

This is a personal homelab project. Report issues via the repository's issue tracker.
