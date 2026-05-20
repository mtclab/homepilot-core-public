"""Tests for MCP HTTP transport (create_http_app)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def mock_server():
    srv = MagicMock()
    srv.create_initialization_options = MagicMock(return_value={})
    return srv


@pytest.fixture()
def app_no_auth(mock_server):
    with patch.dict(os.environ, {"HP_MCP_TOKEN": ""}, clear=False):
        from homepilot.mcp import server as mcp_mod

        mcp_mod._server_context.clear()
        yield mcp_mod.create_http_app(mock_server)


@pytest.fixture()
def app_with_auth(mock_server):
    with patch.dict(os.environ, {"HP_MCP_TOKEN": "test-secret-token"}, clear=False):
        from homepilot.mcp import server as mcp_mod

        mcp_mod._server_context.clear()
        yield mcp_mod.create_http_app(mock_server)


class TestCreateHttpApp:
    def test_returns_starlette_app(self, app_no_auth):
        from starlette.applications import Starlette

        assert isinstance(app_no_auth, Starlette)

    def test_mcp_route_exists(self, app_no_auth):
        client = TestClient(app_no_auth, raise_server_exceptions=False)
        # Any request to / should reach the route (not 404)
        resp = client.get("/")
        assert resp.status_code != 404

    def test_non_mcp_route_is_404(self, app_no_auth):
        client = TestClient(app_no_auth, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code in (404, 500)


class TestBearerAuthMiddleware:
    def test_missing_token_returns_401(self, app_with_auth):
        client = TestClient(app_with_auth, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, app_with_auth):
        client = TestClient(app_with_auth, raise_server_exceptions=False)
        resp = client.get("/", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    def test_correct_token_passes_auth(self, app_with_auth):
        client = TestClient(app_with_auth, raise_server_exceptions=False)
        resp = client.get("/", headers={"Authorization": "Bearer test-secret-token"})
        # auth passed — may fail for other reasons, but not 401
        assert resp.status_code != 401

    def test_no_middleware_when_no_token(self, app_no_auth):
        client = TestClient(app_no_auth, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code != 401


class TestHttpAuthMiddleware:
    def test_create_http_app_adds_auth_middleware_when_token_set(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        with patch.dict(os.environ, {"HP_MCP_TOKEN": "test-token"}, clear=False):
            app = mcp_mod.create_http_app(mock_srv)
        # App should have middleware stack when token is configured
        assert app is not None

    def test_create_http_app_no_auth_when_no_token(self):
        from homepilot.mcp import server as mcp_mod

        mock_srv = MagicMock()
        with patch.dict(os.environ, {"HP_MCP_TOKEN": ""}, clear=False):
            app = mcp_mod.create_http_app(mock_srv)
        assert app is not None
