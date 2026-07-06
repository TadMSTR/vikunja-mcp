"""Optional telemetry — OTLP spans+metrics plus InfluxDB3 and NATS sinks.

Everything here is **off by default** and degrades to a no-op when either the relevant
environment variable is unset *or* the optional dependency is not installed. The base
install (and CI's ``[dev]`` extra) carry none of these libraries, so importing this module
must never fail — every backend import is lazy and guarded.

Enable a backend by setting its endpoint env var (all credentials come from the
environment only — never a config file or tool argument):

- **OTLP traces + metrics** — ``OTEL_EXPORTER_OTLP_ENDPOINT`` (e.g. SigNoz collector).
  Installs the ``[telemetry]`` extra (``opentelemetry-*``).
- **InfluxDB 3** — ``VIKUNJA_INFLUXDB3_URL`` (+ ``VIKUNJA_INFLUXDB3_TOKEN``,
  ``VIKUNJA_INFLUXDB3_DATABASE``). Installs ``[telemetry]`` (``influxdb3-python``).
  forge runs ``influxdb:3-core`` — the v3 write API, not the 2.x client.
- **NATS** — ``VIKUNJA_NATS_URL`` (+ optional ``VIKUNJA_NATS_SUBJECT``, default
  ``vikunja.mcp.metrics``). Installs ``[telemetry]`` (``nats-py``).

Per-tool signal recorded: call count, error count, and upstream latency (seconds). The
InfluxDB/NATS sinks are best-effort and fire-and-forget — a telemetry backend being down
never breaks a tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# OTLP (traces + metrics) — opentelemetry, gated on OTEL_EXPORTER_OTLP_ENDPOINT
# ---------------------------------------------------------------------------

_tracer: Any = None
_calls_counter: Any = None
_errors_counter: Any = None
_latency_hist: Any = None


def _init_otlp() -> None:
    """Wire OTLP tracing + metrics if an endpoint is set and the SDK is importable."""
    global _tracer, _calls_counter, _errors_counter, _latency_hist
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("otlp_import_failed", hint="pip install 'vikunja-mcp[telemetry]'")
        return

    resource = Resource.create({"service.name": "vikunja-mcp"})

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(tp)
    _tracer = trace.get_tracer("vikunja-mcp")

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)
    meter = metrics.get_meter("vikunja-mcp")
    _calls_counter = meter.create_counter("vikunja_mcp.tool.calls", unit="1")
    _errors_counter = meter.create_counter("vikunja_mcp.tool.errors", unit="1")
    _latency_hist = meter.create_histogram("vikunja_mcp.tool.latency", unit="s")
    log.info("otlp_enabled", endpoint=endpoint)


# ---------------------------------------------------------------------------
# InfluxDB 3 sink — influxdb3-python, gated on VIKUNJA_INFLUXDB3_URL
# ---------------------------------------------------------------------------

_influx_client: Any = None


def _init_influx() -> None:
    global _influx_client
    url = os.getenv("VIKUNJA_INFLUXDB3_URL", "").strip()
    if not url:
        return
    try:
        from influxdb_client_3 import InfluxDBClient3
    except ImportError:
        log.warning("influxdb3_import_failed", hint="pip install 'vikunja-mcp[telemetry]'")
        return
    try:
        _influx_client = InfluxDBClient3(
            host=url,
            token=os.getenv("VIKUNJA_INFLUXDB3_TOKEN", ""),
            database=os.getenv("VIKUNJA_INFLUXDB3_DATABASE", "vikunja_mcp"),
        )
        log.info("influxdb3_enabled", host=url)
    except Exception as exc:
        log.warning("influxdb3_init_failed", error=str(exc))


def _influx_write(tool: str, duration: float, error: str | None) -> None:
    if _influx_client is None:
        return
    try:
        from influxdb_client_3 import Point

        point = (
            Point("tool_call")
            .tag("tool", tool)
            .tag("status", "error" if error else "ok")
            .field("latency_s", float(duration))
            .field("count", 1)
        )
        if error:
            point = point.tag("error", error)
        _influx_client.write(point)
    except Exception as exc:
        log.warning("influxdb3_write_failed", tool=tool, error=str(exc))


# ---------------------------------------------------------------------------
# NATS sink — nats-py, gated on VIKUNJA_NATS_URL
# ---------------------------------------------------------------------------

_nats_conn: Any = None
_nats_lock: asyncio.Lock | None = None


async def _nats_publish(tool: str, duration: float, error: str | None) -> None:
    global _nats_conn, _nats_lock
    url = os.getenv("VIKUNJA_NATS_URL", "").strip()
    if not url:
        return
    try:
        import nats
    except ImportError:
        log.warning("nats_import_failed", hint="pip install 'vikunja-mcp[telemetry]'")
        return
    subject = os.getenv("VIKUNJA_NATS_SUBJECT", "vikunja.mcp.metrics")
    if _nats_lock is None:
        _nats_lock = asyncio.Lock()
    try:
        async with _nats_lock:
            if _nats_conn is None or not _nats_conn.is_connected:
                _nats_conn = await nats.connect(url)
        payload = {
            "tool": tool,
            "latency_s": round(duration, 6),
            "status": "error" if error else "ok",
            "error": error,
        }
        await _nats_conn.publish(subject, json.dumps(payload).encode())
    except Exception as exc:
        log.warning("nats_publish_failed", tool=tool, error=str(exc))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_initialized = False

# Strong refs to in-flight fire-and-forget sink tasks. asyncio holds only a weak reference
# to a bare create_task() result, so without this the task can be GC'd mid-flight and the
# metric silently dropped (F-03). Tasks discard themselves on completion.
_bg_tasks: set[asyncio.Task] = set()


def _schedule(coro: Any) -> None:
    """Fire-and-forget a coroutine on the running loop, retaining a strong ref.

    No-op (and closes the coroutine) when no loop is running — e.g. a synchronous caller.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def init() -> None:
    """Initialise every enabled backend. Idempotent; safe to call at import time."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    _init_otlp()
    _init_influx()


def _emit(tool: str, duration: float, error: str | None) -> None:
    """Fan a recorded call out to all enabled sinks. Never raises."""
    attrs = {"tool": tool, "status": "error" if error else "ok"}
    if _calls_counter is not None:
        _calls_counter.add(1, attrs)
    if _latency_hist is not None:
        _latency_hist.record(duration, attrs)
    if error and _errors_counter is not None:
        _errors_counter.add(1, {"tool": tool, "error": error})

    # InfluxDB's write() is synchronous and blocking — offload it to a worker thread so a
    # slow/hung endpoint can't stall the event loop (and every concurrent tool call) behind
    # it (F-01). Skipped cleanly when the sink is disabled or no loop is running.
    if _influx_client is not None:
        _schedule(asyncio.to_thread(_influx_write, tool, duration, error))

    # NATS publish is async; schedule it fire-and-forget on the running loop.
    if os.getenv("VIKUNJA_NATS_URL", "").strip():
        _schedule(_nats_publish(tool, duration, error))


@contextlib.asynccontextmanager
async def record_tool_call(tool: str) -> AsyncIterator[None]:
    """Time a tool call, opening an OTLP span and emitting metrics on exit.

    Records an error label if the wrapped block raises, then re-raises. When no backend is
    enabled this is a thin timer with no observable effect.
    """
    start = time.perf_counter()
    error: str | None = None
    span_cm = (
        _tracer.start_as_current_span(f"tool.{tool}")
        if _tracer is not None
        else contextlib.nullcontext()
    )
    with span_cm as span:
        try:
            yield
        except Exception as exc:
            error = type(exc).__name__
            if span is not None and hasattr(span, "record_exception"):
                span.record_exception(exc)
            raise
        finally:
            _emit(tool, time.perf_counter() - start, error)


async def aclose() -> None:
    """Close backend connections (shutdown / test cleanup)."""
    global _nats_conn, _influx_client, _initialized
    if _nats_conn is not None:
        with contextlib.suppress(Exception):
            await _nats_conn.drain()
        _nats_conn = None
    if _influx_client is not None:
        with contextlib.suppress(Exception):
            _influx_client.close()
        _influx_client = None
    _initialized = False


def reset_for_tests() -> None:
    """Drop all cached provider/sink state so a test can re-init from a clean slate."""
    global _tracer, _calls_counter, _errors_counter, _latency_hist
    global _influx_client, _nats_conn, _initialized
    _tracer = _calls_counter = _errors_counter = _latency_hist = None
    _influx_client = None
    _nats_conn = None
    _initialized = False
