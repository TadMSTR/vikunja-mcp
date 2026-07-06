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
- Webhook registration (`webhook_create`) forwards `target_url` to Vikunja, which enforces
  its own SSRF protection (rejects RFC1918 destinations). Always target public hostnames.

## Reporting

This is a personal homelab project. Report issues via the repository's issue tracker.
