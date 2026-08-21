# syntax=docker/dockerfile:1

# python:3.13-slim, pinned by multi-arch index digest.
#
# slim rather than alpine, deliberately: the dependency tree includes nh3 (Rust) and,
# under the [telemetry] extra, grpc wheels. Those publish manylinux wheels but musl wheel
# coverage is the kind of thing that quietly turns into a build toolchain living in the
# runtime image. slim keeps both stages wheel-only.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

FROM ${PYTHON_IMAGE} AS build

# Build into a self-contained venv so the runtime stage can take the tree wholesale
# without pip, setuptools, or any build metadata coming with it.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
# pyproject and the package tree only — hatchling needs both to build the wheel, and
# copying just these keeps the layer cache from busting on a docs or test edit.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# [telemetry] is included deliberately. Setting OTEL_EXPORTER_OTLP_ENDPOINT without the
# extra installed does not fail — _init_otlp() logs otlp_import_failed once and then
# exports nothing at all, which is a failure mode this project has already shipped into
# (vikunja#336). Since telemetry is off unless env-configured, carrying the extra costs
# image size and nothing else, and removes the trap entirely.
RUN pip install --no-cache-dir '.[telemetry]'

FROM ${PYTHON_IMAGE} AS runtime

# 0.0.0.0 is required for the server to be reachable from outside its own namespace, and
# is NOT an exposure decision — a bind address is a no-op as a security control inside a
# namespace. The `ports:` publish is the actual control; publish loopback-only if you want
# loopback-only. See docs/docker.md.
#
# Deliberately absent: VIKUNJA_URL. get_settings() raises when it is unset, and that loud
# failure is the point — a baked default would let a misconfigured deployment silently
# point at someone else's Vikunja instance.
ENV VIKUNJA_TRANSPORT=http \
    VIKUNJA_HOST=0.0.0.0 \
    VIKUNJA_PORT=8501 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=build /opt/venv /opt/venv

# Non-root. Fixed uid/gid 1000 rather than a name lookup, so a bind mount for the audit
# log (the only writable path this server has) can be chowned predictably on the host.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home app
USER 1000:1000

EXPOSE 8501

# /health is unauthenticated by design and returns no config, so this needs no token.
# It is a pure liveness check: it does not probe upstream Vikunja, so a Vikunja restart
# does not mark this container unhealthy.
#
# python rather than wget/curl: slim ships neither, and adding one to the runtime image
# for a healthcheck is a worse trade than using the interpreter that is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('VIKUNJA_PORT','8501')+'/health', timeout=4).status==200 else 1)" \
  || exit 1

CMD ["vikunja-mcp"]
