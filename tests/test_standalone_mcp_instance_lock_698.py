"""A standalone MCP server is never a second control plane (#698).

WHAT THIS FORBIDS: `hp mcp-serve` (stdio, or its own HTTP port) starting next to
a running backend. It calls `create_app_state`, which migrates the live DB and
builds its own task runner, executor, SSE bus and agent hub - none of which the
backend's agents are connected to. Measured against a real bootstrap:
`exec_on_host` and `check_host_reachable` answered "no agent connected" for
every host, and applies ran inside the MCP process where the UI never saw them.

The contract: while a backend holds the instance lock, a standalone MCP server
refuses BEFORE it opens or migrates anything, and says to use the backend's own
/mcp. With no backend, it takes the lock for its lifetime, so a backend started
meanwhile refuses instead.

Teeth: drop `_claim_data_dir()` from run_server / run_server_http and the
refusal tests fail (the bootstrap runs, the database file is created).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from homepilot.instance_lock import InstanceLock, InstanceLockError, another_instance_is_running
from homepilot.mcp import server as mcp_server


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from homepilot.config import Settings

    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=str(data),
        artifacts_dir=str(data / "artifacts"),
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    bootstrapped: list[bool] = []

    async def spy_bootstrap() -> dict[str, Any]:
        bootstrapped.append(another_instance_is_running(data))
        raise _StopError

    monkeypatch.setattr(mcp_server, "_bootstrap", spy_bootstrap)
    return data, bootstrapped


class _StopError(Exception):
    """Ends the server right after the bootstrap would have run."""


@pytest.mark.parametrize("start", ["stdio", "http"])
def test_refuses_while_a_backend_holds_the_data_dir(data_dir, start):
    data, bootstrapped = data_dir
    run = mcp_server.run_server() if start == "stdio" else mcp_server.run_server_http(port=0)

    with InstanceLock(data), pytest.raises(InstanceLockError, match="/mcp"):
        asyncio.run(run)

    assert bootstrapped == [], "the MCP server built its own control plane beside the backend"
    assert not (data / "homepilot.db").exists(), "the live database was opened/migrated"


@pytest.mark.parametrize("start", ["stdio", "http"])
def test_standalone_holds_the_lock_for_its_lifetime(data_dir, start):
    data, bootstrapped = data_dir
    run = mcp_server.run_server() if start == "stdio" else mcp_server.run_server_http(port=0)

    with pytest.raises(_StopError):
        asyncio.run(run)

    assert bootstrapped == [True], "a backend could have started beside the standalone server"
    assert not another_instance_is_running(data), "the lock outlived the server"


def test_hp_mcp_serve_explains_the_refusal(tmp_path, monkeypatch):
    from homepilot.cli.main import app
    from homepilot.config import get_settings

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HP_DATA_DIR", str(data))
    monkeypatch.setenv("HP_ARTIFACTS_DIR", str(data / "artifacts"))
    get_settings.cache_clear()
    try:
        with InstanceLock(Path(data)):
            r = CliRunner().invoke(app, ["mcp-serve"])
    finally:
        get_settings.cache_clear()

    assert r.exit_code == 1, r.output
    assert "/mcp" in r.output
    assert not (data / "homepilot.db").exists()
