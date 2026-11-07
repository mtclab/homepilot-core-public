#!/usr/bin/env bash
# Pre-release shipped-artifact smoke: build the Docker image, start it, and assert
# it actually boots and serves. The unit gate runs against the local .venv, so it
# CANNOT catch a build-resolution drift (see #399: an unpinned dep pulled a newer
# mcp that dropped an API, breaking every fresh image while the suite stayed green).
# Only building and running the real image catches that. Requires docker.
set -euo pipefail

#
# It runs the SHIPPED path (#721): the repo's own docker-compose.yml, `docker
# compose up -d`, from a fresh project directory - no `.env`, no data dir made
# beforehand, no chmod, no secrets in the environment. Every one of those used
# to be supplied here, and each hid half of a fresh-host crash-loop: the bind
# mount Docker creates root:root, and the `.env` compose insisted on. Then it
# claims the instance the way the README says and opens MCP with the token the
# claim returned, so nothing the operator is not given is used.
#
# "Up" is judged over TIME, not by one probe: a crash-looping container answers
# nothing between restarts and may answer once in between. The backend must
# reach compose's own `healthy`, then stay running with zero restarts and a
# 200 /health on every probe for a whole watch window.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="hp-smoke-$$"
TAG="homepilot:smoke-$$"
NAME="hp-smoke-$$"
PORT="${SMOKE_PORT:-8009}"
HUB_PORT="${SMOKE_HUB_PORT:-8449}"
WATCH_S="${SMOKE_WATCH_S:-45}"
HEALTHY_TIMEOUT_S="${SMOKE_HEALTHY_TIMEOUT_S:-150}"
WORK="$(mktemp -d)"

# Only what must differ from a real install to run beside one on the same
# Docker host: its own container name, image tag and host ports. The data
# mount, env_file, entrypoint and user are the shipped compose's, untouched.
cat >"$WORK/smoke.override.yml" <<YAML
services:
  hp-data-init:
    image: $TAG
    build:
      context: $REPO
  backend:
    container_name: $NAME
    image: $TAG
    build:
      context: $REPO
YAML

dc() {
    HP_DAEMON_PORT="$PORT" HP_AGENT_HUB_PORT="$HUB_PORT" \
        docker compose -p "$PROJECT" --project-directory "$WORK" \
        -f "$REPO/docker-compose.yml" -f "$WORK/smoke.override.yml" "$@"
}

cleanup() {
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    # The data dir is owned by the container's user: remove it from inside one.
    docker run --rm --user 0 --entrypoint sh -v "$WORK":/w "$TAG" \
        -c 'rm -rf /w/data' >/dev/null 2>&1 || true
    docker rmi "$TAG" >/dev/null 2>&1 || true
    rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

fail() {
    echo "smoke: FAIL — $*" >&2
    dc ps -a >&2 || true
    dc logs --no-color --tail 40 >&2 || true
    exit 1
}

[[ ! -e "$WORK/.env" && ! -e "$WORK/data" ]] || fail "the project dir is not fresh"

echo "smoke: building image and starting the shipped compose on :$PORT (no .env, no data dir)…"
dc up -d --build --quiet-pull >/dev/null || fail "docker compose up failed"

echo "smoke: waiting for compose to report the backend healthy (≤${HEALTHY_TIMEOUT_S}s)…"
state=""
for _ in $(seq 1 "$HEALTHY_TIMEOUT_S"); do
    state="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$NAME" 2>/dev/null || echo missing)"
    [[ "$state" == "running/healthy" ]] && break
    [[ "$state" == running/* || "$state" == restarting/* || "$state" == created/* ]] \
        || fail "backend is $state during startup"
    sleep 1
done
[[ "$state" == "running/healthy" ]] || fail "backend never became healthy (last: $state)"

echo "smoke: watching it stay up for ${WATCH_S}s (no restarts, /health every probe)…"
for _ in $(seq 1 "$WATCH_S"); do
    status="$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || echo missing)"
    restarts="$(docker inspect -f '{{.RestartCount}}' "$NAME" 2>/dev/null || echo '?')"
    [[ "$status" == "running" ]] || fail "backend left running state: $status"
    [[ "$restarts" == "0" ]] || fail "backend restarted $restarts time(s) - it is crash-looping"
    curl -fsm3 -o /dev/null "http://localhost:$PORT/health" || fail "/health stopped answering"
    sleep 1
done
health="$(curl -fsm5 "http://localhost:$PORT/health")" || fail "/health unreachable"
echo "  $health"

echo "smoke: checking the backend owns its data (database on the bind mount)…"
dc exec -T backend sh -c 'test -s "$HOME/.hp/homepilot.db" && test -w "$HOME/.hp"' \
    || fail "no writable database in the backend's data dir"

echo "smoke: claiming the instance as the README says…"
claim="$(curl -sm10 -X POST "http://localhost:$PORT/claim" -H 'Content-Type: application/json' -d '{}')"
TOKEN="$(printf '%s' "$claim" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
    # Not judged same-network through Docker's port proxy: claim with the code,
    # read the way the README's `hp claim-code` reads it.
    code="$(dc exec -T backend sh -c 'cat "$HOME/.hp/.claim_code"' 2>/dev/null | tr -d '[:space:]')"
    [[ -n "$code" ]] || fail "claim refused ($claim) and no claim code to retry with"
    claim="$(curl -sm10 -X POST "http://localhost:$PORT/claim" -H 'Content-Type: application/json' \
        -d "{\"code\": \"$code\"}")"
    TOKEN="$(printf '%s' "$claim" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)"
fi
[[ -n "$TOKEN" ]] || fail "the claim returned no token: $claim"

echo "smoke: checking the image ships an ssh client (archive push needs it, #550)…"
if ! dc exec -T backend sh -c 'command -v ssh >/dev/null'; then
    echo "smoke: FAIL — no ssh in the image; git push to an SSH artifacts remote cannot work" >&2
    exit 1
fi

echo "smoke: checking POST /mcp/ initialize (regression guard for #382/#399)…"
code="$(curl -sm8 -o /dev/null -w '%{http_code}' -X POST "http://localhost:$PORT/mcp/" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')"
if [[ "$code" != "200" ]]; then
    echo "smoke: FAIL — /mcp initialize returned $code (want 200)" >&2
    exit 1
fi

echo "smoke: PASS — the shipped compose starts on a fresh host, stays up, is claimable, and serves MCP."
