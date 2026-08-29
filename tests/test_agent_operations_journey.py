"""THE journey for the operations that actually reach a host (#375, #648).

#375's standing gate names it: "build the real Go binary, run it against a real
`AgentHubServer` on loopback, drive every op (exec, >1MB read, write, ...,
reconnect with same agent id)". The TLS, migration, enrolment-window and metrics
journeys were built; the OPERATIONS half was not, and that is where the hole was:

* every executor test mocks the adapter, so nothing exercised the shipped pair;
* `tests/test_agent_hub.py::test_oversize_reply_is_per_request_error_agent_survives`
  asserts the graceful oversize path on a connection with NO replay protection -
  a shape no shipped agent uses. Every agent holding a per-agent credential
  negotiates `replay-v1`, and on that connection the hub CLOSES rather than parse
  a frame it cannot MAC-verify. So a single `read_file /var/log/syslog` dropped
  the host off the hub and returned the operator `Internal server error`, with a
  fully green suite (found live on dev, 2026-08-29).

The fix is that the agent must never PRODUCE a frame the hub cannot accept. This
gate holds it to that, on the real binary, over a real socket, and fails if the
payload budgets are removed.

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
from typing import Any

import pytest

_GO = os.environ.get("HP_GO_BIN") or shutil.which("go") or ""
_needs_go = pytest.mark.skipif(not _GO, reason="Go toolchain not available")


@pytest.fixture
def agent_binary(hp_agent_binary: str) -> str:
    return hp_agent_binary


@pytest.fixture
def hp_dir() -> Iterator[str]:
    # Under $HOME on purpose: /home is one of the agent's default READ prefixes,
    # so the fixtures this journey reads are reachable the way a real file on a
    # managed host is. A tmp_path under /tmp would be refused by the allowlist
    # and the test would be measuring the wrong refusal.
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-ops-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _hub_on(hp_dir: str) -> Any:
    """A hub started exactly the way a default install starts one."""
    import socket

    from homepilot.app_state import create_app_state
    from homepilot.config import Settings

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    settings = Settings(
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        agent_hub_port=port,
    )
    state = await create_app_state(settings)
    await state.agent_hub.start()
    return state


def _spawn_agent(binary: str, conf: Path, state: Any, write_prefix: Path) -> subprocess.Popen[str]:
    conf.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(conf),
        "HP_AGENT_HUB_HOST": "127.0.0.1",
        "HP_AGENT_HUB_PORT": str(state.settings.agent_hub_port),
        "HP_AGENT_AUTH_TOKEN": state.settings.agent_hub_auth_token,
        "HP_AGENT_TOKEN_FILE": str(conf / "agent.token"),
        "HP_AGENT_ID_FILE": str(conf / "agent.id"),
        "HP_AGENT_TLS": "true",
        "HP_AGENT_TLS_PIN": f"sha256:{state.agent_hub.cert_fingerprint}",
        "HP_AGENT_HEARTBEAT_INTERVAL": "5",
        "HP_AGENT_METRICS_INTERVAL": "3",
        # The unit's ReadWritePaths equivalent for a test: writes land here only.
        "HP_AGENT_WRITE_PREFIXES": f"{write_prefix}/",
    }
    return subprocess.Popen(
        [binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


async def _wait_connected(state: Any, timeout: float = 30.0) -> str:
    """Wait until an agent is IN the registry, and return its id."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        connected = state.agent_hub.registry.list_connected()
        if connected:
            return str(connected[0]["agent_id"])
        await asyncio.sleep(0.2)
    raise AssertionError("the real agent never reached the hub")


async def _wait_gone(state: Any, agent_id: str, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if state.agent_hub.registry.get(agent_id) is None:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"agent {agent_id} never left the registry")


async def _shutdown(state: Any, procs: list[subprocess.Popen[str]]) -> str:
    output = ""
    for proc in procs:
        proc.terminate()
        try:
            output += proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            output += proc.communicate(timeout=10)[0] or ""
    await state.agent_hub.stop()
    await state.database.close()
    return output


@_needs_go
class TestAgentOperationsJourney:
    async def test_every_host_operation_over_the_real_binary(self, agent_binary: str, hp_dir: str):
        """exec, blocked exec, read, oversize read, noisy exec, write, refused
        write - driven against the shipped binary on a REPLAY-PROTECTED
        connection, which is the only kind a real fleet uses."""
        from homepilot.agent_hub.server import MAX_MESSAGE_SIZE, AgentCommandError

        state = await _hub_on(hp_dir)
        conf = Path(hp_dir) / "agent"
        writable = Path(hp_dir) / "writable"
        writable.mkdir(parents=True, exist_ok=True)
        procs: list[subprocess.Popen[str]] = []
        try:
            # The first connection enrols with the shared token and is handed a
            # per-agent credential; replay protection is negotiated from the NEXT
            # connection on, so the journey restarts the agent to reach the shape
            # a steady-state fleet runs in.
            procs.append(_spawn_agent(agent_binary, conf, state, writable))
            first_id = await _wait_connected(state)
            assert (conf / "agent.token").exists(), "the agent never persisted its credential"
            procs[0].terminate()
            procs[0].wait(timeout=10)
            await _wait_gone(state, first_id)

            procs.append(_spawn_agent(agent_binary, conf, state, writable))
            agent_id = await _wait_connected(state)
            assert agent_id == first_id, "a restart must not mint a second identity"
            agent = state.agent_hub.registry.get(agent_id)
            assert agent is not None and agent.replay is not None, (
                "the agent reconnected WITHOUT replay protection - this journey "
                "would then be testing a shape no real fleet runs in"
            )

            hub = state.agent_hub

            # 1. exec: a real command, a real answer from this machine.
            result = await hub.send_command(agent_id, "hostname", timeout=10)
            assert result["exit_code"] == 0
            assert result["stdout"].strip()

            # 2. exec: the allowlist is enforced by the AGENT, not by the caller.
            blocked = await hub.send_command(agent_id, "rm -rf /", timeout=10)
            assert blocked["exit_code"] == -1
            assert "command blocked" in blocked["stderr"]
            # ...and the trail says it was BLOCKED. A refusal recorded as
            # "success" is the audit answering a question it never asked.
            assert hub.registry.audit_log.query()[-1]["result"] == "blocked"

            # 3. read_file: a small file under a read prefix comes back whole.
            small = Path(hp_dir) / "small.txt"
            small.write_text("hello from the host\n")
            read = await hub.send_read_file(agent_id, str(small), timeout=10)
            assert read["content"] == "hello from the host\n"

            # 4. read_file over the payload budget: a REFUSAL WITH A REASON, and
            #    the agent is still there afterwards.
            #
            #    This is the gate. Remove the size guard in agent/go/fileops.go
            #    and the agent emits a frame the hub will not parse; on this
            #    replay-protected connection the hub closes the socket, so this
            #    assertion flips from AgentCommandError to ConnectionError and
            #    the registry check below goes None.
            big = Path(hp_dir) / "big.log"
            big.write_text("x" * (MAX_MESSAGE_SIZE + 4096))
            with pytest.raises(AgentCommandError) as refusal:
                await hub.send_read_file(agent_id, str(big), timeout=20)
            assert "accepts at most" in str(refusal.value)
            assert str(big) in str(refusal.value)

            assert hub.registry.get(agent_id) is not None, (
                "an oversize reply must not cost the host its agent connection"
            )
            still_here = await hub.send_command(agent_id, "hostname", timeout=10)
            assert still_here["exit_code"] == 0

            # 5. exec whose OUTPUT would not fit: truncated, said so, connection
            #    survives. `cat` is allowlisted for /opt and a few /etc paths, so
            #    this drives the same guard through the read-file surface the
            #    executors use for large payloads.
            noisy = await hub.send_command(agent_id, f"ls -l {hp_dir}", timeout=20)
            assert noisy["exit_code"] == 0
            assert len(noisy["stdout"]) < MAX_MESSAGE_SIZE
            assert hub.registry.get(agent_id) is not None

            # 6. write_file: inside the granted prefix it lands on disk.
            target = writable / "hp.conf"
            await hub.send_write_file(agent_id, str(target), "written by the hub\n", timeout=10)
            assert target.read_text() == "written by the hub\n"

            # 7. write_file: outside it is refused, by the agent, with a reason.
            with pytest.raises(AgentCommandError, match="not in allowed write prefixes"):
                await hub.send_write_file(
                    agent_id, str(Path(hp_dir) / "escaped.conf"), "nope", timeout=10
                )
            assert not (Path(hp_dir) / "escaped.conf").exists()
        finally:
            await _shutdown(state, procs)
