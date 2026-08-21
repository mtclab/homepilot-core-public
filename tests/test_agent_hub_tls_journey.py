"""The shipped Go agent must actually reach a self-configured hub (ADR-004 S3).

Everything else in the hub TLS gates is one-sided: the Python tests prove the
hub serves a certificate, the Go tests prove a pinned client refuses the wrong
one. Neither proves the two halves agree. This drives the REAL `hp-agent`
binary, built from `agent/go`, against a REAL hub started from
``create_app_state`` on a default config, and asserts the journey's endpoint -
the host shows up as a connected agent.

The negative case is the same journey with the WRONG pin: the agent must never
appear, because the certificate is not the one it was told to trust.

Skipped when no Go toolchain is present (set ``HP_GO_BIN`` to point at one).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "agent" / "go"

_GO = os.environ.get("HP_GO_BIN") or shutil.which("go") or ""
pytestmark = pytest.mark.skipif(not _GO, reason="Go toolchain not available")


# The binary is built ONCE per session by the shared `hp_agent_binary` fixture
# (tests/conftest.py) - two cold Go builds in one run is enough to trip the
# per-test timeout on whichever test owns the build.
@pytest.fixture
def agent_binary(hp_agent_binary: str) -> str:
    return hp_agent_binary


@pytest.fixture
def hp_dir() -> Iterator[str]:
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-journey-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _hub_on(hp_dir: str):
    """A hub started exactly the way a default install starts one."""
    import socket

    from homepilot.app_state import create_app_state
    from homepilot.config import Settings

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    settings = Settings(
        secret_key="test-secret-key-for-pytest-only-not-for-production",
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        agent_hub_port=port,
    )
    state = await create_app_state(settings)
    await state.agent_hub.start()
    return state, settings


def _spawn_agent(binary: str, conf: Path, settings, pin: str) -> subprocess.Popen[str]:
    conf.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(conf),
        "HP_AGENT_HUB_HOST": "127.0.0.1",
        "HP_AGENT_HUB_PORT": str(settings.agent_hub_port),
        "HP_AGENT_AUTH_TOKEN": settings.agent_hub_auth_token,
        "HP_AGENT_TOKEN_FILE": str(conf / "agent.token"),
        "HP_AGENT_ID_FILE": str(conf / "agent.id"),
        "HP_AGENT_TLS": "true",
        "HP_AGENT_TLS_PIN": pin,
        "HP_AGENT_HEARTBEAT_INTERVAL": "5",
    }
    return subprocess.Popen(
        [binary],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


async def _wait_connected(state, timeout: float) -> list:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        connected = state.agent_registry.list_connected()
        if connected:
            return connected
        await asyncio.sleep(0.2)
    return []


async def _shutdown(state, proc: subprocess.Popen[str] | None) -> str:
    output = ""
    if proc is not None:
        proc.terminate()
        try:
            output = proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            output = proc.communicate(timeout=10)[0] or ""
    await state.agent_hub.stop()
    await state.database.close()
    return output


class TestAgentReachesASelfConfiguredHub:
    async def test_pinned_agent_enrolls_with_no_operator_tls_setup(
        self, agent_binary: str, hp_dir: str, tmp_path: Path
    ):
        """The whole point of the slice: an operator configures NOTHING, and a
        host still enrolls over a verified TLS connection."""
        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            pin = "sha256:" + state.agent_hub.cert_fingerprint
            proc = _spawn_agent(agent_binary, tmp_path / "host-ok", settings, pin)
            connected = await _wait_connected(state, timeout=30)
            assert connected, "the agent never registered against the self-configured hub"
            assert connected[0]["hostname"]
        finally:
            log = await _shutdown(state, proc)
            assert "hub certificate pinned to" in log, log

    async def test_agent_with_the_wrong_pin_never_enrolls(
        self, agent_binary: str, hp_dir: str, tmp_path: Path
    ):
        """A hub whose certificate is not the pinned one is refused, so the
        host does NOT appear - the agent is not merely logging a complaint
        while connecting anyway."""
        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            wrong = "sha256:" + "00" * 32
            proc = _spawn_agent(agent_binary, tmp_path / "host-bad", settings, wrong)
            connected = await _wait_connected(state, timeout=8)
            assert not connected, "agent enrolled despite a certificate it never trusted"
        finally:
            log = await _shutdown(state, proc)
            assert "does not match the pinned fingerprint" in log, log
