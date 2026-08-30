from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.admin.router import _require_admin_dep
from homepilot.admin.router import router as admin_router


def _make_admin_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=None)
    mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
    app.state.db = mock_db
    app.state.repo = MagicMock()
    app.state.settings = MagicMock(proxmox_host="", proxmox_port=8006, proxmox_verify_ssl=True)
    return app


def _admin_token():
    return {
        "user_id": 1,
        "token_id": 1,
        "scope": "*",
        "role": "admin",
        "display_name": "admin",
    }


class TestReloadSecrets:
    @pytest.fixture
    def client(self):
        app = _make_admin_app()
        client = TestClient(app)
        app.dependency_overrides[_require_admin_dep.dependency] = _admin_token
        yield client
        app.dependency_overrides.clear()

    def test_vault_not_configured(self, client):
        if hasattr(client.app.state, "vault"):
            del client.app.state.vault

        resp = client.post("/admin/reload-secrets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "not configured" in data["message"].lower()

    def test_requires_auth(self):
        app = _make_admin_app()
        client = TestClient(app)
        resp = client.post("/admin/reload-secrets")
        assert resp.status_code == 401

    def test_locked_returns_already_running(self, client):
        client.app.state.vault = MagicMock()
        client.app.state.vault.get_secret = AsyncMock(return_value={})

        from homepilot.admin.router import _reload_lock

        try:
            import asyncio as _asyncio

            loop = _asyncio.new_event_loop()
            loop.run_until_complete(_reload_lock.acquire())
            resp = client.post("/admin/reload-secrets")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "already_running"
        finally:
            _reload_lock.release()
