"""Telemetry degrades to a no-op when no backend is configured (the CI/base path).

The optional OTLP/InfluxDB3/NATS libraries are not installed under the ``[dev]`` extra, so
these tests assert the disabled behaviour: importing is safe, ``record_tool_call`` is a
transparent timer, and errors still propagate through it.
"""

from __future__ import annotations

import pytest

from vikunja_mcp import telemetry


@pytest.fixture(autouse=True)
def _reset():
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


async def test_record_tool_call_is_transparent_when_disabled():
    async with telemetry.record_tool_call("whoami"):
        value = 21 * 2
    assert value == 42


async def test_record_tool_call_reraises_and_still_emits(monkeypatch):
    emitted = {}

    def _capture(tool, duration, error):
        emitted["tool"] = tool
        emitted["error"] = error

    monkeypatch.setattr(telemetry, "_emit", _capture)

    with pytest.raises(RuntimeError, match="boom"):
        async with telemetry.record_tool_call("task_delete"):
            raise RuntimeError("boom")

    assert emitted["tool"] == "task_delete"
    assert emitted["error"] == "RuntimeError"


def test_init_is_idempotent_and_noop_without_env(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("VIKUNJA_INFLUXDB3_URL", raising=False)
    telemetry.init()
    telemetry.init()  # second call short-circuits on the _initialized guard
    # nothing wired up → no exporters
    assert telemetry._tracer is None
    assert telemetry._influx_client is None


def test_emit_without_backends_does_not_raise(monkeypatch):
    monkeypatch.delenv("VIKUNJA_NATS_URL", raising=False)
    telemetry._emit("whoami", 0.01, None)  # must be a silent no-op


def test_emit_records_to_otlp_instruments_when_present(monkeypatch):
    from unittest.mock import MagicMock

    calls, errors, hist = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(telemetry, "_calls_counter", calls)
    monkeypatch.setattr(telemetry, "_errors_counter", errors)
    monkeypatch.setattr(telemetry, "_latency_hist", hist)
    monkeypatch.delenv("VIKUNJA_NATS_URL", raising=False)

    telemetry._emit("task_delete", 0.05, "VikunjaAPIError")

    calls.add.assert_called_once()
    hist.record.assert_called_once()
    errors.add.assert_called_once()  # only on error


def test_influx_write_is_best_effort_and_swallows_errors(monkeypatch):
    from unittest.mock import MagicMock

    client = MagicMock()
    client.write.side_effect = RuntimeError("influx down")
    monkeypatch.setattr(telemetry, "_influx_client", client)
    # a failing backend must not raise out of the telemetry path
    telemetry._influx_write("whoami", 0.01, None)


async def test_nats_publish_short_circuits_without_url(monkeypatch):
    monkeypatch.delenv("VIKUNJA_NATS_URL", raising=False)
    # returns immediately, never attempts a connection
    await telemetry._nats_publish("whoami", 0.01, None)
    assert telemetry._nats_conn is None
