# Warp Engine - Production Dockerfile (multi-stage)
#
# Stage 1 (builder) installs the *runtime* dependency set from the hashed,
# universal lock file (`requirements-prod.lock`, generated with
# `make lock`) into an isolated venv using uv. Every wheel is hash-verified,
# so the image is reproducible and tamper-evident (see ADR-0003).
# Stage 2 (runtime) copies only the venv + application code, runs as a
# non-root user, and has no dev extras and no hot-reload.
#
# For local development the hot-reload command and source mounts are provided by
# docker-compose.yml, which overrides APP_ENV and the container command.

# ---- Stage 1: builder ----
FROM python:3.11-slim AS builder

# uv: pinned release, copied from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv

WORKDIR /app

# Build-time system dependencies (excluded from the runtime image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 1) Dependencies only, from the hashed runtime lock (cached layer).
COPY requirements-prod.lock ./
RUN uv venv /opt/venv \
    && uv pip sync --python /opt/venv/bin/python --require-hashes requirements-prod.lock

# 2) The application itself, without re-resolving dependencies.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --python /opt/venv/bin/python --no-deps --no-cache .

# ---- Stage 2: runtime ----
FROM python:3.11-slim AS runtime

WORKDIR /app

# Microsoft ODBC Driver 18 for SQL Server, used by `type: mssql` connections
# (the Python side, aioodbc/pyodbc, comes from the lock). Installed by default;
# build with `--build-arg WITH_MSSQL_ODBC=0` for a slimmer image without it.
ARG WITH_MSSQL_ODBC=1

# Runtime-only system dependencies + a dedicated non-root user.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && if [ "$WITH_MSSQL_ODBC" = "1" ]; then \
         . /etc/os-release \
         && curl -fsSL -o /tmp/packages-microsoft-prod.deb \
              "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb" \
         && dpkg -i /tmp/packages-microsoft-prod.deb \
         && rm -f /tmp/packages-microsoft-prod.deb \
         && apt-get update \
         && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
              msodbcsql18 unixodbc libgssapi-krb5-2 ; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/catalogs \
    && chown -R appuser:appuser /app

# Copy the prebuilt venv and the runtime config.
COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv
COPY --chown=appuser:appuser config/ /app/config/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    CONFIG_PATH=/app/config/database.yaml

# Drop privileges.
USER appuser

EXPOSE 8000

# Production command — no --reload.
CMD ["uvicorn", "warp.main:app", "--host", "0.0.0.0", "--port", "8000"]
