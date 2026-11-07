"""THE GATE for #711: acceptance is not completion - and "finished" is PVE's word, not ours.

Four ways the product read a PVE answer wrongly, all found in the 2026-09 audit:

* **An asynchronous config write taken for a finished one.** POST
  `.../qemu/{vmid}/config` answers with a qmconfig UPID. Provisioning moved on
  at the acceptance, so a config task that died left the template's NIC and
  cloud-init on the guest while the task said "succeeded". Template builds did
  the same with the cloud-init drive.
* **Success-with-warnings taken for failure.** PVE's own rule
  (`upid_status_is_error`) is that "OK" and "WARNINGS: <n>" both mean success.
  A VM start that warned failed the provision and destroyed a working guest.
* **A killed guest command taken for a clean exit.** qemu-guest-agent reports
  a SIGKILLed process with `signal` and no `exitcode`; reading the missing
  exitcode as 0 recorded an OOM-killed `tailscale up` as "joined".
* **A destroy fired at a guest our own task still holds.** A clone that
  outlived `task_timeout_s` went straight to destroy; PVE refused ("VM is
  locked (clone)"), the clone then finished, and the guest was orphaned.

The fake is PVE's HTTP API at the httpx boundary with REAL task state: a UPID
is a task that runs until it is finished or stopped, a locked guest refuses its
destroy, and every answer has the shape PVE gives. The assertions read the
fake's end state (is the guest still on the cluster? did it ever boot?).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import Request, Response

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError, task_warnings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.models import GuestTemplateRequest, ProvisionRequest
from homepilot.provision.service import ProvisionService
from homepilot.provision.template import GuestTemplateService
from homepilot.tasks.repository import TaskRepository

pytestmark = pytest.mark.asyncio

NODE = "pve1"
TEMPLATE = 9000
API = "https://pve.example:8006/api2/json"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"


class TaskPVE:
    """A PVE whose tasks have state, and whose guests are locked while one runs."""

    def __init__(self) -> None:
        self.vms: dict[int, dict[str, Any]] = {TEMPLATE: {"name": "tmpl", "template": 1}}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.started: list[int] = []
        self.destroyed: list[int] = []
        self.stopped_tasks: list[str] = []
        self.converted: list[int] = []
        # How each kind of task ENDS: "OK", "WARNINGS: 1", an error string, or
        # "hang" for a task that runs until somebody stops it.
        self.outcome: dict[str, str] = {}
        # Only a config write carrying this key gets the "qmconfig" ending
        # (None: every config write does).
        self.failing_config_key: str | None = None
        self._seq = 0

    def _task(self, kind: str, vmid: int, ending: str | None = None) -> str:
        self._seq += 1
        upid = f"UPID:{NODE}:{self._seq:08X}:0:0:{kind}:{vmid}:root@pam:"
        ending = ending or self.outcome.get(kind, "OK")
        if ending == "hang":
            self.tasks[upid] = {"status": "running", "vmid": vmid}
            self.vms[vmid]["lock"] = kind
        else:
            self.tasks[upid] = {"status": "stopped", "exitstatus": ending, "vmid": vmid}
        return upid

    async def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        method = request.method
        body = json.loads(request.content) if request.content else {}

        if path == "/cluster/nextid":
            candidate = 100
            while candidate in self.vms:
                candidate += 1
            return Response(200, json={"data": candidate})
        if path == "/cluster/resources":
            return Response(
                200,
                json={
                    "data": [{"vmid": v, "node": NODE, "type": "qemu"} for v in sorted(self.vms)]
                },
            )
        if path.startswith(f"/nodes/{NODE}/tasks/"):
            upid = httpx.URL(request.url).raw_path.decode().split("/tasks/")[1]
            upid = upid.removesuffix("/status")
            from urllib.parse import unquote

            upid = unquote(upid)
            task = self.tasks[upid]
            if method == "DELETE":
                self.stopped_tasks.append(upid)
                if task["status"] == "running":
                    task.update(status="stopped", exitstatus="interrupted by signal")
                    self.vms.get(task["vmid"], {}).pop("lock", None)
                return Response(200, json={"data": None})
            return Response(200, json={"data": {k: v for k, v in task.items() if k != "vmid"}})
        if path.startswith("/storage/"):
            return Response(200, json={"data": {"content": "images,import"}})
        if path == f"/nodes/{NODE}/qemu" and method == "POST":
            vmid = int(body["vmid"])
            self.vms[vmid] = {k: v for k, v in body.items() if k != "vmid"}
            return Response(200, json={"data": self._task("qmcreate", vmid)})

        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "nodes" and parts[2] == "qemu":
            vmid = int(parts[3])
            tail = "/".join(parts[4:])
            if tail == "clone":
                newid = int(body["newid"])
                self.vms[newid] = {"name": body["name"], "description": body.get("description")}
                return Response(200, json={"data": self._task("qmclone", newid)})
            if vmid not in self.vms:
                return Response(
                    500,
                    text=f"Configuration file 'nodes/{NODE}/qemu-server/{vmid}.conf' does not exist",
                )
            if tail == "" and method == "DELETE":
                if "lock" in self.vms[vmid]:
                    return Response(500, text=f"VM is locked ({self.vms[vmid]['lock']})")
                self.destroyed.append(vmid)
                del self.vms[vmid]
                return Response(200, json={"data": self._task("qmdestroy", vmid)})
            if tail == "config" and method == "GET":
                return Response(200, json={"data": dict(self.vms[vmid])})
            if tail == "config":
                # PVE's real shape: the POST config write is a qmconfig worker.
                ending = self.outcome.get("qmconfig", "OK")
                if self.failing_config_key is not None and self.failing_config_key not in body:
                    ending = "OK"
                if ending == "OK":
                    self.vms[vmid].update(body)
                return Response(200, json={"data": self._task("qmconfig", vmid, ending)})
            if tail == "status/current":
                running = vmid in self.started and vmid not in self.destroyed
                return Response(200, json={"data": {"status": "running" if running else "stopped"}})
            if tail == "status/start":
                self.started.append(vmid)
                return Response(200, json={"data": self._task("qmstart", vmid)})
            if tail == "status/stop":
                if vmid in self.started:
                    self.started.remove(vmid)
                return Response(200, json={"data": self._task("qmstop", vmid)})
            if tail == "resize":
                return Response(200, json={"data": None})
            if tail == "template":
                self.converted.append(vmid)
                return Response(200, json={"data": None})
            if tail == "agent/network-get-interfaces":
                return Response(500, text="QEMU guest agent is not running")
        return Response(501, text=f"unhandled {method} {path}")


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "hp.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def pve() -> TaskPVE:
    return TaskPVE()


def _client(pve: TaskPVE) -> ProxmoxClient:
    client = ProxmoxClient(base_url="https://pve.example:8006", token="root@pam!t=uuid")
    fake = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(pve.handle))
    client._client = fake
    client._write_client = fake
    return client


async def _provision(db: Database, pve: TaskPVE, task_timeout_s: float = 2.0) -> dict[str, Any]:
    service = ProvisionService(
        proxmox=_client(pve),
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=task_timeout_s,
        ip_wait_s=0.0,
        ip_interval=0.01,
    )
    request = ProvisionRequest(
        name="web-01", node=NODE, template_vmid=TEMPLATE, ssh_authorized_key=PUBKEY, owner="a"
    )
    task_id = await service.start(request, actor="tester")
    while service._running_tasks:
        await asyncio.gather(*list(service._running_tasks), return_exceptions=True)
    row = await service.task_repo.get_task(task_id)
    assert row is not None
    return dict(row)


class TestAConfigWriteIsWaitedFor:
    async def test_a_config_task_that_fails_fails_the_provision_and_the_guest_never_boots(
        self, db, pve
    ):
        pve.outcome["qmconfig"] = "unable to parse net0"
        row = await _provision(db, pve)

        assert row["status"] == "failed", "a config task that died was reported as a success"
        assert "configure" in row["error"]
        assert pve.started == [], "the guest booted on the template's config"
        assert 100 not in pve.vms and pve.destroyed == [100]

    async def test_a_template_whose_cloud_init_write_failed_is_never_converted(self, db, pve):
        pve.outcome["qmconfig"] = "unable to create cloudinit drive"
        pve.failing_config_key = "ide2"  # the disk import succeeds; cloud-init fails
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
            download_timeout_s=2.0,
        )
        task_id = await service.start(
            GuestTemplateRequest(
                node=NODE, template_vmid=150, storage="local", source_volid="local:import/u.qcow2"
            ),
            actor="tester",
        )
        while service._running_tasks:
            await asyncio.gather(*list(service._running_tasks), return_exceptions=True)
        row = await service.task_repo.get_task(task_id)

        assert row["status"] == "failed"
        assert pve.converted == [], "a template was made from a shell whose config never landed"
        assert 150 not in pve.vms


class TestWarningsAreSuccess:
    async def test_a_start_that_warned_is_a_running_guest_not_a_destroyed_one(self, db, pve):
        pve.outcome["qmstart"] = "WARNINGS: 1"
        row = await _provision(db, pve)

        assert row["status"] == "succeeded", row["error"]
        assert pve.destroyed == [], "a guest PVE started (with a warning) was destroyed"
        assert pve.started == [100]

    @pytest.mark.parametrize(
        ("exitstatus", "warnings"),
        [("OK", 0), ("WARNINGS: 1", 1), ("WARNINGS: 12", 12), ("WARNINGS", None), ("bad", None)],
    )
    async def test_pve_success_rule(self, exitstatus: str, warnings: int | None):
        assert task_warnings(exitstatus) == warnings

    async def test_wait_for_task_returns_the_warning_count(self):
        client = ProxmoxClient(base_url="https://pve.example:8006", token="t")
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__regex=rf"{API}/nodes/pve1/tasks/.*/status").mock(
                return_value=Response(
                    200, json={"data": {"status": "stopped", "exitstatus": "WARNINGS: 3"}}
                )
            )
            data = await client.wait_for_task("pve1", "UPID:pve1:1:qmstart:", poll_interval=0)
        assert data["warnings"] == 3


class TestAKilledGuestCommandIsNotAnExitZero:
    @pytest.mark.parametrize(
        ("status", "rc", "said"),
        [
            ({"exited": 1, "signal": 9}, 137, "SIGKILL"),
            ({"exited": 1, "signal": 15, "err-data": "boom"}, 143, "SIGTERM"),
            ({"exited": 1}, 255, "no exit code"),
            ({"exited": 1, "exitcode": 0, "out-data": "hi"}, 0, ""),
        ],
    )
    async def test_agent_run_reports_how_the_command_ended(
        self, status: dict[str, Any], rc: int, said: str
    ):
        client = ProxmoxClient(base_url="https://pve.example:8006", token="t")
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{API}/nodes/pve1/qemu/101/agent/exec").mock(
                return_value=Response(200, json={"data": {"pid": 7}})
            )
            mock.get(f"{API}/nodes/pve1/qemu/101/agent/exec-status").mock(
                return_value=Response(200, json={"data": status})
            )
            got_rc, _out, err = await client.agent_run("pve1", 101, "tailscale up", poll_interval=0)

        assert got_rc == rc
        assert said in err


class TestATimedOutTaskIsStoppedBeforeTheDestroy:
    async def test_a_clone_that_outlives_its_budget_is_stopped_and_its_guest_removed(self, db, pve):
        """The orphan class: without the stop, PVE refuses the destroy because
        the clone holds the lock, and the clone later finishes into a guest
        nobody removes."""
        pve.outcome["qmclone"] = "hang"
        row = await _provision(db, pve, task_timeout_s=0.1)

        assert row["status"] == "failed"
        clone_upids = [u for u in pve.tasks if ":qmclone:" in u]
        assert pve.stopped_tasks == clone_upids, "the still-running clone was never stopped"
        assert 100 not in pve.vms, f"the guest was orphaned: {row['error']}"
        outcome = json.loads(row["result_json"])
        assert outcome["cleanup"] == "deleted"
        assert outcome["stop_task"] == "stopped"

    async def test_a_task_that_already_failed_is_not_stopped(self, db, pve):
        pve.outcome["qmclone"] = "clone failed: no space left on device"
        row = await _provision(db, pve)

        assert row["status"] == "failed"
        assert pve.stopped_tasks == [], "a finished task was sent a stop"
        assert "stop_task" not in json.loads(row["result_json"])


async def test_a_task_that_never_stops_is_an_error_not_a_hang():
    client = ProxmoxClient(base_url="https://pve.example:8006", token="t")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=rf"{API}/nodes/pve1/tasks/.*/status").mock(
            return_value=Response(200, json={"data": {"status": "running"}})
        )
        with pytest.raises(ProxmoxError):
            await client.wait_until_stopped("pve1", "UPID:pve1:1:x:", timeout_s=0, poll_interval=0)
