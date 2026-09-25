# Reel: a small self-hosted video server.
#
#   docker compose up -d --build        (see compose.yaml)
#
# The app reads your media from /media (mount it read-only) and keeps its
# database, thumbnails and uploaded images in /data.

# ---- ffmpeg: the newest release, not the distribution's older package ------------
# ffmpeg/ffprobe do the probing, thumbnails, remuxing and live conversion.
# FFMPEG_SERIES=auto picks the newest release series (e.g. 9.0, including its
# latest point release); set it to e.g. 9.0 to pin. Rebuild with --no-cache to
# pick up a newer build.
FROM debian:trixie-slim AS ffmpeg
ARG TARGETARCH=amd64
ARG FFMPEG_SERIES=auto
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/fetch-ffmpeg.sh /usr/local/bin/fetch-ffmpeg
RUN fetch-ffmpeg /opt/ffmpeg "$TARGETARCH" "$FFMPEG_SERIES"

# ---- The app ------------------------------------------------------------------------
FROM python:3.14-slim

COPY --from=ffmpeg /opt/ffmpeg /opt/ffmpeg

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY reel ./reel
RUN uv sync --frozen --no-dev

# Runs as an unprivileged user. compose.yaml can switch to your own uid/gid
# (PUID/PGID) so the NAS share and ./data are readable/writable.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin reel \
    && mkdir -p /data /media \
    && chown reel:reel /data
USER reel

ENV PATH="/app/.venv/bin:/opt/ffmpeg/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    REEL_MEDIA_ROOT=/media \
    REEL_DATA_DIR=/data

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]

# One worker on purpose: scans and streams are managed inside the process.
# Forwarded headers (X-Forwarded-For...) are only trusted from FORWARDED_ALLOW_IPS,
# which defaults to 127.0.0.1; set it to your reverse proxy's address if you add one.
CMD ["uvicorn", "--factory", "reel.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
