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

# Stage 3: Final image
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN groupadd --system homepilot && \
    useradd --system --gid homepilot --create-home homepilot

COPY --from=py-builder /install /usr/local
COPY --from=web-builder /web/dist /app/web/dist

WORKDIR /app

RUN mkdir -p /home/homepilot/.hp && \
    chown -R homepilot:homepilot /home/homepilot/.hp /app

USER homepilot

EXPOSE 8000

ENTRYPOINT ["uvicorn", "homepilot.main:app", "--host", "0.0.0.0", "--port", "8000"]
