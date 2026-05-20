"""
End-to-end tests against a live HomePilot instance.

Requires:
    pip install playwright && playwright install chromium

Environment variables:
    HP_TEST_URL    Base URL of the running instance (default: http://localhost:8000)
    HP_TEST_TOKEN  Valid API token (hp_...)
    HP_TEST_ADMIN_SECRET  Admin secret for token creation (default: empty)

Run:
    HP_TEST_TOKEN=hp_... pytest tests/test_e2e.py -v

Skip in CI (no live server):
    pytest tests/ --ignore=tests/test_e2e.py
"""

from __future__ import annotations

import json
import os
from typing import ClassVar

import pytest

BASE_URL = os.environ.get("HP_TEST_URL", "http://localhost:8000")
TOKEN = os.environ.get("HP_TEST_TOKEN", "")
ADMIN_SECRET = os.environ.get("HP_TEST_ADMIN_SECRET", "")
UI = BASE_URL + "/ui"

pytestmark = pytest.mark.skipif(not TOKEN, reason="HP_TEST_TOKEN not set — skipping e2e tests")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_type_launch_args() -> dict:
    return {"headless": True}


@pytest.fixture
def auth_page(page):
    """Page fixture pre-authenticated via login API with both cookies set."""
    page.goto(f"{BASE_URL}/ui/login", wait_until="networkidle")
    resp = page.request.post(
        f"{BASE_URL}/auth/login",
        headers={"Content-Type": "application/json"},
        data=f'{{"token": "{TOKEN}"}}',
    )
    assert resp.ok, f"Login failed: {resp.status} {resp.text()}"
    return page


# ---------------------------------------------------------------------------
# Auth / redirect
# ---------------------------------------------------------------------------


class TestAuthRedirect:
    def test_fresh_browser_redirects_to_login(self, page):
        """Unauthenticated visit → redirected to /ui/login."""
        page.goto(UI + "/artifacts", wait_until="networkidle")
        assert "/login" in page.url, f"Expected redirect to /login, got {page.url}"

    def test_settings_page_has_token_input(self, page):
        page.goto(UI + "/settings", wait_until="networkidle")
        assert page.locator("input#login-token").count() > 0

    def test_login_sets_cookie_and_redirects(self, page):
        """Login via login page → session cookie set → redirect back."""
        page.goto(UI + "/login?returnTo=%2Fui%2Fartifacts", wait_until="networkidle")
        token_input = page.locator("input[type='password'], input#login-token")
        token_input.fill(TOKEN)
        page.locator("button", has_text="Connect").click()
        page.wait_for_url("**/artifacts**", timeout=5000)
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert "hp_token" in cookies or "hp_csrf" in cookies


# ---------------------------------------------------------------------------
# API health
# ---------------------------------------------------------------------------


class TestAPIHealth:
    def test_health_endpoint(self, page):
        resp = page.request.get(f"{BASE_URL}/health")
        assert resp.ok
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body

    def test_authenticated_api_call(self, page):
        resp = page.request.get(
            f"{BASE_URL}/artifacts",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "items" in body

    def test_unauthenticated_api_returns_401(self, page):
        resp = page.request.get(f"{BASE_URL}/artifacts")
        assert resp.status == 401


# ---------------------------------------------------------------------------
# UI pages (authenticated)
# ---------------------------------------------------------------------------


class TestUIPages:
    ROUTES: ClassVar[list[str]] = [
        "artifacts",
        "inventory",
        "kb",
        "drift",
        "review",
        "journal",
        "settings",
    ]

    def test_all_routes_render_without_error(self, auth_page):
        """Navigate all 7 UI routes — none should show an inline 401 error."""
        errors: list[str] = []
        for route in self.ROUTES:
            auth_page.goto(f"{UI}/{route}", wait_until="networkidle")
            body = auth_page.locator("body").inner_text()
            if "401" in body or "Missing credentials" in body:
                errors.append(route)
        assert not errors, f"Routes showed 401 errors: {errors}"

    def test_artifacts_page_loads(self, auth_page):
        auth_page.goto(f"{UI}/artifacts", wait_until="networkidle")
        assert auth_page.title() != ""
        assert auth_page.locator("nav").count() > 0

    def test_inventory_page_loads(self, auth_page):
        auth_page.goto(f"{UI}/inventory", wait_until="networkidle")
        assert "inventory" in auth_page.url.lower()

    def test_settings_page_shows_active_session(self, auth_page):
        auth_page.goto(f"{UI}/settings", wait_until="networkidle")
        body = auth_page.locator("body").inner_text()
        assert "Session active" in body or "session" in body.lower()


# ---------------------------------------------------------------------------
# Token create via API (before rate limiting tests to avoid 429 bleed)
# ---------------------------------------------------------------------------


class TestAdminTokenCreate:
    def test_token_create_without_admin_secret_returns_403(self, page):
        resp = page.request.post(
            f"{BASE_URL}/auth/tokens",
            data='{"label": "ci", "scope": "read"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403


# ---------------------------------------------------------------------------
# Token CRUD
# ---------------------------------------------------------------------------


class TestTokenCRUD:
    @pytest.fixture(autouse=True)
    def _skip_no_admin_secret(self):
        if not ADMIN_SECRET:
            pytest.skip("HP_TEST_ADMIN_SECRET not set")

    def test_create_token_with_admin_secret(self, auth_page):
        resp = auth_page.request.post(
            f"{BASE_URL}/auth/tokens",
            data=json.dumps({"label": "e2e-crud", "scope": "read_only"}),
            headers={
                "Content-Type": "application/json",
                "x-hp-admin-secret": ADMIN_SECRET,
            },
        )
        assert resp.status == 201, f"Expected 201, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "token" in body
        assert body["scope"] == "read_only"
        assert body["token"].startswith("hp_")

    def test_list_tokens(self, auth_page):
        resp = auth_page.request.get(
            f"{BASE_URL}/auth/tokens",
        )
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "items" in body
        assert "total" in body

    def test_revoke_token(self, auth_page):
        create_resp = auth_page.request.post(
            f"{BASE_URL}/auth/tokens",
            data=json.dumps({"label": "e2e-revoke", "scope": "read_only"}),
            headers={
                "Content-Type": "application/json",
                "x-hp-admin-secret": ADMIN_SECRET,
            },
        )
        assert create_resp.status == 201
        new_token = create_resp.json()["token"]
        prefix = new_token[:8]

        revoke_resp = auth_page.request.delete(
            f"{BASE_URL}/auth/tokens/{prefix}",
        )
        assert revoke_resp.status == 204, (
            f"Expected 204, got {revoke_resp.status}: {revoke_resp.text()}"
        )

        verify_resp = auth_page.request.get(
            f"{BASE_URL}/auth/me",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert verify_resp.status == 401, "Revoked token should return 401"


# ---------------------------------------------------------------------------
# KB CRUD
# ---------------------------------------------------------------------------


class TestKBCRUD:
    def test_create_note(self, auth_page):
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "E2E test note", "kind": "note"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "id" in body

    def test_search_notes(self, auth_page):
        resp = auth_page.request.get(
            f"{BASE_URL}/kb/search",
            params={"q": "E2E"},
        )
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "results" in body

    def test_update_note(self, auth_page):
        create_resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "E2E note to update", "kind": "note"}),
            headers={"Content-Type": "application/json"},
        )
        assert create_resp.ok
        doc_id = create_resp.json()["id"]

        update_resp = auth_page.request.put(
            f"{BASE_URL}/kb/{doc_id}",
            data=json.dumps({"content": "E2E note updated"}),
            headers={"Content-Type": "application/json"},
        )
        assert update_resp.ok, f"Expected 200, got {update_resp.status}"
        assert "updated" in update_resp.json()["content"].lower()

    def test_delete_note(self, auth_page):
        create_resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "E2E note to delete", "kind": "note"}),
            headers={"Content-Type": "application/json"},
        )
        assert create_resp.ok
        doc_id = create_resp.json()["id"]

        delete_resp = auth_page.request.delete(f"{BASE_URL}/kb/{doc_id}")
        assert delete_resp.status == 204

    def test_kb_admin_ingest(self, auth_page):
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/ingest",
            data=json.dumps({"sources": [{"path": "test", "kind": "note"}]}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"


# ---------------------------------------------------------------------------
# Auth bypass
# ---------------------------------------------------------------------------


class TestAuthBypass:
    @pytest.mark.parametrize("path", ["/inventory", "/kb", "/artifacts/drift"])
    def test_unauthenticated_returns_401(self, page, path):
        resp = page.request.get(f"{BASE_URL}{path}")
        assert resp.status in (401, 403, 429), f"Expected 401/403/429 for {path}, got {resp.status}"


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------


class TestCSRFProtection:
    def test_mutation_without_csrf_rejected(self, auth_page):
        """Cookie-authenticated mutation without X-CSRF-Token header must be rejected."""
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "csrf-blocked", "kind": "note"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403, (
            f"Mutation with cookie auth but no CSRF header should return 403, got {resp.status}"
        )

    def test_mutation_with_csrf_accepted(self, auth_page):
        cookies = {c["name"]: c["value"] for c in auth_page.context.cookies()}
        csrf = cookies.get("hp_csrf", "")
        if not csrf:
            pytest.skip("CSRF cookie not set by login")
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "csrf-allowed", "kind": "note"}),
            headers={
                "Content-Type": "application/json",
                "x-csrf-token": csrf,
            },
        )
        assert resp.ok, (
            f"Mutation with CSRF header should succeed, got {resp.status}: {resp.text()}"
        )


# ---------------------------------------------------------------------------
# Token scope enforcement
# ---------------------------------------------------------------------------


class TestTokenScopeEnforcement:
    @pytest.fixture(autouse=True)
    def _skip_no_admin_secret(self):
        if not ADMIN_SECRET:
            pytest.skip("HP_TEST_ADMIN_SECRET not set")

    def test_read_only_token_cannot_delete_tokens(self, auth_page):
        create_resp = auth_page.request.post(
            f"{BASE_URL}/auth/tokens",
            data=json.dumps({"label": "target-token", "scope": "full"}),
            headers={
                "Content-Type": "application/json",
                "x-hp-admin-secret": ADMIN_SECRET,
            },
        )
        assert create_resp.status == 201
        target_token = create_resp.json()["token"]
        target_prefix = target_token[:8]

        ro_resp = auth_page.request.post(
            f"{BASE_URL}/auth/tokens",
            data=json.dumps({"label": "ro-scoped", "scope": "read_only"}),
            headers={
                "Content-Type": "application/json",
                "x-hp-admin-secret": ADMIN_SECRET,
            },
        )
        assert ro_resp.status == 201
        read_only_token = ro_resp.json()["token"]

        delete_resp = auth_page.request.delete(
            f"{BASE_URL}/auth/tokens/{target_prefix}",
            headers={"Authorization": f"Bearer {read_only_token}"},
        )
        assert delete_resp.status == 403, (
            f"Read-only token should not DELETE, got {delete_resp.status}"
        )

    def test_read_only_token_cannot_access_admin_endpoints(self, auth_page):
        ro_resp = auth_page.request.post(
            f"{BASE_URL}/auth/tokens",
            data=json.dumps({"label": "ro-admin-test", "scope": "read_only"}),
            headers={
                "Content-Type": "application/json",
                "x-hp-admin-secret": ADMIN_SECRET,
            },
        )
        assert ro_resp.status == 201
        read_only_token = ro_resp.json()["token"]

        ingest_resp = auth_page.request.post(
            f"{BASE_URL}/kb/ingest",
            data=json.dumps({"sources": []}),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {read_only_token}",
            },
        )
        assert ingest_resp.status == 403, (
            f"Read-only should not access admin endpoint, got {ingest_resp.status}"
        )


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def test_path_traversal_in_ingest_rejected(self, auth_page):
        resp = auth_page.request.get(
            f"{BASE_URL}/kb/ingest",
            params={"path": "../../../etc/passwd"},
        )
        assert resp.status in (401, 403, 404, 422), (
            f"Path traversal should be rejected, got {resp.status}"
        )

    def test_double_dot_in_kb_path(self, page):
        resp = page.request.get(f"{BASE_URL}/kb/..%2F..%2Fetc%2Fpasswd")
        assert resp.status in (401, 403, 404, 422, 429), "Encoded path traversal should be rejected"


# ---------------------------------------------------------------------------
# Session indicator
# ---------------------------------------------------------------------------


class TestSessionIndicator:
    def test_auth_me_returns_authenticated(self, auth_page):
        resp = auth_page.request.get(f"{BASE_URL}/auth/me")
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert body.get("authenticated") is True
        assert "scope" in body
        assert "token_label" in body


# ---------------------------------------------------------------------------
# Auth round-trip
# ---------------------------------------------------------------------------


class TestAuthRoundTrip:
    def test_login_me_logout_me(self, page):
        page.goto(f"{BASE_URL}/ui/login", wait_until="networkidle")
        login_resp = page.request.post(
            f"{BASE_URL}/auth/login",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"token": TOKEN}),
        )
        if login_resp.status == 429:
            pytest.skip("Rate limited — server too busy for auth round-trip test")
        assert login_resp.ok, f"Login failed: {login_resp.status}"

        me_resp = page.request.get(f"{BASE_URL}/auth/me")
        assert me_resp.ok, f"auth/me should succeed after login: {me_resp.status}"
        body = me_resp.json()
        assert body.get("authenticated") is True

        logout_resp = page.request.post(f"{BASE_URL}/auth/logout")
        assert logout_resp.ok, f"Logout failed: {logout_resp.status}"

        me_after_resp = page.request.get(f"{BASE_URL}/auth/me")
        assert me_after_resp.status == 401, (
            f"auth/me should return 401 after logout, got {me_after_resp.status}"
        )


# ---------------------------------------------------------------------------
# Rate limiting (last — floods server with unauthenticated requests)
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_authenticated_requests_have_higher_rate_limit(self, page):
        """Authenticated requests use a higher rate limit (120/min) but can still be rate-limited."""
        responses = []
        for _ in range(30):
            resp = page.request.get(
                f"{BASE_URL}/artifacts",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            responses.append(resp.status)
        assert 429 not in responses, (
            "Authenticated requests within auth limit should not be rate-limited"
        )

    def test_unauthenticated_rapid_requests_rate_limited(self, page):
        """60+ unauthenticated requests should trigger rate limiting."""
        statuses = []
        for _ in range(70):
            resp = page.request.post(
                f"{BASE_URL}/auth/login",
                data='{"token": "hp_badtoken"}',
                headers={"Content-Type": "application/json"},
            )
            statuses.append(resp.status)
        assert 429 in statuses, "Unauthenticated rapid requests should be rate-limited"
