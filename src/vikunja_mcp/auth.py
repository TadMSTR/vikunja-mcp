"""Per-request credential resolution — the token-passthrough model.

This server holds **no** Vikunja tokens. Each agent's scoped-mcp instance injects that
agent's own Vikunja API token as the ``Authorization`` header on every proxied request
(the manifest ``headers`` block, resolved from Vault by scoped-mcp's credential
machinery). This module lifts that token off the incoming request and hands it to the
client, which forwards it upstream unchanged.

Why this shape: the blast radius of a compromise of *this* process is a single in-flight
request's token, never the full set of five agent credentials — which is exactly what a
Vault-brokering design would have to hold. Per-agent attribution in Vikunja is preserved
for free, because every call reaches Vikunja as the agent that made it.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_http_headers

from .exceptions import AuthError


def caller_token() -> str:
    """Return the caller's Vikunja bearer token from the current HTTP request.

    Raises:
        AuthError: if no Authorization header / bearer token is present (fail closed).
    """
    # SECURITY: get_http_headers() strips `authorization` by default — it is on the
    # library's internal deny-forward list to stop accidental credential leakage to
    # downstream services. The whole point of this server is to read it, so we must
    # explicitly opt it back in. Omitting include= here would return {} and make every
    # authenticated call look anonymous.
    headers = get_http_headers(include={"authorization"})
    raw = headers.get("authorization", "").strip()
    if not raw:
        raise AuthError(
            "No Authorization header on request. vikunja-mcp requires the caller's "
            "Vikunja bearer token, injected by the per-agent scoped-mcp manifest."
        )

    # Split scheme from token. A lone "Bearer" (no value) must fail closed rather than be
    # mistaken for a raw token — hence scheme-aware parsing, not a plain prefix strip.
    scheme, _, rest = raw.partition(" ")
    token = rest.strip() if scheme.lower() == "bearer" else raw
    if not token:
        raise AuthError("Authorization header present but carried no bearer token.")
    return token
