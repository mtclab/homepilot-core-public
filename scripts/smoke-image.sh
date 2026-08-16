#!/usr/bin/env bash
# Pre-release shipped-artifact smoke: build the Docker image, start it, and assert
# it actually boots and serves. The unit gate runs against the local .venv, so it
# CANNOT catch a build-resolution drift (see #399: an unpinned dep pulled a newer
# mcp that dropped an API, breaking every fresh image while the suite stayed green).
# Only building and running the real image catches that. Requires docker.
set -euo pipefail

TAG="homepilot:smoke-$$"
NAME="hp-smoke-$$"
PORT="${SMOKE_PORT:-8009}"
DATA="$(mktemp -d)"
TOKEN="smoke-token-$$"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rmi "$TAG" >/dev/null 2>&1 || true
    docker run --rm -v "$DATA":/d alpine sh -c 'rm -rf /d/* /d/.* 2>/dev/null' >/dev/null 2>&1 || true
    rmdir "$DATA" 2>/dev/null || true
}
trap cleanup EXIT

echo "smoke: building image…"
docker build -q -t "$TAG" . >/dev/null
chmod 777 "$DATA"

echo "smoke: starting container on :$PORT…"
docker run -d --name "$NAME" -p "$PORT":8000 \
    -e HP_MCP_TOKEN="$TOKEN" -e HP_SECRET_KEY=smoke-secret \
    -e HP_VAULT_PASSPHRASE=smoke-pass -e HP_ENV=dev \
    -v "$DATA":/home/homepilot/.hp "$TAG" >/dev/null

# Wait for the app to boot (or fail fast if the container exits).
for _ in $(seq 1 30); do
    if ! docker ps --format '{{.Names}}' | grep -q "^$NAME$"; then
        echo "smoke: FAIL — container exited during startup" >&2
        docker logs "$NAME" 2>&1 | tail -20 >&2
        exit 1
    fi
    if curl -fsm3 "http://localhost:$PORT/health" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "smoke: checking /health…"
health="$(curl -fsm5 "http://localhost:$PORT/health")" || { echo "smoke: FAIL — /health unreachable" >&2; exit 1; }
echo "  $health"

echo "smoke: checking POST /mcp/ initialize (regression guard for #382/#399)…"
code="$(curl -sm8 -o /dev/null -w '%{http_code}' -X POST "http://localhost:$PORT/mcp/" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')"
if [[ "$code" != "200" ]]; then
    echo "smoke: FAIL — /mcp initialize returned $code (want 200)" >&2
    exit 1
fi

echo "smoke: PASS — image builds, starts, and serves MCP."
