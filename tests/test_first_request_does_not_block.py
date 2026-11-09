"""The first request after a start does not block the event loop (3.10.0).

FastAPI 0.137+ builds each included route's effective context (dependant +
include-time dependencies + prefix) lazily, the first time a request is matched
against its router. On the locked FastAPI 0.141.1 that is ~300 ms of CPU for
this app, and it ran ON the loop inside the first request after every start -
usually the container's own /health probe - holding every other request and the
agent hub. The loop-block detector caught it in
test_gate_qa_pr253::test_response_has_status_and_checks_keys (281-305 ms against
the 250 ms budget). The 3.9.0 gate had missed it because its venv had drifted
from uv.lock to a FastAPI that is cheaper here; `make gate` now refuses a
drifted venv (scripts/check_venv_lock.sh).

The fix builds the table at import, before uvicorn starts the loop
(`homepilot.main.build_route_table`). These tests hold it the way production
meets it: a FRESH interpreter (the suite's own `homepilot.main` has long been
warmed by earlier tests, so an in-process check would pass in any order), an
isolated environment, the real lifespan run to completion, and only then the
first request, with every loop callback timed.

Teeth: drop the `build_route_table(app)` call in main.py and both fail - the
first request builds ~140 route contexts and blocks the loop ~300 ms.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.loop_block_detector import BUDGET_S

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runs in the child. Times every loop callback while the first request is
# served, and counts the route contexts FastAPI builds while serving it.
_CHILD = r"""
import asyncio, json, sys, time

budget_path = sys.argv[1]
import homepilot.main as main
from fastapi.testclient import TestClient

built_during_request = None
try:
    from fastapi.routing import _EffectiveRouteContext as _ERC
except ImportError:  # a FastAPI without the lazy build: nothing to count
    _ERC = None

longest = [0.0, ""]
timing = [False]
_orig_run = asyncio.events.Handle._run


def _timed_run(self):
    if not timing[0]:
        return _orig_run(self)
    start = time.perf_counter()
    try:
        return _orig_run(self)
    finally:
        took = time.perf_counter() - start
        if took > longest[0]:
            longest[0], longest[1] = took, repr(self)[:200]


asyncio.events.Handle._run = _timed_run

with TestClient(main.app) as client:  # runs the real lifespan to completion
    if _ERC is not None:
        built_during_request = 0
        _orig_from_api_route = _ERC.from_api_route.__func__

        def _counting(cls, *args, **kwargs):
            global built_during_request
            built_during_request += 1
            return _orig_from_api_route(cls, *args, **kwargs)

        _ERC.from_api_route = classmethod(_counting)
    timing[0] = True
    response = client.get(budget_path)
    timing[0] = False

print(
    "RESULT "
    + json.dumps(
        {
            "status": response.status_code,
            "longest_ms": round(longest[0] * 1000, 1),
            "longest_handle": longest[1],
            "built_at_import": main.ROUTE_CONTEXTS_BUILT,
            "built_during_request": built_during_request,
        }
    )
)
"""


def _first_request(tmp_path: Path, path: str) -> dict[str, object]:
    home = tmp_path / "home"
    home.mkdir()
    # Under $HOME, not tmp_path: create_app_state refuses an artifacts dir in
    # /tmp (see test_sigterm_stops_the_server).
    data = Path(tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-first-request-"))
    # An EMPTY environment plus what the app needs (as in
    # test_sigterm_stops_the_server): no operator config can reach it.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HP_DATA_DIR": str(data),
        "HP_ARTIFACTS_DIR": str(data / "artifacts"),
        "HP_SECRET_KEY": "first-request-test-secret",
        "HP_VAULT_PASSPHRASE": "first-request-test-pass",
        "HP_AGENT_HUB_ENABLED": "false",
        "HP_UPDATE_CHECK_ENABLED": "0",
        "PROXMOX_SECRETS": "/nonexistent",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, path],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        shutil.rmtree(data, ignore_errors=True)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, (
        f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    )
    return dict(json.loads(lines[-1][len("RESULT ") :]))


@pytest.mark.parametrize(
    "path",
    [
        # The container's healthcheck: in production the first request of all.
        "/health",
        # An unknown address walks every route of every router before its 404.
        "/no-such-page",
    ],
)
def test_the_first_request_after_start_does_not_block_the_loop(tmp_path: Path, path: str) -> None:
    result = _first_request(tmp_path, path)
    assert result["status"] in (200, 404, 503), result
    # The table was built at import, before any loop existed ...
    assert int(result["built_at_import"]) > 100, result
    # ... so serving the first request builds nothing (where FastAPI has the
    # lazy build to count) ...
    if result["built_during_request"] is not None:
        assert result["built_during_request"] == 0, (
            f"the first {path} built {result['built_during_request']} route contexts on the "
            f"event loop: {result}"
        )
    # ... and no loop callback of it runs past the detector's budget.
    assert float(result["longest_ms"]) <= BUDGET_S * 1000, (
        f"the first {path} after start blocked the loop {result['longest_ms']} ms "
        f"(budget {BUDGET_S * 1000:.0f} ms): {result['longest_handle']}"
    )
