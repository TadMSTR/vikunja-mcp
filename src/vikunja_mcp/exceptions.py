"""Custom exception hierarchy.

Bare ``Exception`` / ``ValueError`` would blur the line between "the caller sent a bad
token" (auth), "Vikunja rejected the request" (upstream), and "this server is
misconfigured" (config). Each gets its own type so callers and the audit log can tell
them apart.
"""

from __future__ import annotations


class VikunjaMCPError(Exception):
    """Base for every error raised by this server."""


class ConfigError(VikunjaMCPError):
    """Invalid or missing server configuration."""


class AuthError(VikunjaMCPError):
    """The request carried no usable caller credential.

    Raised fail-closed when the Authorization header is absent or empty. There is no
    ambient fallback token by design, so this always means either a scoped-mcp manifest
    that isn't injecting the agent token, or a direct unproxied call to the port.
    """


class VikunjaAPIError(VikunjaMCPError):
    """The upstream Vikunja API returned an error (or was unreachable).

    ``status_code`` is 0 when the request never completed (network/timeout), otherwise
    the HTTP status Vikunja returned.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Vikunja API error {status_code}: {message}")
