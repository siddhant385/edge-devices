# IBVAP Edge Client - Production Docker Image
# CPU-only RTSP/ONNX inference pipeline for edge surveillance.
#
# Build:   docker build -t ibvap-edge .
# Run:     docker run --rm --network host --env-file .env \
#            -v $(pwd)/cameras:/app/cameras:ro \
#            -v $(pwd)/models:/app/models:ro \
#            -v ibvap-data:/app/data ibvap-edge

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System dependencies: OpenCV (libgl, libsm), FFmpeg shared libs (libavcodec, libavformat,
# libswscale, libswresample), and PIL (libjpeg, libpng).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxrender1 \
        libxext6 \
        libavcodec60 \
        libavformat60 \
        libavutil58 \
        libswscale5 \
        libswresample4 \
        libjpeg62-turbo \
        libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv for fast dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install Python dependencies first to leverage Docker layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source.
COPY main.py ./
COPY config/ ./config/
COPY core/ ./core/
COPY plugins/ ./plugins/

# Runtime directories. Cameras and models are mounted as volumes in production.
RUN mkdir -p /app/cameras /app/models /app/data /app/logs

# Non-root user.
RUN useradd --create-home --shell /bin/bash ibvap && \
    chown -R ibvap:ibvap /app
USER ibvap

# Healthcheck: probe python process liveness.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD pgrep -f "python main.py" >/dev/null 2>&1 || exit 1

CMD ["uv", "run", "python", "main.py"]
