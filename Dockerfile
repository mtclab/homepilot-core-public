# Stage 1: Build web UI
FROM node:22-alpine AS web-builder
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Stage 2: Install Python dependencies
FROM python:3.12-slim AS py-builder
WORKDIR /build
RUN pip install --no-cache-dir uv
# Install the EXACT locked dependency set (reproducible), then the project itself
# without re-resolving. `pip install .` alone resolves from pyproject and would
# drift to newer, incompatible versions on every build (see #399 — mcp 1.28
# dropped the API this code uses). The lock is the source of truth.
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt && \
    pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt && \
    pip install --no-cache-dir --prefix=/install --no-deps .

# Stage 3: Build the agent binaries the control plane will serve itself
#
# HomePilot hands these to a guest during enrolment instead of sending the guest
# to GitHub (#464). An isolated guest has no route to the internet - and the
# friend-portal VLAN is egress-limited by design - so "installable automatically"
# was failing exactly where the network is tightest. Building them here also ties
# the agent version to the control plane that manages it, which removes a whole
# class of version-skew.
#
# Built from source rather than downloaded from a release: a build cannot then
# depend on the network, and the binary is provably the one this source tree
# describes.
FROM golang:1.25-alpine AS agent-builder
WORKDIR /agent
COPY agent/go/ ./
ENV CGO_ENABLED=0
# Stamp the build (#430). `-X main.version` only does anything because package
# main now HAS that symbol; it silently did nothing for every release before
# that, so an unstamped binary reported itself as "dev" forever. The release
# passes --build-arg HP_VERSION=<tag>; a local build is honestly "dev".
ARG HP_VERSION=dev
RUN go build -ldflags="-s -w -X main.version=${HP_VERSION}" -o /dist/hp-agent-linux-amd64 . && \
    GOARCH=arm64 go build -ldflags="-s -w -X main.version=${HP_VERSION}" -o /dist/hp-agent-linux-arm64 .

# Stage 4: Final image
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN groupadd --system homepilot && \
    useradd --system --gid homepilot --create-home homepilot

COPY --from=py-builder /install /usr/local
COPY --from=web-builder /web/dist /app/web/dist
# The agent payload HomePilot serves to guests: the binaries plus the installer
# that lands them. Kept together under one directory so the serving code has a
# single root to resolve against (HP_AGENT_DIST_DIR overrides it for tests).
COPY --from=agent-builder /dist/ /app/agent-dist/
COPY scripts/install-agent.sh /app/agent-dist/install-agent.sh

WORKDIR /app

RUN mkdir -p /home/homepilot/.hp && \
    chown -R homepilot:homepilot /home/homepilot/.hp /app

USER homepilot

EXPOSE 8000

ENTRYPOINT ["uvicorn", "homepilot.main:app", "--host", "0.0.0.0", "--port", "8000"]
