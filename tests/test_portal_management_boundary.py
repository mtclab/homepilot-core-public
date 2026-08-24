"""The guest portal and the operator API share one process - prove the wall.

`/guest/*` and `/invite/*` authenticate with a three-factor mTLS trust
(trusted source address + proxy shared secret + a verified-certificate header,
see `homepilot/portal/trust.py`); every management route authenticates with a
bearer token instead. Both surfaces are mounted on the SAME FastAPI app, and
only the front nginx keeps a guest's packets away from `/inventory`. If the
operator's proxy is ever misconfigured - or someone reaches the backend port
directly - the app itself is the last wall.

The property gated here: **a request carrying PERFECT guest trust cannot reach
ANY management route.** Perfect means genuinely valid, not malformed: the same
headers are asserted to WORK on a guest route in the same setup, so a false
pass from a typo'd header is distinguishable from real enforcement.

This is NOT the #405 route-scope guard. That one asserts every non-public route
declares a scope dependency; this one asserts the guest identity satisfies none
of them - a different failure mode (a route could carry a scope dep that a
future trust-aware dependency happens to satisfy).
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from homepilot import main
from homepilot.auth import deps as auth_deps
from homepilot.auth.deps import require_scope, require_token
from homepilot.config import Settings
from homepilot.main import _PUBLIC_ROUTES, _walk_api_routes
from homepilot.main import app as real_app
from homepilot.portal.trust import PROXY_SECRET_HEADER, assert_trusted_cn, load_trust

from .portal_support import CN_A, PROXY_IP, PROXY_SECRET, cert_headers, portal_settings

# A denial. Anything else means the request got further than "who are you?".
DENIED = (401, 403)

_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _dummy_path(path: str) -> str:
    """Substitute path params with a value no real row can carry."""
    return re.sub(r"\{[^}]+\}", "hp-boundary-probe", path)


def management_targets(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, full path) on `app` that is not on the public allowlist.

    Walked, never hand-listed: a hand list rots the moment someone adds a route,
    which is exactly the moment this gate has to fire.
    """
    targets: set[tuple[str, str]] = set()
    for path, route, _include_deps in _walk_api_routes(list(app.routes)):
        for method in route.methods or set():
            if method in ("HEAD", "OPTIONS"):
                continue
            if (method, path) in _PUBLIC_ROUTES:
                continue
            targets.add((method, path))
    return sorted(targets)


def _fire(client: TestClient, method: str, path: str, headers: dict[str, str]) -> Any:
    # The global per-IP rate limiter answers 429 before authentication ever
    # runs; a 429 would mask an open route as "denied". Clear it each time so
    # every probe reaches the real verdict.
    main._RATE_WINDOW.clear()
    return client.request(
        method,
        _dummy_path(path),
        headers=headers,
        json={} if method in _BODY_METHODS else None,
    )


def reachable_with_guest_trust(
    app: FastAPI,
    client: TestClient,
    headers: dict[str, str],
) -> list[tuple[str, str, int]]:
    """(method, path, status) for every management route the guest identity did
    NOT get denied on."""
    breached: list[tuple[str, str, int]] = []
    for method, path in management_targets(app):
        response = _fire(client, method, path, headers)
        if response.status_code not in DENIED:
            breached.append((method, path, response.status_code))
    return breached


@pytest.fixture
def boundary():
    """The REAL assembled app, wired so the guest trust headers are genuinely valid.

    `app.state` is process-global on the imported app, so it is snapshotted and
    restored - no other test module inherits this wiring.
    """
    snapshot = dict(real_app.state._state)

    repo = MagicMock()
    repo.db = MagicMock()
    repo.db.fetchall = AsyncMock(return_value=[])
    # No token exists: any bearer credential would be rejected anyway, and the
    # guest presents none at all.
    repo.get_token_by_prefix = AsyncMock(return_value=None)

    real_app.state.settings = Settings(
        portal_trusted_proxy=f"{PROXY_IP}/32",
        portal_proxy_secret=PROXY_SECRET,
        portal_cn_header="ssl-client-subject-dn",
        portal_verify_header="ssl-client-verify",
    )
    real_app.state.repo = repo
    # Every side-effect boundary a management handler could reach, mocked, so
    # "the handler ran" is observable as a call rather than inferred.
    real_app.state.provision_service = MagicMock()
    real_app.state.artifact_store = MagicMock()
    real_app.state.artifact_lifecycle = MagicMock()
    real_app.state.artifact_executor = MagicMock()
    real_app.state.agent_registry = MagicMock()
    real_app.state.agent_hub = MagicMock()
    real_app.state.task_runner = MagicMock()
    real_app.state.task_repo = MagicMock()
    real_app.state.inventory_service = MagicMock()
    real_app.state.kb_service = MagicMock()
    real_app.state.proxmox = MagicMock()

    client = TestClient(
        real_app,
        # The source address the portal trusts. Without this the FIRST factor
        # fails and every probe would be denied for the wrong reason - which is
        # why the guest-route positive control below is not optional.
        client=(PROXY_IP, 44444),
        raise_server_exceptions=False,
    )
    main._RATE_WINDOW.clear()
    try:
        yield SimpleNamespace(
            client=client,
            headers=cert_headers(),
            repo=repo,
            app=real_app,
        )
    finally:
        main._RATE_WINDOW.clear()
        real_app.state._state.clear()
        real_app.state._state.update(snapshot)


class TestTheGuestHeadersAreGenuinelyValid:
    """Positive control. Without this, a boundary gate that only ever sees
    denials proves nothing: malformed headers are denied everywhere too."""

    def test_guest_route_accepts_them(self, boundary):
        response = boundary.client.get("/guest/vms", headers=boundary.headers)
        assert response.status_code == 200, (
            "the trust headers are NOT valid on the guest surface, so every denial "
            f"below is meaningless: {response.status_code} {response.text[:200]}"
        )
        assert response.json() == {"items": [], "total": 0}

    def test_the_control_is_load_bearing_without_the_proxy_secret(self, boundary):
        """Teeth for the control itself: blank one factor and the guest route
        refuses - proof the 200 above came from the headers, not from the route
        being open to anyone."""
        headers = dict(boundary.headers)
        headers[PROXY_SECRET_HEADER] = ""
        response = boundary.client.get("/guest/vms", headers=headers)
        assert response.status_code == 403

    def test_trust_resolves_the_cn_from_exactly_these_headers(self, boundary):
        trust = load_trust(real_app.state.settings)
        assert assert_trusted_cn(PROXY_IP, boundary.headers, trust) == CN_A


class TestGuestTrustReachesNoManagementRoute:
    def test_every_management_route_denies_the_guest_identity(self, boundary):
        breached = reachable_with_guest_trust(boundary.app, boundary.client, boundary.headers)
        assert breached == [], (
            "a management route answered a request carrying only guest portal trust - "
            "the guest surface and the operator API share one process, so this is a "
            f"live privilege boundary breach, not a test gap: {breached}"
        )

    def test_the_walk_actually_covers_the_management_api(self):
        """An empty breach list must mean "nothing got through", never "nothing
        was probed" - the same failure mode #472 hit on the scope guard."""
        targets = management_targets(real_app)
        assert len(targets) >= 80, (
            f"only {len(targets)} management routes were probed - the walker is no "
            "longer landing on the API and the clean verdict means nothing"
        )
        # Spot-check the shapes that matter: a read, a mutation, an admin route.
        assert ("GET", "/inventory") in targets
        assert ("POST", "/artifacts") in targets
        assert ("DELETE", "/agents/{agent_id}") in targets

    def test_no_guest_or_invite_route_is_treated_as_management(self):
        """The public allowlist must still cover the whole guest surface - if a
        guest route fell off it, the walk above would demand a 401 from a route
        that is meant to answer 200 and the gate would be self-contradictory.

        Note the one-letter trap: `/guests/*` (plural) is the OPERATOR's
        provisioning API and belongs on the management side; only `/guest/*`
        (singular) is the friend's surface. The prefixes are matched with their
        trailing slash so the two never blur.
        """
        for method, path in management_targets(real_app):
            assert not path.startswith("/guest/"), (method, path)
            assert not path.startswith("/invite/"), (method, path)
        guest_surface = {
            path for _method, path in _PUBLIC_ROUTES if path.startswith(("/guest/", "/invite/"))
        }
        assert len(guest_surface) >= 6, guest_surface


class TestMutatingRoutesProduceNoSideEffect:
    """A non-2xx is not proof of nothing happening. For the routes that
    provision, execute and destroy, assert the boundary objects were never
    touched at all."""

    @staticmethod
    def _assert_untouched(mock: MagicMock, label: str) -> None:
        assert mock.mock_calls == [], (
            f"{label} was used on a guest-trust request: {mock.mock_calls}"
        )

    def test_provision_is_never_invoked(self, boundary):
        response = _fire(boundary.client, "POST", "/guests/provision", boundary.headers)
        assert response.status_code in DENIED
        self._assert_untouched(real_app.state.provision_service, "provision_service")

    def test_artifact_creation_never_reaches_the_store(self, boundary):
        response = _fire(boundary.client, "POST", "/artifacts", boundary.headers)
        assert response.status_code in DENIED
        self._assert_untouched(real_app.state.artifact_store, "artifact_store")
        self._assert_untouched(real_app.state.artifact_lifecycle, "artifact_lifecycle")

    def test_agent_deletion_never_reaches_the_registry(self, boundary):
        response = _fire(boundary.client, "DELETE", "/agents/{agent_id}", boundary.headers)
        assert response.status_code in DENIED
        self._assert_untouched(real_app.state.agent_registry, "agent_registry")

    def test_agent_exec_never_reaches_the_hub(self, boundary):
        response = _fire(boundary.client, "POST", "/agents/{agent_id}/exec", boundary.headers)
        assert response.status_code in DENIED
        self._assert_untouched(real_app.state.agent_hub, "agent_hub")

    def test_no_repository_write_is_attempted(self, boundary):
        """The one shared object a guest request DOES legitimately touch is the
        repo (the guest routes read hosts through it). Assert the management
        probes never got past the token lookup into a write."""
        for method, path in (
            ("POST", "/inventory"),
            ("PATCH", "/inventory/{host_id}"),
            ("DELETE", "/inventory/{host_id}"),
            ("POST", "/kb"),
        ):
            boundary.repo.reset_mock()
            response = _fire(boundary.client, method, path, boundary.headers)
            assert response.status_code in DENIED
            touched = {call[0].split(".")[0] for call in boundary.repo.mock_calls if call[0]}
            assert touched <= {"get_token_by_prefix"}, (
                f"{method} {path} reached the repository as a guest: {boundary.repo.mock_calls}"
            )


class TestTheManagementTokenPathIgnoresPortalHeaders:
    """`require_token` must derive NO principal from portal trust. The three
    portal headers are not credentials to it - they are not even read."""

    @pytest.fixture
    def token_app(self):
        app = FastAPI()

        @app.get("/whoami", dependencies=[Depends(require_scope("admin"))])
        async def whoami(principal: dict[str, Any] = Depends(require_token)):  # noqa: B008
            return principal

        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(return_value=None)
        app.state.repo = repo
        app.state.settings = portal_settings()
        return app

    def test_portal_headers_alone_are_unauthenticated(self, token_app):
        client = TestClient(token_app, client=(PROXY_IP, 44444))
        response = client.get("/whoami", headers=cert_headers())
        assert response.status_code == 401
        assert response.json()["detail"] == "Missing credentials"
        # It never even looked a token up: there was nothing token-shaped to look up.
        token_app.state.repo.get_token_by_prefix.assert_not_called()

    def test_the_token_path_never_consults_the_portal_header_names(self):
        """Static half: no amount of request shape can make the token path read
        a portal header, because the token path does not name them."""
        source = inspect.getsource(auth_deps)
        for header in (PROXY_SECRET_HEADER, "ssl-client-subject-dn", "ssl-client-verify"):
            assert header not in source, (
                f"{header} appears in homepilot.auth.deps - the management token path "
                "must not derive anything from portal trust headers"
            )
        # ...and the portal's own resolver is not imported there either.
        assert "portal" not in source


class TestTheBoundaryGateHasTeeth:
    """The walk above is only worth its runtime if it FLAGS a route that a guest
    can reach. Both teeth build a throwaway app with a management-shaped route
    and run the SAME probe helper over it."""

    @staticmethod
    def _client(app: FastAPI) -> TestClient:
        app.state.settings = portal_settings()
        return TestClient(app, client=(PROXY_IP, 44444), raise_server_exceptions=False)

    def test_a_management_route_without_a_token_dependency_is_flagged(self):
        app = FastAPI()

        @app.post("/inventory")
        async def forgot_the_token_dep():
            return {"created": True}

        breached = reachable_with_guest_trust(app, self._client(app), cert_headers())
        assert ("POST", "/inventory", 200) in breached

    def test_a_trust_aware_management_route_is_flagged(self):
        """The subtler failure: a management route that grows a dependency
        accepting the portal CN. It carries an auth dependency, so the scope
        guard is happy - and the guest walks straight in."""

        async def cn_of(request: Request) -> str:
            trust = load_trust(request.app.state.settings)
            peer = request.client.host if request.client else None
            return assert_trusted_cn(peer, request.headers, trust)

        app = FastAPI()

        @app.delete("/inventory/{host_id}")
        async def trust_aware(host_id: str, cn: str = Depends(cn_of)):
            return {"deleted": host_id, "by": cn}

        breached = reachable_with_guest_trust(app, self._client(app), cert_headers())
        assert ("DELETE", "/inventory/{host_id}", 200) in breached

    def test_a_token_gated_route_is_not_flagged(self):
        """The other half of teeth: the probe must not cry wolf on a route that
        IS gated, or the gate above passes for the wrong reason."""
        app = FastAPI()

        @app.post("/inventory", dependencies=[Depends(require_scope("write"))])
        async def gated():
            return {"created": True}

        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(return_value=None)
        app.state.repo = repo
        assert reachable_with_guest_trust(app, self._client(app), cert_headers()) == []
