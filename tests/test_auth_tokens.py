"""Tests for token scope enforcement — read_only vs full."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_scope, require_token
from homepilot.auth.tokens import (
    SCOPE_ALL,
    SCOPE_FULL_LEGACY,
    SCOPE_READ_ONLY,
    generate_api_token,
    normalize_scope,
    scope_allows,
)
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


class TestNormalizeScope:
    def test_all_normalizes_to_star(self):
        assert normalize_scope(SCOPE_ALL) == ["*"]

    def test_legacy_full_normalizes_to_star_forever(self):
        """#579: 'full' was the old spelling of 'all'. Existing tokens and
        scripts carry it, so it must keep normalizing to '*' for as long as
        this function exists - removing the alias bricks every token minted
        before the rename."""
        assert normalize_scope(SCOPE_FULL_LEGACY) == ["*"]

    def test_read_only_normalizes_to_read(self):
        assert normalize_scope(SCOPE_READ_ONLY) == ["read"]

    def test_none_returns_empty(self):
        assert normalize_scope(None) == []

    def test_empty_returns_empty(self):
        assert normalize_scope("") == []

    def test_star_normalizes_to_star(self):
        assert normalize_scope("*") == ["*"]

    def test_comma_separated(self):
        assert normalize_scope("read,write") == ["read", "write"]


class TestScopeAllows:
    def test_full_allows_write(self):
        assert scope_allows(SCOPE_FULL_LEGACY, "write") is True

    def test_full_allows_read(self):
        assert scope_allows(SCOPE_FULL_LEGACY, "read") is True

    def test_read_only_allows_read(self):
        assert scope_allows(SCOPE_READ_ONLY, "read") is True

    def test_read_only_blocks_write(self):
        assert scope_allows(SCOPE_READ_ONLY, "write") is False

    def test_none_blocks_everything(self):
        assert scope_allows(None, "read") is False

    def test_star_allows_write(self):
        assert scope_allows("*", "write") is True

    def test_comma_scope_allows_write(self):
        assert scope_allows("read,write", "write") is True


@pytest_asyncio.fixture()
async def scope_app(tmp_path):
    db = Database(str(tmp_path / "scope_test.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    app = FastAPI()
    app.state.repo = repo

    @app.get("/read", dependencies=[Depends(require_scope("read"))])
    async def read_endpoint(token=Depends(require_token)):  # noqa: B008
        return {"ok": True}

    @app.post("/write", dependencies=[Depends(require_scope("write"))])
    async def write_endpoint(token=Depends(require_token)):  # noqa: B008
        return {"ok": True}

    yield app, repo
    await db.close()


class TestReadOnlyTokenBlockedOnMutatingTools:
    @pytest.mark.asyncio
    async def test_read_only_token_blocked_on_write_scope(self, scope_app):
        app, repo = scope_app
        user_id = await repo.create_user("reader", "reader@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="read_only",
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/write", headers={"Authorization": f"Bearer {full_token}"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_read_only_token_blocked_on_write_with_comma_scope(self, scope_app):
        app, repo = scope_app
        user_id = await repo.create_user("reader2", "reader2@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="read",
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/write", headers={"Authorization": f"Bearer {full_token}"})
        assert resp.status_code == 403


class TestReadOnlyTokenAllowedOnReadOnlyTools:
    @pytest.mark.asyncio
    async def test_read_only_token_allowed_on_read_scope(self, scope_app):
        app, repo = scope_app
        user_id = await repo.create_user("reader3", "reader3@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="read_only",
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/read", headers={"Authorization": f"Bearer {full_token}"})
        assert resp.status_code == 200


class TestFullTokenAllowedOnAllTools:
    @pytest.mark.asyncio
    async def test_full_token_allowed_on_write(self, scope_app):
        app, repo = scope_app
        user_id = await repo.create_user("writer", "writer@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="full",
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/write", headers={"Authorization": f"Bearer {full_token}"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_full_token_allowed_on_read(self, scope_app):
        app, repo = scope_app
        user_id = await repo.create_user("writer2", "writer2@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="full",
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/read", headers={"Authorization": f"Bearer {full_token}"})
        assert resp.status_code == 200


class TestMCPToolScopeEnforcement:
    def test_mcp_read_only_scope_blocks_mutating_tool(self):
        from homepilot.mcp.server import _MUTATING_TOOLS

        assert "propose_artifact" in _MUTATING_TOOLS
        assert "record_fact" in _MUTATING_TOOLS

    def test_mcp_read_only_tools_listed(self):
        from homepilot.mcp.server import _READ_ONLY_TOOLS

        for tool in (
            "query_inventory",
            "search_kb",
            "query_artifacts",
            "get_environment_doc",
            "proxmox_api_read",
            "http_call_read",
            "read_file_on_guest",
            "exec_on_guest_readonly",
            "check_artifact_drift",
        ):
            assert tool in _READ_ONLY_TOOLS


class TestMCPHttpScopeMiddleware:
    def test_read_only_env_uses_contextvar(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        mock_srv.create_initialization_options = MagicMock(return_value={})
        with patch.dict(
            os.environ,
            {"HP_MCP_TOKEN": "test-ro-token", "HP_MCP_TOKEN_SCOPE": "read_only"},
            clear=False,
        ):
            mcp_mod._server_context.clear()
            _app = mcp_mod.create_http_app(mock_srv)
            assert "_mcp_token_scope" not in mcp_mod._server_context
            assert "_mcp_caller_id" not in mcp_mod._server_context

    def test_full_env_set(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        mock_srv.create_initialization_options = MagicMock(return_value={})
        with patch.dict(
            os.environ,
            {"HP_MCP_TOKEN": "test-full-token", "HP_MCP_TOKEN_SCOPE": "full"},
            clear=False,
        ):
            mcp_mod._server_context.clear()
            app = mcp_mod.create_http_app(mock_srv)
            assert app is not None

    def test_default_scope_is_full(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        mock_srv.create_initialization_options = MagicMock(return_value={})
        with patch.dict(os.environ, {"HP_MCP_TOKEN": "test-default-token"}, clear=False):
            mcp_mod._server_context.clear()
            app = mcp_mod.create_http_app(mock_srv)
            assert app is not None

    def test_contextvar_default_is_full(self):
        from homepilot.mcp.server import _mcp_token_scope_var

        assert _mcp_token_scope_var.get() == "full"

    def test_contextvar_set_propagates_to_handle_tool(self):
        from homepilot.mcp.server import _handle_tool, _mcp_token_scope_var

        async def _test():
            _mcp_token_scope_var.set("read_only")
            try:
                with pytest.raises(ValueError, match="write scope"):
                    await _handle_tool(
                        "propose_artifact",
                        {"spec": {"id": "test", "kind": "shell-script", "intent": "test"}},
                        {"lifecycle": MagicMock(), "store": MagicMock()},
                    )
            finally:
                _mcp_token_scope_var.set("full")

        asyncio.run(_test())

    def test_contextvar_isolation(self):
        from homepilot.mcp.server import _mcp_token_scope_var

        async def _task(scope: str) -> str:
            _mcp_token_scope_var.set(scope)
            await asyncio.sleep(0.01)
            return _mcp_token_scope_var.get()

        async def _test():
            _mcp_token_scope_var.set("full")
            results = await asyncio.gather(
                _task("read_only"),
                _task("admin"),
            )
            assert results == ["read_only", "admin"]
            assert _mcp_token_scope_var.get() == "full"

        asyncio.run(_test())
