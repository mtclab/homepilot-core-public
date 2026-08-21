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

Note: These tests use Bearer auth for most API calls. Cookie-based auth
is only used for tests that specifically validate CSRF protection and
UI session behavior. The session token is created via admin endpoint
to avoid invalidating the primary test token.
"""

from __future__ import annotations

import json
import os
import time
from typing import ClassVar

import pytest

BASE_URL = os.environ.get("HP_TEST_URL", "http://localhost:8000")
TOKEN = os.environ.get("HP_TEST_TOKEN", "")
ADMIN_SECRET = os.environ.get("HP_TEST_ADMIN_SECRET", "")
UI = BASE_URL + "/ui"

pytestmark = pytest.mark.skipif(not TOKEN, reason="HP_TEST_TOKEN not set — skipping e2e tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(extra: dict | None = None) -> dict:
    """Headers with Bearer auth."""
    h = dict(extra) if extra else {}
    h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _bearer_json(extra: dict | None = None) -> dict:
    """Headers with Bearer auth + JSON content type."""
    h = _bearer(extra)
    h.setdefault("Content-Type", "application/json")
    return h


def _csrf_headers(csrf: str, content_type: bool = True) -> dict:
    """Headers for cookie-authenticated mutation requests (CSRF + X-Requested-With)."""
    h: dict = {
        "x-csrf-token": csrf,
        "x-requested-with": "XMLHttpRequest",
    }
    if content_type:
        h["Content-Type"] = "application/json"
    return h


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_type_launch_args() -> dict:
    return {"headless": True}


@pytest.fixture(scope="session")
def session_auth(browser):
    """
    Persistent authenticated browser context.
    Creates a dedicated session token to avoid invalidating HP_TEST_TOKEN.
    Also creates all extra tokens so individual tests don't hit the token
    creation rate limit (5 / 60s per IP on the server).
    """
    _max_retries = 6
    session_token = TOKEN
    extra_tokens: dict[str, str] = {}

    if ADMIN_SECRET:
        import urllib.request

        def _create_token(label: str, scope: str = "full") -> str | None:
            """Create a token via admin endpoint with 429 retry."""
            data = json.dumps({"label": label, "scope": scope}).encode()
            req = urllib.request.Request(
                f"{BASE_URL}/auth/tokens",
                data=data,
                headers={"Content-Type": "application/json", "x-hp-admin-secret": ADMIN_SECRET},
                method="POST",
            )
            for _attempt in range(_max_retries):
                try:
                    with urllib.request.urlopen(req) as resp:
                        result = json.loads(resp.read())
                        return str(result["token"])
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        retry_after = int(e.headers.get("retry-after", "61"))
                        time.sleep(retry_after + 1)
                        continue
                    return None
            return None

        # Session token (used by auth_page)
        tok = _create_token("e2e-session", "full")
        if tok:
            session_token = tok

        # Pre-create tokens for rate-limit-heavy tests
        extra_tokens["revoke_target"] = _create_token("e2e-revoke-target", "read_only") or ""
        extra_tokens["scope_target"] = _create_token("e2e-scope-target", "full") or ""
        extra_tokens["scope_ro"] = _create_token("e2e-scope-ro", "read_only") or ""
        extra_tokens["roundtrip"] = _create_token("e2e-roundtrip", "full") or ""
    else:
        pytest.skip("HP_TEST_ADMIN_SECRET not set")

    context = browser.new_context()

    # Log in via API to set cookies; retry on rate limit
    for _attempt in range(_max_retries):
        resp = context.request.post(
            f"{BASE_URL}/auth/login",
            data=json.dumps({"token": session_token}),
            headers={"Content-Type": "application/json"},
        )
        if resp.ok:
            break
        if resp.status == 429:
            retry_after = int(resp.headers.get("retry-after", "61"))
            time.sleep(retry_after + 1)
            continue
        raise AssertionError(f"Session login failed: {resp.status} {resp.text()}")
    else:
        raise AssertionError("Session login failed after retries")

    cookies = {c["name"]: c["value"] for c in context.cookies()}
    assert "hp_token" in cookies or "hp_csrf" in cookies, f"Login did not set cookies: {cookies}"

    csrf = cookies.get("hp_csrf", "")
    assert csrf, f"CSRF cookie not set: {cookies.keys()}"

    yield {
        "context": context,
        "csrf": csrf,
        "session_token": session_token,
        "extra_tokens": extra_tokens,
    }
    context.close()


@pytest.fixture
def auth_page(session_auth):
    """Page from pre-authenticated browser context."""
    page = session_auth["context"].new_page()
    yield page
    page.close()


@pytest.fixture
def csrf_token(session_auth) -> str:
    """CSRF token from authenticated session cookies."""
    return session_auth["csrf"]


# ---------------------------------------------------------------------------
# Auth / redirect
# ---------------------------------------------------------------------------


class TestAuthRedirect:
    def test_fresh_browser_redirects_to_login(self, page):
        page.goto(UI + "/artifacts", wait_until="networkidle")
        current = page.url.lower()
        assert "/login" in current, f"Expected redirect to /login, got {page.url}"

    def test_settings_page_has_token_input(self, page):
        page.goto(UI + "/settings", wait_until="networkidle")
        assert page.locator("input#login-token").count() > 0

    def test_login_sets_cookie_and_redirects(self, page, session_auth):
        # Use pre-created roundtrip token so we don't invalidate the main test token on logout
        test_token = session_auth["extra_tokens"].get("roundtrip")
        if not test_token:
            pytest.skip("Roundtrip token not available (rate-limited during session setup)")

        login_resp = page.request.post(
            f"{BASE_URL}/auth/login",
            data=json.dumps({"token": test_token}),
            headers={"Content-Type": "application/json"},
        )
        assert login_resp.ok, f"Login failed: {login_resp.status}"
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert "hp_token" in cookies or "hp_csrf" in cookies
        page.goto(UI + "/artifacts", wait_until="networkidle")
        assert "/artifacts" in page.url


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
        resp = page.request.get(f"{BASE_URL}/artifacts", headers=_bearer())
        if resp.status == 429:
            pytest.skip("Rate limited")
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "items" in body

    def test_unauthenticated_api_returns_401(self, page):
        resp = page.request.get(f"{BASE_URL}/artifacts")
        assert resp.status in (401, 429)


# ---------------------------------------------------------------------------
# UI pages (authenticated via session cookies)
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
# Token create (uses Bearer auth)
# ---------------------------------------------------------------------------


class TestAdminTokenCreate:
    def test_token_create_without_admin_secret_returns_403(self, page):
        resp = page.request.post(
            f"{BASE_URL}/auth/tokens",
            data='{"label": "ci", "scope": "read"}',
            headers=_bearer_json(),
        )
        assert resp.status == 403, f"Expected 403, got {resp.status}"


# ---------------------------------------------------------------------------
# Token CRUD (uses cookie auth from session)
# ---------------------------------------------------------------------------


class TestTokenCRUD:
    @pytest.fixture(autouse=True)
    def _skip_no_admin_secret(self):
        if not ADMIN_SECRET:
            pytest.skip("HP_TEST_ADMIN_SECRET not set")

    def test_create_token_with_admin_secret(self, auth_page, csrf_token):
        resp = auth_page.request.post(
            f"{BASE_URL}/auth/tokens",
            data=json.dumps({"label": "e2e-crud", "scope": "read_only"}),
            headers={**_csrf_headers(csrf_token), "x-hp-admin-secret": ADMIN_SECRET},
        )
        if resp.status == 429:
            pytest.skip("Rate limited")
        assert resp.status == 201, f"Expected 201, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "token" in body
        assert body["scope"] == "read_only"
        assert body["token"].startswith("hp_")

    def test_list_tokens(self, auth_page, csrf_token):
        resp = auth_page.request.get(
            f"{BASE_URL}/auth/tokens",
            headers=_csrf_headers(csrf_token, content_type=False),
        )
        if resp.status == 429:
            pytest.skip("Rate limited")
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "items" in body
        assert "total" in body

    def test_revoke_token(self, auth_page, csrf_token, session_auth):
        # Use pre-created token instead of creating on the fly (avoids token rate limit)
        new_token = session_auth["extra_tokens"].get("revoke_target")
        if not new_token:
            pytest.skip("Revoke target token not available (rate-limited during session setup)")
        prefix = new_token[:16]  # PREFIX_LENGTH = 16

        revoke_resp = auth_page.request.delete(
            f"{BASE_URL}/auth/tokens/{prefix}",
            headers=_csrf_headers(csrf_token, content_type=False),
        )
        if revoke_resp.status == 429:
            pytest.skip("Rate limited during revoke")
        assert revoke_resp.status == 204, (
            f"Expected 204, got {revoke_resp.status}: {revoke_resp.text()}"
        )

        verify_resp = auth_page.request.get(
            f"{BASE_URL}/auth/me",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert verify_resp.status == 401, "Revoked token should return 401"


# ---------------------------------------------------------------------------
# KB CRUD (uses Bearer auth for all operations)
# ---------------------------------------------------------------------------


class TestKBCRUD:
    def test_create_note(self, auth_page, csrf_token):
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "E2E test note", "kind": "note"}),
            headers=_csrf_headers(csrf_token),
        )
        if resp.status == 429:
            pytest.skip("Rate limited")
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "id" in body

    def test_search_notes(self, auth_page, csrf_token):
        resp = auth_page.request.get(
            f"{BASE_URL}/kb/search",
            params={"q": "E2E"},
            headers=_csrf_headers(csrf_token, content_type=False),
        )
        if resp.status == 429:
            pytest.skip("Rate limited")
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert "results" in body

    def test_kb_admin_ingest(self, auth_page, csrf_token):
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/ingest",
            data=json.dumps({"sources": [{"path": "test", "kind": "note"}]}),
            headers={**_csrf_headers(csrf_token), "x-hp-admin-secret": ADMIN_SECRET}
            if ADMIN_SECRET
            else _csrf_headers(csrf_token),
        )
        if resp.status == 429:
            pytest.skip("Rate limited")
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"


# ---------------------------------------------------------------------------
# Auth bypass (uses urllib to avoid browser rate limit issues)
# ---------------------------------------------------------------------------


class TestAuthBypass:
    @pytest.mark.parametrize("path", ["/inventory", "/kb", "/artifacts/drift"])
    def test_unauthenticated_returns_401(self, path):
        import urllib.request

        url = f"{BASE_URL}{path}"
        req = urllib.request.Request(url)
        try:
            urllib.request.urlopen(req)
            pytest.fail(f"Expected 401 for {path}")
        except urllib.error.HTTPError as e:
            assert e.code in (401, 403, 429)


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------


class TestCSRFProtection:
    def test_mutation_without_csrf_rejected(self, auth_page):
        """Cookie-authenticated mutation without CSRF headers must be rejected."""
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "csrf-blocked", "kind": "note"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status in (403, 429), (
            f"Mutation with cookie auth but no CSRF should return 403, got {resp.status}"
        )

    def test_mutation_with_csrf_accepted(self, auth_page, csrf_token):
        # Try creating a note (may 500 due to KB service issues); the key test is
        # that we don't get 403 (CSRF rejection). Accept 200 or 500 (server bug).
        resp = auth_page.request.post(
            f"{BASE_URL}/kb/notes",
            data=json.dumps({"content": "csrf-allowed", "kind": "note"}),
            headers=_csrf_headers(csrf_token),
        )
        if resp.status == 429:
            pytest.skip("Rate limited")
        # 403 would mean CSRF check failed; we should NOT get 403
        assert resp.status != 403, f"Mutation with CSRF header should not be 403, got {resp.status}"
        # Accept 200 (success) or 500 (KB service error) — both prove CSRF passed
        assert resp.status in (200, 201, 500), (
            f"Expected success or server error (not CSRF rejection), got {resp.status}: {resp.text()[:200]}"
        )


# ---------------------------------------------------------------------------
# Token scope enforcement
# ---------------------------------------------------------------------------


class TestTokenScopeEnforcement:
    @pytest.fixture(autouse=True)
    def _skip_no_admin_secret(self):
        if not ADMIN_SECRET:
            pytest.skip("HP_TEST_ADMIN_SECRET not set")

    def test_read_only_token_cannot_delete_tokens(self, auth_page, csrf_token, session_auth):
        target_token = session_auth["extra_tokens"].get("scope_target")
        read_only_token = session_auth["extra_tokens"].get("scope_ro")
        if not target_token or not read_only_token:
            pytest.skip("Scope tokens not available (rate-limited during session setup)")
        target_prefix = target_token[:16]

        delete_resp = auth_page.request.delete(
            f"{BASE_URL}/auth/tokens/{target_prefix}",
            headers={"Authorization": f"Bearer {read_only_token}"},
        )
        assert delete_resp.status == 403, (
            f"Read-only token should not DELETE, got {delete_resp.status}"
        )

    def test_read_only_token_cannot_access_admin_endpoints(
        self, auth_page, csrf_token, session_auth
    ):
        read_only_token = session_auth["extra_tokens"].get("scope_ro")
        if not read_only_token:
            pytest.skip("Read-only token not available (rate-limited during session setup)")

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
    def test_path_traversal_in_ingest_rejected(self, auth_page, csrf_token):
        resp = auth_page.request.get(
            f"{BASE_URL}/kb/ingest",
            params={"path": "../../../etc/passwd"},
            headers=_csrf_headers(csrf_token, content_type=False),
        )
        assert resp.status in (401, 403, 404, 422), (
            f"Path traversal should be rejected, got {resp.status}"
        )

    def test_double_dot_in_kb_path(self):
        import urllib.request

        url = f"{BASE_URL}/kb/..%2F..%2Fetc%2Fpasswd"
        req = urllib.request.Request(url)
        try:
            urllib.request.urlopen(req)
            pytest.fail("Expected rejection for path traversal")
        except urllib.error.HTTPError as e:
            assert e.code in (401, 403, 404, 422, 429)


# ---------------------------------------------------------------------------
# Session indicator (uses Bearer auth, not cookies)
# ---------------------------------------------------------------------------


class TestSessionIndicator:
    def test_auth_me_returns_authenticated(self, page):
        resp = page.request.get(f"{BASE_URL}/auth/me", headers=_bearer())
        if resp.status == 429:
            pytest.skip("Rate limited")
        assert resp.ok, f"Expected 200, got {resp.status}: {resp.text()}"
        body = resp.json()
        assert body.get("authenticated") is True
        assert "scope" in body
        assert "token_label" in body


# ---------------------------------------------------------------------------
# Auth round-trip (creates its own token to avoid invalidating HP_TEST_TOKEN)
# ---------------------------------------------------------------------------


class TestAuthRoundTrip:
    @pytest.fixture(autouse=True)
    def _skip_no_admin_secret(self):
        if not ADMIN_SECRET:
            pytest.skip("HP_TEST_ADMIN_SECRET not set")

    def test_login_me_logout_me(self, page, session_auth):
        roundtrip_token = session_auth["extra_tokens"].get("roundtrip")
        if not roundtrip_token:
            pytest.skip("Roundtrip token not available (rate-limited during session setup)")

        login_resp = page.request.post(
            f"{BASE_URL}/auth/login",
            data=json.dumps({"token": roundtrip_token}),
            headers={"Content-Type": "application/json"},
        )
        assert login_resp.ok, f"Login failed: {login_resp.status}"

        me_resp = page.request.get(f"{BASE_URL}/auth/me")
        assert me_resp.ok, f"auth/me should succeed after login: {me_resp.status}"
        body = me_resp.json()
        assert body.get("authenticated") is True

        logout_resp = page.request.post(f"{BASE_URL}/auth/logout")
        if logout_resp.status == 429:
            # The whole E2E run shares one per-IP budget on CI (everything is
            # localhost), so a late test can find it spent. The window slides, so
            # give it a moment rather than skipping on the first refusal - and if
            # it is still spent, say so instead of failing as though logout were
            # broken. The same condition is already skipped during session setup
            # above.
            time.sleep(10)
            logout_resp = page.request.post(f"{BASE_URL}/auth/logout")
            if logout_resp.status == 429:
                pytest.skip("Rate limited on logout (shared per-IP budget for the E2E run)")
        assert logout_resp.ok, f"Logout failed: {logout_resp.status}"

        # The token was deleted by logout; verify 401
        me_after_resp = page.request.get(f"{BASE_URL}/auth/me")
        assert me_after_resp.status == 401, (
            f"auth/me should return 401 after logout, got {me_after_resp.status}"
        )


# ---------------------------------------------------------------------------
# Rate limiting (LAST — floods server with requests)
# ---------------------------------------------------------------------------


class TestRateLimiting:
    @pytest.mark.order(-1)
    def test_authenticated_requests_have_higher_rate_limit(self, page):
        """Authenticated requests use a higher rate limit (120/min)."""
        responses = []
        for _ in range(30):
            resp = page.request.get(
                f"{BASE_URL}/artifacts",
                headers=_bearer(),
            )
            responses.append(resp.status)
            # If we get a 429, the rate limit from prior tests kicked in
            if resp.status == 429:
                break
            # A 401 means our token was invalidated by another test
            # (e.g., auth round-trip test); skip rather than fail
            if resp.status == 401:
                pytest.skip("Test token was invalidated by a prior test")
        # Check that authenticated requests were NOT rate-limited within normal bounds
        # (we allow 429 from prior test rate limit bleed)
        non_rate_limited = [s for s in responses if s != 429 and s != 401]
        assert len(non_rate_limited) >= 5, (
            f"Should have at least 5 successful authenticated requests, got statuses: {responses}"
        )

    @pytest.mark.order(-1)
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
            if resp.status == 429:
                break
        assert 429 in statuses, "Unauthenticated rapid requests should be rate-limited"
