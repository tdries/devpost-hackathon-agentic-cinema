# Customs: the Cloud Run image. python:3.12-slim + ffmpeg (every probe,
# transcode and thumbnail call in customs/media.py) + the linux/amd64
# mcp-grafana release binary, so the publisher agent's real MCP connection
# works in the deployed service exactly as it does on a developer's machine
# (grafana_ops.py falls back to plain HTTP whenever the binary is missing or
# unusable, but the live stack talks MCP when it can, and that needs a binary
# that actually runs on this OS/arch).
#
# Single instance by design (--max-instances 1 --min-instances 1 in
# scripts/deploy.sh): the run store is SQLite on the container's own
# filesystem and the panel-render path takes an in-process lock, neither of
# which survives more than one replica. Stated tradeoff, not an oversight --
# demo-grade persistence, not a production database.
FROM python:3.12-slim

# ffmpeg: customs/media.py's ffmpeg/ffprobe subprocess calls (duration,
# resolution, signalstats flash detection, thumbnails, audio extraction).
# curl + ca-certificates: fetch the mcp-grafana release binary below over
# HTTPS. Nothing else -- base image plus these three apt packages is the
# entire system dependency list.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before source: requirements.txt changes far less often than
# src/, and google-adk + google-genai are the slow, heavy part of this
# install. A source-only edit should never re-resolve or re-download them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# mcp-grafana v1.1.0, linux/amd64 -- the same version as the darwin/arm64
# binary developers run locally (bin/mcp-grafana, gitignored; see
# grafana_ops.py's module docstring for the live tool inventory that version
# was taken against). The mac binary is never copied into this image; it is
# the wrong OS/arch and would not run here. GrafanaOps' binary resolution
# falls back to /usr/local/bin/mcp-grafana whenever the repo-relative dev
# path (bin/mcp-grafana) is absent, which inside this image it always is.
RUN curl -fsSL \
        https://github.com/grafana/mcp-grafana/releases/download/v1.1.0/mcp-grafana_Linux_x86_64.tar.gz \
        | tar -xz -C /usr/local/bin mcp-grafana \
    && chmod +x /usr/local/bin/mcp-grafana

# The application. Templates and static files live inside src/customs/ and
# come along with it. markets/ and grafana/ are data the app reads at
# request time (market packs, dashboard JSON), not build inputs -- both are
# read via paths relative to the process's cwd (packs.py) or to this file
# tree's root (grafana_ops.py), so they must land at /app/markets and
# /app/grafana. docs/samples/ ships the demo asset (test_ad.mp4): the test
# ad judges see referenced throughout the design, kept with the image that
# serves them rather than assumed to exist wherever someone happens to run
# this from.
COPY src/ src/
COPY markets/ markets/
COPY grafana/ grafana/
COPY docs/samples/ docs/samples/

ENV PYTHONPATH=/app/src
ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "customs.app:app", "--host", "0.0.0.0", "--port", "8080"]
