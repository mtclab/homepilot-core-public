"""ProvisionService: the clone→configure→start→record run and its failure paths.

The gate that matters here is the terminal-state one (#386): every way the run
can end must leave the task record in a terminal state. A provision stranded in
'running' would sit in the operator's in-flight list forever and keep blocking
its own name.

The second gate is the cancel one (#452), in TestCancelReachesProxmox. It
FORBIDS a cancel that does not reach the running coroutine: marking the row
'cancelled' while the provision job keeps talking to Proxmox, so the job
overwrites the row with 'succeeded'/'failed' afterwards and leaves a guest on
the node that nobody asked for. Every case there asserts BOTH what the Proxmox
client was actually asked to do and what the task row finally says.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
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
    # The guest EXISTS and is running. Modelled explicitly because the join now
    # asks before naming a cause for a silent agent: "no answer" and "no
    # machine" are different answers and must not share a sentence.
    px.get_vm_current = AsyncMock(return_value={"data": {"status": "running"}})
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
            # How the guest was addressed, in the guest's own words (#630).
            # This instance describes no guest network, so nothing was
            # allocated and ip=dhcp is what the guest was actually built with.
            "ipconfig0": "ip=dhcp",
            "host_id": result["host_id"],
            "ciuser": "friend",
            # No tailscale key was requested, so no tailnet join was attempted.
            "tailnet": None,
            # WHY, when there is a why. A bare "failed" with no reason is what
            # the first live run left the operator with (#628).
            "tailnet_detail": None,
            # The guest network fence (#553), stated even when there is none:
            # "this guest is not fenced" is a fact about the machine an operator
            # must be able to read, and an absent key would leave it ambiguous.
            "guest_network_fence": None,
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


def _wire_agent_run(px: Any) -> None:
    """Give a mocked client the REAL exec-and-wait loop.

    A copy of the one in tests/test_agent_enroll.py, deliberately - the rule it
    encodes is the same and each gate should stand on its own: STUBBING
    `agent_run` deletes the assertions that ARE that loop (a pid is not a
    result, a non-zero exit is terminal, a command that never exits times out).
    Binding the real implementation over the fake's exec/exec-status keeps them.

    This file's fake used to stub `agent_run` outright, which is one of the two
    reasons a fully green suite never saw any of #628.
    """
    from homepilot.adapters.proxmox import ProxmoxClient

    async def run(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        return await ProxmoxClient.agent_run(px, *args, **kwargs)

    px.agent_run = run


def _guest(
    proxmox: AsyncMock, *, has_tailscale: bool = True, rc: int = 0, agent: bool = True
) -> list[str]:
    """A guest at the exec/exec-status boundary, with the real wait loop above it.

    `agent_ping` is set EXPLICITLY, and that is not decoration: `AsyncMock(spec=
    ProxmoxClient)` answers any un-stubbed coroutine with a truthy mock, so a
    fake that stays silent about the ping silently says "the agent is up" - the
    other reason the suite was green through #628. A test that wants a guest
    with no agent has to say so.

    Script SEMANTICS are not tested here; tests/test_tailnet_join.py runs these
    same scripts through a real /bin/sh. This fake only decides what each script
    exits with, so the provision run's own branches can be driven.
    """
    scripts: list[str] = []
    state = {"installed": has_tailscale}
    exits: dict[int, int] = {}

    def _outcome(script: str) -> int:
        if script.startswith("command -v tailscale"):
            return 0 if state["installed"] else 1
        if "install.sh" in script:
            state["installed"] = True
            return 0
        if script.startswith("command -v cloud-init") or script.startswith("rm -f"):
            return 0
        return rc

    async def agent_exec(node, vmid, command):
        script = command[-1]
        scripts.append(script)
        pid = len(scripts)
        exits[pid] = _outcome(script)
        return {"data": {"pid": pid}}

    async def agent_exec_status(node, vmid, pid):
        return {"data": {"exited": 1, "exitcode": exits[pid], "out-data": "", "err-data": ""}}

    proxmox.agent_ping = AsyncMock(return_value=agent)
    proxmox.agent_exec = AsyncMock(side_effect=agent_exec)
    proxmox.agent_exec_status = AsyncMock(side_effect=agent_exec_status)
    _wire_agent_run(proxmox)
    proxmox.agent_write_file = AsyncMock(return_value={"data": {}})
    return scripts


class TestTailnetJoin:
    """A requester's own tailscale key joins their guest, and never fails the build."""

    async def test_the_key_is_used_once_and_the_join_is_reported(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        key = "tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk"
        scripts = _guest(proxmox)

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
        script = scripts[-1]
        assert key not in script, "the auth key must never appear in an argv"
        assert "tailscale up" in script
        assert written_path in script and f"rm -f {written_path}" in script, (
            "the staged key file must be deleted by the same shell that reads it"
        )
        # The key belongs to the requester and is not ours to keep: it must not
        # reach the task record.
        assert key not in str(task)

    async def test_a_join_that_cannot_run_does_not_fail_the_provision(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """The guest EXISTS by the time the join runs; the join must not undo it.

        And the outcome is `unknown`, not `failed`: a guest agent that refused
        the command told us nothing about the machine's tailnet, and #642 is the
        rule that a read which did not happen is not a verdict. It matters to
        the person holding the key - `failed` means "get a fresh one", `unknown`
        means "a fresh one will not help".
        """
        proxmox.agent_ping = AsyncMock(return_value=True)
        proxmox.agent_exec = AsyncMock(
            side_effect=ProxmoxError("POST", "/agent/exec", 500, "no guest agent")
        )
        _wire_agent_run(proxmox)

        task = await _run_to_completion(
            service, _request(tailscale_auth_key="tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk")
        )

        assert task["status"] == "succeeded", task["error"]
        result = json.loads(task["result_json"])
        assert result["tailnet"] == "unknown"
        assert result["tailnet_detail"], "an outcome with no reason is what #628 was"

    async def test_a_guest_whose_agent_never_answers_is_told_so(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """#628's live failure: the join fired at a guest that could not answer yet.

        WHAT THIS FORBIDS: reporting a tailnet verdict off a refused exec. The
        provision must wait for the agent, and when it never comes must say
        THAT, not "your join failed".
        """
        service.agent_wait_s = 0.05
        service.agent_interval = 0.01
        scripts = _guest(proxmox, agent=False)

        task = await _run_to_completion(
            service, _request(tailscale_auth_key="tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk")
        )

        result = json.loads(task["result_json"])
        assert result["tailnet"] == "unknown"
        assert "qemu-guest-agent" in result["tailnet_detail"]
        assert scripts == [], "commands were sent to a guest that never answered"

    async def test_a_guest_without_tailscale_gets_it_installed_first(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """#628 - the join could NEVER succeed, because nothing installed it.

        WHAT THIS FORBIDS: running `tailscale up` at a stock cloud image and
        calling the result the requester's problem. The first real guest was
        given a valid key, recorded tailnet "failed", and no retry could ever
        have worked - the binary was not there and nothing was going to put it
        there.
        """
        scripts = _guest(proxmox, has_tailscale=False)

        task = await _run_to_completion(
            service, _request(tailscale_auth_key="tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk")
        )

        assert json.loads(task["result_json"])["tailnet"] == "joined"
        assert any("install.sh" in s for s in scripts), "a guest with no tailscale got no installer"
        # Order matters: installing after the join is no use to anyone.
        assert scripts.index(next(s for s in scripts if "install.sh" in s)) < scripts.index(
            next(s for s in scripts if "tailscale up" in s)
        )

    async def test_an_image_that_already_has_tailscale_is_not_reinstalled(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        scripts = _guest(proxmox, has_tailscale=True)

        await _run_to_completion(
            service, _request(tailscale_auth_key="tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk")
        )

        assert not any("install.sh" in s for s in scripts)

    async def test_a_join_that_exits_nonzero_is_reported_failed_not_joined(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """#628 - acceptance is not success, one layer below a PVE task.

        WHAT THIS FORBIDS: reporting "joined" off the guest agent's pid. The
        exec is fire-and-forget, so a `tailscale up` that exits 1 on an expired
        or already-used key was recorded as a successful join - and the status
        page told the requester their machine was on the tailnet when it was
        not.
        """
        _guest(proxmox, rc=1)

        task = await _run_to_completion(
            service, _request(tailscale_auth_key="tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk")
        )

        assert task["status"] == "succeeded", task["error"]
        assert json.loads(task["result_json"])["tailnet"] == "failed", (
            "a tailscale up that exited non-zero was reported as joined"
        )

    async def test_install_can_be_turned_off_for_an_image_that_ships_its_own(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """Off means off: no installer, and an honest 'failed' rather than a hang."""
        service.tailscale_install = False
        scripts = _guest(proxmox, has_tailscale=False)

        task = await _run_to_completion(
            service, _request(tailscale_auth_key="tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk")
        )

        assert not any("install.sh" in s for s in scripts)
        assert json.loads(task["result_json"])["tailnet"] == "failed"

    async def test_no_key_means_no_guest_command_at_all(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        scripts = _guest(proxmox)

        task = await _run_to_completion(service, _request())

        assert json.loads(task["result_json"])["tailnet"] is None
        assert scripts == [], "a provision with no key still ran commands in the guest"


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
        assert task["error"].startswith(f"failed at {step}:")
        assert "pve exploded" in task["error"]

    async def test_clone_task_that_fails_in_pve_fails_the_provision(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.wait_for_task.side_effect = ProxmoxError(
            "GET", "/tasks", 0, "PVE task finished with exitstatus 'no space left'"
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert task["error"].startswith("failed at clone:")
        assert "no space left" in task["error"]

    async def test_host_row_failure_still_terminates_the_task(
        self, service: ProvisionService, monkeypatch
    ):
        async def boom(*args, **kwargs):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(service.repo, "create_host", boom)

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert task["error"].startswith("failed at record_host:")

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


class TestPostCloneFailureIsUnwound:
    """A provision that clones then fails must not orphan the guest (#595).

    Reproduced live: a provision failed at start_vm and left a stopped VM 101 on
    the node. clone_vm makes a guest; a failure at any later step strands it and
    nothing else removes it. The recorded error must name the cleanup outcome so
    an operator knows whether a guest is still on the node.
    """

    async def test_a_failure_after_clone_destroys_the_guest(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.start_vm.side_effect = ProxmoxError("POST", "/x", 500, "boom at boot")

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        # The guest clone_vm made is taken back, not left behind.
        proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        assert task["error"].startswith("failed at start_vm:")
        assert "boom at boot" in task["error"]
        # The error names the cleanup OUTCOME, not just the original failure.
        assert "destroyed guest vmid 105" in task["error"]

    async def test_a_running_guest_is_stopped_before_it_is_destroyed(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        # start_vm succeeded and the guest is up; the NEXT step fails.
        proxmox.get_vm_current = AsyncMock(return_value={"data": {"status": "running"}})
        proxmox.stop_vm = AsyncMock(return_value="UPID:pve1:stop:")

        # Make the failure land at record_host (after start), guest running.
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("db is gone")

        service.repo.create_host = boom  # type: ignore[method-assign]

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        proxmox.stop_vm.assert_awaited_once_with("pve1", 105)
        proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        assert "destroyed guest vmid 105" in task["error"]

    async def test_a_failure_before_clone_destroys_nothing(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        # next_vmid is the one step before the clone is issued: a failure here has
        # nothing on the node to unwind, and delete_vm must NOT be called.
        proxmox.next_vmid = AsyncMock(side_effect=ProxmoxError("GET", "/x", 500, "no id"))

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert task["error"].startswith("failed at next_vmid:")
        proxmox.delete_vm.assert_not_awaited()

    async def test_a_cleanup_that_fails_says_the_guest_may_remain(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.start_vm.side_effect = ProxmoxError("POST", "/x", 500, "boom at boot")
        proxmox.delete_vm = AsyncMock(side_effect=ProxmoxError("DELETE", "/x", 500, "busy"))

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        assert "cleanup FAILED" in task["error"]
        assert "105" in task["error"] and "pve1" in task["error"]
        assert "may remain" in task["error"]


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


class TestCancelReachesProxmox:
    """#452 - the gate: a cancelled provision must actually be cancelled.

    WHAT THIS FORBIDS: a cancel that only marks the row while the provision
    coroutine keeps talking to Proxmox. The old behaviour marked the task
    'cancelled' and then let the still-running job overwrite it with
    'succeeded' seconds later, leaving a guest nobody asked for on the node.
    So these tests assert the OUTCOME on both sides - what the Proxmox client
    was actually asked to do, and what the task row finally says - and they let
    the blocked run resume AFTER the cancel to prove it can never come back and
    clobber the record.
    """

    @staticmethod
    async def _spin(predicate, limit: int = 400) -> None:
        """Give the loop turns until `predicate()` holds - never read the
        pre-cancel state as the post-cancel one.

        A real sleep, not `sleep(0)`: the run has database awaits in it, and
        those resolve on aiosqlite's worker THREAD, which a bare loop turn does
        not wait for."""
        for _ in range(limit):
            if predicate():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("condition never became true")

    @staticmethod
    async def _settle(service: ProvisionService) -> None:
        """Wait out the run task AND the cleanup task it spawns."""
        for _ in range(10):
            pending = [t for t in service._running_tasks if not t.done()]
            if not pending:
                return
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5.0)

    @staticmethod
    def _block_wait_for_task(proxmox: AsyncMock) -> asyncio.Event:
        gate = asyncio.Event()

        async def blocking_wait(*args, **kwargs):
            await gate.wait()
            return {"status": "stopped", "exitstatus": "OK"}

        proxmox.wait_for_task = AsyncMock(side_effect=blocking_wait)
        proxmox.stop_task = AsyncMock(return_value={"data": None})
        proxmox.delete_vm = AsyncMock(return_value="UPID:pve1:destroy:")
        return gate

    async def test_cancel_mid_clone_stops_the_task_and_destroys_the_guest(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        gate = self._block_wait_for_task(proxmox)
        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)

        row = await service.cancel(task_id)
        assert row is not None and row["status"] == "cancelled"

        # Release the clone wait AFTER the cancel: if the cancel had not reached
        # the coroutine, this is where the run would march on and overwrite the
        # record. It must not.
        gate.set()
        await self._settle(service)

        proxmox.stop_task.assert_awaited_once_with("pve1", "UPID:pve1:clone:")
        proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        # The run never got past the clone wait.
        proxmox.set_vm_config.assert_not_awaited()
        proxmox.start_vm.assert_not_awaited()

        final = await service.task_repo.get_task(task_id)
        assert final["status"] == "cancelled", "a cancelled provision must stay cancelled"
        assert final["error"] is None
        assert json.loads(final["result_json"]) == {
            "cancelled": True,
            "vmid": 105,
            "stop_task": "stopped",
            "cleanup": "deleted",
        }

    async def test_cancel_stops_a_running_guest_before_destroying_it(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """#626 - the cleanup must reach a guest that already booted.

        WHAT THIS FORBIDS: a cancel that fires the destroy at a RUNNING guest.
        PVE refuses that outright ("VM 101 is running - destroy failed"), and
        the cancel then records a failed cleanup while the guest stays up and
        billing - which is exactly what happened on dev: a cancelled provision
        left vmid 101 running on the node for the rest of the session.
        """
        gate = self._block_wait_for_task(proxmox)
        proxmox.get_vm_current = AsyncMock(return_value={"data": {"status": "running"}})

        # The order is the whole point: PVE refuses a destroy on a running
        # guest, so the stop must be issued first.
        order: list[str] = []

        async def stop_vm(node, vmid):
            order.append("stop")
            return "UPID:pve1:qmstop:"

        async def delete_vm(node, vmid):
            order.append("destroy")
            return "UPID:pve1:destroy:"

        proxmox.stop_vm = AsyncMock(side_effect=stop_vm)
        proxmox.delete_vm = AsyncMock(side_effect=delete_vm)

        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        proxmox.stop_vm.assert_awaited_once_with("pve1", 105)
        proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        assert order == ["stop", "destroy"], "the destroy was issued before the guest was stopped"

        final = await service.task_repo.get_task(task_id)
        assert final["status"] == "cancelled"
        assert final["error"] is None, "cleanup must not fail on a running guest"
        assert json.loads(final["result_json"])["cleanup"] == "deleted"

    async def test_cancel_waits_for_the_destroy_task_before_calling_it_deleted(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """A destroy UPID is acceptance, not removal - "deleted" must mean gone."""
        gate = self._block_wait_for_task(proxmox)
        proxmox.get_vm_current = AsyncMock(return_value={"data": {"status": "stopped"}})

        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        waited = [c.args[1] for c in proxmox.wait_for_task.await_args_list if len(c.args) > 1]
        assert "UPID:pve1:destroy:" in waited, "the destroy task was never waited on"

    async def test_an_unfenced_guest_is_not_called_destroyed_until_it_is(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """#642/#626 - the one destroy claim that must not be made off acceptance.

        WHAT THIS FORBIDS: reporting "the half-made guest was destroyed" on the
        UPID `delete_vm` returns. That sentence is the assurance an UNFENCED
        guest is off the guest wire - the one place being wrong means a machine
        nobody has walled off is still running, while the operator has been told
        it is gone. The sibling path was fixed in #626; this one was missed.
        """
        waited: list[str] = []

        async def delete_vm(node, vmid):
            return "UPID:pve1:destroy-unfenced:"

        async def wait_for_task(node, upid, **kwargs):
            waited.append(upid)
            return {"status": "stopped", "exitstatus": "OK"}

        proxmox.delete_vm = AsyncMock(side_effect=delete_vm)
        proxmox.wait_for_task = AsyncMock(side_effect=wait_for_task)

        said = await service._destroy_unfenced(_request(), 105)

        assert "destroyed" in said
        assert "UPID:pve1:destroy-unfenced:" in waited, (
            "an unfenced guest was reported destroyed without waiting for the destroy"
        )

    async def test_a_destroy_that_fails_asynchronously_is_not_reported_as_gone(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """The honest arm: the wait must be able to CHANGE the answer."""
        proxmox.delete_vm = AsyncMock(return_value="UPID:pve1:destroy-unfenced:")
        proxmox.wait_for_task = AsyncMock(
            side_effect=ProxmoxError("GET", "/tasks", 0, "exitstatus 'destroy failed'")
        )

        said = await service._destroy_unfenced(_request(), 105)

        assert "could NOT be destroyed" in said
        assert "may still" in said

    async def test_cancel_writes_an_audit_row_and_releases_the_name(
        self, service: ProvisionService, proxmox: AsyncMock, db: Database
    ):
        gate = self._block_wait_for_task(proxmox)
        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        assert not service.is_inflight("web-01")
        rows = await Repository(db).query_audit_log(action="provision_cancelled")
        assert len(rows) == 1
        assert rows[0]["target_host"] == "web-01"
        assert rows[0]["user_id"] == "tester"
        assert json.loads(rows[0]["details_json"])["cleanup"] == "deleted"

    async def test_a_failed_audit_write_does_not_lose_the_cancel_outcome(
        self, service: ProvisionService, proxmox: AsyncMock, monkeypatch
    ):
        gate = self._block_wait_for_task(proxmox)

        async def boom(*args, **kwargs):
            raise RuntimeError("audit table is gone")

        monkeypatch.setattr(service.repo, "log_audit", boom)
        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        final = await service.task_repo.get_task(task_id)
        assert final["status"] == "cancelled"
        assert json.loads(final["result_json"])["cleanup"] == "deleted"

    async def test_cleanup_failure_names_the_guest_that_may_be_left_behind(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        gate = self._block_wait_for_task(proxmox)
        proxmox.delete_vm = AsyncMock(
            side_effect=ProxmoxError("DELETE", "/nodes/pve1/qemu/105", 500, "storage busy")
        )
        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        final = await service.task_repo.get_task(task_id)
        assert final["status"] == "cancelled"
        # The operator has to be able to go and look: vmid AND node, in words.
        assert "delete_vm" in final["error"]
        assert "105" in final["error"]
        assert "pve1" in final["error"]
        assert "may remain" in final["error"]
        assert json.loads(final["result_json"])["cleanup"] == "failed"

    async def test_a_stop_task_failure_still_destroys_the_guest(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        gate = self._block_wait_for_task(proxmox)
        proxmox.stop_task = AsyncMock(
            side_effect=ProxmoxError("DELETE", "/nodes/pve1/tasks/x", 500, "already stopped")
        )
        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        result = json.loads((await service.task_repo.get_task(task_id))["result_json"])
        assert result["stop_task"] == "failed"
        assert result["cleanup"] == "deleted"

    async def test_cancel_before_any_pve_work_creates_nothing_to_unwind(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        gate = asyncio.Event()

        async def blocking_next_vmid(node):
            await gate.wait()
            return 105

        proxmox.next_vmid = AsyncMock(side_effect=blocking_next_vmid)
        proxmox.stop_task = AsyncMock(return_value={"data": None})
        proxmox.delete_vm = AsyncMock(return_value="UPID:pve1:destroy:")

        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.next_vmid.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        proxmox.clone_vm.assert_not_awaited()
        proxmox.stop_task.assert_not_awaited()
        proxmox.delete_vm.assert_not_awaited()
        final = await service.task_repo.get_task(task_id)
        assert final["status"] == "cancelled"
        assert json.loads(final["result_json"]) == {
            "cancelled": True,
            "cleanup": "nothing_created",
        }

    async def test_cancel_after_the_clone_finished_still_destroys_the_guest(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        gate = asyncio.Event()

        async def blocking_config(*args, **kwargs):
            await gate.wait()
            return {"data": None}

        proxmox.set_vm_config = AsyncMock(side_effect=blocking_config)
        proxmox.stop_task = AsyncMock(return_value={"data": None})
        proxmox.delete_vm = AsyncMock(return_value="UPID:pve1:destroy:")

        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.set_vm_config.await_count == 1)
        await service.cancel(task_id)
        gate.set()
        await self._settle(service)

        # No PVE task was in flight at that point, but the guest exists.
        proxmox.stop_task.assert_not_awaited()
        proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        result = json.loads((await service.task_repo.get_task(task_id))["result_json"])
        assert result == {
            "cancelled": True,
            "vmid": 105,
            "stop_task": "not_needed",
            "cleanup": "deleted",
        }

    async def test_shutdown_drain_waits_for_the_cleanup(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """A cancel followed immediately by shutdown must not orphan the unwind."""
        gate = self._block_wait_for_task(proxmox)
        released = asyncio.Event()

        async def slow_delete(node, vmid):
            await asyncio.sleep(0)
            released.set()
            return "UPID:pve1:destroy:"

        proxmox.delete_vm = AsyncMock(side_effect=slow_delete)
        task_id = await service.start(_request(), actor="tester")
        await self._spin(lambda: proxmox.wait_for_task.await_count == 1)
        await service.cancel(task_id)
        gate.set()

        await service.drain(timeout=5.0)

        assert released.is_set(), "drain returned before the cleanup destroy ran"
        final = await service.task_repo.get_task(task_id)
        assert json.loads(final["result_json"])["cleanup"] == "deleted"

    async def test_cancel_of_a_restart_orphan_says_the_state_is_unknown(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        # A row left 'running' by a process that is no longer here: this service
        # never held the coroutine, so it cannot know what PVE did.
        task_id = await service.task_repo.create_task(None, "provision")
        await service.task_repo.update_task_status(task_id, "running")
        proxmox.stop_task = AsyncMock(return_value={"data": None})
        proxmox.delete_vm = AsyncMock(return_value="UPID:pve1:destroy:")

        row = await service.cancel(task_id)

        assert row["status"] == "cancelled"
        assert row["error"] == "process restarted; in-flight PVE state unknown"
        assert row["result_json"] is None
        proxmox.stop_task.assert_not_awaited()
        proxmox.delete_vm.assert_not_awaited()
        proxmox.clone_vm.assert_not_awaited()

    async def test_cancel_of_a_succeeded_provision_is_a_noop(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        task = await _run_to_completion(service, _request())
        assert task["status"] == "succeeded"
        proxmox.stop_task = AsyncMock(return_value={"data": None})
        proxmox.delete_vm = AsyncMock(return_value="UPID:pve1:destroy:")

        row = await service.cancel(task["id"])

        assert row["status"] == "succeeded"
        assert row["result_json"] == task["result_json"]
        proxmox.stop_task.assert_not_awaited()
        proxmox.delete_vm.assert_not_awaited()

    async def test_cancel_of_an_unknown_task_returns_none(self, service: ProvisionService):
        assert await service.cancel("no-such-task") is None


@pytest.mark.asyncio
class TestGuestVmidsAreNeverReused:
    """PVE's /cluster/nextid hands back the LOWEST free id (#648).

    Destroy a guest and the next one gets its number, so `hosts` ends up with
    two rows for one id - and on prod a third, from an unrelated machine
    imported months earlier. That is how a live guest came to be marked absent
    three minutes after it was built, with its owner logged into it.

    A configured range is allocated HIGHEST-first, so a guest's id is never
    reused and can never be one of the operator's own machines.

    Teeth: return `min(used)` instead of `max(used) + 1` and the first test
    fails; drop the range check and the exhaustion test fails.
    """

    @staticmethod
    def _defaults(span: str):
        from homepilot.provision.defaults import ProvisioningDefaults

        return ProvisioningDefaults(node="pve1", template_vmid=9001, vmid_range=span)

    async def test_a_range_allocates_above_everything_in_use(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.cluster_vmids = AsyncMock(return_value={100, 116, 8000, 8001, 8003, 9001})

        vmid = await service._next_vmid("pve1", self._defaults("8000-8999"))

        assert vmid == 8004, "the id must clear everything in the range, not fill a gap"
        proxmox.next_vmid.assert_not_awaited()

    async def test_an_empty_range_falls_through_to_pve(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """No range configured is the previous behaviour, unchanged."""
        vmid = await service._next_vmid("pve1", self._defaults(""))

        assert vmid == 105
        proxmox.next_vmid.assert_awaited()

    async def test_an_empty_range_starts_at_the_floor(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.cluster_vmids = AsyncMock(return_value={100, 116, 9001})

        assert await service._next_vmid("pve1", self._defaults("8000-8999")) == 8000

    async def test_a_full_range_refuses_rather_than_reusing(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """The day the range fills is the worst day to silently start reusing."""
        proxmox.cluster_vmids = AsyncMock(return_value={8000, 8001, 8002})

        with pytest.raises(RuntimeError, match="full"):
            await service._next_vmid("pve1", self._defaults("8000-8002"))
        proxmox.next_vmid.assert_not_awaited()


@pytest.mark.asyncio
class TestARefusedResizeIsNotASuccess:
    """A disk that could not be resized must not report a finished provision.

    From prod (#648): an invite promised 30 GB from a template carrying 32 GB.
    PVE refuses to SHRINK, so the resize task failed - and the provision
    reported `succeeded` anyway, because `resize_disk` answers with a UPID that
    nobody waited on. "Acceptance is not completion", the fifth site of it in
    this codebase.

    Benign in that direction (the redeemer got more than promised). Not benign
    in the other: ask 40 GB of a 32 GB template and you would be told you got
    40, and find out when the disk filled.

    Teeth: drop the current-size check and the shrink test fails on an attempted
    resize; stop waiting on the UPID and the failure test fails.
    """

    @staticmethod
    def _sized(gb: int):
        return {"data": {"scsi0": f"fast1:116/vm-116-disk-0.raw,discard=on,size={gb * 1024}M"}}

    async def test_a_shrink_is_not_attempted_at_all(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.get_vm_config = AsyncMock(return_value=self._sized(32))
        request = _request(disk_gb=30)

        await service._resize_disk(request, 116)

        proxmox.resize_disk.assert_not_awaited()

    async def test_a_grow_is_attempted_and_waited_for(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        proxmox.get_vm_config = AsyncMock(return_value=self._sized(32))
        proxmox.resize_disk = AsyncMock(return_value={"data": "UPID:pve1:resize:116:"})
        request = _request(disk_gb=64)

        await service._resize_disk(request, 116)

        proxmox.resize_disk.assert_awaited_once()
        proxmox.wait_for_task.assert_awaited()

    async def test_a_resize_that_fails_fails_the_provision(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """The whole point: the caller must learn the disk is not what was asked."""
        proxmox.get_vm_config = AsyncMock(return_value=self._sized(32))
        proxmox.resize_disk = AsyncMock(return_value={"data": "UPID:pve1:resize:116:"})
        proxmox.wait_for_task = AsyncMock(side_effect=RuntimeError("storage is full"))
        request = _request(disk_gb=64)

        with pytest.raises(RuntimeError, match="storage is full"):
            await service._resize_disk(request, 116)

    async def test_an_unreadable_config_still_attempts_the_resize(
        self, service: ProvisionService, proxmox: AsyncMock
    ):
        """Not knowing the size is not a reason to skip: attempt, and report."""
        proxmox.get_vm_config = AsyncMock(side_effect=RuntimeError("no answer"))
        proxmox.resize_disk = AsyncMock(return_value={"data": "UPID:pve1:resize:116:"})

        await service._resize_disk(_request(disk_gb=30), 116)

        proxmox.resize_disk.assert_awaited_once()
