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

from homepilot import main
from homepilot.auth.deps import require_scope, require_token
from homepilot.main import (
    _walk_api_routes,
    assert_all_routes_scoped,
    find_unscoped_routes,
)
from homepilot.main import (
    app as real_app,
)

# FastAPI < 0.137 flattens include_router into the parent's route list; 0.137+
# keeps an _IncludedRouter wrapper. The guard handles both shapes, but the two
# teeth tests below describe the WRAPPER shape specifically (there is nothing to
# descend into, and no separate include-time dependencies, when the framework
# has already flattened everything), so they are skipped on the older shape
# rather than silently asserting nothing.
_FASTAPI_KEEPS_INCLUDE_WRAPPERS = any(
    main._include_wrapper_parts(route) is not None for route in real_app.routes
)
_wrapper_shape_only = pytest.mark.skipif(
    not _FASTAPI_KEEPS_INCLUDE_WRAPPERS,
    reason="this FastAPI flattens include_router; the wrapper-shape teeth do not apply",
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
        real app never uses. #472 taught the guard to descend into the wrapper;
        this asserts it still lands on the whole API.

        The floor tracks the real route count (98 at 3.2.0) instead of the old
        "> 50": at 5 visible routes the old floor was already an order of
        magnitude away, so it has room to spare and still catches a guard going
        blind rather than a release adding or removing a handful of routes.
        """
        inspected = _walk_api_routes(list(real_app.routes))
        assert len(inspected) >= 80, (
            f"the scope guard can only see {len(inspected)} routes on the shipped app - "
            "it is walking a route list that no longer holds the API, so its clean "
            "verdict means nothing (see _walk_api_routes in main.py)"
        )

    @_wrapper_shape_only
    def test_without_wrapper_descent_the_guard_goes_blind(self, monkeypatch):
        """Teeth for the headline fix: simulate the pre-#472 isinstance-only walk
        (no descent into the include wrapper) and prove the floor above FAILS on
        the installed FastAPI. If this ever stops failing, the count gate has
        stopped proving anything."""
        monkeypatch.setattr(main, "_include_wrapper_parts", lambda route: None)

        blind = _walk_api_routes(list(real_app.routes))
        assert len(blind) < 80, (
            "the isinstance-only walk still sees the API - this FastAPI flattens "
            "include_router again, so re-check what the count gate proves"
        )
        # ... and a blind guard reports a clean bill of health, which is the
        # exact failure mode #472 exists to prevent.
        assert main.find_unscoped_routes(real_app) == []

    def test_public_routes_are_matched_by_their_full_path(self):
        """_PUBLIC_ROUTES holds FULL paths. Routes behind an include carry
        UNPREFIXED paths on 0.137+, so the guard has to rebuild the path from the
        accumulated include prefixes - get that wrong and the allowlist silently
        stops matching (routes wrongly flagged) or starts matching the wrong
        thing (a route wrongly treated public)."""
        walked = {path for path, _route, _deps in _walk_api_routes(list(real_app.routes))}

        # portal_router is included with prefix="/invite" and this path is on the
        # public allowlist by its full form.
        assert ("GET", "/invite/{token}") in main._PUBLIC_ROUTES
        assert "/invite/{token}" in walked
        # The unprefixed form must NOT appear - that would mean the prefix was
        # dropped and the allowlist is matching by accident, not by path.
        assert "/{token}" not in walked

    def test_nested_include_prefixes_accumulate(self):
        from fastapi import APIRouter

        inner = APIRouter()

        @inner.get("/leaf")
        async def leaf():
            return {"ok": True}

        middle = APIRouter()
        middle.include_router(inner, prefix="/inner")
        app = FastAPI()
        app.include_router(middle, prefix="/outer")

        walked = {path for path, _route, _deps in _walk_api_routes(list(app.routes))}
        assert walked == {"/outer/inner/leaf"}
        assert ("GET", "/outer/inner/leaf") in find_unscoped_routes(app)


class TestScopeIsFoundWhereverItIsDeclared:
    """A scope dependency counts no matter which of the four places it is
    attached - and include-time scopes only exist as an `include_context`
    attribute on 0.137+, never merged into the route's dependant."""

    @staticmethod
    def _scoped_router():
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("/x")
        async def x():
            return {"ok": True}

        return router

    def test_scope_on_the_route_itself(self):
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("/x", dependencies=[Depends(require_scope("read"))])
        async def x():
            return {"ok": True}

        app = FastAPI()
        app.include_router(router, prefix="/r")
        assert find_unscoped_routes(app) == []

    def test_scope_on_the_router(self):
        from fastapi import APIRouter

        router = APIRouter(dependencies=[Depends(require_scope("read"))])

        @router.get("/x")
        async def x():
            return {"ok": True}

        app = FastAPI()
        app.include_router(router, prefix="/r")
        assert find_unscoped_routes(app) == []

    def test_scope_attached_at_include_time(self):
        app = FastAPI()
        app.include_router(
            self._scoped_router(),
            prefix="/r",
            dependencies=[Depends(require_scope("read"))],
        )
        assert find_unscoped_routes(app) == []

    def test_scope_attached_at_an_outer_include_covers_a_nested_router(self):
        from fastapi import APIRouter

        middle = APIRouter()
        middle.include_router(self._scoped_router(), prefix="/inner")
        app = FastAPI()
        app.include_router(middle, prefix="/outer", dependencies=[Depends(require_scope("read"))])
        assert find_unscoped_routes(app) == []

    def test_indirect_include_time_scope_dep_counts(self):
        """The include-time dependency need not BE the scope enforcer; it may
        depend on one."""

        async def wrapper(token=Depends(require_scope("read"))):  # noqa: B008
            return token

        app = FastAPI()
        app.include_router(self._scoped_router(), prefix="/r", dependencies=[Depends(wrapper)])
        assert find_unscoped_routes(app) == []

    @_wrapper_shape_only
    def test_teeth_without_include_dependency_reading_it_screams_false_positives(self, monkeypatch):
        """Remove the include-time dependency reading and every one of the
        include-scoped shapes above must be reported unscoped. This is what
        proves that reading `include_context.dependencies` is load-bearing and
        not decoration."""
        monkeypatch.setattr(main, "_dependency_enforces_scope", lambda dep: False)

        app = FastAPI()
        app.include_router(
            self._scoped_router(),
            prefix="/r",
            dependencies=[Depends(require_scope("read"))],
        )
        assert find_unscoped_routes(app) == [("GET", "/r/x")]

        from fastapi import APIRouter

        nested = FastAPI()
        middle_parent = APIRouter()
        middle_parent.include_router(self._scoped_router(), prefix="/inner")
        nested.include_router(
            middle_parent, prefix="/outer", dependencies=[Depends(require_scope("read"))]
        )
        assert find_unscoped_routes(nested) == [("GET", "/outer/inner/x")]


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
        app.state.repo.count_hosts = AsyncMock(return_value=0)
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
