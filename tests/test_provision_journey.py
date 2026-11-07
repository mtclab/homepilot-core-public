"""THE JOURNEY GATE for provision-from-template (#442 stage 1).

Asserts the OPERATOR'S GOAL is reached, not that a function returned success:
POST /guests/provision, then poll the real GET /tasks/{id} the UI polls, and
require that the task ends up carrying a real vmid AND that the guest is in
inventory with its owner recorded.

Wiring is real end to end — real routers, real auth dependency tree, real
migrated SQLite, real TaskRepository/Repository, real ProxmoxClient. Only the
HTTP transport under the Proxmox client is faked, so URL building, JSON bodies,
UPID quoting and error wrapping are all exercised for real.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import Request, Response

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.router import router as provision_router
from homepilot.provision.service import ProvisionService
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.router import router as tasks_router

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"
CLONE_UPID = "UPID:pve1:0000A1B2:0123ABCD:65F0C0DE:qmclone:9000:root@pam:"
START_UPID = "UPID:pve1:0000A1B3:0123ABCE:65F0C0DF:qmstart:105:root@pam:"

BODY = {
    "name": "web-01",
    "node": "pve1",
    "template_vmid": 9000,
    "cores": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "ssh_authorized_key": PUBKEY,
    "owner": "olli",
}


class FakePVE:
    """A minimal, stateful stand-in for the Proxmox API at the httpx boundary."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []
        self.bodies: dict[str, Any] = {}
        self.destroyed: list[int] = []

    def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        self.seen.append((request.method, path))
        if request.content:
            self.bodies[path] = json.loads(request.content)

        if path == "/cluster/nextid":
            return Response(200, json={"data": 105})
        if path == "/nodes/pve1/qemu/9000/clone":
            return Response(200, json={"data": CLONE_UPID})
        if path == "/nodes/pve1/qemu/105/status/current":
            return Response(200, json={"data": {"status": "stopped"}})
        if path == "/nodes/pve1/qemu/105/status/stop":
            return Response(200, json={"data": "UPID:pve1:stop:"})
        if request.method == "DELETE" and path == "/nodes/pve1/qemu/105":
            self.destroyed.append(105)
            return Response(200, json={"data": "UPID:pve1:destroy:"})
        if path.startswith("/nodes/pve1/tasks/") and path.endswith("/status"):
            return Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
        if path == "/nodes/pve1/qemu/105/config":
            return Response(200, json={"data": None})
        if path == "/nodes/pve1/qemu/105/resize":
            return Response(200, json={"data": None})
        if path == "/nodes/pve1/qemu/105/status/start":
            return Response(200, json={"data": START_UPID})
        if path == "/nodes/pve1/qemu/105/agent/network-get-interfaces":
            return Response(
                200,
                json={
                    "data": {
                        "result": [
                            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
                            {"name": "eth0", "ip-addresses": [{"ip-address": "10.0.0.42"}]},
                        ]
                    }
                },
            )
        return Response(501, text=f"unhandled {request.method} {path}")


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "journey.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def pve() -> FakePVE:
    return FakePVE()


@pytest.fixture
def proxmox(pve: FakePVE) -> ProxmoxClient:
    client = ProxmoxClient(base_url="https://pve.example:8006", token="root@pam!t=uuid")
    transport = httpx.MockTransport(pve.handle)
    fake = httpx.AsyncClient(base_url="https://pve.example:8006/api2/json", transport=transport)
    client._client = fake
    client._write_client = fake
    return client


@pytest.fixture
def app(db: Database, proxmox: ProxmoxClient) -> FastAPI:
    repo = Repository(db)
    task_repo = TaskRepository(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The shutdown half of main.lifespan, and the reason this app has one at
        # all: the provision job runs behind an accepted request on the
        # TestClient's portal loop, and that loop is CLOSED when the `with`
        # block exits. A test that leaves as soon as the task row reads
        # terminal used to race the job's last write (the audit row lands
        # after the status): about one gate run in a few, the write was still
        # in flight, aiosqlite's worker thread resolved it onto the closed loop
        # and died with "Event loop is closed". Production drains before the
        # database closes (main.lifespan, gated in test_background_shutdown);
        # this harness now does exactly that before its loop goes.
        yield
        await app.state.provision_service.drain()

    application = FastAPI(lifespan=lifespan)
    application.include_router(
        provision_router, prefix="/guests", dependencies=[Depends(require_token)]
    )
    application.include_router(tasks_router, prefix="/tasks", dependencies=[Depends(require_token)])
    application.state.repo = repo
    application.state.task_repo = task_repo
    application.state.provision_service = ProvisionService(
        proxmox=proxmox,
        task_repo=task_repo,
        repo=repo,
        poll_interval=0.01,
        task_timeout_s=5.0,
        ip_wait_s=2.0,
        ip_interval=0.05,
    )
    application.dependency_overrides[require_token] = lambda: {
        "user_id": "olli",
        "token_id": "t1",
        "scope": "admin",
        "role": "admin",
        "display_name": "olli",
    }
    return application


def _host_by_vmid(db_path: str, vmid: int) -> dict[str, Any] | None:
    """Read the hosts row with a plain sqlite3 connection.

    Deliberately not through Repository: the service wrote it from the app's own
    event loop, and this asserts what actually landed on disk.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM hosts WHERE proxmox_id = ?", (vmid,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _poll_task(client: TestClient, task_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200, resp.text
        task = resp.json()
        if task["status"] in ("succeeded", "failed", "cancelled"):
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached a terminal state")


class TestProvisionJourney:
    def test_operator_gets_a_running_guest_in_inventory(
        self, app: FastAPI, pve: FakePVE, db: Database
    ):
        with TestClient(app) as client:
            accepted = client.post("/guests/provision", json=BODY)
            assert accepted.status_code == 202, accepted.text
            task_id = accepted.json()["task_id"]

            task = _poll_task(client, task_id)
            assert task["status"] == "succeeded", task["error"]
            assert task["action"] == "provision"
            assert task["artifact_id"] is None

            result = json.loads(task["result_json"])
            assert result["vmid"] == 105
            assert result["name"] == "web-01"
            assert result["node"] == "pve1"
            assert result["ip"] == "10.0.0.42"

            # The guest is really in inventory, owned, and reachable by the same
            # key the reconciler matches on.
            host = _host_by_vmid(db.db_path, 105)
            assert host is not None
            assert host["hostname"] == "web-01"
            assert host["owner"] == "olli"
            assert host["ip_address"] == "10.0.0.42"
            assert host["host_type"] == "qemu"
            assert host["id"] == result["host_id"]

            # A provision task must serialize through the tasks listing too — the
            # UI's "what is running" view lists it with a NULL artifact_id.
            listing = client.get("/tasks")
            assert listing.status_code == 200
            ids = [t["id"] for t in listing.json()["items"]]
            assert task_id in ids

        # The real PVE call sequence happened, in order.
        methods = [f"{m} {p}" for m, p in pve.seen]
        assert methods[0] == "GET /cluster/nextid"
        assert "POST /nodes/pve1/qemu/9000/clone" in methods
        assert "POST /nodes/pve1/qemu/105/config" in methods
        assert "PUT /nodes/pve1/qemu/105/resize" in methods
        assert "POST /nodes/pve1/qemu/105/status/start" in methods
        # cloud-init got the operator's key, url-encoded exactly once for PVE.
        from urllib.parse import unquote

        sent_key = pve.bodies["/nodes/pve1/qemu/105/config"]["sshkeys"]
        assert unquote(sent_key) == PUBKEY

    def test_a_pve_failure_lands_a_failed_task_not_a_stranded_one(
        self, app: FastAPI, db: Database, pve: FakePVE
    ):
        # The clone task reports a non-OK exitstatus: the operator must SEE a
        # failed task naming the step, never a provision stuck 'running'.
        service = app.state.provision_service

        # Only the CLONE task fails. The cleanup behind it stops and destroys
        # the half-made guest, and those are PVE tasks too - a fake that failed
        # every wait would also fail the destroy's wait and make this assert
        # the wrong thing (that a successful cleanup reports as failed).
        async def failing_wait(node: Any, upid: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
            from homepilot.adapters.proxmox import ProxmoxError

            if "clone" in str(upid):
                raise ProxmoxError("GET", "/tasks", 0, "exitstatus 'no space left on device'")
            return {"status": "stopped", "exitstatus": "OK"}

        service.proxmox.wait_for_task = failing_wait

        with TestClient(app) as client:
            accepted = client.post("/guests/provision", json=BODY)
            assert accepted.status_code == 202
            task = _poll_task(client, accepted.json()["task_id"])

        assert task["status"] == "failed"
        assert task["error"].startswith("failed at clone:")
        assert "no space left on device" in task["error"]
        assert task["finished_at"] is not None
        assert _host_by_vmid(db.db_path, 105) is None
        # The clone was issued before the wait failed, so the guest it may have
        # left behind is destroyed, not orphaned (#595). The error says so.
        assert pve.destroyed == [105]
        assert "destroyed guest vmid 105" in task["error"]

    def test_the_failed_jobs_last_write_lands_before_the_app_loop_goes(
        self, app: FastAPI, db: Database, pve: FakePVE
    ):
        """The gate flake, made deterministic.

        A provision job writes its terminal status and THEN its audit row, so a
        poller can see 'failed' while the job is still writing. When that poller
        was a TestClient leaving its `with` block, the portal loop closed under
        the write and aiosqlite's worker thread died with "Event loop is
        closed" - about one gate run in a few, in the failure journey above.
        Here the audit write is held back on purpose, so the job is ALWAYS
        still writing when the row reads 'failed': the write must land anyway,
        because whatever owns the loop drains the service before it goes.

        Teeth: drop the drain from the `app` fixture's lifespan and the audit
        row is lost on every run.
        """
        service = app.state.provision_service

        async def failing_wait(node: Any, upid: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
            from homepilot.adapters.proxmox import ProxmoxError

            if "clone" in str(upid):
                raise ProxmoxError("GET", "/tasks", 0, "exitstatus 'no space left on device'")
            return {"status": "stopped", "exitstatus": "OK"}

        service.proxmox.wait_for_task = failing_wait
        real_log_audit = service.repo.log_audit

        async def slow_log_audit(**kwargs: Any) -> Any:
            await asyncio.sleep(0.3)
            return await real_log_audit(**kwargs)

        service.repo.log_audit = slow_log_audit

        with TestClient(app) as client:
            accepted = client.post("/guests/provision", json=BODY)
            assert accepted.status_code == 202
            task = _poll_task(client, accepted.json()["task_id"])
            assert task["status"] == "failed"

        conn = sqlite3.connect(db.db_path)
        try:
            rows = conn.execute(
                "SELECT action FROM audit_log WHERE source = 'provision'"
            ).fetchall()
        finally:
            conn.close()
        assert ("provision_failed",) in rows, (
            "the failed provision's audit row never landed: the job was still "
            "writing when the app's loop closed, and nothing drained it"
        )


async def test_a_shutdown_drain_outlasts_the_cleanup_it_starts(
    db: Database, proxmox: ProxmoxClient, pve: FakePVE
) -> None:
    """A drain that has to cancel a provision must not return while the
    cleanup that cancel started is still running.

    Cancelling a job mid-clone spawns a tracked cleanup (stop, destroy, record
    the outcome). The drain used to wait only for the jobs it saw on entry, so
    a cleanup slower than its one-second grace was still running - still about
    to write the task row - when the lifespan closed the database. Here the
    destroy's wait never finishes on its own, so the drain must cancel the
    cleanup too, and nothing tracked may be left running when it returns.

    Teeth: make drain_tasks snapshot its tasks once (the old behaviour) and the
    cleanup is still running here.
    """
    from homepilot.provision.models import ProvisionRequest

    service = ProvisionService(
        proxmox=proxmox,
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=30.0,
    )
    clone_waiting = asyncio.Event()
    cleanup_waiting = asyncio.Event()

    async def wait(node: Any, upid: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if "clone" in str(upid):
            clone_waiting.set()
        else:
            cleanup_waiting.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    service.proxmox.wait_for_task = wait  # type: ignore[method-assign]
    await service.start(ProvisionRequest(**BODY))
    await asyncio.wait_for(clone_waiting.wait(), timeout=5)

    await asyncio.wait_for(service.drain(timeout=0.1), timeout=15)

    assert cleanup_waiting.is_set(), "the cancel never started its cleanup"
    still_running = [t for t in service._running_tasks if not t.done()]
    assert not still_running, (
        f"the drain returned with {len(still_running)} provision task(s) still running: "
        "the database would close under them"
    )
