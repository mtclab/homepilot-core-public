"""THE journey for systemd unit secrets at the read tier (#853), on the shipped pair.

Unit files and drop-ins are readable at the read tier, and they carry other
services' secrets: ``Environment=TOKEN=...`` in a drop-in, ``SetCredential=``, a
WireGuard key, and the files a unit loads its environment from
(``EnvironmentFile=``, ``LoadCredential=``), which were readable whenever they
sat under a read prefix and nothing in their name said "env".

The REAL ``hp-agent`` binary connects to a REAL hub, and a fixture host's units
(a service, its drop-in, a netdev, a manager config) and the files they name sit
under ``/tmp/homepilot/`` - a default write prefix, so the control plane's own
lexical gate lets the MCP read tools ask for them exactly as it would on a real
host. ``HP_AGENT_UNIT_DIRS`` tells the agent the fixture's unit directory. Every
file is read through every read-tier path an assistant has - the MCP
``read_file_on_guest`` and ``exec_on_guest_readonly cat`` at the ``read_only``
tier - and through the hub's ``read_file`` directly; no answer may carry a
sentinel. The control: the sentinels are on disk, and the units ARE served,
variable names intact.

Teeth: make the agent's readFile skip redactUnitText, or drop
unitSecretSourceRefusal from checkReadPath, rebuild, and this fails.

Skipped when no Go toolchain is present (set ``HP_GO_BIN`` to point at one).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .go_toolchain import GO as _GO
from .go_toolchain import GO_SKIP_REASON

_needs_go = pytest.mark.skipif(not _GO, reason=GO_SKIP_REASON)

pytestmark = pytest.mark.timeout(600)

_MARK = "hpJOURNEY" + "853"


def _secret(name: str) -> str:
    # Assembled, so no credential-shaped literal sits in the tree.
    return _MARK + name + "zq"


def build_fixture_units(root: Path) -> tuple[list[Path], list[Path]]:
    """Units under root/units, the files they load secrets from under
    root/conf. Returns (secret sources, unit files)."""

    def write(rel: str, text: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    conf = root / "conf"
    sources = [
        write("conf/app-settings", f"API_KEY={_secret('envfile')}\n"),
        write("conf/instance-web", f"DB_URL={_secret('specifier')}\n"),
        write("conf/dbpass.bin", f"{_secret('loadcred')}\n"),
        write("conf/peer-psk", f"{_secret('pskfile')}\n"),
    ]
    write("conf/readme.txt", "plain notes\n")
    units = [
        write(
            "units/app.service",
            "[Unit]\nDescription=app\n[Service]\n"
            f"Environment=TOKEN={_secret('env')}\n"
            f'Environment="A={_secret("quoted")}" "B=two {_secret("quoted2")}"\n'
            f"Environment=C=1 \\\n  D={_secret('continued')}\n"
            f"EnvironmentFile={conf}/app-settings\n"
            f"EnvironmentFile=-{conf}/instance-%i\n"
            f"LoadCredential=db:{conf}/dbpass.bin\n"
            f"SetCredential=api:{_secret('setcred')}\n"
            "ExecStart=/usr/bin/app\n",
        ),
        write(
            "units/app.service.d/override.conf",
            f'[Service]\nEnvironment="PASSWORD={_secret("dropin")}"\n'
            # The #817 class inside a unit: argv carries a token.
            f"ExecStart=\nExecStart=/usr/bin/app --token={_secret('execarg')}\n",
        ),
        write(
            "units/wg0.netdev",
            f"[NetDev]\nName=wg0\nKind=wireguard\n[WireGuard]\nPrivateKey={_secret('wgkey')}\n"
            f"[WireGuardPeer]\nPresharedKeyFile={conf}/peer-psk\n",
        ),
        write("units/system.conf", f"[Manager]\nDefaultEnvironment=PROXY={_secret('default')}\n"),
    ]
    return sources, units


@pytest.fixture
def agent_binary(hp_agent_binary: str) -> str:
    return hp_agent_binary


@pytest.fixture
def hp_dir() -> Iterator[str]:
    # Under $HOME: create_app_state refuses an artifacts dir in /tmp.
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-units-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def host_root() -> Iterator[Path]:
    # Under /tmp/homepilot/, a default write prefix: the MCP tools' lexical gate
    # admits it, so the journey measures the AGENT's answer, not that refusal.
    base = Path("/tmp/homepilot")
    created = not base.exists()
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(base), prefix="test-853-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        if created:
            # Something else may have put files there meanwhile: leave them.
            with contextlib.suppress(OSError):
                base.rmdir()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _hub_on(hp_dir: str) -> Any:
    from homepilot.app_state import create_app_state
    from homepilot.config import Settings

    settings = Settings(
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        agent_hub_port=_free_port(),
    )
    state = await create_app_state(settings)
    await state.agent_hub.start()
    return state


def _spawn_agent(binary: str, conf: Path, state: Any, root: Path) -> subprocess.Popen[str]:
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
        "HP_AGENT_METRICS_ENABLED": "false",
        "HP_AGENT_UNIT_DIRS": str(root / "units"),
    }
    return subprocess.Popen(
        [binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


async def _wait_connected(state: Any, timeout: float = 30.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        connected = state.agent_hub.registry.list_connected()
        if connected:
            return state.agent_hub.registry.get(connected[0]["agent_id"])
        await asyncio.sleep(0.2)
    raise AssertionError("the real agent never reached the hub")


async def _shutdown(state: Any, proc: subprocess.Popen[str] | None) -> str:
    output = ""
    if proc is not None:
        proc.terminate()
        try:
            output = proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            output = proc.communicate(timeout=10)[0] or ""
    await state.kb_service.drain()
    await state.agent_hub.stop()
    await state.database.close()
    return output


def _as_text(answer: Any) -> str:
    if isinstance(answer, list):
        return "\n".join(str(getattr(part, "text", part)) for part in answer)
    return json.dumps(answer)


@_needs_go
class TestUnitSecretsJourney:
    async def test_no_read_tier_path_shows_a_units_secret(
        self, agent_binary: str, hp_dir: str, host_root: Path, tmp_path: Path
    ):
        from homepilot.adapters.agent import AgentAdapter
        from homepilot.mcp.server import _handle_tool, _mcp_token_scope_var

        sources, units = build_fixture_units(host_root)
        every_file = sorted(p for p in host_root.rglob("*") if p.is_file())
        on_disk = sum(p.read_text("utf-8").count(_MARK) for p in every_file)
        assert on_disk >= 12, "the fixture is not holding the secrets the journey looks for"

        state = await _hub_on(hp_dir)
        proc = None
        try:
            proc = _spawn_agent(agent_binary, tmp_path / "agent-conf", state, host_root)
            agent = await _wait_connected(state)
            ctx = {"agent_adapter": AgentAdapter(hub_server=state.agent_hub)}
            host = agent.hostname

            answers: dict[str, str] = {}
            refusals: dict[str, str] = {}
            tok = _mcp_token_scope_var.set("read_only")
            try:
                for path in every_file:
                    for how, call in (
                        ("read_file_on_guest", ("read_file_on_guest", {"path": str(path)})),
                        (
                            "exec_on_guest_readonly",
                            ("exec_on_guest_readonly", {"command": f"cat {path}"}),
                        ),
                    ):
                        name, args = call
                        try:
                            answer = await _handle_tool(name, {"host": host, **args}, ctx)
                        except Exception as exc:  # a refusal is an answer too
                            refusals[f"{how} {path}"] = str(exc)
                            continue
                        answers[f"{how} {path}"] = _as_text(answer)
            finally:
                _mcp_token_scope_var.reset(tok)
            for path in every_file:
                try:
                    hub = await state.agent_hub.send_read_file(
                        agent.agent_id, str(path), timeout=10
                    )
                    answers[f"hub read_file {path}"] = json.dumps(hub)
                except Exception as exc:  # the agent's refusal
                    refusals[f"hub read_file {path}"] = str(exc)

            for where, text in {**answers, **refusals}.items():
                assert _MARK not in text, f"a unit's secret reached {where}:\n{text}"

            # The secret sources are refused by the agent, naming why.
            for path in sources:
                why = refusals.get(f"read_file_on_guest {path}", "")
                assert "secret source" in why, f"{path} was not refused as a secret source: {why}"
                key = f"exec_on_guest_readonly {path}"
                cat = answers.get(key, "") + refusals.get(key, "")
                assert "secret source" in cat, f"cat {path}: {cat}"
            # The units are served, names intact: the read tier still explains them.
            app = answers[f"read_file_on_guest {units[0]}"]
            for want in (
                "Environment=TOKEN=<redacted>",
                'Environment="A=<redacted>" "B=<redacted>"',
                "Environment=C=<redacted> D=<redacted>",
                "SetCredential=api:<redacted>",
                "ExecStart=/usr/bin/app",
            ):
                assert want in app, f"the served unit lost {want!r}:\n{app}"
            for unit in units:
                assert "<redacted>" in answers[f"read_file_on_guest {unit}"], unit
            dropin = answers[f"read_file_on_guest {units[1]}"]
            assert "ExecStart=/usr/bin/app <arguments redacted>" in dropin, dropin

            # The write path's snapshot (the rollback's read) is a privileged
            # agent's alone: this unprivileged real agent advertises it and
            # still refuses it, so no read-scope reach yields the raw bytes.
            assert "snapshot_file" in agent.system_info.get("capabilities", [])
            from homepilot.agent_hub.server import AgentCommandError

            with pytest.raises(AgentCommandError, match="requires privileged"):
                await state.agent_hub.send_action(
                    agent.agent_id, "snapshot_file", {"path": str(units[1])}
                )
            readme = answers[f"read_file_on_guest {host_root / 'conf' / 'readme.txt'}"]
            assert "plain notes" in readme
        finally:
            output = await _shutdown(state, proc)
        assert _MARK not in output, "a unit's secret reached the agent's log"
