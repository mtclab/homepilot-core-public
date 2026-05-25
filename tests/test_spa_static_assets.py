from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from fastapi.responses import FileResponse, HTMLResponse


@pytest.fixture
def spa_app(tmp_path: Path) -> FastAPI:
    dist = tmp_path / "dist"
    app_assets = dist / "_app" / "immutable" / "chunks"
    app_assets.mkdir(parents=True)

    (app_assets / "test-chunk.js").write_text("// JS file content")
    (app_assets / "test-style.css").write_text("body { color: red; }")
    (dist / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    (dist / "index.html").write_text(
        "<!DOCTYPE html><html><head></head><body><div id='app'></div></body></html>"
    )

    app = FastAPI()

    @app.get("/ui/_app/{path:path}", include_in_schema=False)
    async def ui_static_assets(path: str):
        full_path = dist / "_app" / path
        if full_path.exists() and full_path.is_file():
            suffix = full_path.suffix.lower()
            media_map = {
                ".js": "application/javascript",
                ".mjs": "application/javascript",
                ".css": "text/css",
                ".json": "application/json",
                ".html": "text/html",
                ".ico": "image/x-icon",
                ".png": "image/png",
                ".svg": "image/svg+xml",
                ".woff": "font/woff",
                ".woff2": "font/woff2",
            }
            content_type = media_map.get(suffix, "application/octet-stream")
            with open(full_path, "rb") as f:
                return HTMLResponse(content=f.read(), media_type=content_type)
        return HTMLResponse(content="Not Found", status_code=404)

    @app.get("/ui/{path:path}", include_in_schema=False)
    async def ui_spa(path: str):
        full = dist / path
        if full.exists() and full.is_file():
            return FileResponse(str(full))
        return FileResponse(str(dist / "index.html"))

    return app


@pytest.fixture
def client(spa_app: FastAPI) -> TestClient:
    return TestClient(spa_app)


class TestSPAStaticAssets:
    def test_js_files_served_directly(self, client: TestClient):
        resp = client.get("/ui/_app/immutable/chunks/test-chunk.js")
        assert resp.status_code == 200
        assert "JS file content" in resp.text

    def test_css_files_served_directly(self, client: TestClient):
        resp = client.get("/ui/_app/immutable/chunks/test-style.css")
        assert resp.status_code == 200
        assert "color: red" in resp.text

    def test_known_static_files_served_not_spa_fallback(self, client: TestClient):
        resp = client.get("/ui/favicon.ico")
        assert resp.status_code == 200
        assert len(resp.content) > 0

    def test_unknown_path_returns_spa_index(self, client: TestClient):
        resp = client.get("/ui/some/spa/route")
        assert resp.status_code == 200
        assert "app" in resp.text.lower()

    def test_root_ui_returns_spa_index(self, client: TestClient):
        resp = client.get("/ui/")
        assert resp.status_code == 200


class TestSPARouteOrdering:
    def test_static_assets_not_intercepted_by_spa_catchall(self, spa_app: FastAPI):
        routes = spa_app.routes
        static_routes = [r for r in routes if hasattr(r, 'path') and '/_app/' in r.path]
        spa_routes = [r for r in routes if hasattr(r, 'path') and r.path == '/ui/{path:path}']
        assert len(static_routes) > 0, "/ui/_app/ route must exist"
        assert len(spa_routes) > 0, "/ui/{path:path} SPA catch-all route must exist"

        static_index = None
        spa_index = None
        for i, r in enumerate(routes):
            if hasattr(r, 'path'):
                if '/_app/' in r.path:
                    static_index = i
                if r.path == '/ui/{path:path}':
                    spa_index = i
        assert static_index is not None, "Static asset route not found"
        assert spa_index is not None, "SPA catch-all route not found"
        assert static_index < spa_index, (
            f"Static route at index {static_index} must come before SPA catch-all at {spa_index}"
        )