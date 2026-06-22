# Warp Engine - Production Dockerfile (multi-stage)
#
# Stage 1 (builder) compiles dependencies into an isolated venv using build
# tooling that is NOT shipped in the final image. Stage 2 (runtime) copies only
# the venv + application code, runs as a non-root user, and has no dev extras
# and no hot-reload.
#
# For local development the hot-reload command and source mounts are provided by
# docker-compose.yml, which overrides APP_ENV and the container command.

# ---- Stage 1: builder ----
FROM python:3.11-slim AS builder

WORKDIR /app

# Build-time system dependencies (excluded from the runtime image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install runtime dependencies (+ llm extra) into a self-contained venv.
# The "dev" extra (pytest/ruff/mypy/...) is intentionally NOT installed.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir ".[llm]"

# ---- Stage 2: runtime ----
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime-only system dependencies + a dedicated non-root user.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
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
