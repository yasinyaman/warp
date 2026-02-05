# Warp Engine - Development Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies using pyproject.toml
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY config/ ./src/config/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[dev]"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    APP_ENV=development \
    LOG_LEVEL=DEBUG \
    LOG_FORMAT=colored

# Expose port
EXPOSE 8000

# Run with hot-reload
CMD ["uvicorn", "warp.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
