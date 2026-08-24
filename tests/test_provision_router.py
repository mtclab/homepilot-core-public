"""POST /guests/provision: auth, validation, availability and conflict handling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_token
from homepilot.provision.router import router as provision_router
from homepilot.provision.service import ProvisionConflictError

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"

GOOD_BODY = {
    "name": "web-01",
    "node": "pve1",
    "template_vmid": 9000,
    "cores": 2,
    "memory_mb": 2048,
    "ssh_authorized_key": PUBKEY,
    "owner": "olli",
}


def _admin_token() -> dict:
    return {
        "user_id": "u1",
        "token_id": "t1",
        "scope": "admin",
        "role": "admin",
        "display_name": "admin",
    }


def _read_token() -> dict:
    return {
        "user_id": "u2",
        "token_id": "t2",
        "scope": "read",
        "role": "viewer",
        "display_name": "viewer",
    }


def _make_app(service) -> FastAPI:
    app = FastAPI()
    app.include_router(provision_router, prefix="/guests", dependencies=[Depends(require_token)])
    app.state.repo = MagicMock()
    app.state.provision_service = service
    return app


@pytest.fixture
def service() -> MagicMock:
    svc = MagicMock()
    svc.proxmox = MagicMock()
    svc.start = AsyncMock(return_value="task-abc")
    return svc


@pytest.fixture
def client(service: MagicMock):
    app = _make_app(service)
    app.dependency_overrides[require_token] = _admin_token
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestAccepted:
    def test_returns_202_with_task_id(self, client: TestClient, service: MagicMock):
        resp = client.post("/guests/provision", json=GOOD_BODY)
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"task_id": "task-abc", "status": "pending"}
        request = service.start.await_args.args[0]
        assert request.name == "web-01"
        assert service.start.await_args.kwargs["actor"] == "u1"

    def test_defaults_are_applied(self, client: TestClient, service: MagicMock):
        client.post("/guests/provision", json=GOOD_BODY)
        request = service.start.await_args.args[0]
        assert request.ciuser == "friend"
        assert request.ipconfig0 == "ip=dhcp"
        assert request.disk == "scsi0"
        assert request.full is True


class TestAuth:
    def test_no_credentials_is_401(self, service: MagicMock):
        app = _make_app(service)
        with TestClient(app) as c:
            assert c.post("/guests/provision", json=GOOD_BODY).status_code == 401
        service.start.assert_not_awaited()

    def test_non_admin_scope_is_403(self, service: MagicMock):
        app = _make_app(service)
        app.dependency_overrides[require_token] = _read_token
        with TestClient(app) as c:
            assert c.post("/guests/provision", json=GOOD_BODY).status_code == 403
        service.start.assert_not_awaited()


class TestUnavailable:
    def test_no_proxmox_is_503(self, service: MagicMock):
        service.proxmox = None
        app = _make_app(service)
        app.dependency_overrides[require_token] = _admin_token
        with TestClient(app) as c:
            resp = c.post("/guests/provision", json=GOOD_BODY)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Proxmox not configured"

    def test_no_service_is_503(self):
        app = _make_app(None)
        app.dependency_overrides[require_token] = _admin_token
        with TestClient(app) as c:
            assert c.post("/guests/provision", json=GOOD_BODY).status_code == 503


class TestValidation:
    @pytest.mark.parametrize(
        "override",
        [
            {"name": "Web-01"},
            {"name": "-web"},
            {"name": "web-"},
            {"name": "ab"},
            {"name": "a" * 64},
            {"name": "web_01"},
            {"node": ""},
            {"template_vmid": 0},
            {"cores": 0},
            {"cores": 33},
            {"memory_mb": 128},
            {"memory_mb": 70000},
            {"disk_gb": 0},
            {"disk_gb": 5000},
            {"disk": "nvme0"},
            {"ciuser": "Root"},
            {"owner": "o" * 65},
        ],
    )
    def test_invalid_field_is_422(self, client: TestClient, service: MagicMock, override: dict):
        resp = client.post("/guests/provision", json={**GOOD_BODY, **override})
        assert resp.status_code == 422, override
        service.start.assert_not_awaited()

    @pytest.mark.parametrize(
        "key",
        [
            "not-a-key",
            "ssh-ed25519",
            "ssh-dss AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq k@h",
            "ssh-ed25519 not!base64! k@h",
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq k@h\n"
            "ssh-rsa AAAAB3NzaC1yc2E attacker@evil",
        ],
    )
    def test_invalid_pubkey_is_422(self, client: TestClient, service: MagicMock, key: str):
        resp = client.post("/guests/provision", json={**GOOD_BODY, "ssh_authorized_key": key})
        assert resp.status_code == 422
        service.start.assert_not_awaited()

    def test_key_is_optional(self, client: TestClient):
        body = {k: v for k, v in GOOD_BODY.items() if k != "ssh_authorized_key"}
        assert client.post("/guests/provision", json=body).status_code == 202


class TestConflict:
    def test_duplicate_inflight_name_is_409(self, client: TestClient, service: MagicMock):
        service.start = AsyncMock(side_effect=ProvisionConflictError("already in flight"))
        resp = client.post("/guests/provision", json=GOOD_BODY)
        assert resp.status_code == 409
        assert "already in flight" in resp.json()["detail"]


# ── POST /tasks/{id}/cancel for a provision (#452) ───────────────────────────
# The cancel endpoint lives in the tasks router, but a provision coroutine is
# owned by ProvisionService, and the runner has never heard of it. These run the
# REAL endpoint over a REAL service so the routing itself is under test: a cancel
# that goes to the runner marks the row and leaves the clone running, and the
# run then overwrites the row - the bug in #452.


@pytest_asyncio.fixture
async def cancel_db(tmp_path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    database = Database(str(tmp_path / "tasks.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def cancel_proxmox() -> AsyncMock:
    from homepilot.adapters.proxmox import ProxmoxClient

    px = AsyncMock(spec=ProxmoxClient)
    px.next_vmid = AsyncMock(return_value=105)
    px.clone_vm = AsyncMock(return_value="UPID:pve1:clone:")
    px.set_vm_config = AsyncMock(return_value={"data": None})
    px.resize_disk = AsyncMock(return_value={"data": None})
    px.start_vm = AsyncMock(return_value="UPID:pve1:start:")
    px.get_vm_agent_network = AsyncMock(return_value=None)
    px.stop_task = AsyncMock(return_value={"data": None})
    px.delete_vm = AsyncMock(return_value="UPID:pve1:destroy:")
    return px


@pytest_asyncio.fixture
async def cancel_app(cancel_db, cancel_proxmox: AsyncMock):
    """The tasks router over a real ProvisionService, plus a stub runner that
    records whether the non-provision path was used."""
    from homepilot.db.repository import Repository
    from homepilot.provision.service import ProvisionService
    from homepilot.tasks.repository import TaskRepository
    from homepilot.tasks.router import router as tasks_router

    task_repo = TaskRepository(cancel_db)
    service = ProvisionService(
        proxmox=cancel_proxmox,
        task_repo=task_repo,
        repo=Repository(cancel_db),
        poll_interval=0.01,
        task_timeout_s=2.0,
        ip_wait_s=0.0,
        ip_interval=0.01,
    )
    runner = MagicMock()
    runner.cancel_task = AsyncMock(return_value={"id": "x", "status": "cancelled"})

    app = FastAPI()
    app.state.task_repo = task_repo
    app.state.provision_service = service
    app.state.task_runner = runner
    app.include_router(tasks_router, prefix="/tasks", dependencies=[Depends(require_token)])
    app.dependency_overrides[require_token] = _admin_token
    app.state._service = service
    app.state._runner = runner
    yield app
    app.dependency_overrides.clear()


def _cancel_client(app):
    import httpx

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestProvisionCancelEndpoint:
    async def test_cancel_mid_clone_reaches_proxmox_and_the_row_stays_cancelled(
        self, cancel_app, cancel_proxmox: AsyncMock
    ):
        import asyncio

        from homepilot.provision.models import ProvisionRequest

        service = cancel_app.state._service
        gate = asyncio.Event()

        async def blocking_wait(*args, **kwargs):
            await gate.wait()
            return {"status": "stopped", "exitstatus": "OK"}

        cancel_proxmox.wait_for_task = AsyncMock(side_effect=blocking_wait)

        task_id = await service.start(
            ProvisionRequest(**{**GOOD_BODY, "template_vmid": 9000}), actor="u1"
        )
        for _ in range(400):
            if cancel_proxmox.wait_for_task.await_count == 1:
                break
            await asyncio.sleep(0.005)

        async with _cancel_client(cancel_app) as client:
            resp = await client.post(f"/tasks/{task_id}/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"

        # Release the clone wait only now: the run must be gone, not merely
        # marked, so nothing comes back to overwrite the record.
        gate.set()
        for _ in range(10):
            pending = [t for t in service._running_tasks if not t.done()]
            if not pending:
                break
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5.0)

        cancel_proxmox.stop_task.assert_awaited_once_with("pve1", "UPID:pve1:clone:")
        cancel_proxmox.delete_vm.assert_awaited_once_with("pve1", 105)
        # The runner must never see a provision.
        cancel_app.state._runner.cancel_task.assert_not_awaited()

        row = await cancel_app.state.task_repo.get_task(task_id)
        assert row["status"] == "cancelled", "a cancelled provision must stay cancelled"
        assert json.loads(row["result_json"]) == {
            "cancelled": True,
            "vmid": 105,
            "stop_task": "stopped",
            "cleanup": "deleted",
        }

    async def test_cancel_of_a_restart_orphan_records_the_unknown_state(
        self, cancel_app, cancel_proxmox: AsyncMock
    ):
        task_repo = cancel_app.state.task_repo
        task_id = await task_repo.create_task(None, "provision")
        await task_repo.update_task_status(task_id, "running")

        async with _cancel_client(cancel_app) as client:
            resp = await client.post(f"/tasks/{task_id}/cancel")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "cancelled"
        assert body["error"] == "process restarted; in-flight PVE state unknown"
        cancel_proxmox.stop_task.assert_not_awaited()
        cancel_proxmox.delete_vm.assert_not_awaited()
        cancel_proxmox.clone_vm.assert_not_awaited()

    async def test_cancel_without_a_provision_service_still_marks_and_says_so(
        self, cancel_app, cancel_proxmox: AsyncMock
    ):
        task_repo = cancel_app.state.task_repo
        task_id = await task_repo.create_task(None, "provision")
        await task_repo.update_task_status(task_id, "running")
        del cancel_app.state.provision_service

        async with _cancel_client(cancel_app) as client:
            resp = await client.post(f"/tasks/{task_id}/cancel")

        assert resp.status_code == 200, resp.text
        assert resp.json()["error"] == "process restarted; in-flight PVE state unknown"
        cancel_app.state._runner.cancel_task.assert_not_awaited()

    async def test_a_non_provision_task_still_goes_through_the_runner(self, cancel_app):
        task_repo = cancel_app.state.task_repo
        task_id = await task_repo.create_task("2026-01-01-alpha-aaa111", "apply")

        async with _cancel_client(cancel_app) as client:
            resp = await client.post(f"/tasks/{task_id}/cancel")

        assert resp.status_code == 200, resp.text
        cancel_app.state._runner.cancel_task.assert_awaited_once_with(task_id)

    async def test_unknown_task_is_404(self, cancel_app):
        async with _cancel_client(cancel_app) as client:
            resp = await client.post("/tasks/no-such-task/cancel")
        assert resp.status_code == 404
