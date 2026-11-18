"""The self-check says the same thing over HTTP and over MCP (#814).

On dev and prod 3.10.0 ``get_selfcheck`` over MCP said ``loop_watchdog: off
(HP_LOOP_STALL_MS=0)`` while ``GET /admin/selfcheck`` and the log showed it
running - it had fired on dev. A running app has two state objects: the
``AppState`` it is built from (the MCP tools hold it) and Starlette's
``app.state`` (the HTTP routes hold it), mirrored by hand one field at a time.
The report read whichever its caller held, so a field mirrored onto only one of
them split the answer: the watchdog lived only on ``app.state``; the hub's
refusal reason only on the ``AppState``. Every surface now reads the one
``AppState`` (``selfcheck.report_state``).

This holds the outcome on the shipped path, in a FRESH interpreter (the suite's
``homepilot.main`` must not be booted twice in one process): the real lifespan,
the real ``GET /admin/selfcheck`` with an admin token, and the real MCP
transport (``POST /mcp/`` initialize + ``tools/call get_selfcheck``). Every
subsystem must be in the same state on both, with the same sentence.

Teeth: drop ``state.loop_watchdog = loop_watchdog`` from the lifespan (MCP says
the watchdog is off), or make the admin route read ``request.app.state`` again
(HTTP loses the hub's refusal sentence) - and this fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_ADMIN_SECRET = "selfcheck-parity-admin-secret"  # pragma: allowlist secret - throwaway

_CHILD = r"""
import json, os, sys
import homepilot.main as main
from fastapi.testclient import TestClient


def parse(resp):
    # The MCP transport answers JSON or one SSE event, whichever it chose.
    text = resp.text
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = data[-1]
    return json.loads(text)


with TestClient(main.app) as client:  # the real lifespan, run to completion
    minted = client.post(
        "/auth/tokens",
        json={"label": "parity", "scope": "admin"},
        headers={"x-hp-admin-secret": os.environ["HP_ADMIN_SECRET"]},
    )
    assert minted.status_code in (200, 201), minted.text
    token = minted.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    http = client.get("/admin/selfcheck", headers=auth)
    assert http.status_code == 200, http.text

    mcp_headers = {**auth, "Accept": "application/json, text/event-stream"}
    init = client.post(
        "/mcp/",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "hp-parity", "version": "1.0"},
            },
        },
    )
    assert init.status_code == 200, init.text
    session = init.headers.get("mcp-session-id")
    if session:
        mcp_headers["mcp-session-id"] = session
    client.post("/mcp/", headers=mcp_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    call = client.post(
        "/mcp/",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "get_selfcheck", "arguments": {}}},
    )
    assert call.status_code == 200, call.text
    result = parse(call)["result"]
    assert not result.get("isError"), result
    mcp = result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def table(report):
        return {s["name"]: [s["state"], s["consequence"]] for s in report["subsystems"]}

    print("RESULT " + json.dumps({"http": table(http.json()), "mcp": table(mcp)}))
"""


def _both_reports(tmp_path: Path, extra_env: dict[str, str]) -> dict[str, dict[str, list[str]]]:
    home = tmp_path / "home"
    home.mkdir()
    # Under $HOME, not tmp_path: create_app_state refuses an artifacts dir in /tmp.
    data = Path(tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-selfcheck-parity-"))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HP_DATA_DIR": str(data),
        "HP_ARTIFACTS_DIR": str(data / "artifacts"),
        "HP_SECRET_KEY": "selfcheck-parity-secret",
        "HP_VAULT_PASSPHRASE": "selfcheck-parity-pass",
        "HP_ADMIN_SECRET": _ADMIN_SECRET,
        "HP_UPDATE_CHECK_ENABLED": "0",
        "PROXMOX_SECRETS": "/nonexistent",
        **extra_env,
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        shutil.rmtree(data, ignore_errors=True)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, (
        f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    )
    return dict(json.loads(lines[-1][len("RESULT ") :]))


def _differences(reports: dict[str, dict[str, list[str]]]) -> list[str]:
    http, mcp = reports["http"], reports["mcp"]
    return [
        f"{name}: HTTP {http.get(name)} / MCP {mcp.get(name)}"
        for name in sorted(set(http) | set(mcp))
        if http.get(name) != mcp.get(name)
    ]


def test_http_and_mcp_report_every_subsystem_alike_with_the_watchdog_on(tmp_path: Path) -> None:
    # The agent hub enabled on an exposed plaintext bind, which it refuses
    # (#468): it never binds a port, and it has a refusal sentence to report.
    reports = _both_reports(
        tmp_path,
        {"HP_LOOP_STALL_MS": "500", "HP_AGENT_HUB_TLS": "false", "HP_AGENT_HUB_HOST": "0.0.0.0"},
    )
    assert reports["http"]["loop_watchdog"][0] == "ok", reports["http"]["loop_watchdog"]
    assert "refusing to start" in reports["mcp"]["agent_hub"][1], reports["mcp"]["agent_hub"]
    assert not _differences(reports), "self-check differs by transport:\n" + "\n".join(
        _differences(reports)
    )


def test_http_and_mcp_both_say_off_when_the_watchdog_is_off(tmp_path: Path) -> None:
    reports = _both_reports(tmp_path, {"HP_LOOP_STALL_MS": "0", "HP_AGENT_HUB_ENABLED": "false"})
    assert reports["mcp"]["loop_watchdog"][0] == "off", reports["mcp"]["loop_watchdog"]
    assert not _differences(reports), "self-check differs by transport:\n" + "\n".join(
        _differences(reports)
    )
