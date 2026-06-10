from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from homepilot.main import app

    return TestClient(app)


def _setup_state(client, db=None, proxmox=None, vault=None, settings=None):
    if db is not None:
        client.app.state.db = db
    if proxmox is not None:
        client.app.state.proxmox = proxmox
    else:
        if hasattr(client.app.state, "proxmox"):
            del client.app.state.proxmox
    if vault is not None:
        client.app.state.vault = vault
    else:
        if hasattr(client.app.state, "vault"):
            del client.app.state.vault
    if settings is not None:
        client.app.state.settings = settings
    else:
        if not hasattr(client.app.state, "settings"):
            client.app.state.settings = MagicMock(
                proxmox_host="", agent_hub_enabled=False, cors_origins="http://localhost:5173"
            )


class TestHealthEndpoint:
    def test_all_ok(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["vault"] == "ok"
        assert data["checks"]["proxmox"] == "not_configured"

    def test_db_error_yields_down(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=Exception("connection lost"))
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "down"
        assert data["checks"]["database"] in ("connection lost", "error")

    def test_proxmox_unreachable_returns_false(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_proxmox = MagicMock()
        mock_proxmox.test_connection = AsyncMock(return_value=False)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = "pve.example.com"

        _setup_state(
            client, db=mock_db, proxmox=mock_proxmox, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "unreachable"
        assert data["status"] == "down"

    def test_proxmox_unreachable_raises(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_proxmox = MagicMock()
        mock_proxmox.test_connection = AsyncMock(side_effect=Exception("timeout"))
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = "pve.example.com"

        _setup_state(
            client, db=mock_db, proxmox=mock_proxmox, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "unreachable"
        assert data["status"] == "down"

    def test_proxmox_ok(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_proxmox = MagicMock()
        mock_proxmox.test_connection = AsyncMock(return_value=True)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = "pve.example.com"

        _setup_state(
            client, db=mock_db, proxmox=mock_proxmox, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "ok"
        assert data["status"] == "ok"

    def test_vault_locked(self, client):
        from homepilot.vault import VaultError

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(side_effect=VaultError("locked"))
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["checks"]["vault"] == "locked"
        assert data["status"] == "down"

    def test_vault_not_configured(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=None, settings=mock_settings)
        # vault=None means not configured, remove it from state
        if hasattr(client.app.state, "vault"):
            del client.app.state.vault
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["vault"] == "not_configured"
        assert data["status"] == "ok"

    def test_db_not_initialized(self, client):
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""
        _setup_state(client, settings=mock_settings)
        # Ensure db is not on state
        if hasattr(client.app.state, "db"):
            del client.app.state.db
        if hasattr(client.app.state, "vault"):
            del client.app.state.vault
        if hasattr(client.app.state, "proxmox"):
            del client.app.state.proxmox
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["checks"]["database"] in ("not initialized", "error")
        assert data["status"] == "down"

    def test_proxmox_not_configured(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "not_configured"

    def test_degraded_when_vault_down_db_ok(self, client):
        from homepilot.vault import VaultError

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(side_effect=VaultError("locked"))
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["vault"] == "locked"
        assert data["checks"]["proxmox"] == "not_configured"

    def test_degraded_returns_503(self, client):
        from homepilot.vault import VaultError

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(side_effect=VaultError("locked"))
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        assert resp.status_code == 503


class TestMetricsEndpoint:
    def test_metrics_returns_200(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_content_type(self, client):
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_metrics_contains_process_info(self, client):
        resp = client.get("/metrics")
        text = resp.text
        assert "process_" in text or "python_" in text

    def test_metrics_exposed_without_auth(self, client):
        resp = client.get("/metrics")
        assert resp.status_code != 401

    def test_metrics_not_rate_limited(self, client):
        for _ in range(65):
            client.get("/metrics")
        resp = client.get("/metrics")
        assert resp.status_code == 200


class TestCORSWildcardGuard:
    def test_wildcard_origin_disables_credentials(self):
        from homepilot.main import validate_cors_config

        result = validate_cors_config(type("S", (), {"cors_origins": "*"})())
        assert result["allow_credentials"] is False
        assert result["misconfigured"] is True

    def test_explicit_origins_keep_credentials(self):
        from homepilot.main import validate_cors_config

        result = validate_cors_config(type("S", (), {"cors_origins": "http://localhost:5173"})())
        assert result["allow_credentials"] is True
        assert result["misconfigured"] is False
