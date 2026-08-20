"""POST /guests/provision: auth, validation, availability and conflict handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
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
