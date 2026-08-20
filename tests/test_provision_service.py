"""ProvisionService: the clone→configure→start→record run and its failure paths.

The gate that matters here is the terminal-state one (#386): every way the run
can end must leave the task record in a terminal state. A provision stranded in
'running' would sit in the operator's in-flight list forever and keep blocking
its own name.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.models import ProvisionRequest
from homepilot.provision.service import ProvisionConflictError, ProvisionService
from homepilot.tasks.repository import TaskRepository

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"
AGENT_OK = {
    "data": {
        "result": [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
            {"name": "eth0", "ip-addresses": [{"ip-address": "10.0.0.42"}]},
        ]
    }
}


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def proxmox() -> AsyncMock:
    px = AsyncMock(spec=ProxmoxClient)
    px.next_vmid = AsyncMock(return_value=105)
    px.clone_vm = AsyncMock(return_value="UPID:pve1:clone:")
    px.wait_for_task = AsyncMock(return_value={"status": "stopped", "exitstatus": "OK"})
    px.set_vm_config = AsyncMock(return_value={"data": None})
    px.resize_disk = AsyncMock(return_value={"data": None})
    px.start_vm = AsyncMock(return_value="UPID:pve1:start:")
    px.get_vm_agent_network = AsyncMock(return_value=AGENT_OK)
    return px


@pytest_asyncio.fixture
async def service(db: Database, proxmox: AsyncMock) -> ProvisionService:
    return ProvisionService(
        proxmox=proxmox,
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=2.0,
        ip_wait_s=0.0,
        ip_interval=0.01,
    )


def _request(**overrides) -> ProvisionRequest:
    payload = {
        "name": "web-01",
        "node": "pve1",
        "template_vmid": 9000,
        "cores": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "ssh_authorized_key": PUBKEY,
        "owner": "olli",
    }
    payload.update(overrides)
    return ProvisionRequest(**payload)


async def _run_to_completion(service: ProvisionService, request: ProvisionRequest) -> dict:
    task_id = await service.start(request, actor="tester")
    for task in list(service._running_tasks):
        await task
    result = await service.task_repo.get_task(task_id)
    assert result is not None
    return result


class TestHappyPath:
    async def test_drives_the_whole_flow_and_records_the_host(
        self, service: ProvisionService, proxmox: AsyncMock, db: Database
    ):
        task = await _run_to_completion(service, _request())

        assert task["status"] == "succeeded", task["error"]
        assert task["artifact_id"] is None
        assert task["action"] == "provision"
        result = json.loads(task["result_json"])
        # Equality, not a subset: the result is a contract other surfaces render
        # (the invite portal's status page reads exactly these keys), so a new
        # key must be a deliberate change, never a silent addition.
        assert result == {
            "vmid": 105,
            "name": "web-01",
            "node": "pve1",
            "ip": "10.0.0.42",
            "host_id": result["host_id"],
            "ciuser": "friend",
            # No tailscale key was requested, so no tailnet join was attempted.
            "tailnet": None,
        }

        proxmox.clone_vm.assert_awaited_once()
        assert proxmox.clone_vm.await_args.kwargs["template_vmid"] == 9000
        assert proxmox.clone_vm.await_args.kwargs["new_vmid"] == 105
        config = proxmox.set_vm_config.await_args.args[2]
        assert config["ciuser"] == "friend"
        assert config["sshkeys"] == PUBKEY
        assert config["ipconfig0"] == "ip=dhcp"
        assert config["cores"] == 2
        assert config["memory"] == 2048
        proxmox.resize_disk.assert_awaited_once_with("pve1", 105, "scsi0", "20G")
        proxmox.start_vm.assert_awaited_once_with("pve1", 105)

        host = await Repository(db).get_host_by_proxmox_id(105)
        assert host is not None
        assert host["hostname"] == "web-01"
        assert host["owner"] == "olli"
        assert host["ip_address"] == "10.0.0.42"
        # host_type + proxmox_id must match what inventory refresh writes, or the
        # reconciler creates a duplicate row for the same VM.
        assert host["host_type"] == "qemu"
        assert host["node"] == "pve1"
        assert host["source"] == "hp_created"

    async def test_skips_resize_when_no_disk_requested(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        task = await _run_to_completion(service, _request(disk_gb=None))
        assert task["status"] == "succeeded"
        proxmox.resize_disk.assert_not_awaited()

    async def test_absent_guest_agent_still_succeeds_with_null_ip(
        self, service: ProvisionService, proxmox: AsyncMock, db: Database
    ):
        proxmox.get_vm_agent_network = AsyncMock(return_value=None)

        task = await _run_to_completion(service, _request())

        assert task["status"] == "succeeded", task["error"]
        assert json.loads(task["result_json"])["ip"] is None
        host = await Repository(db).get_host_by_proxmox_id(105)
        assert host is not None
        assert host["ip_address"] is None

    async def test_writes_an_audit_row(self, service: ProvisionService, db: Database):
        await _run_to_completion(service, _request())
        rows = await Repository(db).query_audit_log(action="provision")
        assert len(rows) == 1
        assert rows[0]["target_host"] == "web-01"
        assert rows[0]["user_id"] == "tester"


class TestTailnetJoin:
    """A requester's own tailscale key joins their guest, and never fails the build."""

    async def test_the_key_is_used_once_and_the_join_is_reported(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        key = "tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk"
        proxmox.agent_exec = AsyncMock(return_value={"data": {"pid": 1}})
        proxmox.agent_write_file = AsyncMock(return_value={"data": {}})

        task = await _run_to_completion(service, _request(tailscale_auth_key=key))

        assert task["status"] == "succeeded", task["error"]
        assert json.loads(task["result_json"])["tailnet"] == "joined"

        # THE POINT: the key reaches the guest as a file, never as an argument.
        # An argv is readable in the guest's process list and PVE echoes it back
        # inside task errors, which is why tailscale's own guidance is to pass
        # the key through the environment.
        proxmox.agent_write_file.assert_awaited_once()
        written_path, written_content = proxmox.agent_write_file.await_args.args[2:4]
        assert written_content == key
        command = proxmox.agent_exec.await_args.args[2]
        assert key not in " ".join(command), "the auth key must never appear in an argv"
        script = command[-1]
        assert "tailscale up" in script
        assert written_path in script and f"rm -f {written_path}" in script, (
            "the staged key file must be deleted by the same shell that reads it"
        )
        # The key belongs to the requester and is not ours to keep: it must not
        # reach the task record.
        assert key not in str(task)

    async def test_a_join_that_fails_does_not_fail_the_provision(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.agent_exec = AsyncMock(
            side_effect=ProxmoxError("POST", "/agent/exec", 500, "no guest agent")
        )

        task = await _run_to_completion(
            service, _request(tailscale_auth_key="tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk")
        )

        assert task["status"] == "succeeded", task["error"]
        assert json.loads(task["result_json"])["tailnet"] == "failed"

    async def test_no_key_means_no_guest_command_at_all(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.agent_exec = AsyncMock(return_value={"data": {"pid": 1}})

        task = await _run_to_completion(service, _request())

        assert json.loads(task["result_json"])["tailnet"] is None
        proxmox.agent_exec.assert_not_awaited()


class TestEveryFailingStepIsTerminal:
    @pytest.mark.parametrize(
        ("method", "step"),
        [
            ("next_vmid", "next_vmid"),
            ("clone_vm", "clone"),
            ("set_vm_config", "configure"),
            ("resize_disk", "resize_disk"),
            ("start_vm", "start_vm"),
        ],
    )
    async def test_failed_step_marks_task_failed_with_step_name(
        self, service: ProvisionService, proxmox: AsyncMock, method: str, step: str
    ):
        getattr(proxmox, method).side_effect = ProxmoxError("POST", "/x", 500, "pve exploded")

        task = await _run_to_completion(service, _request())

        # NOT stranded in running/pending — a terminal state, every time.
        assert task["status"] == "failed"
        assert task["finished_at"] is not None
        assert task["error"].startswith(f"{step}:")
        assert "pve exploded" in task["error"]

    async def test_clone_task_that_fails_in_pve_fails_the_provision(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.wait_for_task.side_effect = ProxmoxError(
            "GET", "/tasks", 0, "PVE task finished with exitstatus 'no space left'"
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert task["error"].startswith("clone:")
        assert "no space left" in task["error"]

    async def test_host_row_failure_still_terminates_the_task(
        self, service: ProvisionService, monkeypatch
    ):
        async def boom(*args, **kwargs):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(service.repo, "create_host", boom)

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert task["error"].startswith("record_host:")

    async def test_no_proxmox_fails_the_task(self, service: ProvisionService):
        service.proxmox = None

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert "Proxmox not configured" in task["error"]

    async def test_failed_run_releases_the_name(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.clone_vm.side_effect = ProxmoxError("POST", "/x", 500, "nope")
        await _run_to_completion(service, _request())
        assert not service.is_inflight("web-01")


class TestInflightNames:
    async def test_second_provision_for_same_name_conflicts(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        # Hold the run open at the first PVE call so the name stays in flight.
        import asyncio

        gate: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        proxmox.next_vmid = AsyncMock(side_effect=lambda node: gate)

        await service.start(_request())
        with pytest.raises(ProvisionConflictError):
            await service.start(_request())

        gate.set_result(105)
        for task in list(service._running_tasks):
            await task
        # Released once finished: the same name can be provisioned again.
        assert not service.is_inflight("web-01")

    async def test_different_names_do_not_conflict(self, service: ProvisionService):
        await service.start(_request())
        await service.start(_request(name="web-02"))
        for task in list(service._running_tasks):
            await task
