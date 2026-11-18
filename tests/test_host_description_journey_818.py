"""THE journey for how a host is built (#818), on the shipped artifacts.

The REAL ``hp-agent`` binary built from ``agent/go`` connects to a REAL hub
started by ``create_app_state``, and describes a fixture host
(``HP_AGENT_DESCRIBE_ROOT``): an nginx reverse proxy with an upstream, a bearer
token in a header, a credential in a ``proxy_pass`` URL, a private key beside
its certificate, an env file holding a token, and fake ``systemctl`` /
``timedatectl`` that put a token in a unit's argv and environment. Then:

1. ``POST /inventory/{id}/description`` collects it through the hub and STORES
   it; ``GET`` answers it from the database;
2. the MCP ``describe_host`` tool answers the same description at the
   ``read_only`` tier;
3. ``hp hosts describe NAME --json`` - the installed console script, over HTTP -
   prints it;
4. the export writes the host's as-built KB note; changing the host (a new
   vhost), re-describing and exporting again SUPERSEDES that note;

and at every one of those places none of the fixture's secrets appears.

Skipped when no Go toolchain is present (set ``HP_GO_BIN`` to point at one).
"""

from __future__ import annotations

import asyncio
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

from .cli_live_server import run_hp
from .go_toolchain import GO as _GO
from .go_toolchain import GO_SKIP_REASON

_needs_go = pytest.mark.skipif(not _GO, reason=GO_SKIP_REASON)

pytestmark = pytest.mark.timeout(600)


def _secret(name: str) -> str:
    # Assembled, so no credential-shaped literal sits in the tree.
    return "hpJOURNEY" + "818" + name + "zq"


SECRETS = ["envfile", "keyfile", "bearer", "proxyuser", "unitargv", "unitenv", "cookie"]


def _write(root: Path, rel: str, text: str, mode: int = 0o644) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def build_fixture_host(root: Path) -> None:
    _write(root, "etc/hostname", "proxy-fixture\n")
    _write(root, "etc/os-release", 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\n')
    _write(root, "opt/app/.env", f"API_TOKEN={_secret('envfile')}\n")
    _write(
        root,
        "etc/ssl/private/site.key",
        "-----BEGIN " + f"PRIVATE KEY-----\n{_secret('keyfile')}\n-----END " + "PRIVATE KEY-----\n",
    )
    _write(
        root,
        "etc/nginx/nginx.conf",
        "events {}\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n"
        "    include /etc/nginx/sites-enabled/*;\n}\n",
    )
    _write(root, "etc/nginx/conf.d/upstream.conf", "upstream app { server 10.0.0.10:8080; }\n")
    _write(
        root,
        "etc/nginx/sites-enabled/estate",
        "server {\n"
        "    listen 443 ssl;\n"
        "    server_name estate.example.com;\n"
        "    ssl_certificate_key /etc/ssl/private/site.key;\n"
        "    ssl_verify_client on;\n"
        "    ssl_client_certificate /etc/nginx/client-ca.pem;\n"
        f'    proxy_set_header Authorization "Bearer {_secret("bearer")}";\n'
        f'    proxy_set_header Cookie "sid={_secret("cookie")}";\n'
        "    proxy_set_header Host $host;\n"
        "    location / { proxy_pass http://app; }\n"
        f"    location /old {{ proxy_pass https://svc:{_secret('proxyuser')}@10.0.0.30:8443; }}\n"
        "}\n",
    )
    _write(root, "etc/ufw/ufw.conf", "ENABLED=yes\n")
    _write(
        root,
        "var/lib/update-notifier/updates-available",
        "4 updates can be applied immediately.\n1 of these updates is a standard security update.\n",
    )
    _write(
        root,
        "usr/bin/systemctl",
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list-units) echo 'nginx.service loaded active running nginx'\n"
        f"              echo 'app.service loaded active running app --token={_secret('unitargv')}' ;;\n"
        "  list-unit-files) echo 'nginx.service enabled enabled'; echo 'app.service enabled enabled' ;;\n"
        "  show) printf 'Id=nginx.service\\nExecStart={ path=/usr/sbin/nginx ; argv[]=/usr/sbin/nginx -g x ; }\\n"
        "FragmentPath=/lib/systemd/system/nginx.service\\n\\n"
        f"Id=app.service\\nExecStart={{ path=/opt/app/bin/app ; argv[]=/opt/app/bin/app --token={_secret('unitargv')} ; }}\\n"
        f"Environment=APP_PASSWORD={_secret('unitenv')}\\n"
        "FragmentPath=/etc/systemd/system/app.service\\n\\n' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        0o755,
    )
    _write(
        root,
        "usr/bin/timedatectl",
        "#!/bin/sh\necho NTP=yes\necho NTPSynchronized=yes\necho Timezone=UTC\n",
        0o755,
    )


@pytest.fixture
def agent_binary(hp_agent_binary: str) -> str:
    return hp_agent_binary


@pytest.fixture
def hp_dir() -> Iterator[str]:
    # Under $HOME: create_app_state refuses an artifacts dir in /tmp.
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-describe-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


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
        "HP_AGENT_DESCRIBE_ROOT": str(root),
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


def _app(state: Any) -> Any:
    from fastapi import FastAPI

    from homepilot.adapters.agent import AgentAdapter
    from homepilot.auth.deps import require_token
    from homepilot.inventory.description import HostDescriptionService
    from homepilot.inventory.router import router as inventory_router
    from homepilot.inventory.service import InventoryService

    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = state.repo
    app.state.agent_hub = state.agent_hub
    app.state.agent_registry = state.agent_hub.registry
    app.state.inventory_service = InventoryService(state.repo)
    app.state.kb_service = state.kb_service
    app.state.host_descriptions = HostDescriptionService(
        state.repo, AgentAdapter(hub_server=state.agent_hub)
    )
    app.dependency_overrides[require_token] = lambda: {
        "user_id": "1",
        "token_id": "1",
        "scope": "*",
        "role": "admin",
        "display_name": "admin",
    }
    return app


def _assert_no_secret(where: str, text: str) -> None:
    for name in SECRETS:
        assert _secret(name) not in text, f"the {name} secret reached {where}"
    for marker in ("PRIVATE KEY", "Bearer ", "APP_PASSWORD", "argv[]"):
        assert marker not in text, f"{marker!r} reached {where}"


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


@_needs_go
class TestHostDescriptionJourney:
    async def test_describe_store_show_everywhere_export_and_supersede(
        self, agent_binary: str, hp_dir: str, tmp_path: Path
    ):
        import uvicorn
        from httpx import ASGITransport, AsyncClient

        from homepilot.mcp.server import _handle_tool, _mcp_token_scope_var

        root = tmp_path / "host"
        build_fixture_host(root)
        state = await _hub_on(hp_dir)
        proc = None
        server = None
        serve_task = None
        try:
            proc = _spawn_agent(agent_binary, tmp_path / "agent", state, root)
            agent = await _wait_connected(state)
            assert "describe_host" in agent.system_info.get("capabilities", [])
            # The agent registers under the machine's own name, and registration
            # links (or creates) its inventory row: that row is the host.
            hostname = agent.hostname
            linked = await state.repo.get_host_by_agent_id(agent.agent_id)
            assert linked is not None, "registration did not link the agent to a host row"
            host_id = linked["id"]
            app = _app(state)

            # 1. REST: collect through the hub, store, read back.
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.post(f"/inventory/{host_id}/description")
                assert resp.status_code == 200, resp.text
                collected = resp.json()
                got = await c.get(f"/inventory/{host_id}/description")
            assert got.status_code == 200
            view = got.json()
            assert view["description"] == collected["description"]
            doc = view["description"]
            assert doc["schema"] == "hp.host-description/1"
            assert doc["host"]["hostname"] == "proxy-fixture"
            server_block = doc["nginx"]["servers"][0]
            assert server_block["server_names"] == ["estate.example.com"]
            assert server_block["tls"]["certificate_key"] == "present"
            assert server_block["tls"]["verify_client"] == "on"
            locations = {loc["match"]: loc for loc in server_block["locations"]}
            assert locations["/"]["proxy_pass"] == "http://app"
            assert locations["/old"]["proxy_pass"] == "https://[redacted]@10.0.0.30:8443"
            assert doc["nginx"]["upstreams"][0]["servers"] == ["10.0.0.10:8080"]
            units = {u["name"]: u for u in doc["units"]["units"]}
            assert units["app.service"]["programs"] == ["/opt/app/bin/app"]
            assert units["app.service"]["custom"] is True
            assert doc["time"]["synchronized"] is True
            assert doc["updates"]["pending"] == 4 and doc["updates"]["security"] == 1
            assert doc["firewall"]["ufw"]["enabled"] is True
            assert doc["docker"]["status"] == "not_installed"
            assert doc["redactions"] >= 1

            stored = await state.repo.get_host_description(host_id)
            assert json.loads(stored["description_json"]) == doc
            _assert_no_secret("the REST answer", got.text)
            _assert_no_secret("the database row", json.dumps(stored))

            # 2. MCP, at the read_only tier.
            ctx = {
                "repo": state.repo,
                "kb_service": state.kb_service,
                "host_descriptions": app.state.host_descriptions,
            }
            tok = _mcp_token_scope_var.set("read_only")
            try:
                mcp = await _handle_tool("describe_host", {"host": hostname}, ctx)
                with pytest.raises(ValueError, match="needs the full tier"):
                    await _handle_tool("export_host_asbuilt", {"host": hostname}, ctx)
            finally:
                _mcp_token_scope_var.reset(tok)
            assert isinstance(mcp, dict) and mcp["description"] == doc
            _assert_no_secret("the MCP answer", json.dumps(mcp))

            # 3. The installed `hp`, over HTTP, as an operator runs it.
            port = _free_port()
            server = uvicorn.Server(
                uvicorn.Config(
                    app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
                )
            )
            serve_task = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.05)
            env = {"HP_URL": f"http://127.0.0.1:{port}", "HP_TOKEN": "hp_journey"}
            (tmp_path / "ws").mkdir()
            cli = await asyncio.to_thread(
                run_hp, "hosts", "describe", hostname, "--json", home=tmp_path / "ws", **env
            )
            assert cli.returncode == 0, cli.stderr
            assert json.loads(cli.stdout)["description"] == doc
            _assert_no_secret("hp hosts describe --json", cli.stdout + cli.stderr)
            text = await asyncio.to_thread(
                run_hp, "hosts", "describe", hostname, home=tmp_path / "ws", **env
            )
            assert text.returncode == 0, text.stderr
            assert f"# As-built: {hostname}" in text.stdout
            assert "estate.example.com" in text.stdout
            _assert_no_secret("hp hosts describe", text.stdout + text.stderr)

            # 4. Export (full tier), then change the host and export again.
            tok = _mcp_token_scope_var.set("full")
            try:
                first = await _handle_tool("export_host_asbuilt", {"host": hostname}, ctx)
            finally:
                _mcp_token_scope_var.reset(tok)
            assert isinstance(first, dict) and first["indexed"] is True
            _, body = state.artifact_store.read(first["note_id"])
            assert f"# As-built: {hostname}" in body and "estate.example.com" in body
            _assert_no_secret("the as-built note", body)

            _write(
                root,
                "etc/nginx/sites-enabled/status",
                "server { listen 8443; server_name status.example.com; root /srv/status; }\n",
            )
            await app.state.host_descriptions.collect(
                await app.state.host_descriptions.host(host_id=host_id), force=True
            )
            cli = await asyncio.to_thread(
                run_hp, "hosts", "describe", hostname, "--export", "--json",
                home=tmp_path / "ws", **env,
            )  # fmt: skip
            assert cli.returncode == 0, cli.stderr
            out = json.loads(cli.stdout)
            second = out["export"]
            assert second["superseded"] == first["note_id"]
            assert any(
                "status.example.com" in item
                for change in out["changes"]
                for item in change["added"]
            ), out["changes"]
            old_fm, _ = state.artifact_store.read(first["note_id"])
            assert old_fm["status"] == "superseded"
            assert await state.repo.get_doc_by_source(f"artifact:{first['note_id']}") is None
            new_doc = await state.repo.get_doc_by_source(f"artifact:{second['note_id']}")
            assert new_doc is not None and "status.example.com" in new_doc["content"]
            _assert_no_secret("the second as-built note", new_doc["content"])
        finally:
            if server is not None:
                server.should_exit = True
            if serve_task is not None:
                await serve_task
            output = await _shutdown(state, proc)
            _assert_no_secret("the agent's own log", output)
