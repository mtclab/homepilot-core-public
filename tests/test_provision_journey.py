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

import json
import sqlite3
import time
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

    def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        self.seen.append((request.method, path))
        if request.content:
            self.bodies[path] = json.loads(request.content)

        if path == "/cluster/nextid":
            return Response(200, json={"data": 105})
        if path == "/nodes/pve1/qemu/9000/clone":
            return Response(200, json={"data": CLONE_UPID})
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
    application = FastAPI()
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

    def test_a_pve_failure_lands_a_failed_task_not_a_stranded_one(self, app: FastAPI, db: Database):
        # The clone task reports a non-OK exitstatus: the operator must SEE a
        # failed task naming the step, never a provision stuck 'running'.
        service = app.state.provision_service

        async def failing_wait(*args: Any, **kwargs: Any) -> dict[str, Any]:
            from homepilot.adapters.proxmox import ProxmoxError

            raise ProxmoxError("GET", "/tasks", 0, "exitstatus 'no space left on device'")

        service.proxmox.wait_for_task = failing_wait

        with TestClient(app) as client:
            accepted = client.post("/guests/provision", json=BODY)
            assert accepted.status_code == 202
            task = _poll_task(client, accepted.json()["task_id"])

        assert task["status"] == "failed"
        assert task["error"].startswith("clone:")
        assert "no space left on device" in task["error"]
        assert task["finished_at"] is not None
        assert _host_by_vmid(db.db_path, 105) is None
