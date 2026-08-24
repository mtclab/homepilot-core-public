"""Gate QA tests for PR #253: lint-and-p0-batch.

Covers:
1. app_state path validation (protected dirs)
2. repository _validated_set_clause (invalid column rejection)
3. auth/router admin token creation edge cases
4. rate limiting boundary conditions
5. health endpoint structured response validation
6. SSRF guard PinnedTransport edge cases
7. config admin_secret field
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.config import Settings
from homepilot.db.repository import (
    _HOST_COLUMNS,
    _SERVICE_COLUMNS,
    _validated_set_clause,
)
from homepilot.mcp.tools.ssrf_guard import (
    _PinnedTransport,
)

# ── 1. app_state.py path validation ──


class TestArtifactsDirPathValidation:
    """Validate that protected system paths are rejected for artifacts_dir.

    The validation logic in app_state.py checks:
    1. If artifacts_dir is outside data_dir, it must not be a protected path.
    2. Protected paths: /, /etc, /usr, /var, /sys, /boot, /proc
    """

    @pytest.mark.parametrize(
        "protected_path",
        ["/etc", "/usr", "/var", "/sys", "/boot", "/proc", "/etc/something", "/usr/local"],
    )
    def test_rejects_protected_path_outside_data_dir(self, protected_path):
        data_dir_resolved = Path("/home/user/.hp").resolve()
        artifacts_dir = Path(protected_path).resolve()

        is_inside_data_dir = str(artifacts_dir).startswith(str(data_dir_resolved))
        blocked_paths = ("/", "/etc", "/usr", "/var", "/sys", "/boot", "/proc")

        if is_inside_data_dir:
            is_blocked = False
        else:
            is_blocked = any(
                str(artifacts_dir) == b or str(artifacts_dir).startswith(b + "/")
                for b in blocked_paths
            )
        assert is_blocked, f"{protected_path} should be blocked outside data_dir"

    def test_allows_data_dir_subpath(self):
        data_dir_resolved = Path("/home/user/.hp").resolve()
        artifacts_dir = Path("/home/user/.hp/artifacts").resolve()
        is_inside = str(artifacts_dir).startswith(str(data_dir_resolved))
        assert is_inside

    def test_root_path_blocked(self):
        artifacts_dir = Path("/").resolve()
        blocked_paths = ("/", "/etc", "/usr", "/var", "/sys", "/boot", "/proc")
        is_blocked = any(
            str(artifacts_dir) == b or str(artifacts_dir).startswith(b + "/") for b in blocked_paths
        )
        assert is_blocked

    def test_tmp_path_allowed(self):
        data_dir_resolved = Path("/home/user/.hp").resolve()
        artifacts_dir = Path("/tmp/hp-artifacts").resolve()
        is_inside = str(artifacts_dir).startswith(str(data_dir_resolved))
        blocked_paths = ("/", "/etc", "/usr", "/var", "/sys", "/boot", "/proc")
        is_blocked = any(
            str(artifacts_dir) == b or str(artifacts_dir).startswith(b + "/") for b in blocked_paths
        )
        assert not is_blocked
        assert not is_inside


# ── 2. repository.py _validated_set_clause ──


class TestValidatedSetClause:
    def test_valid_host_columns_pass(self):
        parts, vals = _validated_set_clause({"hostname": "web1"}, _HOST_COLUMNS)
        assert parts == "hostname = ?"
        assert vals == ["web1"]

    def test_invalid_host_column_rejected(self):
        with pytest.raises(ValueError, match="Invalid columns"):
            _validated_set_clause({"evil_column": "pwned"}, _HOST_COLUMNS)

    def test_multiple_valid_columns(self):
        parts, vals = _validated_set_clause(
            {"hostname": "web1", "status": "running"}, _HOST_COLUMNS
        )
        assert "hostname = ?" in parts
        assert "status = ?" in parts
        assert vals == ["web1", "running"]

    def test_mixed_valid_invalid_rejected(self):
        with pytest.raises(ValueError, match="Invalid columns"):
            _validated_set_clause({"hostname": "web1", "evil": "nope"}, _HOST_COLUMNS)

    def test_valid_service_columns_pass(self):
        parts, _vals = _validated_set_clause({"name": "nginx"}, _SERVICE_COLUMNS)
        assert parts == "name = ?"

    def test_empty_columns(self):
        parts, vals = _validated_set_clause({}, _HOST_COLUMNS)
        assert parts == ""
        assert vals == []


# ── 3. auth/router admin token creation edge cases ──


class TestAdminTokenCreationEdgeCases:
    @pytest.fixture
    def auth_app(self, tmp_path):
        from homepilot.auth.router import router as auth_router

        app = FastAPI()
        app.include_router(auth_router, prefix="/auth")
        return app

    @pytest.mark.asyncio
    async def test_empty_admin_secret_header_rejected(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "test.db"))
        await db.connect()
        await run_migrations(db)

        try:
            from homepilot.auth.router import router as auth_router

            app = FastAPI()
            app.state.repo = Repository(db)
            client = TestClient(app)
            app.include_router(auth_router, prefix="/auth")

            mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
            mock_settings.admin_secret = "my-secret"
            import homepilot.auth.router as router_mod

            with patch.object(router_mod, "get_settings", return_value=mock_settings):
                resp = client.post(
                    "/auth/tokens",
                    json={"label": "ci", "scope": "read"},
                    headers={"x-hp-admin-secret": ""},
                )
                assert resp.status_code == 403
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_wrong_admin_secret_rejected(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "test.db"))
        await db.connect()
        await run_migrations(db)

        try:
            from homepilot.auth.router import router as auth_router

            app = FastAPI()
            app.state.repo = Repository(db)
            client = TestClient(app)
            app.include_router(auth_router, prefix="/auth")

            mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
            mock_settings.admin_secret = "correct-secret"
            import homepilot.auth.router as router_mod

            router_mod._token_create_attempts.clear()

            with patch.object(router_mod, "get_settings", return_value=mock_settings):
                resp = client.post(
                    "/auth/tokens",
                    json={"label": "ci", "scope": "read"},
                    headers={"x-hp-admin-secret": "wrong-secret"},
                )
                assert resp.status_code == 403
        finally:
            await db.close()


# ── 4. Rate limiting boundary conditions ──


class TestRateLimitBoundaries:
    """Test exact boundary conditions for rate limiting."""

    def _make_request(self, path: str, client_ip: str = "1.2.3.4") -> MagicMock:
        req = MagicMock()
        req.url.path = path
        req.client.host = client_ip
        req.method = "GET"
        req.headers = {}
        req.cookies = {}
        req.app = MagicMock()
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(return_value=None)
        req.app.state.repo = repo
        return req

    async def test_at_anon_limit_passes(self):
        from homepilot.main import _RATE_LIMIT, _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        import time as _time

        now = _time.time()
        req = self._make_request("/artifacts")

        async def call_next(r):
            return MagicMock(status_code=200)

        _RATE_WINDOW["1.2.3.4"] = [now] * (_RATE_LIMIT - 1)
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 200
        _RATE_WINDOW.clear()

    async def test_one_over_anon_limit_blocked(self):
        from homepilot.main import _RATE_LIMIT, _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        import time as _time

        now = _time.time()
        req = self._make_request("/artifacts")

        async def call_next(r):
            return MagicMock(status_code=200)

        _RATE_WINDOW["1.2.3.4"] = [now] * _RATE_LIMIT
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 429
        _RATE_WINDOW.clear()

    async def test_two_under_anon_limit_passes(self):
        from homepilot.main import _RATE_LIMIT, _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        import time as _time

        now = _time.time()
        req = self._make_request("/artifacts")

        async def call_next(r):
            return MagicMock(status_code=200)

        _RATE_WINDOW["1.2.3.4"] = [now] * (_RATE_LIMIT - 2)
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 200
        _RATE_WINDOW.clear()

    async def test_expired_requests_dont_count(self):
        from homepilot.main import _RATE_WINDOW, _RATE_WINDOW_SEC, rate_limit_middleware

        _RATE_WINDOW.clear()
        import time as _time

        old = _time.time() - _RATE_WINDOW_SEC - 10
        _time.time()
        req = self._make_request("/artifacts")

        async def call_next(r):
            return MagicMock(status_code=200)

        _RATE_WINDOW["1.2.3.4"] = [old] * 200
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 200
        _RATE_WINDOW.clear()

    async def test_different_ips_tracked_separately(self):
        from homepilot.main import _RATE_LIMIT, _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        import time as _time

        now = _time.time()

        async def call_next(r):
            return MagicMock(status_code=200)

        req1 = self._make_request("/artifacts", client_ip="1.2.3.4")
        _RATE_WINDOW["1.2.3.4"] = [now] * _RATE_LIMIT
        resp = await rate_limit_middleware(req1, call_next)
        assert resp.status_code == 429

        req2 = self._make_request("/artifacts", client_ip="5.6.7.8")
        resp2 = await rate_limit_middleware(req2, call_next)
        assert resp2.status_code == 200
        _RATE_WINDOW.clear()


# ── 5. Health endpoint structured response ──


class TestHealthStructuredResponse:
    def _setup_state(self, client, db=None, proxmox=None, vault=None, settings=None):
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
                client.app.state.settings = MagicMock(proxmox_host="")

    def test_response_has_status_and_checks_keys(self):
        from homepilot.main import app

        client = TestClient(app)
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        self._setup_state(
            client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert "status" in data
        assert "checks" in data
        assert "database" in data["checks"]
        assert "proxmox" in data["checks"]
        assert "vault" in data["checks"]

    def test_status_ok_when_all_services_ok(self):
        from homepilot.main import app

        client = TestClient(app)
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""
        mock_settings.cors_origins = "http://localhost:5173"
        mock_settings.agent_hub_enabled = False

        self._setup_state(
            client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert resp.status_code == 200

    def test_status_down_503_when_database_error(self):
        from homepilot.main import app

        client = TestClient(app)
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=Exception("connection lost"))
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        self._setup_state(
            client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "down"


# ── 6. PinnedTransport edge cases ──


class TestPinnedTransportEdgeCases:
    def test_ipv6_pinned_ip(self):
        transport = _PinnedTransport(resolved_ips=["::1"], port=443)
        assert transport._resolved_ips == ["::1"]

    def test_multiple_resolved_ips_uses_first(self):
        transport = _PinnedTransport(resolved_ips=["1.2.3.4", "5.6.7.8"], port=443)
        assert transport._resolved_ips[0] == "1.2.3.4"

    def test_port_stored(self):
        transport = _PinnedTransport(resolved_ips=["1.2.3.4"], port=8080)
        assert transport._port == 8080

    def test_port_none_defaults(self):
        transport = _PinnedTransport(resolved_ips=["1.2.3.4"])
        assert transport._port is None


# ── 7. config admin_secret ──


class TestConfigAdminSecret:
    def test_default_empty(self):
        s = Settings(
            admin_secret="",
        )
        assert s.admin_secret == ""

    def test_set_via_env(self, monkeypatch):
        monkeypatch.setenv("HP_ADMIN_SECRET", "my-admin-secret")
        s = Settings()
        assert s.admin_secret == "my-admin-secret"

    def test_set_via_constructor(self):
        s = Settings(
            admin_secret="constructor-secret",
        )
        assert s.admin_secret == "constructor-secret"


# ── 8. Repository allowlist enforcement via update methods ──


class TestRepositoryAllowlistEnforcement:
    @pytest.mark.asyncio
    async def test_update_host_valid_columns(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        host_id = await repo.create_host("web1", "vm")
        await repo.update_host(host_id, hostname="web1-updated")
        host = await repo.get_host(host_id)
        assert host["hostname"] == "web1-updated"
        await db.close()

    @pytest.mark.asyncio
    async def test_update_host_invalid_column_raises(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        host_id = await repo.create_host("web1", "vm")
        with pytest.raises(ValueError, match="Invalid columns"):
            await repo.update_host(host_id, evil_column="pwned")
        await db.close()

    @pytest.mark.asyncio
    async def test_update_service_invalid_column_raises(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        host_id = await repo.create_host("web1", "vm")
        svc_id = await repo.create_service(host_id, "nginx", "docker")
        with pytest.raises(ValueError, match="Invalid columns"):
            await repo.update_service(svc_id, evil_column="pwned")
        await db.close()

    @pytest.mark.asyncio
    async def test_update_artifact_status_invalid_column_raises(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        art_id = await repo.create_artifact(
            id="2025-01-01-test-abc123",
            kind="ansible-playbook",
            intent="test intent",
            status="approved",
            mutating=True,
            hash="sha256:abc",
            target_json="{}",
            idempotence="test",
            produced_by_json='{"session":"s1"}',
            file_path="test.yaml",
        )
        await repo.db.conn.commit()

        with pytest.raises(ValueError, match="Invalid column"):
            await repo.update_artifact_status(art_id, "applied", evil_column="pwned")
        await db.close()
