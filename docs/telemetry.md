# Telemetry

Logging (structlog JSON) is **on by default**. Metrics and tracing are **off by default**
and turn on per-backend when the relevant environment variable is set. Install the optional
dependencies with:

```bash
pip install 'vikunja-mcp[telemetry]'
```

Every tool call records three signals — **call count**, **error count**, and **upstream
latency (seconds)** — plus an OTLP span. All credentials are read from the environment
only; the InfluxDB/NATS sinks are best-effort and fire-and-forget, so a telemetry backend
being down never breaks a tool call.

## OTLP traces + metrics (SigNoz)

| Env var | Effect |
|---------|--------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | enable OTLP span + metric export (e.g. `http://signoz-otel-collector:4317`) |

Spans are named `tool.<name>`; metrics are `vikunja_mcp.tool.calls`,
`vikunja_mcp.tool.errors`, `vikunja_mcp.tool.latency`.

## InfluxDB 3

forge runs `influxdb:3-core` — this uses the **v3** write API (`influxdb3-python`,
imported as `influxdb_client_3`), not the 2.x client.

| Env var | Default | Notes |
|---------|---------|-------|
| `VIKUNJA_INFLUXDB3_URL` | *(unset → disabled)* | enables the sink when set |
| `VIKUNJA_INFLUXDB3_TOKEN` | `""` | auth token |
| `VIKUNJA_INFLUXDB3_DATABASE` | `vikunja_mcp` | target database/bucket |

Writes a `tool_call` measurement tagged by `tool`/`status` with `latency_s` and `count`.

## NATS

| Env var | Default | Notes |
|---------|---------|-------|
| `VIKUNJA_NATS_URL` | *(unset → disabled)* | enables the sink when set (e.g. `nats://127.0.0.1:4222`) |
| `VIKUNJA_NATS_SUBJECT` | `vikunja.mcp.metrics` | subject to publish JSON metric events on |

Publishes one JSON message per call: `{tool, latency_s, status, error}`.

## Disabled path

With none of the above set, the telemetry layer is a transparent timer — no providers, no
connections, no dependencies required. This is the CI/base-install path.
