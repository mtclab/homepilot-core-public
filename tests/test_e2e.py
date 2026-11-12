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
UI session behavior. The browser session logs in with HP_TEST_TOKEN itself:
neither login nor logout invalidates a token (logout clears the session
cookie only, #389), so only credentials a test destroys or needs weaker than
admin are minted - see the mint budget below.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from homepilot.auth.router import _TOKEN_CREATE_RATE_LIMIT

BASE_URL = os.environ.get("HP_TEST_URL", "http://localhost:8000")
TOKEN = os.environ.get("HP_TEST_TOKEN", "")
ADMIN_SECRET = os.environ.get("HP_TEST_ADMIN_SECRET", "")
UI = BASE_URL + "/ui"

pytestmark = pytest.mark.skipif(not TOKEN, reason="HP_TEST_TOKEN not set — skipping e2e tests")

# The token-mint budget. POST /auth/tokens allows _TOKEN_CREATE_RATE_LIMIT (5)
# attempts per client IP per 60 s - REFUSED attempts included, since the limit
# is checked ahead of authorization on purpose - and the whole run is one IP.
# The harness mints HP_TEST_TOKEN with the admin secret just before the run
# (the mirror's e2e workflow and scripts/gate-e2e.sh both do), and the run
# takes well under a minute, so every attempt below shares one window:
#
#   harness       HP_TEST_TOKEN                                     1
#   session_auth  scope_ro, the non-admin credential                1
#   TestAdminTokenCreate  an admin-bearer mint + a refused one      2
#   TestTokenCRUD  the panel's mint, which its revoke test revokes  1
#
# 3.9.0's suite made eight: session_auth also minted a session token, a
# round-trip token, a revoke target and a scope-test target - most from the
# days when logout deleted the token it logged out (#389 ended that). The
# sixth hit the limit, and session_auth slept out the window (~62 s) - the
# "event loop blocked 62027 ms" its e2e run failed on. An attempt over budget
# now FAILS, naming this table, instead of waiting: a new test that needs a
# fresh credential has to fit it, not stretch the run.
_HARNESS_MINTS = 1
_SUITE_MINTS = 4
assert _HARNESS_MINTS + _SUITE_MINTS <= _TOKEN_CREATE_RATE_LIMIT


def _mint_budget_spent(what: str) -> str:
    return (
        f"{what}: POST /auth/tokens answered 429 - the e2e run's mint budget "
        f"({_TOKEN_CREATE_RATE_LIMIT} per IP per 60 s: {_HARNESS_MINTS} by the harness, "
        f"{_SUITE_MINTS} by this suite) is spent. A test minted a token outside "
        "the budget table at the top of tests/test_e2e.py; fit it there."
    )


def _mint_with_admin_secret(label: str, scope: str) -> str:
    """Mint one token with the admin secret; any refusal fails the run."""
    req = urllib.request.Request(
        f"{BASE_URL}/auth/tokens",
        data=json.dumps({"label": label, "scope": scope}).encode(),
        headers={"Content-Type": "application/json", "x-hp-admin-secret": ADMIN_SECRET},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return str(json.loads(resp.read())["token"])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            pytest.fail(_mint_budget_spent(f"minting {label!r}"))
        pytest.fail(f"minting {label!r} failed: {e.code} {e.read()[:200]!r}")


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
    Persistent authenticated browser context, logged in with HP_TEST_TOKEN.

    Mints only the credential no existing one can stand in for (the mint
    budget at the top of this file): one weaker than admin. Nothing here waits
    out a rate limit - a refusal fails the run.
    """
    if not ADMIN_SECRET:
        pytest.skip("HP_TEST_ADMIN_SECRET not set")

    extra_tokens = {
        # The non-admin credential TestTokenScopeEnforcement holds.
        "scope_ro": _mint_with_admin_secret("e2e-scope-ro", "read_only"),
    }

    context = browser.new_context()
    resp = context.request.post(
        f"{BASE_URL}/auth/login",
        data=json.dumps({"token": TOKEN}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.ok, f"Session login failed: {resp.status} {resp.text()}"

    cookies = {c["name"]: c["value"] for c in context.cookies()}
    assert "hp_token" in cookies or "hp_csrf" in cookies, f"Login did not set cookies: {cookies}"

    csrf = cookies.get("hp_csrf", "")
    assert csrf, f"CSRF cookie not set: {cookies.keys()}"

    yield {
        "context": context,
        "csrf": csrf,
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

    def test_login_sets_cookie_and_redirects(self, page):
        login_resp = page.request.post(
            f"{BASE_URL}/auth/login",
            data=json.dumps({"token": TOKEN}),
            headers={"Content-Type": "application/json"},
        )
        assert login_resp.ok, f"Login failed: {login_resp.status}"
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        assert "hp_token" in cookies or "hp_csrf" in cookies
        # /artifacts is a pre-S4 address; the client-side redirect lands on
        # /changes (#514 S4). The journey asserts the DESTINATION works.
        page.goto(UI + "/artifacts", wait_until="networkidle")
        assert "/changes" in page.url


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
    # Every reachable console surface (#514 S5): the five S4 groups with their
    # sub-views, the host list, and settings. The pre-S4 addresses are covered
    # by their own redirect assertions, not repeated here.
    ROUTES: ClassVar[list[str]] = [
        "",
        "hosts",
        "changes",
        "changes/inbox",
        "changes/drift",
        "records/tasks",
        "records/journal",
        "records/kb",
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
        # Old address on purpose: this doubles as the redirect gate on the
        # shipped artifact (#514 S4) - /inventory must land on /hosts.
        auth_page.goto(f"{UI}/inventory", wait_until="networkidle")
        assert "/hosts" in auth_page.url.lower()

    def test_settings_page_shows_active_session(self, auth_page):
        auth_page.goto(f"{UI}/settings", wait_until="networkidle")
        body = auth_page.locator("body").inner_text()
        assert "Session active" in body or "session" in body.lower()


# ---------------------------------------------------------------------------
# Token create (uses Bearer auth)
# ---------------------------------------------------------------------------


class TestAdminTokenCreate:
    def test_an_admin_bearer_mints_without_the_secret(self, page):
        """The owner's rule since the tokens-are-minted-by-admins slice: a
        logged-in admin creates tokens, no shared secret needed. The e2e token
        IS an admin credential, so this must succeed."""
        resp = page.request.post(
            f"{BASE_URL}/auth/tokens",
            data='{"label": "ci-e2e-rule", "scope": "read"}',
            headers=_bearer_json(),
        )
        assert resp.status != 429, _mint_budget_spent("the admin-bearer mint")
        assert resp.status in (200, 201), f"Expected created, got {resp.status}"

    def test_anonymous_token_create_is_refused(self, page):
        """No bearer, no session, no secret - the refusal names the rule."""
        resp = page.request.post(
            f"{BASE_URL}/auth/tokens",
            data='{"label": "ci-anon", "scope": "read"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status != 429, _mint_budget_spent("the anonymous mint attempt")
        assert resp.status in (401, 403), f"Expected refusal, got {resp.status}"


# ---------------------------------------------------------------------------
# Token CRUD (uses cookie auth from session)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def crud_mint(session_auth) -> dict:
    """TestTokenCRUD's one mint, on the Tokens panel's own path (cookie
    session + CSRF + admin secret). The create test asserts on it and the
    revoke test revokes the token it made - a lifecycle in one budget slot."""
    resp = session_auth["context"].request.post(
        f"{BASE_URL}/auth/tokens",
        data=json.dumps({"label": "e2e-crud", "scope": "read_only"}),
        headers={**_csrf_headers(session_auth["csrf"]), "x-hp-admin-secret": ADMIN_SECRET},
    )
    assert resp.status != 429, _mint_budget_spent("TestTokenCRUD's mint")
    return {"status": resp.status, "text": resp.text()}


class TestTokenCRUD:
    @pytest.fixture(autouse=True)
    def _skip_no_admin_secret(self):
        if not ADMIN_SECRET:
            pytest.skip("HP_TEST_ADMIN_SECRET not set")

    def test_create_token_with_admin_secret(self, crud_mint):
        assert crud_mint["status"] == 201, (
            f"Expected 201, got {crud_mint['status']}: {crud_mint['text']}"
        )
        body = json.loads(crud_mint["text"])
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

    def test_revoke_token(self, auth_page, csrf_token, crud_mint):
        assert crud_mint["status"] == 201, f"nothing to revoke: {crud_mint['text']}"
        new_token = json.loads(crud_mint["text"])["token"]
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
        # The target is the run's own admin token: a read-only credential
        # revoking the admin is exactly the escalation this forbids. (Were it
        # allowed, every later test would fail too - loudly, as it should.)
        read_only_token = session_auth["extra_tokens"]["scope_ro"]
        target_prefix = TOKEN[:16]

        delete_resp = auth_page.request.delete(
            f"{BASE_URL}/auth/tokens/{target_prefix}",
            headers={"Authorization": f"Bearer {read_only_token}"},
        )
        assert delete_resp.status == 403, (
            f"Read-only token should not DELETE, got {delete_resp.status}"
        )
        still = auth_page.request.get(f"{BASE_URL}/auth/me", headers=_bearer())
        assert still.ok, f"the admin token stopped working: {still.status}"

    def test_read_only_token_cannot_access_admin_endpoints(
        self, auth_page, csrf_token, session_auth
    ):
        read_only_token = session_auth["extra_tokens"]["scope_ro"]

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
# Auth round-trip (with HP_TEST_TOKEN: logout ends the session, not the token)
# ---------------------------------------------------------------------------


class TestAuthRoundTrip:
    @pytest.fixture(autouse=True)
    def _skip_no_admin_secret(self):
        if not ADMIN_SECRET:
            pytest.skip("HP_TEST_ADMIN_SECRET not set")

    def test_login_me_logout_me(self, page):
        login_resp = page.request.post(
            f"{BASE_URL}/auth/login",
            data=json.dumps({"token": TOKEN}),
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

        # Logout ended the cookie session...
        me_after_resp = page.request.get(f"{BASE_URL}/auth/me")
        assert me_after_resp.status == 401, (
            f"auth/me should return 401 after logout, got {me_after_resp.status}"
        )
        # ...and only the session: the token itself still works for every other
        # holder (#389 - logout used to delete it).
        bearer_resp = page.request.get(f"{BASE_URL}/auth/me", headers=_bearer())
        assert bearer_resp.ok, f"logout destroyed the token: {bearer_resp.status}"


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


def _landed(url: str) -> str:
    """Path, query and fragment of a URL - what a redirect decided."""
    parts = urlsplit(url)
    return (
        parts.path
        + (f"?{parts.query}" if parts.query else "")
        + (f"#{parts.fragment}" if parts.fragment else "")
    )


class TestEveryOldAddressStillLands:
    """The redirect promise, proven on the SHIPPED artifact: a bookmark from
    any earlier install lands on the page that holds it now - S4's moves
    (#514) and UI slice 1's (3.9.0: tokens to Assistants, agents to Hosts >
    Agents, Monitoring, Guests and Tokens out of Settings). Each case asserts
    the EXACT landing, query and fragment included, so a wrong target cannot
    pass by sharing a prefix with the right one (3.9.0's loose match let
    /agents "pass" on /ui/hosts)."""

    CASES: ClassVar[list[tuple[str, str]]] = [
        ("inventory", "/ui/hosts"),
        ("agents", "/ui/hosts/agents"),
        ("tokens", "/ui/assistants"),
        ("artifacts", "/ui/changes"),
        ("review", "/ui/changes/inbox"),
        ("changes/review", "/ui/changes/inbox"),
        ("drift", "/ui/changes/drift"),
        ("tasks", "/ui/records/tasks"),
        ("journal", "/ui/records/journal"),
        ("kb", "/ui/records/kb"),
        ("records", "/ui/records/tasks"),
        ("monitoring", "/ui/monitoring/alerts"),
        # Sectors that left Settings, by `?tab=` and by the pre-tab fragment.
        ("settings?tab=monitoring", "/ui/monitoring/rules"),
        ("settings#monitoring", "/ui/monitoring/rules"),
        ("settings#alerts", "/ui/monitoring/rules"),
        ("settings?tab=guests", "/ui/guests"),
        ("settings#guests", "/ui/guests"),
        ("settings?tab=tokens", "/ui/assistants"),
        ("settings#tokens", "/ui/assistants"),
        # Intent travels: the query and the fragment reach the new home.
        ("inventory?q=web01", "/ui/hosts?q=web01"),
        ("agents?q=web01#join", "/ui/hosts/agents?q=web01#join"),
        ("tokens#mint", "/ui/assistants#mint"),
        ("settings?tab=guests&q=web01", "/ui/guests?q=web01"),
    ]

    def test_every_pre_move_url_redirects(self, auth_page):
        # The redirect is decided by the shipped bundle alone, so only the shell
        # (/ui/...) and the console's session check (/auth/me) reach the server;
        # each destination's data calls are cut off. A console page load fires
        # ~10 API requests, and the run shares ONE per-IP authenticated budget
        # (300/min): loading every destination in full spent it and pushed the
        # rest of the run into 429s and the login page.
        def only_the_shell(route):
            path = urlsplit(route.request.url).path
            if path.startswith("/ui/") or path == "/auth/me":
                route.continue_()
            else:
                route.abort()

        auth_page.route(f"{BASE_URL}/**", only_the_shell)
        misses: list[str] = []
        for old_route, target in self.CASES:
            auth_page.goto(f"{UI}/{old_route}", wait_until="networkidle")
            try:
                # The redirect is client-side (Redirect.svelte, the Settings
                # page's onMount): wait for the exact landing, not a moment.
                auth_page.wait_for_url(lambda url, t=target: _landed(url) == t, timeout=5000)
            except PlaywrightTimeoutError:
                misses.append(f"/{old_route} -> {_landed(auth_page.url)} (wanted {target})")
        assert not misses, "stale addresses: " + "; ".join(misses)


class TestThePhoneViewport:
    """The console on a phone (#445 B3, re-proven on the shipped path): the
    shell offers the menu, navigation works, and the fleet page renders -
    not merely 'does not crash'."""

    def test_hosts_reachable_and_rendered_at_phone_size(self, session_auth):
        context = session_auth["context"]
        page = context.new_page()
        try:
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{UI}/", wait_until="networkidle")
            # The shell must offer a menu control at this width. Its accessible
            # name comes from an sr-only span ("Open navigation"), so ask by
            # ROLE + NAME - the way assistive tech does - not by aria-label.
            import re as _re

            toggle = page.get_by_role("button", name=_re.compile("open navigation", _re.I))
            assert toggle.count() > 0, "no menu control in the phone-width shell"
            toggle.first.click()
            # ...whose Hosts entry actually navigates.
            page.get_by_role("link", name="Hosts").first.click()
            page.wait_for_url("**/ui/hosts**")
            assert page.locator("h1").first.inner_text().strip() == "Hosts"
            # And nothing forces a horizontal scroll.
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 1, f"the page scrolls sideways by {overflow}px on a phone"
        finally:
            page.close()


# ---------------------------------------------------------------------------
# 3.10.0 journeys that need guests: the e2e fleet server
# ---------------------------------------------------------------------------

_FLEET_ADMIN_SECRET = "e2e-fleet-admin-secret"  # pragma: allowlist secret - throwaway fixture
_PORTAL_SECRET = "e2e-fleet-portal-secret"  # pragma: allowlist secret - throwaway fixture
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="class")
def fleet():
    """The REAL app and the BUILT UI over a fake Proxmox (tests/e2e_fleet_server.py).

    The live instance these tests otherwise drive has no Proxmox, so a journey
    that needs a guest - power from the row menu, an address clash, a change
    that runs - starts this second server: same entry point, same lifespan and
    task runner, same web/dist, with only Proxmox answered by the suite's
    FakeCluster. Its own process, so its own rate windows: its one token mint
    is outside the budget table at the top of this file.
    """
    import shutil
    import signal
    import subprocess
    import sys
    import tempfile

    if not os.path.isfile(os.path.join(_REPO_ROOT, "web", "dist", "index.html")):
        pytest.fail("web/dist is not built; the fleet journeys drive the BUILT UI (npm run build)")
    port = _free_port()
    # Under $HOME: create_app_state refuses an artifacts dir in /tmp.
    data = tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix=".hp-e2e-fleet-")
    home = os.path.join(data, "home")
    os.makedirs(home)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "LANG": "C.UTF-8",
        "PYTHONPATH": os.path.join(_REPO_ROOT, "src"),
        "HP_SECRET_KEY": "e2e-fleet-secret",
        "HP_VAULT_PASSPHRASE": "e2e-fleet-pass",
        "HP_ADMIN_SECRET": _FLEET_ADMIN_SECRET,
        # The guest portal trusts loopback as its proxy here, so a test can
        # speak as the friend's mTLS proxy would.
        "HP_PORTAL_TRUSTED_PROXY": "127.0.0.1/32",
        "HP_PORTAL_PROXY_SECRET": _PORTAL_SECRET,
        "PROXMOX_SECRETS": "/nonexistent",
    }
    log_path = os.path.join(data, "server.log")
    log = open(log_path, "wb")  # noqa: SIM115 - closed in the finally below
    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(_REPO_ROOT, "tests", "e2e_fleet_server.py"),
            os.path.join(data, "hp"),
            str(port),
        ],
        cwd=data,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"

    def server_log() -> str:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return f.read()[-3000:]

    def failure_log() -> str:
        """The server log for a failure message: from the first trouble to the end.

        A fixed 3000-character tail cut the traceback off whenever the shutdown
        that follows a failed start logged enough after it, so the message said
        the server exited without saying why. This keeps everything from the
        first WARNING/ERROR/Traceback on (the startup banner before it is noise),
        and fails with pytrace off so pytest does not truncate it either.
        """
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        marks = [
            i
            for i in (text.find(" WARNING "), text.find("ERROR"), text.find("Traceback"))
            if i >= 0
        ]
        start = text.rfind("\n", 0, min(marks)) + 1 if marks else max(0, len(text) - 3000)
        return text[start:][-30000:]

    try:
        deadline = time.monotonic() + 90
        while "E2E_FLEET_READY" not in server_log():
            if proc.poll() is not None:
                pytest.fail(
                    f"the fleet server exited (code {proc.returncode}):\n{failure_log()}",
                    pytrace=False,
                )
            if time.monotonic() >= deadline:
                pytest.fail(f"the fleet server never got ready:\n{failure_log()}", pytrace=False)
            time.sleep(0.2)
        req = urllib.request.Request(
            f"{base}/auth/tokens",
            data=json.dumps({"label": "e2e-fleet", "scope": "full"}).encode(),
            headers={"Content-Type": "application/json", "x-hp-admin-secret": _FLEET_ADMIN_SECRET},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            token = str(json.loads(resp.read())["token"])
        yield {"base": base, "token": token, "log": server_log}
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        shutil.rmtree(data, ignore_errors=True)


def _fleet_api(
    fleet, method: str, path: str, body: dict | None = None, headers: dict | None = None
):
    """(status, parsed JSON or text) for one call to the fleet server."""
    h = {"Authorization": f"Bearer {fleet['token']}", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(fleet["base"] + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def _fleet_host(fleet, name: str) -> dict:
    status, body = _fleet_api(fleet, "GET", f"/inventory?q={name}")
    assert status == 200, body
    (host,) = [h for h in body["items"] if h["hostname"] == name]
    return host


def _fleet_page(fleet, browser, width: int = 1280, height: int = 900, theme: str | None = None):
    context = browser.new_context(viewport={"width": width, "height": height})
    if theme:
        context.add_init_script(
            f"try {{ localStorage.setItem('hp-theme', '{theme}'); }} catch (e) {{}}"
        )
    resp = context.request.post(
        f"{fleet['base']}/auth/login",
        data=json.dumps({"token": fleet["token"]}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.ok, f"fleet login failed: {resp.status} {resp.text()}"
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return context, page, errors


def _no_sideways_scroll(page) -> None:
    overflow = page.evaluate(
        "Math.max(document.documentElement.scrollWidth - document.documentElement.clientWidth,"
        " ...Array.from(document.querySelectorAll('main')).map(m => m.scrollWidth - m.clientWidth))"
    )
    assert overflow <= 1, f"the page scrolls sideways by {overflow}px"


def _start_spec(name: str, vmid: int) -> dict:
    """A proxmox-api-sequence that starts one guest: a change that RUNS on the fake."""
    return {
        "kind": "proxmox-api-sequence",
        "intent": f"Start {name}",
        "body": (
            f"Start {name}.\n\n```yaml proxmox-api-spec\nsteps:\n  - id: start\n"
            "    method: POST\n"
            "    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/status/start\n```\n"
        ),
        "target": {"kind": "vm", "vmid": vmid, "node": "pve1", "host": name},
        "idempotence": "declared-natural",
        "requires_snapshot": False,
    }


class TestFleetJourneys:
    """The 3.10.0 lanes' journeys on the built UI and the real backend.

    Each is the outcome an operator (or a friend) sees, asserted where they see
    it and re-read from the server - not that a call returned.
    """

    def _open_menu(self, page, name: str):
        from playwright.sync_api import expect

        page.get_by_role("button", name=f"Actions for {name}").first.click()
        menu = page.get_by_role("menu", name=f"Actions for {name}")
        expect(menu).to_be_visible()
        expect(menu.get_by_text("Reading its state from Proxmox")).to_have_count(0, timeout=10000)
        return menu

    def _row(self, page, name: str):
        return page.locator("tr", has=page.get_by_role("link", name=name, exact=True))

    def _open_groups(self, page) -> None:
        """Expand every collapsed fleet group: a settled, healthy row moves to
        Managed on reload, and that group starts collapsed on a fleet this size."""
        from playwright.sync_api import expect

        # The list has loaded (its group headings are rendered) before counting.
        expect(page.get_by_role("button", name=re.compile(r"^Managed \(\d+\)$"))).to_be_visible()
        groups = page.locator("button[aria-expanded=false]", has_text=re.compile(r"\(\d+\)$"))
        for _ in range(groups.count()):
            groups.nth(0).click()

    # -- Hosts row menu (#803) --------------------------------------------------

    def test_row_menu_start_on_a_stopped_vm_ends_running(self, fleet, browser):
        from playwright.sync_api import expect

        context, page, errors = _fleet_page(fleet, browser)
        try:
            page.goto(f"{fleet['base']}/ui/hosts")
            row = self._row(page, "forge-dev")
            expect(row.get_by_text("stopped", exact=True)).to_be_visible()
            menu = self._open_menu(page, "forge-dev")
            menu.get_by_role("menuitem", name="Start", exact=True).click()
            menu.get_by_role("menuitem", name="Confirm start").click()
            expect(page.get_by_text("forge-dev started and is running.")).to_be_visible(
                timeout=30000
            )
            expect(row.get_by_text("running", exact=True)).to_be_visible()
            # The server says so too: the settled state is written on the row.
            assert _fleet_host(fleet, "forge-dev")["pve_status"] == "running"
            page.reload()
            self._open_groups(page)
            expect(self._row(page, "forge-dev").get_by_text("running", exact=True)).to_be_visible()
            assert errors == []
        finally:
            context.close()

    def test_a_start_into_an_address_clash_names_the_holder_in_the_row(self, fleet, browser):
        from playwright.sync_api import expect

        context, page, errors = _fleet_page(fleet, browser)
        try:
            page.goto(f"{fleet['base']}/ui/hosts")
            menu = self._open_menu(page, "mirror")
            menu.get_by_role("menuitem", name="Start", exact=True).click()
            menu.get_by_role("menuitem", name="Confirm start").click()
            alert = page.get_by_role("alert").filter(has_text="Start refused")
            expect(alert).to_contain_text(
                "Address 192.0.2.20 is used by relay (VMID 112)", timeout=15000
            )
            expect(alert).to_contain_text("Nothing was sent")
            expect(self._row(page, "mirror").get_by_text("stopped", exact=True)).to_be_visible()
            assert _fleet_host(fleet, "mirror")["pve_status"] == "stopped"
            assert errors == []
        finally:
            context.close()

    @pytest.mark.parametrize("theme", ["day", "night"])
    def test_the_row_menu_reads_as_one_list(self, fleet, browser, theme):
        """3.10.0 walk: in Day every menu item read as its own outlined button.
        A menu is one list - no box around an item, no gap between items."""
        from playwright.sync_api import expect

        context, page, errors = _fleet_page(fleet, browser, theme=theme)
        try:
            page.goto(f"{fleet['base']}/ui/hosts")
            menu = self._open_menu(page, "llm-test")
            expect(menu.get_by_role("menuitem", name="Start", exact=True)).to_be_visible()
            looks = menu.evaluate(
                """m => ({
                    gap: getComputedStyle(m).rowGap,
                    items: Array.from(m.querySelectorAll('[role=menuitem]'))
                        .filter(e => e.classList.contains('btn-ghost'))
                        .map(e => {
                            const cs = getComputedStyle(e);
                            return {
                                text: e.textContent.trim(),
                                width: cs.borderTopWidth,
                                color: cs.borderTopColor,
                            };
                        }),
                })"""
            )
            assert looks["gap"] in ("0px", "normal"), looks
            assert len(looks["items"]) >= 6, looks
            boxed = [
                i
                for i in looks["items"]
                if i["width"] != "0px" and not re.fullmatch(r"rgba\(\d+, \d+, \d+, 0\)", i["color"])
            ]
            assert boxed == [], f"menu items drawn as boxes in {theme}: {boxed}"
            assert errors == []
        finally:
            context.close()

    def test_the_hosts_list_is_a_card_list_without_sideways_scroll_at_390(self, fleet, browser):
        from playwright.sync_api import expect

        context, page, errors = _fleet_page(fleet, browser, width=390, height=844)
        try:
            page.goto(f"{fleet['base']}/ui/hosts")
            cards = page.locator("[data-testid=host-cards]")
            expect(cards).to_be_visible()
            expect(cards.get_by_role("link", name="mirror", exact=True)).to_be_visible()
            expect(page.locator("table.hosts-table")).to_have_count(0)
            _no_sideways_scroll(page)
            assert errors == []
        finally:
            context.close()

    # -- Guest portal (#803) ----------------------------------------------------

    def test_portal_start_into_an_address_clash_answers_409_naming_no_other_machine(self, fleet):
        alice = _fleet_host(fleet, "alice-web")
        status, body = _fleet_api(
            fleet,
            "POST",
            f"/guest/vms/{alice['id']}/power",
            {"action": "start"},
            # What the friend's mTLS proxy sets; no operator credential.
            headers={
                "Authorization": "",
                "ssl-client-subject-dn": "CN=alice,OU=lab,O=Example",
                "ssl-client-verify": "SUCCESS",
                "x-hp-portal-secret": _PORTAL_SECRET,
            },
        )
        assert status == 409, body
        assert body["code"] == "address_clash", body
        text = json.dumps(body)
        for other in ("relay", "112", "mirror", "pve1"):
            assert other not in text, f"the portal told a friend {other!r}: {text}"
        assert _fleet_host(fleet, "alice-web")["pve_status"] == "stopped"

    # -- Command palette (#806) -------------------------------------------------

    def test_ctrl_k_llm_enter_lands_on_the_host_page(self, fleet, browser):
        from playwright.sync_api import expect

        llm = _fleet_host(fleet, "llm")
        context, page, errors = _fleet_page(fleet, browser)
        try:
            page.goto(f"{fleet['base']}/ui/")
            expect(page.locator("h1").first).to_be_visible()
            # The shell mounts the palette (and its Ctrl+K listener) only once
            # the session check answers, which on a loaded runner is after the
            # first heading paints: a press before that reaches no listener.
            # "Signed in as" is rendered by that same answer.
            expect(page.get_by_text("Signed in as", exact=False)).to_be_visible()
            page.keyboard.press("Control+k")
            box = page.get_by_role("combobox")
            expect(box).to_be_focused()
            box.type("llm")
            # The host answer is in (debounced, remote) and on top before Enter.
            option = page.locator(f'[role=option][data-href="/hosts/{llm["id"]}"]')
            expect(option).to_be_visible()
            expect(option).to_have_attribute("aria-selected", "true")
            page.keyboard.press("Enter")
            page.wait_for_url(f"**/ui/hosts/{llm['id']}")
            expect(page.get_by_role("heading", name="llm")).to_be_visible()
            assert errors == []
        finally:
            context.close()

    # -- Monitoring > Alerts (#806) ---------------------------------------------

    @pytest.mark.parametrize("width", [1280, 390])
    def test_monitoring_alerts_renders_what_is_watched_and_the_deliveries(
        self, fleet, browser, width
    ):
        from playwright.sync_api import expect

        context, page, errors = _fleet_page(fleet, browser, width=width, height=900)
        try:
            page.goto(f"{fleet['base']}/ui/monitoring/alerts")
            status, inventory = _fleet_api(fleet, "GET", "/inventory")
            assert status == 200, inventory
            coverage = page.locator("[data-alerts-coverage]")
            expect(coverage).to_contain_text("Watching")
            # Every host read, none with a connected agent (no hub here).
            expect(coverage).to_contain_text(f"0/{inventory['total']} hosts")
            expect(coverage).to_contain_text("Not watched")
            expect(page.get_by_text(re.compile(r"^Deliveries in the last"))).to_be_visible()
            _no_sideways_scroll(page)
            assert errors == []
        finally:
            context.close()

    # -- Changes > Inbox (#805) -------------------------------------------------

    def test_a_proposal_card_reads_on_a_phone(self, fleet, browser):
        """3.10.0 walk: at 390 px the review card's three buttons sat on its
        title (one word a line) and Reject ran off the card."""
        from playwright.sync_api import expect

        status, body = _fleet_api(fleet, "POST", "/artifacts", _start_spec("llm-test", 121))
        assert status == 200, body
        context, page, errors = _fleet_page(fleet, browser, width=390, height=844)
        try:
            page.goto(f"{fleet['base']}/ui/changes/inbox")
            card = page.get_by_test_id("review-card").filter(has_text="Start llm-test")
            expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()
            box = card.evaluate(
                """c => {
                    const r = (el) => el.getBoundingClientRect();
                    const what = c.querySelector('[data-card-what]');
                    const acts = c.querySelector('[data-card-actions]');
                    return {
                        card: r(c).right,
                        what: { w: r(what).width, bottom: r(what).bottom },
                        acts: { top: r(acts).top },
                        buttons: Array.from(acts.querySelectorAll('button')).map((b) => r(b).right),
                    };
                }"""
            )
            # The title has the card's width, the actions sit under it, and
            # every button stays inside the card.
            assert box["what"]["w"] > 200, box
            assert box["acts"]["top"] >= box["what"]["bottom"] - 1, box
            assert max(box["buttons"]) <= box["card"] + 0.5, box
            _no_sideways_scroll(page)
            assert errors == []
        finally:
            context.close()

    def test_approve_and_apply_runs_a_proposal_and_approve_only_stays_ready(self, fleet, browser):
        from playwright.sync_api import expect

        status, one = _fleet_api(fleet, "POST", "/artifacts", _start_spec("batch-1", 141))
        assert status == 200, one
        status, two = _fleet_api(fleet, "POST", "/artifacts", _start_spec("batch-2", 142))
        assert status == 200, two
        context, page, errors = _fleet_page(fleet, browser)
        try:
            page.goto(f"{fleet['base']}/ui/changes/inbox")
            first = page.get_by_test_id("review-card").filter(has_text="Start batch-1")
            first.get_by_role("button", name="Approve and apply").click()
            # One action: proposed -> applied, and it says so on the page.
            done = page.get_by_test_id("finished-card").filter(has_text="Start batch-1")
            expect(done).to_be_visible(timeout=30000)
            status, detail = _fleet_api(fleet, "GET", f"/artifacts/{one['id']}")
            assert detail["frontmatter"]["status"] == "applied", detail["frontmatter"]

            second = page.get_by_test_id("review-card").filter(has_text="Start batch-2")
            second.get_by_role("button", name="Approve only").click()
            ready = page.get_by_test_id("ready-card").filter(has_text="Start batch-2")
            expect(ready).to_be_visible(timeout=15000)
            expect(page.get_by_role("heading", name="Ready to run")).to_be_visible()
            expect(ready.get_by_role("button", name="Apply")).to_be_visible()
            status, detail = _fleet_api(fleet, "GET", f"/artifacts/{two['id']}")
            assert detail["frontmatter"]["status"] == "approved", detail["frontmatter"]
            assert detail["active_task"] is None
            # Nothing ran for it.
            assert _fleet_host(fleet, "batch-2")["pve_status"] == "stopped"
            page.reload()
            expect(
                page.get_by_test_id("ready-card").filter(has_text="Start batch-2")
            ).to_be_visible()
            assert errors == []
        finally:
            context.close()
