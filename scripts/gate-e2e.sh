#!/usr/bin/env bash
# gate-e2e: tests/test_e2e.py against the REAL built artifact, the way the public
# mirror's `e2e` workflow runs it - build the web UI from the lockfile, start the
# server from this tree, mint the run's token over HTTP with the admin secret,
# drive headless chromium through Playwright.
#
# Why it exists: `make gate` never ran tests/test_e2e.py (it skips without
# HP_TEST_TOKEN), so 3.9.0 shipped with two e2e failures that only the mirror's
# CI found, after release (a redirect expectation the UI slice had moved, and a
# loop-block false positive in an e2e fixture).
#
# Differences from the workflow, all isolation and none of substance:
#   * the server runs with an EMPTY environment and a throwaway HOME, data dir
#     and working directory, so no operator config (~/.hp/.env, HP_*, PVE_*, a
#     ./.env) can reach it - it cannot talk to a real Proxmox, or touch a real
#     database, from here;
#   * it listens on 127.0.0.1 on free ports (the agent hub too), so it runs
#     beside anything else on the box;
#   * the admin secret is random per run, and the update check is off (no
#     call to the release API from a gate run).
#
# Usage: scripts/gate-e2e.sh <venv-bin-dir>      (make gate-e2e passes $(VENV))
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV_BIN="$(cd "${1:-$REPO/.venv/bin}" && pwd)"
PY="$VENV_BIN/python"

# Under $HOME, not $TMPDIR: create_app_state refuses an artifacts dir in /tmp,
# so with the default TMPDIR the server never booted (3.10.0 integration; the
# lane's own runs had TMPDIR under /home). Removed on exit like before.
WORK="$(mktemp -d "$HOME/.hp-gate-e2e.XXXXXX")"
SERVER_PID=""
# SIGTERM, as `docker stop` sends it, then a bounded wait: 0 when the server
# exited, 1 when it was still running and had to be killed. Never an unbounded
# wait - a server that ignores SIGTERM (3.9.0's did) must not hang the gate.
stop_server() {
    [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null || return 0
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
        kill -0 "$SERVER_PID" 2>/dev/null || { wait "$SERVER_PID" 2>/dev/null || true; return 0; }
        sleep 1
    done
    kill -KILL "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    return 1
}
cleanup() {
    stop_server || true
    rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
    echo "gate-e2e: FAIL - $*" >&2
    if [[ -f "$WORK/server.log" ]]; then
        echo "gate-e2e: last server log lines:" >&2
        tail -n 40 "$WORK/server.log" >&2
    fi
    exit 1
}

echo "gate-e2e: building the web UI from the lockfile (npm ci + build)"
(cd "$REPO/web" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null) \
    || fail "web build failed"

# The browser the installed Playwright expects. Idempotent; installs into the
# user's Playwright cache, never system-wide (no --with-deps).
"$VENV_BIN/playwright" install chromium >/dev/null || fail "playwright install chromium failed"

free_port() {
    "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}
PORT="$(free_port)"
HUB_PORT="$(free_port)"
[[ "$PORT" != "$HUB_PORT" ]] || HUB_PORT="$(free_port)"
ADMIN_SECRET="$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
BASE_URL="http://127.0.0.1:$PORT"
mkdir -p "$WORK/home" "$WORK/data"

echo "gate-e2e: starting the server from this tree on $BASE_URL (isolated HOME and data dir)"
(
    cd "$WORK"
    exec env -i \
        PATH="$PATH" HOME="$WORK/home" LANG=C.UTF-8 \
        PYTHONPATH="$REPO/src" \
        HP_DATA_DIR="$WORK/data" HP_ARTIFACTS_DIR="$WORK/data/artifacts" \
        HP_SECRET_KEY=gate-e2e-secret HP_VAULT_PASSPHRASE=gate-e2e-pass \
        HP_ADMIN_SECRET="$ADMIN_SECRET" HP_UPDATE_CHECK_ENABLED=0 \
        HP_PORT="$PORT" HP_AGENT_HUB_HOST=127.0.0.1 HP_AGENT_HUB_PORT="$HUB_PORT" \
        "$VENV_BIN/uvicorn" homepilot.main:app --host 127.0.0.1 --port "$PORT"
) >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

healthy=""
for _ in $(seq 1 60); do
    kill -0 "$SERVER_PID" 2>/dev/null || fail "the server exited during startup"
    if curl -sf "$BASE_URL/health" >/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
[[ -n "$healthy" ]] || fail "the server did not become healthy in 60 s"

# The run's token, minted the way the workflow mints it: one POST with the
# admin secret. It counts against the per-IP mint budget the suite plans for
# (see the header of tests/test_e2e.py).
TOKEN="$(curl -sf -X POST "$BASE_URL/auth/tokens" \
    -H "Content-Type: application/json" \
    -H "x-hp-admin-secret: $ADMIN_SECRET" \
    -d '{"label": "e2e-gate", "scope": "full"}' \
    | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["token"])')" \
    || fail "could not mint the test token"
[[ -n "$TOKEN" ]] || fail "the token response carried no token"

echo "gate-e2e: running tests/test_e2e.py"
cd "$REPO"
HP_TEST_URL="$BASE_URL" HP_TEST_TOKEN="$TOKEN" HP_TEST_ADMIN_SECRET="$ADMIN_SECRET" \
    "$PY" -m pytest tests/test_e2e.py -v -p no:cacheprovider --junitxml="$WORK/e2e.xml" \
    || fail "tests/test_e2e.py failed"

# Every collected test must have RUN. test_e2e.py skips itself without a token
# or an admin secret, so a wiring mistake here would otherwise pass as a gate
# that tested nothing.
"$PY" - "$WORK/e2e.xml" <<'PY' || fail "tests were skipped (see above)"
import sys
import xml.etree.ElementTree as ET

cases = list(ET.parse(sys.argv[1]).getroot().iter("testcase"))
skipped = [c.attrib["classname"] + "::" + c.attrib["name"] for c in cases if c.find("skipped") is not None]
if not cases or skipped:
    print(f"gate-e2e: {len(cases)} collected, skipped: {skipped or 'nothing collected'}")
    sys.exit(1)
print(f"gate-e2e: {len(cases)} tests ran, none skipped")
PY

# The shipped stop path: the image's entry point is this same uvicorn command,
# and `docker stop` is a SIGTERM. The server must shut down on it, gracefully.
stop_server || fail "the server ignored SIGTERM for 30 s and had to be killed"
grep -q "Application shutdown complete." "$WORK/server.log" \
    || fail "the server stopped without completing its shutdown"
echo "gate-e2e: passed"
