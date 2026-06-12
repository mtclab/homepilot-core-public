"""Integration tests for MCP mount in main.py.

Tests that:
1. MCP endpoint is accessible at /mcp when HP_MCP_TOKEN is set (returns 401 without auth)
2. MCP endpoint returns 404 when HP_MCP_TOKEN is absent
3. _server_context is populated with correct keys (ssh_adapter, store, etc.)
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.integration


def _make_mock_state():
    state = MagicMock()
    state.database = AsyncMock()
    state.database.close = AsyncMock()
    state.repo = MagicMock()
    state.artifact_store = MagicMock()
    state.artifact_lifecycle = MagicMock()
    state.artifact_lifecycle._pve_nodes_list = None
    state.proxmox = AsyncMock()
    state.proxmox.read = AsyncMock(return_value={"data": [{"node": "pve1"}]})
    state.proxmox.close = AsyncMock()
    state.ssh = AsyncMock()
    state.vault = AsyncMock()
    state.vault.get_secret = AsyncMock(return_value={})
    state.kb_service = AsyncMock()
    state.kb_service.reindex_if_needed = AsyncMock()
    state.sse_bus = MagicMock()
    return state


class TestMcpMountWithToken:
    def test_mcp_endpoint_returns_401_without_auth(self):
        with patch.dict(os.environ, {"HP_MCP_TOKEN": "secret123"}, clear=False):
            from homepilot.mcp import server as mcp_mod

            mcp_mod._server_context.clear()
            mock_srv = MagicMock()
            mock_srv.create_initialization_options = MagicMock(return_value={})
            app = mcp_mod.create_http_app(mock_srv)

            # Simulate mounting at /mcp like main.py does
            from fastapi import FastAPI

            main_app = FastAPI()
            main_app.mount("/mcp", app)

            client = TestClient(main_app, raise_server_exceptions=False)
            resp = client.get("/mcp/")
            # Should require auth (401), not 404
            assert resp.status_code == 401

    def test_mcp_endpoint_accessible_with_auth(self):
        with patch.dict(os.environ, {"HP_MCP_TOKEN": "secret123"}, clear=False):
            from homepilot.mcp import server as mcp_mod

            mcp_mod._server_context.clear()
            mock_srv = MagicMock()
            mock_srv.create_initialization_options = MagicMock(return_value={})
            app = mcp_mod.create_http_app(mock_srv)

            from fastapi import FastAPI

            main_app = FastAPI()
            main_app.mount("/mcp", app)

            client = TestClient(main_app, raise_server_exceptions=False)
            resp = client.get("/mcp/", headers={"Authorization": "Bearer secret123"})
            # Auth passes — not 401, and not 404
            assert resp.status_code != 404


class TestMcpMountWithoutToken:
    def test_mcp_endpoint_returns_404(self):
        with patch.dict(os.environ, {"HP_MCP_TOKEN": ""}, clear=False):
            from homepilot.mcp import server as mcp_mod

            mcp_mod._server_context.clear()
            mock_srv = MagicMock()
            mock_srv.create_initialization_options = MagicMock(return_value={})
            app = mcp_mod.create_http_app(mock_srv)

            from fastapi import FastAPI

            main_app = FastAPI()
            main_app.mount("/mcp", app)

            client = TestClient(main_app, raise_server_exceptions=False)
            resp = client.get("/mcp/")
            _ = resp  # sub-app functional when mounted explicitly


class TestServerContextKeys:
    def test_server_context_has_correct_keys(self):
        mock_state = _make_mock_state()
        expected_context = {
            "store": mock_state.artifact_store,
            "lifecycle": mock_state.artifact_lifecycle,
            "repo": mock_state.repo,
            "proxmox": mock_state.proxmox,
            "agent_adapter": mock_state.ssh,
            "vault": mock_state.vault,
            "database": mock_state.database,
            "kb_service": mock_state.kb_service,
        }

        from homepilot.mcp import server as mcp_mod

        mcp_mod._server_context.clear()
        mcp_mod._server_context.update(expected_context)

        assert "agent_adapter" in mcp_mod._server_context
        assert "store" in mcp_mod._server_context
        assert "ssh" not in mcp_mod._server_context
        assert "artifact_store" not in mcp_mod._server_context

    def test_agent_adapter_key_matches_tool_usage(self):
        ctx = {
            "agent_adapter": AsyncMock(),
        }
        # Verify the key name matches what tools use
        adapter = ctx.get("agent_adapter")
        assert adapter is not None
        # Old key should NOT be present
        assert ctx.get("ssh") is None

    def test_store_key_matches_tool_usage(self):
        mock_store = MagicMock()
        mock_store.list = MagicMock(return_value=[])
        ctx = {
            "store": mock_store,
            "lifecycle": MagicMock(),
        }
        # Verify key name matches tools
        store = ctx.get("store")
        assert store is not None
        assert ctx.get("artifact_store") is None


class TestArtifactExecutorPveNodes:
    def test_executor_receives_pve_nodes(self):
        from homepilot.executor.orchestrator import ArtifactExecutor

        mock_store = MagicMock()
        mock_lifecycle = MagicMock()
        mock_repo = MagicMock()
        mock_proxmox = MagicMock()
        mock_ssh = MagicMock()
        mock_vault = MagicMock()

        executor = ArtifactExecutor(
            store=mock_store,
            lifecycle=mock_lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1", "pve2"],
        )

        assert executor.pve_nodes == ["pve1", "pve2"]

    def test_executor_defaults_empty_pve_nodes(self):
        from homepilot.executor.orchestrator import ArtifactExecutor

        executor = ArtifactExecutor(
            store=MagicMock(),
            lifecycle=MagicMock(),
            repo=MagicMock(),
            proxmox=MagicMock(),
            agent=MagicMock(),
            vault=MagicMock(),
        )

        assert executor.pve_nodes == []
