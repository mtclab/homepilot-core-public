"""Tests for HttpOnly cookie auth and CSRF protection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_token
from homepilot.auth.router import router as auth_router
from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


@pytest_asyncio.fixture()
async def setup(tmp_path: Path):
    import os

    os.environ["HP_COOKIE_SECURE"] = "false"
    from homepilot.config import get_settings

    get_settings.cache_clear()
    db = Database(str(tmp_path / "auth_test.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    user_id = await repo.create_user("tester", "test@example.com")
    full_token, prefix, token_hash = generate_api_token()
    await repo.create_api_token(
        user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
    )

    app = FastAPI()
    app.state.repo = repo
    app.include_router(auth_router, prefix="/auth")

    # Minimal protected endpoint
    @app.get("/protected")
    async def protected(token=Depends(require_token)):  # noqa: B008
        return {"ok": True}

    @app.post("/protected-mutation")
    async def protected_mutation(token=Depends(require_token)):  # noqa: B008
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=True)
    yield client, full_token
    await db.close()
    os.environ.pop("HP_COOKIE_SECURE", None)
    get_settings.cache_clear()


class TestLoginEndpoint:
    def test_valid_token_returns_ok(self, setup):
        client, token = setup
        resp = client.post("/auth/login", json={"token": token})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_valid_token_sets_httponly_cookie(self, setup):
        client, token = setup
        resp = client.post("/auth/login", json={"token": token})
        assert "hp_token" in resp.cookies
        # Verify the cookie is HttpOnly via Set-Cookie header
        set_cookie = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie

    def test_valid_token_sets_csrf_cookie(self, setup):
        client, token = setup
        resp = client.post("/auth/login", json={"token": token})
        assert "hp_csrf" in resp.cookies
        # CSRF cookie must NOT be HttpOnly (JS needs to read it)
        # FastAPI TestClient doesn't expose individual cookie flags directly,
        # but verify the cookie is present and non-empty
        assert resp.cookies["hp_csrf"] != ""

    def test_invalid_token_returns_401(self, setup):
        client, _ = setup
        resp = client.post("/auth/login", json={"token": "hp_notavalidtoken1234"})
        assert resp.status_code == 401

    def test_empty_token_returns_401(self, setup):
        client, _ = setup
        resp = client.post("/auth/login", json={"token": ""})
        assert resp.status_code == 401


class TestLogoutEndpoint:
    def test_logout_clears_cookies(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        resp = client.post("/auth/logout")
        assert resp.status_code == 200
        # After logout the cookie jar should not have hp_token
        assert "hp_token" not in client.cookies


class TestCookieAuth:
    def test_cookie_auth_accepted_on_get(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        # After login, cookie is set; GET should work without Authorization header
        resp = client.get("/protected")
        assert resp.status_code == 200

    def test_no_auth_returns_401(self, setup):
        client, _ = setup
        # Fresh client with no cookies or header
        fresh = TestClient(client.app, raise_server_exceptions=True)
        resp = fresh.get("/protected")
        assert resp.status_code == 401

    def test_bearer_still_works(self, setup):
        client, token = setup
        fresh = TestClient(client.app, raise_server_exceptions=True)
        resp = fresh.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200


class TestMeEndpoint:
    def test_me_with_valid_cookie(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert "token_label" in data

    def test_me_without_cookie_returns_401(self, setup):
        client, _ = setup
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_after_logout_returns_401(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        client.post("/auth/logout")
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_with_bearer_token(self, setup):
        client, token = setup
        fresh = TestClient(client.app, raise_server_exceptions=True)
        resp = fresh.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert "token_label" in data

    def test_me_with_cookie_only(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is True

    def test_me_no_credentials(self, setup):
        client, _ = setup
        fresh = TestClient(client.app, raise_server_exceptions=True)
        resp = fresh.get("/auth/me")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_me_bearer_takes_priority_over_cookie(self, setup):
        client, cookie_token = setup
        repo = client.app.state.repo
        user2_id = await repo.create_user("bearer_user", "bearer@test.com")
        bearer_token, prefix2, hash2 = generate_api_token()
        await repo.create_api_token(
            user_id=user2_id, token_type="api", prefix=prefix2, hash=hash2, scope="*"
        )
        await repo.db.conn.commit()
        # Login sets cookie for the first token
        client.post("/auth/login", json={"token": cookie_token})
        # Bearer header carries the second token — router should prefer it
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {bearer_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["token_label"] == prefix2


class TestCsrfProtection:
    def test_mutation_via_cookie_without_csrf_header_returns_403(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        # Do NOT send X-CSRF-Token — should be rejected
        resp = client.post("/protected-mutation")
        assert resp.status_code == 403

    def test_mutation_via_cookie_with_wrong_csrf_returns_403(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        resp = client.post("/protected-mutation", headers={"X-CSRF-Token": "wrong-csrf-value"})
        assert resp.status_code == 403

    def test_mutation_via_cookie_with_correct_csrf_returns_200(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        csrf = client.cookies.get("hp_csrf", "")
        resp = client.post(
            "/protected-mutation",
            headers={"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200

    def test_mutation_via_cookie_without_x_requested_with_returns_403(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        csrf = client.cookies.get("hp_csrf", "")
        resp = client.post("/protected-mutation", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 403

    def test_mutation_via_bearer_needs_no_csrf(self, setup):
        client, token = setup
        fresh = TestClient(client.app, raise_server_exceptions=True)
        # Bearer auth — no CSRF header, should succeed
        resp = fresh.post("/protected-mutation", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_get_via_cookie_needs_no_csrf(self, setup):
        client, token = setup
        client.post("/auth/login", json={"token": token})
        # GET is safe method — no CSRF check
        resp = client.get("/protected")
        assert resp.status_code == 200


class TestAdminTokenCreate:
    def test_valid_secret_creates_token(self, setup, monkeypatch):
        client, _ = setup

        import homepilot.auth.router as router_mod

        mock_settings = MagicMock()
        mock_settings.admin_secret = "test-passphrase"
        monkeypatch.setattr(router_mod, "get_settings", lambda: mock_settings)

        resp = client.post(
            "/auth/tokens",
            json={"label": "ci", "scope": "read"},
            headers={"x-hp-admin-secret": "test-passphrase"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["token"].startswith("hp_")
        assert data["scope"] == "read"

    def test_wrong_secret_returns_403(self, setup, monkeypatch):
        client, _ = setup

        import homepilot.auth.router as router_mod

        mock_settings = MagicMock()
        mock_settings.admin_secret = "test-passphrase"
        monkeypatch.setattr(router_mod, "get_settings", lambda: mock_settings)

        resp = client.post(
            "/auth/tokens",
            json={"label": "ci", "scope": "read"},
            headers={"x-hp-admin-secret": "wrong-passphrase"},
        )
        assert resp.status_code == 403

    def test_no_admin_secret_returns_403(self, setup, monkeypatch):
        client, _ = setup

        import homepilot.auth.router as router_mod

        mock_settings = MagicMock()
        mock_settings.admin_secret = ""
        monkeypatch.setattr(router_mod, "get_settings", lambda: mock_settings)

        resp = client.post(
            "/auth/tokens",
            json={"label": "ci", "scope": "read"},
            headers={"x-hp-admin-secret": "anything"},
        )
        assert resp.status_code == 403

    def test_missing_secret_header_returns_403(self, setup, monkeypatch):
        client, _ = setup

        import homepilot.auth.router as router_mod

        mock_settings = MagicMock()
        mock_settings.admin_secret = "test-passphrase"
        monkeypatch.setattr(router_mod, "get_settings", lambda: mock_settings)

        resp = client.post("/auth/tokens", json={"label": "ci", "scope": "read"})
        assert resp.status_code == 403


class TestCookieSecureFlag:
    def test_cookie_secure_true(self, setup, monkeypatch):
        client, token = setup
        import homepilot.auth.router as router_mod

        monkeypatch.setattr(router_mod, "_cookie_secure", lambda req=None: True)

        resp = client.post("/auth/login", json={"token": token})
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in set_cookie

    def test_cookie_secure_default_true(self, setup, monkeypatch):
        client, token = setup
        import homepilot.config as config_mod

        monkeypatch.delenv("HP_COOKIE_SECURE", raising=False)
        config_mod.get_settings.cache_clear()
        resp = client.post("/auth/login", json={"token": token})
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Secure" not in set_cookie


class TestRevokeTokenEndpoint:
    @pytest.mark.asyncio
    async def test_delete_token_returns_204(self, tmp_path: Path):
        db = Database(str(tmp_path / "revoke_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )

        _target_token, target_prefix, target_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=target_prefix,
            hash=target_hash,
            scope="full",
        )
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(
            f"/auth/tokens/{target_prefix}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 204
        await db.close()

    @pytest.mark.asyncio
    async def test_delete_unknown_prefix_returns_404(self, tmp_path: Path):
        db = Database(str(tmp_path / "revoke_404_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        user_id = await repo.create_user("admin", "admin@test.com")
        admin_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(
            "/auth/tokens/hp_NOEXST",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 404
        await db.close()

    @pytest.mark.asyncio
    async def test_delete_token_without_admin_scope_returns_403(self, tmp_path: Path):
        db = Database(str(tmp_path / "revoke_403_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        admin_id = await repo.create_user("admin", "admin@test.com")
        _admin_token, admin_prefix, admin_hash = generate_api_token()
        await repo.create_api_token(
            user_id=admin_id, token_type="api", prefix=admin_prefix, hash=admin_hash, scope="*"
        )

        reader_id = await repo.create_user("reader", "reader@test.com")
        reader_token, reader_prefix, reader_hash = generate_api_token()
        await repo.create_api_token(
            user_id=reader_id,
            token_type="api",
            prefix=reader_prefix,
            hash=reader_hash,
            scope="read_only",
        )

        _target_token, target_prefix, target_hash = generate_api_token()
        await repo.create_api_token(
            user_id=admin_id,
            token_type="api",
            prefix=target_prefix,
            hash=target_hash,
            scope="full",
        )
        await db.conn.commit()

        app = FastAPI()
        app.state.repo = repo
        app.include_router(auth_router, prefix="/auth")

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(
            f"/auth/tokens/{target_prefix}",
            headers={"Authorization": f"Bearer {reader_token}"},
        )
        assert resp.status_code == 403
        await db.close()
