from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_token
from homepilot.inventory.router import router as inventory_router


def _make_inventory_app() -> FastAPI:
    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = MagicMock()
    app.state.inventory_service = MagicMock()
    return app


def _write_token():
    return {
        "user_id": "1",
        "token_id": "1",
        "scope": "write",
        "role": "admin",
        "display_name": "admin",
    }


class TestInventoryRouter:
    @pytest.fixture
    def client(self):
        app = _make_inventory_app()
        client = TestClient(app)
        app.dependency_overrides[require_token] = _write_token
        yield client
        app.dependency_overrides.clear()

    def test_list_inventory_with_new_filters(self, client):
        client.app.state.repo.list_hosts = AsyncMock(return_value=[])
        resp = client.get("/inventory?source=discovered&import_state=pending&pve_status=running")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        client.app.state.repo.list_hosts.assert_awaited_once_with(
            managed=None,
            role=None,
            source="discovered",
            import_state="pending",
            pve_status="running",
            status=None,
            limit=100,
            offset=0,
        )

    def test_list_inventory_status_filter_passed_through(self, client):
        # Regression (#390): the router accepted ?status= but silently dropped it,
        # so filtering by status returned everything. It must reach list_hosts.
        client.app.state.repo.list_hosts = AsyncMock(return_value=[])
        resp = client.get("/inventory?status=running")
        assert resp.status_code == 200
        _, kwargs = client.app.state.repo.list_hosts.await_args
        assert kwargs["status"] == "running"

    def test_patch_host_sets_role_source(self, client):
        client.app.state.repo.get_host = AsyncMock(
            return_value={
                "id": "h1",
                "hostname": "vm1",
                "role": "guest",
                "role_source": "inferred",
                "ip_source": None,
            }
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.patch("/inventory/h1", json={"role": "web-server"})
        assert resp.status_code == 200
        client.app.state.repo.update_host.assert_awaited_once_with(
            "h1", role="web-server", role_source="user"
        )

    def test_patch_host_sets_ip_source(self, client):
        client.app.state.repo.get_host = AsyncMock(
            return_value={
                "id": "h1",
                "hostname": "vm1",
                "ip_address": "",
                "ip_source": None,
            }
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.patch("/inventory/h1", json={"ip_address": "10.0.0.5"})
        assert resp.status_code == 200
        client.app.state.repo.update_host.assert_awaited_once_with(
            "h1", ip_address="10.0.0.5", ip_source="user"
        )

    def test_patch_host_unignore_sets_import_state(self, client):
        # Regression (#390): the UI "Unignore" PATCHes {import_state: 'pending'};
        # before the fix HostPatchRequest dropped the key and the endpoint 400'd
        # "No valid fields to update".
        client.app.state.repo.get_host = AsyncMock(
            return_value={"id": "h1", "hostname": "vm1", "import_state": "ignored"}
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.patch("/inventory/h1", json={"import_state": "pending"})
        assert resp.status_code == 200
        client.app.state.repo.update_host.assert_awaited_once_with("h1", import_state="pending")

    def test_patch_host_sets_status(self, client):
        client.app.state.repo.get_host = AsyncMock(
            return_value={"id": "h1", "hostname": "vm1", "status": "stopped"}
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.patch("/inventory/h1", json={"status": "running"})
        assert resp.status_code == 200
        client.app.state.repo.update_host.assert_awaited_once_with("h1", status="running")

    def test_patch_host_invalid_import_state_rejected(self, client):
        # Regression (#390): an invalid import_state must be rejected, not written
        # through to the DB (where it would violate the CHECK constraint / 500).
        client.app.state.repo.get_host = AsyncMock(
            return_value={"id": "h1", "hostname": "vm1", "import_state": "ignored"}
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.patch("/inventory/h1", json={"import_state": "bogus"})
        assert resp.status_code == 422
        client.app.state.repo.update_host.assert_not_awaited()

    def test_adopt_host(self, client):
        client.app.state.repo.get_host = AsyncMock(
            return_value={
                "id": "h1",
                "hostname": "vm1",
                "managed": 0,
                "source": "discovered",
                "import_state": "pending",
            }
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.post("/inventory/h1/adopt")
        assert resp.status_code == 200
        client.app.state.repo.update_host.assert_awaited_once_with(
            "h1",
            managed=1,
            source="imported",
            import_state="adopted",
        )

    def test_ignore_host(self, client):
        client.app.state.repo.get_host = AsyncMock(
            return_value={
                "id": "h1",
                "hostname": "vm1",
                "import_state": "pending",
            }
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.post("/inventory/h1/ignore")
        assert resp.status_code == 200
        client.app.state.repo.update_host.assert_awaited_once_with("h1", import_state="ignored")

    def test_bulk_adopt(self, client):
        client.app.state.repo.get_host = AsyncMock(
            return_value={
                "id": "h1",
                "hostname": "vm1",
                "managed": 0,
            }
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.post(
            "/inventory/bulk",
            json={"action": "adopt", "host_ids": ["h1"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == 1
        assert data["failed"] == 0

    def test_bulk_ignore(self, client):
        client.app.state.repo.get_host = AsyncMock(
            return_value={
                "id": "h1",
                "hostname": "vm1",
                "import_state": "pending",
            }
        )
        client.app.state.repo.update_host = AsyncMock(return_value=None)
        resp = client.post(
            "/inventory/bulk",
            json={"action": "ignore", "host_ids": ["h1"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == 1
        assert data["failed"] == 0

    def test_enrich_endpoint(self, client):
        service = MagicMock()
        service.enrich_inventory = AsyncMock(
            return_value={"enriched": 2, "failed": 0, "skipped": 1}
        )
        client.app.state.inventory_service = service
        resp = client.post("/inventory/enrich", json={"scope": "pve1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["enriched"] == 2
        assert data["skipped"] == 1
