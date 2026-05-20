"""Tests for auth batch4 issues: admin/reload-secrets, MCP server auth,
inventory PATCH, and KB delete endpoints."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.router import router as auth_router
from homepilot.auth.tokens import PREFIX_LENGTH, generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


@pytest_asyncio.fixture()
async def auth_app(tmp_path: Path):
    db = Database(str(tmp_path / "batch4_test.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    user_id = await repo.create_user("admin", "admin@test.com")
    full_token, prefix, token_hash = generate_api_token()
    await repo.create_api_token(
        user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
    )
    await db.conn.commit()

    app = FastAPI()
    app.state.repo = repo
    app.include_router(auth_router, prefix="/auth")

    client = TestClient(app, raise_server_exceptions=True)
    yield client, full_token, repo
    await db.close()


class TestAdminReloadSecrets:
    @pytest.mark.asyncio
    async def test_reload_secrets_valid_admin(self, tmp_path: Path):
        db = Database(str(tmp_path / "reload_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )
        await db.conn.commit()

        from homepilot.admin.router import router as admin_router

        app = FastAPI()
        app.state.repo = repo
        app.state.settings = MagicMock()
        app.state.settings.proxmox_host = ""
        app.include_router(admin_router, prefix="/admin")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/admin/reload-secrets",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "already_running", "error")
        await db.close()

    @pytest.mark.asyncio
    async def test_reload_secrets_invalid_token(self, tmp_path: Path):
        db = Database(str(tmp_path / "reload_401_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.admin.router import router as admin_router

        app = FastAPI()
        app.state.repo = repo
        app.include_router(admin_router, prefix="/admin")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/admin/reload-secrets")
        assert resp.status_code in (401, 403)
        await db.close()

    @pytest.mark.asyncio
    async def test_reload_secrets_read_only_scope_denied(self, tmp_path: Path):
        db = Database(str(tmp_path / "reload_403_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        user_id = await repo.create_user("reader", "reader@test.com")
        reader_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="read_only"
        )
        await db.conn.commit()

        from homepilot.admin.router import router as admin_router

        app = FastAPI()
        app.state.repo = repo
        app.include_router(admin_router, prefix="/admin")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/admin/reload-secrets",
            headers={"Authorization": f"Bearer {reader_token}"},
        )
        assert resp.status_code == 403
        await db.close()


class TestMCPServerAuth:
    def test_mcp_http_with_valid_bearer(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        mock_srv.create_initialization_options = MagicMock(return_value={})
        with patch.dict(os.environ, {"HP_MCP_TOKEN": "valid-mcp-token"}, clear=False):
            mcp_mod._server_context.clear()
            app = mcp_mod.create_http_app(mock_srv)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"Authorization": "Bearer valid-mcp-token"})
        assert resp.status_code != 401

    def test_mcp_http_with_invalid_bearer(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        mock_srv.create_initialization_options = MagicMock(return_value={})
        with patch.dict(os.environ, {"HP_MCP_TOKEN": "valid-mcp-token"}, clear=False):
            mcp_mod._server_context.clear()
            app = mcp_mod.create_http_app(mock_srv)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    def test_mcp_http_without_token_configured(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        mock_srv.create_initialization_options = MagicMock(return_value={})
        with patch.dict(os.environ, {"HP_MCP_TOKEN": ""}, clear=False):
            mcp_mod._server_context.clear()
            app = mcp_mod.create_http_app(mock_srv)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code != 401

    def test_mcp_http_missing_auth_header(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        mock_srv.create_initialization_options = MagicMock(return_value={})
        with patch.dict(os.environ, {"HP_MCP_TOKEN": "valid-mcp-token"}, clear=False):
            mcp_mod._server_context.clear()
            app = mcp_mod.create_http_app(mock_srv)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 401


class TestInventoryPatch:
    @pytest.mark.asyncio
    async def test_patch_managed_field(self, tmp_path: Path):
        db = Database(str(tmp_path / "inv_patch_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.inventory.router import router as inv_router

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )

        host_id = await repo.create_host(hostname="pve1", host_type="node", role="node")
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.state.inventory_service = MagicMock()
        app.include_router(inv_router, prefix="/inventory")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/inventory/{host_id}",
            json={"managed": True},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["managed"] == 1 or data["managed"] is True
        await db.close()

    @pytest.mark.asyncio
    async def test_patch_no_fields_returns_400(self, tmp_path: Path):
        db = Database(str(tmp_path / "inv_patch_400_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.inventory.router import router as inv_router

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )

        host_id = await repo.create_host(hostname="pve2", host_type="node", role="node")
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.state.inventory_service = MagicMock()
        app.include_router(inv_router, prefix="/inventory")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/inventory/{host_id}",
            json={},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 400
        await db.close()

    @pytest.mark.asyncio
    async def test_patch_nonexistent_host_returns_404(self, tmp_path: Path):
        db = Database(str(tmp_path / "inv_patch_404_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.inventory.router import router as inv_router

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.state.inventory_service = MagicMock()
        app.include_router(inv_router, prefix="/inventory")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            "/inventory/nonexistent-id",
            json={"managed": True},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 404
        await db.close()

    @pytest.mark.asyncio
    async def test_patch_tags_field(self, tmp_path: Path):
        db = Database(str(tmp_path / "inv_patch_tags_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.inventory.router import router as inv_router

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )

        host_id = await repo.create_host(hostname="pve3", host_type="node", role="node")
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.state.inventory_service = MagicMock()
        app.include_router(inv_router, prefix="/inventory")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/inventory/{host_id}",
            json={"tags": "prod,monitor"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        await db.close()


class TestKBDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_kb_entry(self, tmp_path: Path):
        db = Database(str(tmp_path / "kb_del_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.artifacts.lifecycle import ArtifactLifecycle
        from homepilot.artifacts.store import ArtifactStore
        from homepilot.kb.router import router as kb_router
        from homepilot.kb.service import KBService

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        store = ArtifactStore(artifact_dir)
        lc = ArtifactLifecycle(store, repo)
        kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )

        doc_id = await repo.create_doc_metadata(
            source="test:delete", title="Delete me", content="content", kind="note"
        )
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.state.kb_service = kb_svc
        app.include_router(kb_router, prefix="/kb")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(
            f"/kb/{doc_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 204
        await db.close()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_kb_entry(self, tmp_path: Path):
        db = Database(str(tmp_path / "kb_del_404_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.artifacts.lifecycle import ArtifactLifecycle
        from homepilot.artifacts.store import ArtifactStore
        from homepilot.kb.router import router as kb_router
        from homepilot.kb.service import KBService

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        store = ArtifactStore(artifact_dir)
        lc = ArtifactLifecycle(store, repo)
        kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.state.kb_service = kb_svc
        app.include_router(kb_router, prefix="/kb")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(
            "/kb/99999",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 404
        await db.close()

    @pytest.mark.asyncio
    async def test_delete_requires_admin_scope(self, tmp_path: Path):
        db = Database(str(tmp_path / "kb_del_403_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        from homepilot.artifacts.lifecycle import ArtifactLifecycle
        from homepilot.artifacts.store import ArtifactStore
        from homepilot.kb.router import router as kb_router
        from homepilot.kb.service import KBService

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        store = ArtifactStore(artifact_dir)
        lc = ArtifactLifecycle(store, repo)
        kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

        user_id = await repo.create_user("reader", "reader@test.com")
        reader_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="read_only"
        )
        await db.conn.commit()

        doc_id = await repo.create_doc_metadata(
            source="test:delete2", title="Delete me too", content="content", kind="note"
        )
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.state.kb_service = kb_svc
        app.include_router(kb_router, prefix="/kb")
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(
            f"/kb/{doc_id}",
            headers={"Authorization": f"Bearer {reader_token}"},
        )
        assert resp.status_code == 403
        await db.close()


class TestTokenPrefixLength:
    def test_prefix_is_16_chars(self):
        assert PREFIX_LENGTH == 16
        _full_token, prefix, _ = generate_api_token()
        assert len(prefix) == 16
        assert prefix.startswith("hp_")

    def test_prefix_has_sufficient_entropy(self):
        _full_token, prefix, _ = generate_api_token()
        hex_part = prefix[3:]
        assert len(hex_part) == 13
