#!/usr/bin/env bash
set -uo pipefail

HOST="${1:-http://localhost:8000}"
FAIL=0

fetch_status() {
  local url="$1"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "$url" 2>/dev/null) || code="000"
  echo "$code"
}

check_health() {
  local status
  status=$(fetch_status "${HOST}/health")
  if [ "$status" != "200" ]; then
    echo "FAIL: /health returned $status (expected 200)"
    FAIL=1
  else
    echo "PASS: /health returned 200"
  fi
}

check_inventory() {
  local status body
  status=$(fetch_status "${HOST}/inventory")
  if [ "$status" = "000" ]; then
    echo "FAIL: /inventory unreachable"
    FAIL=1
    return
  fi
  body=$(curl -s --connect-timeout 5 --max-time 10 -H "Authorization: Bearer ${HP_TOKEN:-invalid}" "${HOST}/inventory" 2>/dev/null || echo "")
  if echo "$body" | python3 -c "import sys,json; json.load(sys.stdin)" >/dev/null 2>&1; then
    echo "PASS: /inventory returned JSON (status $status)"
  elif [ "$status" = "401" ] || [ "$status" = "403" ]; then
    echo "PASS: /inventory requires auth (status $status)"
  else
    echo "FAIL: /inventory did not return JSON (status $status)"
    FAIL=1
  fi
}

check_kb_auth() {
  local status
  status=$(fetch_status "${HOST}/kb")
  if [ "$status" = "000" ]; then
    echo "FAIL: /kb unreachable"
    FAIL=1
    return
  fi
  if [ "$status" != "401" ] && [ "$status" != "403" ]; then
    echo "FAIL: /kb returned $status (expected 401 or 403)"
    FAIL=1
  else
    echo "PASS: /kb requires auth (status $status)"
  fi
}

check_drift() {
  local status
  status=$(fetch_status "${HOST}/artifacts/drift")
  if [ "$status" = "000" ]; then
    echo "FAIL: /artifacts/drift unreachable"
    FAIL=1
    return
  fi
  if [ "$status" != "200" ] && [ "$status" != "401" ]; then
    echo "FAIL: /artifacts/drift returned $status (expected 200 or 401)"
    FAIL=1
  else
    echo "PASS: /artifacts/drift returned $status"
  fi
}

check_health
check_inventory
check_kb_auth
check_drift

if [ "$FAIL" -ne 0 ]; then
  echo "SMOKE TEST FAILED"
  exit 1
fi
echo "SMOKE TEST PASSED"
exit 0
