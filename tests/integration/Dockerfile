# Minimal helmlog image for integration testing.
#
# Runs the FastAPI app with federation endpoints, or pytest for the
# test-runner service. No CAN bus, no audio, no Signal K.

FROM python:3.12-slim

# System deps needed by cryptography wheel and soundfile (transitive dep)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libportaudio2 libsndfile1 gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy project files and install deps (cached layer)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen

# Copy source + tests
COPY src/ src/
COPY tests/ tests/

# Create data and identity dirs
RUN mkdir -p /data /identity

ENV DB_PATH=/data/helmlog.db
ENV HELMLOG_IDENTITY_DIR=/identity
ENV HELMLOG_PORT=8080
ENV AUTH_DISABLED=true
ENV LOG_LEVEL=DEBUG

# Healthcheck: hit /healthz
HEALTHCHECK --interval=2s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${HELMLOG_PORT}/healthz')" || exit 1

# Default: run the web server via our minimal entry point
CMD ["uv", "run", "python", "tests/integration/serve.py"]
