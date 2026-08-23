"""Configuration via environment variables.

Mostly the *upstream* Vikunja location and transport binding. On any network transport
this holds no Vikunja API token at all — see ``auth.py`` for the token-passthrough model
that makes that possible.

The one exception is ``VIKUNJA_TOKEN`` under ``transport=stdio``, where there is no HTTP
request to carry a credential and passthrough therefore cannot work. That combination is
opt-in, and its inverse — a token on a network transport — is refused at startup by
``get_settings()``.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .exceptions import ConfigError


def parse_task_ids(raw: str) -> list[int]:
    """Parse ``"180, 42"`` into ``[180, 42]``. Empty or blank yields ``[]``.

    Raises ValueError naming the offending entry, so a typo in a compose file is
    attributable rather than just "invalid".
    """
    ids: list[int] = []
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        try:
            ids.append(int(entry))
        except ValueError:
            raise ValueError(f"{entry!r} is not an integer task id") from None
    return ids


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIKUNJA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base URL of the Vikunja instance (without the /api/v2 suffix — the client appends it).
    # No default: an environment-specific hostname baked in here would (a) publish it to
    # every reader of this public repo and (b) let a misconfigured deployment silently fall
    # back to someone else's instance instead of failing. See vikunja#344 (id 363, SC-01).
    url: str = ""
    request_timeout: float = 30.0

    # Project that a bare `"#454"` ticket reference is resolved within. Unset by default:
    # `index` is only unique *per project*, so an unscoped lookup can match more than one
    # task (live case: `index = 1` matches id 9 in project 7 and id 344 in project 2).
    # When unset, resolution runs unscoped and **raises** on more than one match rather
    # than picking a winner — guessing here is how vikunja#331 mutated three unrelated
    # tickets. Set this to the project agents actually file against to make `#N` unambiguous.
    default_project_id: int | None = None

    # Transport binding. Loopback-only by default: the token-passthrough model means any
    # local process reaching this port could forward a token it already holds, so exposure
    # beyond localhost is never intended (see SECURITY.md).
    transport: str = "http"
    host: str = "127.0.0.1"
    port: int = 8501

    # Single-user fallback credential, for **stdio only**. Under stdio there is no HTTP
    # request to carry an Authorization header, so passthrough has nothing to pass through
    # and every tool call fails (vikunja#461). This is the escape hatch for that case, not
    # a general-purpose service account.
    #
    # Never set it for a network transport. A static token on a shared port collapses every
    # caller into a single Vikunja identity — forge runs six agents through this server and
    # relies on per-agent attribution, which is the whole reason the passthrough model
    # exists. get_settings() refuses that combination at startup rather than documenting it
    # as a footgun.
    token: str | None = None

    # Age in days past which a task is reported `stale: true` on every read path.
    #
    # 90 is a judgement call, not a measurement: long enough that ordinary in-flight work
    # is never flagged, short enough to catch a ticket whose text has drifted from reality.
    # It is a *weak* signal by construction — see `server._staleness` for what it does and
    # does not claim.
    stale_after_days: int = 90

    # Task ids excluded from every `backlog_summary` bucket, comma-separated. Empty by
    # default, and that default is the important part.
    #
    # The motivating case: a tracker can hold a "vocabulary anchor" task that carries every
    # label deliberately, so the vocabulary is visible in the UI and cannot be pruned. Such
    # a task lands in *every* label bucket and inflates each count by exactly one — a
    # summary that is wrong by one everywhere, which is worse than one that is obviously
    # broken. A tracker with exactly that convention is how the problem was found.
    #
    # The id is NOT baked in here. This is a public repo and another deployment's anchor is
    # a different id or does not exist; a hardcoded 180 would be the SC-01 pattern
    # (environment-specific values published in source) that `url` and
    # `FileAuditLogger`'s directory argument already exist to avoid. The deployment names
    # its own anchor.
    summary_exclude_ids: str = ""

    @field_validator("summary_exclude_ids", mode="after")
    @classmethod
    def _exclude_ids_must_parse(cls, v: str) -> str:
        """Reject a malformed list at startup rather than at summary time.

        A typo here would otherwise surface as a filter Vikunja rejects with a 400, on a
        tool call, long after the deploy that caused it.
        """
        try:
            parse_task_ids(v)
        except ValueError as exc:
            raise ValueError(
                f"VIKUNJA_SUMMARY_EXCLUDE_IDS is not a comma-separated list of task ids: "
                f"{exc}. Example: VIKUNJA_SUMMARY_EXCLUDE_IDS=180,42"
            ) from exc
        return v

    @property
    def excluded_task_ids(self) -> list[int]:
        """``summary_exclude_ids`` parsed. Validated at startup, so this cannot raise."""
        return parse_task_ids(self.summary_exclude_ids)

    @field_validator("stale_after_days", mode="after")
    @classmethod
    def _threshold_must_be_positive(cls, v: int) -> int:
        """Refuse a threshold of 0 or less rather than marking the whole backlog stale.

        A zero threshold makes `stale` true for every task including ones updated seconds
        ago — a wrong answer with no symptom, since the field looks like it is working.
        Same reasoning as the token/transport refusal below: fail at startup, where it is
        attributable, rather than at read time, where it is not.
        """
        if v <= 0:
            raise ValueError(
                f"VIKUNJA_STALE_AFTER_DAYS must be at least 1, got {v}. A threshold of "
                "zero or less marks every task stale — including one updated a moment "
                "ago — which makes the flag useless without looking broken."
            )
        return v

    @field_validator("token", mode="after")
    @classmethod
    def _blank_token_is_unset(cls, v: str | None) -> str | None:
        """Treat `VIKUNJA_TOKEN=""` (or whitespace) as unset, not as a token.

        Otherwise an empty assignment in a compose file or `.env` would satisfy the
        "stdio needs a token" check at startup and then fail on the first tool call —
        which is exactly the deferred failure this change exists to remove.
        """
        return v.strip() or None if v else None


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        settings = Settings()
        if not settings.url:
            raise ConfigError(
                "VIKUNJA_URL is not set. This server has no default Vikunja instance — "
                "set VIKUNJA_URL to the base URL of your Vikunja deployment (without the "
                "/api/v2 suffix) before starting."
            )

        # Both of these are hard refusals at startup, not warnings.
        #
        # The first is a security boundary: a static token on a *network* transport makes
        # every caller indistinguishable in Vikunja's audit trail, silently, with no
        # symptom until someone asks who changed a ticket. That must not be reachable by
        # misconfiguration. Note the check is `!= "stdio"` rather than `== "http"` — `sse`
        # and any future network transport are just as unsafe, and enumerating the safe
        # case is what keeps this correct as transports are added.
        if settings.token and settings.transport != "stdio":
            raise ConfigError(
                f"VIKUNJA_TOKEN is set but VIKUNJA_TRANSPORT is {settings.transport!r}. "
                "A static token is only supported for stdio, where there is no HTTP "
                "request to carry the caller's own credential. On a network transport it "
                "would make every caller act as one Vikunja identity and destroy per-agent "
                "attribution — so this combination is refused rather than silently "
                "accepted. Unset VIKUNJA_TOKEN and let each caller send its own "
                "Authorization header, or switch to VIKUNJA_TRANSPORT=stdio."
            )

        # The second is a usability fix: without it the server starts, logs cleanly,
        # registers every tool, and then fails 100% of calls at invocation time.
        if settings.transport == "stdio" and not settings.token:
            raise ConfigError(
                "VIKUNJA_TRANSPORT=stdio requires VIKUNJA_TOKEN. Under stdio there is no "
                "HTTP request to carry an Authorization header, so the token-passthrough "
                "model has no token to pass through and every tool call would fail. Set "
                "VIKUNJA_TOKEN to your Vikunja API token."
            )

        _settings = settings
    return _settings


def reset_settings() -> None:
    """Test hook: drop the cached Settings so the next get_settings() re-reads the env."""
    global _settings
    _settings = None
