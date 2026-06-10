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
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir --prefix=/install .

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
