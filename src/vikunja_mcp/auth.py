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

**One exception, and it is deliberately narrow.** Under ``transport=stdio`` there is no
HTTP request, so there is no header to lift and passthrough cannot work at all — the
server used to start cleanly and then fail every tool call (vikunja#461). In that mode
only, ``VIKUNJA_TOKEN`` supplies the credential. It is a single-user escape hatch: stdio
has exactly one caller by construction, so there is no attribution to collapse. The
combination that *would* collapse it — a static token on a network transport — is refused
at startup in ``config.py``, and the fallback here additionally requires that no HTTP
request be in scope, so the two guards are independent.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_http_headers, get_http_request

from .config import get_settings
from .exceptions import AuthError


def _in_http_request() -> bool:
    """True when a real HTTP request is in scope.

    This is the discriminator that keeps the stdio fallback from ever applying to a
    network call. An empty header dict alone cannot tell the two cases apart: a proxied
    HTTP request that simply forgot its ``Authorization`` header looks identical to stdio,
    where no request exists at all. Only the first must fail closed.
    """
    try:
        get_http_request()
    except RuntimeError:
        return False
    return True


def caller_token() -> str:
    """Return the acting Vikunja token — the caller's header, or the stdio fallback.

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
        # The header is strictly preferred and is never overridden while one is present —
        # the fallback is reached only when there is no header *and* no request at all.
        if not _in_http_request():
            token = get_settings().token
            if token:
                return token
        raise AuthError(
            "No Authorization header on request. vikunja-mcp requires the caller's "
            "Vikunja bearer token, injected by the per-agent scoped-mcp manifest. "
            "(Running over stdio, where no header exists? Set VIKUNJA_TOKEN.)"
        )

    # Split scheme from token. A lone "Bearer" (no value) must fail closed rather than be
    # mistaken for a raw token — hence scheme-aware parsing, not a plain prefix strip.
    scheme, _, rest = raw.partition(" ")
    token = rest.strip() if scheme.lower() == "bearer" else raw
    if not token:
        raise AuthError("Authorization header present but carried no bearer token.")
    return token
