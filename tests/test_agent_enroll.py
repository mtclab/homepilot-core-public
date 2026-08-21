"""Zero-touch agent rollout: install hp-agent into a guest over qemu-guest-agent.

ADR-004 S4, epic #458. Three gates carry this slice:

* THE JOURNEY - the task succeeds only when the agent is ACTUALLY ENROLLED, i.e.
  present in the hub's registry. An installer that exits 0 and never connects is
  a FAILED install, because "the exec returned 0" is not the outcome the operator
  asked for. Teeth: make the run trust the exit code and
  ``test_exit_zero_without_a_connected_agent_fails`` goes green on a broken product.
* SECRETS - the enrolment token and the certificate pin reach the guest as a
  tmpfs file that the same shell deletes, never as an argv (mirrors the tailscale
  gate in test_provision_service.py: an argv is readable by every process in the
  guest and PVE echoes commands back inside task errors).
* TERMINAL STATE (#386) - every failing step lands the task in 'failed' naming
  the step, with nothing left stranded in 'running'.

The Proxmox boundary is mocked; the service, the task repository and the agent
registry are all real.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.agent_hub.enroll import (
    _AGENT_ID_MARKER,
    _ENV_PATH,
    AgentEnrollService,
    EnrollConflictError,
    EnrollPreconditionError,
)
from homepilot.agent_hub.registry import AgentRegistry
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.tasks.repository import TaskRepository

HUB_HOST = "hub.lan"
HUB_PORT = 8443
FINGERPRINT = "ab" * 32
GUEST_AGENT_ID = "hp-agent-Zm9vYmFyYmF6cXV1eA"


class _FakeTokenStore:
    """The hub's bootstrap-token store: every call mints a fresh single-use token."""

    def __init__(self) -> None:
        self.created: list[str] = []

    async def create(self) -> str:
        token = f"hpbat_enrol-token-{len(self.created)}"
        self.created.append(token)
        return token


class _FakeHub:
    host = "10.5.5.5"
    port = HUB_PORT
    tls_enabled = True
    cert_fingerprint = FINGERPRINT

    def __init__(self) -> None:
        self._token_store = _FakeTokenStore()


class _FakeGuest:
    """A guest that answers the way a real one does.

    The installer runs, systemd starts hp-agent, and hp-agent dials the hub - so
    the fake registers itself in the REAL registry, which is what makes the
    journey gate an outcome assertion rather than a mock assertion.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        hostname: str,
        *,
        connects: bool = True,
        exit_code: int = 0,
        emit_agent_id: bool = True,
        stdout_extra: str = "",
    ) -> None:
        self.registry = registry
        self.hostname = hostname
        self.connects = connects
        self.exit_code = exit_code
        self.emit_agent_id = emit_agent_id
        self.stdout_extra = stdout_extra
        self.commands: list[list[str]] = []
        self.written: list[tuple[str, str]] = []

    async def write_file(self, node: str, vmid: int, path: str, content: str) -> dict[str, Any]:
        self.written.append((path, content))
        return {"data": {}}

    async def exec(
        self, node: str, vmid: int, command: list[str], capture_output: bool = False
    ) -> dict[str, Any]:
        self.commands.append(command)
        return {"data": {"pid": 4242}}

    async def exec_status(self, node: str, vmid: int, pid: int) -> dict[str, Any]:
        if self.connects:
            self.registry.register(GUEST_AGENT_ID, self.hostname)
        out = "=== Installing hp-agent (linux-amd64) ===\n" + self.stdout_extra
        if self.emit_agent_id:
            out += f"{_AGENT_ID_MARKER}{GUEST_AGENT_ID}\n"
        return {
            "data": {
                "exited": 1,
                "exitcode": self.exit_code,
                "out-data": out,
                "err-data": "",
            }
        }


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "enroll.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.hub_server = _FakeHub()  # type: ignore[assignment]
    return reg


@pytest.fixture
def proxmox() -> AsyncMock:
    px = AsyncMock(spec=ProxmoxClient)
    px.get_vm_current = AsyncMock(return_value={"data": {"status": "running"}})
    px.agent_ping = AsyncMock(return_value=True)
    return px


@pytest.fixture
def guest(registry: AgentRegistry, proxmox: AsyncMock) -> _FakeGuest:
    g = _FakeGuest(registry, "web-01")
    proxmox.agent_write_file = AsyncMock(side_effect=g.write_file)
    proxmox.agent_exec = AsyncMock(side_effect=g.exec)
    proxmox.agent_exec_status = AsyncMock(side_effect=g.exec_status)
    return g


@pytest_asyncio.fixture
async def service(db: Database, proxmox: AsyncMock, registry: AgentRegistry) -> AgentEnrollService:
    return AgentEnrollService(
        proxmox=proxmox,
        task_repo=TaskRepository(db),
        repo=Repository(db),
        registry=registry,
        install_timeout_s=2.0,
        poll_interval=0.01,
        connect_wait_s=0.2,
        connect_interval=0.01,
    )


@pytest_asyncio.fixture
async def host(db: Database) -> dict[str, Any]:
    repo = Repository(db)
    host_id = await repo.create_host(
        hostname="web-01",
        host_type="qemu",
        proxmox_id=105,
        node="pve1",
        managed=True,
    )
    row = await repo.get_host(host_id)
    assert row is not None
    return dict(row)


async def _run_to_completion(service: AgentEnrollService, host: dict[str, Any]) -> dict[str, Any]:
    task_id = await service.start(host, HUB_HOST, HUB_PORT, actor="tester")
    for task in list(service._running_tasks):
        await task
    result = await service.task_repo.get_task(task_id)
    assert result is not None
    return result


async def _running_tasks(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT id, action, status FROM tasks WHERE status = 'running'")
    return [dict(r) for r in rows]


class TestTheJourney:
    """Trigger the install; assert the agent is enrolled, not that a call returned."""

    async def test_the_agent_actually_enrolls(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        guest: _FakeGuest,
    ):
        task = await _run_to_completion(service, host)

        assert task["status"] == "succeeded", task["error"]
        assert task["action"] == "install_agent"
        assert task["artifact_id"] is None

        # THE OUTCOME: the host now has a live agent in the hub's registry. This
        # assertion is on the registry, not on the mock's call log, so an install
        # that merely "returned ok" cannot satisfy it.
        assert registry.is_connected("web-01")
        agent = registry.get(GUEST_AGENT_ID)
        assert agent is not None
        assert agent.hostname == "web-01"

        result = json.loads(task["result_json"])
        assert result == {
            "host_id": host["id"],
            "hostname": "web-01",
            "node": "pve1",
            "vmid": 105,
            "agent_id": GUEST_AGENT_ID,
            "connected": True,
            "verified_by": "agent_id",
        }

    async def test_exit_zero_without_a_connected_agent_fails(
        self,
        db: Database,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
    ):
        # The installer runs and exits 0, but hp-agent never reaches the hub (a
        # wrong hub address, a blocked port, a unit that crash-loops). Trusting
        # the exit code here is exactly the bug this slice must not ship.
        guest = _FakeGuest(registry, "web-01", connects=False)
        proxmox.agent_write_file = AsyncMock(side_effect=guest.write_file)
        proxmox.agent_exec = AsyncMock(side_effect=guest.exec)
        proxmox.agent_exec_status = AsyncMock(side_effect=guest.exec_status)

        task = await _run_to_completion(service, host)

        assert task["status"] == "failed"
        assert task["error"].startswith("verify:")
        assert "no agent connected" in task["error"]
        assert not registry.is_connected("web-01")
        assert await _running_tasks(db) == []

    async def test_falls_back_to_the_hostname_when_the_guest_reports_no_id(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
    ):
        # An older guest agent that hands back no captured output still enrols;
        # the already-enrolled precondition makes the hostname unambiguous.
        guest = _FakeGuest(registry, "web-01", emit_agent_id=False)
        proxmox.agent_write_file = AsyncMock(side_effect=guest.write_file)
        proxmox.agent_exec = AsyncMock(side_effect=guest.exec)
        proxmox.agent_exec_status = AsyncMock(side_effect=guest.exec_status)

        task = await _run_to_completion(service, host)

        assert task["status"] == "succeeded", task["error"]
        result = json.loads(task["result_json"])
        assert result["verified_by"] == "hostname"
        assert result["agent_id"] == GUEST_AGENT_ID
        assert registry.is_connected("web-01")

    async def test_the_install_is_audited(
        self, service: AgentEnrollService, host: dict[str, Any], db: Database, guest: _FakeGuest
    ):
        await _run_to_completion(service, host)
        rows = await Repository(db).query_audit_log(action="install_agent")
        assert len(rows) == 1
        assert rows[0]["target_host"] == "web-01"
        assert rows[0]["user_id"] == "tester"


class TestSecretsNeverReachAnArgv:
    async def test_token_and_pin_are_staged_as_a_file_and_deleted_by_the_shell(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        guest: _FakeGuest,
    ):
        task = await _run_to_completion(service, host)
        assert task["status"] == "succeeded", task["error"]

        hub = registry.hub_server
        token = hub._token_store.created[-1]  # type: ignore[union-attr]

        # The credentials reached the guest as a tmpfs file...
        assert len(guest.written) == 1
        path, content = guest.written[0]
        assert path == _ENV_PATH
        assert path.startswith("/run/")
        assert f"TOKEN={token}" in content
        assert f"TLS_PIN=sha256:{FINGERPRINT}" in content
        assert f"HUB={HUB_HOST}:{HUB_PORT}" in content

        # ...and never as an argument. An argv is readable by every process in
        # the guest and PVE echoes the command back inside task errors.
        for command in guest.commands:
            joined = " ".join(command)
            assert token not in joined, "the enrolment token must never appear in an argv"
            assert FINGERPRINT not in joined, "the TLS pin must never appear in an argv"

        # The same shell that reads the file deletes it, before the installer is
        # even fetched.
        script = guest.commands[0][-1]
        assert f". {_ENV_PATH}" in script
        assert f"rm -f {_ENV_PATH}" in script
        assert script.index(f"rm -f {_ENV_PATH}") < script.index("curl")

        # And neither value is kept in the record of the run.
        assert token not in str(task)
        assert FINGERPRINT not in str(task)

    async def test_a_failed_install_keeps_the_token_out_of_the_error(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
    ):
        # A guest whose installer echoes its environment back on failure: the
        # captured output is the one place the token could leak into a task
        # record an operator reads.
        hub = registry.hub_server
        guest = _FakeGuest(registry, "web-01", exit_code=1)

        async def _leaky_status(node: str, vmid: int, pid: int) -> dict[str, Any]:
            token = hub._token_store.created[-1]  # type: ignore[union-attr]
            return {
                "data": {
                    "exited": 1,
                    "exitcode": 1,
                    "out-data": "",
                    "err-data": f"curl: (7) failed with TOKEN={token}",
                }
            }

        proxmox.agent_write_file = AsyncMock(side_effect=guest.write_file)
        proxmox.agent_exec = AsyncMock(side_effect=guest.exec)
        proxmox.agent_exec_status = AsyncMock(side_effect=_leaky_status)

        task = await _run_to_completion(service, host)

        assert task["status"] == "failed"
        token = hub._token_store.created[-1]  # type: ignore[union-attr]
        assert token not in str(task)
        assert "<redacted>" in task["error"]


class TestPreconditionsRefuseWithTheirOwnReason:
    async def test_guest_not_running(
        self, service: AgentEnrollService, host: dict[str, Any], proxmox: AsyncMock, db: Database
    ):
        proxmox.get_vm_current = AsyncMock(return_value={"data": {"status": "stopped"}})
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start(host, HUB_HOST, HUB_PORT)
        assert exc.value.code == "not_running"
        assert "stopped" in exc.value.message
        assert "Start the guest" in exc.value.message
        # A refusal is an answer, not a failed task: nothing was recorded.
        rows = await db.fetchall("SELECT id FROM tasks")
        assert list(rows) == []

    async def test_no_guest_agent(
        self, service: AgentEnrollService, host: dict[str, Any], proxmox: AsyncMock
    ):
        proxmox.agent_ping = AsyncMock(return_value=False)
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start(host, HUB_HOST, HUB_PORT)
        assert exc.value.code == "no_guest_agent"
        assert "qemu-guest-agent" in exc.value.message
        assert "install-agent.sh" in exc.value.message

    async def test_already_enrolled(
        self, service: AgentEnrollService, host: dict[str, Any], registry: AgentRegistry
    ):
        registry.register("hp-agent-already-here", "web-01")
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start(host, HUB_HOST, HUB_PORT)
        assert exc.value.code == "already_enrolled"
        assert "hp-agent-already-here" in exc.value.message

    async def test_not_a_qemu_guest(self, service: AgentEnrollService, host: dict[str, Any]):
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start({**host, "host_type": "lxc"}, HUB_HOST, HUB_PORT)
        assert exc.value.code == "not_a_qemu_guest"
        assert "install-agent.sh" in exc.value.message

    async def test_guest_without_a_recorded_vmid(
        self, service: AgentEnrollService, host: dict[str, Any]
    ):
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start({**host, "proxmox_id": None}, HUB_HOST, HUB_PORT)
        assert exc.value.code == "not_a_qemu_guest"

    async def test_proxmox_not_configured(self, service: AgentEnrollService, host: dict[str, Any]):
        service.proxmox = None
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start(host, HUB_HOST, HUB_PORT)
        assert exc.value.code == "proxmox_unavailable"
        assert "Settings" in exc.value.message

    async def test_hub_not_running(self, service: AgentEnrollService, host: dict[str, Any]):
        service.registry = None
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start(host, HUB_HOST, HUB_PORT)
        assert exc.value.code == "hub_unavailable"

    async def test_unresolvable_hub_address(
        self, service: AgentEnrollService, host: dict[str, Any]
    ):
        # The hub host the agent would be told to dial comes from a validated
        # resolution chain; when it lands on the non-dialable placeholder there
        # is no point installing anything.
        from homepilot.agent_hub.router import _INVALID_HUB_HOST_PLACEHOLDER

        with pytest.raises(EnrollPreconditionError) as exc:
            await service.start(host, _INVALID_HUB_HOST_PLACEHOLDER, HUB_PORT)
        assert exc.value.code == "hub_address_unresolved"

    async def test_a_second_install_for_the_same_host_is_refused(
        self, service: AgentEnrollService, host: dict[str, Any], guest: _FakeGuest
    ):
        await service.start(host, HUB_HOST, HUB_PORT)
        with pytest.raises(EnrollConflictError):
            await service.start(host, HUB_HOST, HUB_PORT)
        for task in list(service._running_tasks):
            await task
        assert not service.is_inflight(host["id"])


class TestEveryFailingStepIsTerminal:
    async def test_staging_failure_names_its_step(
        self, db: Database, service: AgentEnrollService, host: dict[str, Any], proxmox: AsyncMock
    ):
        proxmox.agent_write_file = AsyncMock(
            side_effect=ProxmoxError("POST", "/agent/file-write", 500, "no space left")
        )
        task = await _run_to_completion(service, host)
        assert task["status"] == "failed"
        assert task["error"].startswith("stage_credentials:")
        assert await _running_tasks(db) == []

    async def test_exec_failure_names_its_step(
        self, db: Database, service: AgentEnrollService, host: dict[str, Any], proxmox: AsyncMock
    ):
        proxmox.agent_write_file = AsyncMock(return_value={"data": {}})
        proxmox.agent_exec = AsyncMock(
            side_effect=ProxmoxError("POST", "/agent/exec", 500, "guest agent not running")
        )
        task = await _run_to_completion(service, host)
        assert task["status"] == "failed"
        assert task["error"].startswith("install:")
        assert await _running_tasks(db) == []

    async def test_a_nonzero_installer_exit_fails_with_the_guest_output(
        self,
        db: Database,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
    ):
        guest = _FakeGuest(registry, "web-01", exit_code=3, connects=False)
        proxmox.agent_write_file = AsyncMock(side_effect=guest.write_file)
        proxmox.agent_exec = AsyncMock(side_effect=guest.exec)
        proxmox.agent_exec_status = AsyncMock(side_effect=guest.exec_status)

        task = await _run_to_completion(service, host)

        assert task["status"] == "failed"
        assert task["error"].startswith("install:")
        assert "exited 3" in task["error"]
        assert await _running_tasks(db) == []

    async def test_an_installer_that_never_exits_times_out(
        self, db: Database, service: AgentEnrollService, host: dict[str, Any], proxmox: AsyncMock
    ):
        service.install_timeout_s = 0.05
        proxmox.agent_write_file = AsyncMock(return_value={"data": {}})
        proxmox.agent_exec = AsyncMock(return_value={"data": {"pid": 1}})
        proxmox.agent_exec_status = AsyncMock(return_value={"data": {"exited": 0}})

        task = await _run_to_completion(service, host)

        assert task["status"] == "failed"
        assert task["error"].startswith("install:")
        assert "did not finish" in task["error"]
        assert await _running_tasks(db) == []

    async def test_a_failed_run_clears_the_staged_credentials(
        self, service: AgentEnrollService, host: dict[str, Any], proxmox: AsyncMock
    ):
        proxmox.agent_write_file = AsyncMock(return_value={"data": {}})
        proxmox.agent_exec = AsyncMock(
            side_effect=[
                ProxmoxError("POST", "/agent/exec", 500, "boom"),
                {"data": {"pid": 9}},
            ]
        )
        task = await _run_to_completion(service, host)
        assert task["status"] == "failed"
        # The wrapper deletes the file itself, but a shell that never ran cannot:
        # the cleanup exec is what keeps the token from sitting in the guest.
        cleanup = proxmox.agent_exec.await_args.args[2]
        assert cleanup[:2] == ["rm", "-f"]
        assert _ENV_PATH in cleanup

    async def test_an_audit_row_records_the_failure(
        self, db: Database, service: AgentEnrollService, host: dict[str, Any], proxmox: AsyncMock
    ):
        proxmox.agent_write_file = AsyncMock(
            side_effect=ProxmoxError("POST", "/agent/file-write", 500, "denied")
        )
        await _run_to_completion(service, host)
        rows = await Repository(db).query_audit_log(action="install_agent_failed")
        assert len(rows) == 1
        assert rows[0]["target_host"] == "web-01"


class TestTheApiSurface:
    """What the UI reads: an eligibility answer that carries the reason, and a
    trigger that refuses with the same reason rather than failing deep inside."""

    @pytest_asyncio.fixture
    async def client(self, db: Database, service: AgentEnrollService, registry: AgentRegistry):
        import httpx
        from fastapi import FastAPI

        from homepilot.agent_hub.router import router as agent_router
        from homepilot.auth.deps import require_token

        app = FastAPI()
        app.include_router(agent_router)
        app.state.repo = Repository(db)
        app.state.agent_enroll_service = service
        app.state.agent_registry = registry
        app.dependency_overrides[require_token] = lambda: {
            "user_id": "1",
            "token_id": "1",
            "scope": "admin",
            "role": "admin",
            "display_name": "admin",
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hp.test") as http:
            yield http
        app.dependency_overrides.clear()

    async def test_eligibility_says_yes_for_a_qualifying_guest(self, client, host, guest):
        resp = await client.get(f"/agents/install/{host['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["eligible"] is True
        assert body["reason"] is None
        assert body["hostname"] == "web-01"

    async def test_eligibility_explains_a_host_that_does_not_qualify(
        self, client, host, proxmox: AsyncMock
    ):
        proxmox.agent_ping = AsyncMock(return_value=False)
        resp = await client.get(f"/agents/install/{host['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["eligible"] is False
        assert body["reason"] == "no_guest_agent"
        assert "qemu-guest-agent" in body["message"]

    async def test_unknown_host_is_a_404(self, client):
        resp = await client.get("/agents/install/nope")
        assert resp.status_code == 404

    async def test_install_refuses_a_host_that_does_not_qualify(
        self, client, host, proxmox: AsyncMock
    ):
        proxmox.get_vm_current = AsyncMock(return_value={"data": {"status": "stopped"}})
        resp = await client.post("/agents/install", json={"host_id": host["id"]})
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "not_running"

    async def test_install_queues_a_task_that_reaches_the_agent(
        self, client, host, guest, service: AgentEnrollService, registry: AgentRegistry
    ):
        resp = await client.post("/agents/install", json={"host_id": host["id"]})
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        assert resp.json()["status"] == "pending"

        for task in list(service._running_tasks):
            await task
        record = await service.task_repo.get_task(task_id)
        assert record is not None
        assert record["status"] == "succeeded", record["error"]
        assert registry.is_connected("web-01")


class TestShellSafety:
    async def test_a_token_that_could_break_out_of_the_env_file_is_refused(
        self, service: AgentEnrollService, host: dict[str, Any], registry: AgentRegistry
    ):
        # The env file is SOURCED, so a value carrying a newline or a command
        # substitution would be executed inside the guest. It must be refused
        # before it is written, not escaped afterwards.
        hub = registry.hub_server

        class _EvilStore:
            async def create(self) -> str:
                return "hpbat_x\nrm -rf /"

        hub._token_store = _EvilStore()  # type: ignore[union-attr]

        task = await _run_to_completion(service, host)

        assert task["status"] == "failed"
        assert task["error"].startswith("stage_credentials:")
        assert "cannot be staged" in task["error"]
