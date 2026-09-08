"""``vikunja-mcp --check`` — the config-validation entry point.

Three properties are under test, and only the first is the obvious one:

1. It agrees with ``get_settings()`` about what is valid — every fail-closed combination
   exits non-zero, every good one exits zero.
2. **It never prints the token.** A ``--check`` is the thing an operator runs in a
   terminal, pastes into a chat, or leaves in a CI log. If the success path echoed the
   credential it would turn a debugging aid into a disclosure channel, and the failure
   would be invisible because the command otherwise worked.
3. **It never contacts Vikunja.** A green ``--check`` must mean "this configuration is
   coherent", not "the instance is up". Conflating the two makes a passing check mean less
   than it appears to, which is worse than having no check — so the test asserts the
   HTTP client is never constructed, rather than trusting that it is not.
"""

from __future__ import annotations

import pytest

from vikunja_mcp import config, server


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start from a known env, whatever the ambient one is, and never inherit a real URL."""
    for var in ("VIKUNJA_URL", "VIKUNJA_TOKEN", "VIKUNJA_TRANSPORT", "VIKUNJA_STALE_AFTER_DAYS"):
        monkeypatch.delenv(var, raising=False)
    config.reset_settings()
    yield
    config.reset_settings()


# ---------------------------------------------------------------------------------------
# 1. Agrees with get_settings() about validity
# ---------------------------------------------------------------------------------------


def test_missing_url_exits_nonzero(monkeypatch, capsys):
    assert server.check_config() == 1
    assert "VIKUNJA_URL is not set" in capsys.readouterr().err


def test_valid_network_config_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    assert server.check_config() == 0
    out = capsys.readouterr().out
    assert "config: ok" in out
    assert "https://vikunja.example.com" in out


def test_stdio_without_token_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    assert server.check_config() == 1
    assert "requires VIKUNJA_TOKEN" in capsys.readouterr().err


def test_stdio_with_token_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    monkeypatch.setenv("VIKUNJA_TOKEN", "tok")
    assert server.check_config() == 0
    assert "config: ok" in capsys.readouterr().out


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_token_on_a_network_transport_exits_nonzero(monkeypatch, capsys, transport):
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    monkeypatch.setenv("VIKUNJA_TRANSPORT", transport)
    monkeypatch.setenv("VIKUNJA_TOKEN", "tok")
    assert server.check_config() == 1
    assert "only supported for stdio" in capsys.readouterr().err


def test_a_field_validation_error_is_reported_not_raised(monkeypatch, capsys):
    """A bad scalar must be reported as INVALID, not escape as a traceback.

    ConfigError and pydantic's ValidationError arrive by different routes; catching only
    the first would let this one crash the command.
    """
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    monkeypatch.setenv("VIKUNJA_STALE_AFTER_DAYS", "0")
    assert server.check_config() == 1
    assert "config: INVALID" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------
# 2. Never prints the token
# ---------------------------------------------------------------------------------------


def test_check_never_prints_the_token(monkeypatch, capsys):
    secret = "tk_do_not_disclose_9c3f1a"
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    monkeypatch.setenv("VIKUNJA_TRANSPORT", "stdio")
    monkeypatch.setenv("VIKUNJA_TOKEN", secret)

    assert server.check_config() == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert secret not in combined
    # Control: the assertion above is only meaningful if the check reports the token at
    # all. Without this, deleting the token line entirely would still pass.
    assert "set (stdio fallback)" in captured.out


def test_check_reports_an_absent_token_distinguishably(monkeypatch, capsys):
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    assert server.check_config() == 0
    assert "not set (passthrough)" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------
# 3. Never contacts Vikunja
# ---------------------------------------------------------------------------------------


def test_main_dispatches_check_and_never_starts_the_server(monkeypatch, capsys):
    """Drive ``main()`` through argv, not ``check_config()`` directly.

    Added because a mutation that disconnected the flag — ``if False:`` in place of
    ``if args.check:`` — survived the whole suite. Every other test here calls
    ``check_config()`` directly, so all of them stayed green while ``--check`` did nothing
    at all and fell through to starting the server. A validated function nothing reaches is
    not a validated entry point.
    """
    started: list[object] = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: started.append((a, k)))
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")
    with pytest.raises(SystemExit) as exc:
        server.main(["--check"])

    assert exc.value.code == 0
    assert "config: ok" in capsys.readouterr().out
    assert started == [], "--check started the server"


def test_main_check_propagates_a_nonzero_exit(monkeypatch, capsys):
    """A bad config must leave the process with a failing status, not just print.

    This is what makes `vikunja-mcp --check` usable in a compose healthcheck, a CI gate or
    a deploy preflight — the printed text is for a human, the exit code is for the machine.
    """
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(SystemExit) as exc:
        server.main(["--check"])

    assert exc.value.code == 1
    assert "VIKUNJA_URL is not set" in capsys.readouterr().err


def test_check_does_not_contact_vikunja(monkeypatch, capsys):
    """Assert the upstream request path is never entered, rather than trusting that it is not.

    A --check that quietly dialled the instance would turn "is my config coherent" into
    "is my network up", and would fail for reasons that have nothing to do with config.
    """
    called: list[object] = []

    async def _explode(*args, **kwargs):  # pragma: no cover - must never run
        called.append(args)
        raise AssertionError("check_config() contacted Vikunja")

    monkeypatch.setattr(server, "request", _explode)
    monkeypatch.setenv("VIKUNJA_URL", "https://vikunja.example.com")

    assert server.check_config() == 0
    assert called == []
