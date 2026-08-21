"""Tests for the startup route-scope guard (#376 item 3).

(a) Every non-public API route must carry a scope dependency; the guard fails
    fast if one does not. Proven with teeth: an unscoped route IS flagged.
(b) The read routes that previously accepted any valid token now require the
    'read' scope — a read-scoped token reaches them, an empty-scope token does
    not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_scope, require_token
from homepilot.main import (
    app as real_app,
)
from homepilot.main import (
    assert_all_routes_scoped,
    find_unscoped_routes,
)


class TestRealAppFullyScoped:
    def test_real_app_has_no_unscoped_routes(self):
        # The shipped app must be clean — no non-public route without a scope dep.
        assert find_unscoped_routes(real_app) == []

    def test_the_guard_actually_sees_the_api(self):
        """An empty finding must mean "nothing is wrong", never "nothing was checked".

        FastAPI 0.137 stopped flattening `include_router` into the parent's route
        list, which dropped what this guard inspects from 81 routes to 5 - every
        route behind an include, i.e. essentially the whole API - while
        `find_unscoped_routes` still returned `[]`. The suite stayed green because
        the teeth tests below build routes with a bare `@app.get`, a shape the
        real app never uses.

        So the count is asserted, not just the verdict. The floor is deliberately
        far below the current number: this catches a guard going blind, not a
        release that adds or removes a handful of routes.
        """
        from fastapi.routing import APIRoute

        inspected = [route for route in real_app.routes if isinstance(route, APIRoute)]
        assert len(inspected) > 50, (
            f"the scope guard can only see {len(inspected)} routes on the shipped app - "
            "it is walking a route list that no longer holds the API, so its clean "
            "verdict means nothing (see the fastapi bound in pyproject.toml)"
        )

    def test_a_route_added_by_include_router_is_inspected(self):
        """The real app is built entirely from include_router, so the guard has to
        work on THAT shape - the gap the version bump walked through."""
        from fastapi import APIRouter

        app = FastAPI()
        router = APIRouter()

        @router.get("/danger")
        async def danger():  # forgot its scope dep, and lives behind an include
            return {"ok": True}

        app.include_router(router, prefix="/nested")

        assert ("GET", "/nested/danger") in find_unscoped_routes(app)


class TestGuardHasTeeth:
    def test_unscoped_route_is_flagged(self):
        app = FastAPI()

        @app.get("/danger")
        async def danger():  # a new route that forgot its scope dep
            return {"ok": True}

        missing = find_unscoped_routes(app)
        assert ("GET", "/danger") in missing

    def test_assert_raises_on_unscoped_route(self):
        app = FastAPI()

        @app.get("/danger")
        async def danger():
            return {"ok": True}

        with pytest.raises(RuntimeError, match="no scope dependency"):
            assert_all_routes_scoped(app)

    def test_scoped_route_is_not_flagged(self):
        app = FastAPI()

        @app.get("/safe", dependencies=[Depends(require_scope("read"))])
        async def safe():
            return {"ok": True}

        assert find_unscoped_routes(app) == []
        assert_all_routes_scoped(app)  # does not raise


def _token(scope, role=None):
    def _dep():
        return {
            "user_id": "1",
            "token_id": "1",
            "scope": scope,
            "role": role,
            "display_name": "t",
        }

    return _dep


class TestReadRoutesEnforceReadScope:
    """The inventory read routes now require the 'read' scope, not merely a
    valid token."""

    @pytest.fixture
    def app(self):
        from homepilot.inventory.router import router as inventory_router

        app = FastAPI()
        app.include_router(inventory_router, prefix="/inventory")
        app.state.repo = MagicMock()
        app.state.repo.list_hosts = AsyncMock(return_value=[])
        app.state.inventory_service = MagicMock()
        return app

    def test_read_scope_reaches_read_route(self, app):
        app.dependency_overrides[require_token] = _token("read_only")
        client = TestClient(app)
        try:
            resp = client.get("/inventory")
            assert resp.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_empty_scope_denied(self, app):
        app.dependency_overrides[require_token] = _token(None, None)
        client = TestClient(app)
        try:
            resp = client.get("/inventory")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()

    def test_empty_scope_denied_on_get_host(self, app):
        app.state.repo.get_host = AsyncMock(return_value={"id": "h1", "hostname": "vm1"})
        app.dependency_overrides[require_token] = _token(None, None)
        client = TestClient(app)
        try:
            resp = client.get("/inventory/h1")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()
