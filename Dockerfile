# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# node-deps: single-file-cli is only used for the optional HTML snapshots
# (--singlefile). Installing it in a separate stage keeps the default image
# free of Node.js and Chromium.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS node-deps

# The lockfile pins single-file-cli to a GitHub commit. Fetch git dependencies
# over HTTPS so builds never require SSH credentials.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# ---------------------------------------------------------------------------
# base: the Python exporter on its own. Exports JSON data, course files and
# attachments without a browser, which keeps the image small (~200 MB).
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY export.py singlefile.py ./

# Run as a regular user (uid 1000 usually matches the host user, which keeps
# bind-mounted output directories writable on Linux). Use `--user` to override.
RUN useradd --create-home --uid 1000 exporter \
    && mkdir -p /data /config \
    && chown exporter:exporter /data /config

USER exporter

# Container defaults: mount credentials at /config and output at /data.
# Every value can also be supplied through CANVAS_* environment variables.
ENV CANVAS_CONFIG=/config/credentials.yaml
VOLUME ["/data"]

ENTRYPOINT ["python", "export.py"]
CMD ["-o", "/data"]

# ---------------------------------------------------------------------------
# singlefile: adds Node.js + Chromium so `--singlefile` HTML snapshots work.
# Use this target only when you want browsable HTML copies of Canvas pages.
# ---------------------------------------------------------------------------
FROM base AS singlefile

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-liberation \
        libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Node.js is copied from the node image (built on the same Debian release).
# npm is not needed at runtime, only the node binary and node_modules.
COPY --from=node-deps /usr/local/bin/node /usr/local/bin/node
COPY --from=node-deps /app/node_modules /app/node_modules

USER exporter

# ---------------------------------------------------------------------------
# runtime: the default target, so a plain `docker build .` stays browser-free.
# ---------------------------------------------------------------------------
FROM base AS runtime
