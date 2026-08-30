from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from homepilot.main import app

    return TestClient(app)


def _setup_state(client, db=None, proxmox=None, vault=None, settings=None):
    # `app` is a module-level singleton, so its state SURVIVES between tests.
    # Anything a test attaches and does not remove silently becomes the next
    # test's starting condition - which is how a passing hub test made an
    # unrelated one fail here. Cleared on every setup so each test states its
    # own world.
    for leaked in ("agent_registry", "agent_hub", "mcp_app", "proxmox_host"):
        if hasattr(client.app.state, leaked):
            delattr(client.app.state, leaked)
    if db is not None:
        client.app.state.db = db
    if proxmox is not None:
        client.app.state.proxmox = proxmox
    else:
        if hasattr(client.app.state, "proxmox"):
            del client.app.state.proxmox
    if vault is not None:
        client.app.state.vault = vault
    else:
        if hasattr(client.app.state, "vault"):
            del client.app.state.vault
    if settings is not None:
        client.app.state.settings = settings
    else:
        if not hasattr(client.app.state, "settings"):
            client.app.state.settings = MagicMock(
                proxmox_host="", agent_hub_enabled=False, cors_origins="http://localhost:5173"
            )


class TestTheAgentHubVerdictIsAskedOfTheHub:
    """/health is the LIVENESS probe an orchestrator acts on.

    It reported `agent_hub: ok` from `agent_registry is not None` alone. The
    registry object is built whether or not the hub ever bound, so a hub that
    refused its own transport - the case `agent_hub_disabled_reason` exists to
    describe - was reported healthy while no managed host could reach it. The
    same mistake the MCP check was fixed for in #382, one block further up.
    """

    @staticmethod
    def _state(client, *, listening: bool, enabled: bool = True):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        settings = MagicMock(agent_hub_enabled=enabled, cors_origins="http://localhost:5173")
        settings.proxmox_host = ""
        _setup_state(client, db=mock_db, proxmox=None, vault=None, settings=settings)

        registry = MagicMock()
        registry.list_connected = MagicMock(return_value=[])
        hub = MagicMock()
        hub.is_listening = MagicMock(return_value=listening)
        client.app.state.agent_registry = registry
        client.app.state.agent_hub = hub

    def test_a_hub_that_never_bound_is_not_ok(self, client):
        self._state(client, listening=False)

        checks = client.get("/health").json()["checks"]

        assert checks["agent_hub"] == "error", (
            "a hub that is not listening was reported healthy to the liveness probe"
        )

    def test_a_listening_hub_is_ok(self, client):
        self._state(client, listening=True)

        checks = client.get("/health").json()["checks"]

        assert checks["agent_hub"] == "ok"
        assert checks["agents_connected"] == "0"


class TestProxmoxIsJudgedFromTheResolvedAddress:
    """#642 - #631's bug, unfixed on the liveness surface.

    An install claimed through the web UI keeps its hypervisor in the vault, so
    `settings.proxmox_host` is empty and the resolved address lives on the app
    state. /health read settings alone, so a vault-configured install whose
    client failed to build answered `not_configured` - an OFF-BY-CHOICE verdict,
    which the rollup counts as HEALTHY - about a hypervisor it is configured to
    use and cannot reach.
    """

    @staticmethod
    def _no_client(client, *, resolved_host: str):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        settings.proxmox_host = ""  # the ENV half says nothing
        _setup_state(client, db=mock_db, proxmox=None, vault=None, settings=settings)
        client.app.state.proxmox_host = resolved_host
        return client.get("/health").json()

    def test_a_vault_configured_hypervisor_reads_unreachable_not_unconfigured(self, client):
        data = self._no_client(client, resolved_host="pve.vault.example")

        assert data["checks"]["proxmox"] == "unreachable", (
            "a configured-but-unreachable hypervisor was reported as not configured"
        )
        assert data["status"] != "ok", "an unreachable hypervisor was rolled up as healthy"

    def test_a_genuinely_unconfigured_hypervisor_still_says_so(self, client):
        """The honest arm: no address anywhere is still an off-by-choice answer."""
        data = self._no_client(client, resolved_host="")

        assert data["checks"]["proxmox"] == "not_configured"


class TestHealthEndpoint:
    def test_all_ok(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_vault.ensure_master_identity = AsyncMock(return_value="age1x")
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["vault"] == "ok"
        assert data["checks"]["proxmox"] == "not_configured"

    def test_db_error_yields_down(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=Exception("connection lost"))
        mock_db.fetchall = AsyncMock(side_effect=Exception("connection lost"))
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_vault.ensure_master_identity = AsyncMock(return_value="age1x")
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "down"
        assert data["checks"]["database"] in ("connection lost", "error")

    def test_proxmox_unreachable_returns_false(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_proxmox = MagicMock()
        mock_proxmox.test_connection = AsyncMock(return_value=False)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_vault.ensure_master_identity = AsyncMock(return_value="age1x")
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = "pve.example.com"

        _setup_state(
            client, db=mock_db, proxmox=mock_proxmox, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "unreachable"
        # Proxmox being unreachable is worth surfacing, but HomePilot is serving
        # and a restart would not bring the hypervisor back (#470).
        assert data["status"] == "degraded"
        assert resp.status_code == 200

    def test_proxmox_unreachable_raises(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_proxmox = MagicMock()
        mock_proxmox.test_connection = AsyncMock(side_effect=Exception("timeout"))
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_vault.ensure_master_identity = AsyncMock(return_value="age1x")
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = "pve.example.com"

        _setup_state(
            client, db=mock_db, proxmox=mock_proxmox, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "unreachable"
        # Proxmox being unreachable is worth surfacing, but HomePilot is serving
        # and a restart would not bring the hypervisor back (#470).
        assert data["status"] == "degraded"
        assert resp.status_code == 200

    def test_proxmox_ok(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_proxmox = MagicMock()
        mock_proxmox.test_connection = AsyncMock(return_value=True)
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_vault.ensure_master_identity = AsyncMock(return_value="age1x")
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = "pve.example.com"

        _setup_state(
            client, db=mock_db, proxmox=mock_proxmox, vault=mock_vault, settings=mock_settings
        )
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "ok"
        assert data["status"] == "ok"

    def test_vault_locked(self, client):
        from homepilot.vault import VaultError

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(side_effect=VaultError("locked"))
        mock_vault.ensure_master_identity = AsyncMock(side_effect=VaultError("locked"))
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        # A locked vault is real trouble and stays visible - but the process is
        # serving, and killing it does not unlock anything (#470).
        assert resp.status_code == 200
        data = resp.json()
        assert data["checks"]["vault"] == "locked"
        assert data["status"] == "degraded"

    def test_vault_not_configured(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=None, settings=mock_settings)
        # vault=None means not configured, remove it from state
        if hasattr(client.app.state, "vault"):
            del client.app.state.vault
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["vault"] == "not_configured"
        assert data["status"] == "ok"

    def test_db_not_initialized(self, client):
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""
        _setup_state(client, settings=mock_settings)
        # Ensure db is not on state
        if hasattr(client.app.state, "db"):
            del client.app.state.db
        if hasattr(client.app.state, "vault"):
            del client.app.state.vault
        if hasattr(client.app.state, "proxmox"):
            del client.app.state.proxmox
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["checks"]["database"] in ("not initialized", "error")
        assert data["status"] == "down"

    def test_proxmox_not_configured(self, client):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_vault.ensure_master_identity = AsyncMock(return_value="age1x")
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["proxmox"] == "not_configured"

    def test_degraded_when_vault_down_db_ok(self, client):
        from homepilot.vault import VaultError

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(side_effect=VaultError("locked"))
        mock_vault.ensure_master_identity = AsyncMock(side_effect=VaultError("locked"))
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        data = resp.json()
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["vault"] == "locked"
        assert data["checks"]["proxmox"] == "not_configured"

    def test_degraded_still_serves_200(self, client):
        """Degraded is not dead.

        This test used to assert 503 and was the contract that made a serving
        instance look unhealthy to everything reading the probe (#470).
        """
        from homepilot.vault import VaultError

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(side_effect=VaultError("locked"))
        mock_vault.ensure_master_identity = AsyncMock(side_effect=VaultError("locked"))
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""

        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"


class TestMetricsEndpoint:
    def test_metrics_returns_200(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_content_type(self, client):
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_metrics_contains_process_info(self, client):
        resp = client.get("/metrics")
        text = resp.text
        assert "process_" in text or "python_" in text

    def test_metrics_exposed_without_auth(self, client):
        resp = client.get("/metrics")
        assert resp.status_code != 401

    def test_metrics_not_rate_limited(self, client):
        for _ in range(65):
            client.get("/metrics")
        resp = client.get("/metrics")
        assert resp.status_code == 200


class TestCORSWildcardGuard:
    def test_wildcard_origin_disables_credentials(self):
        from homepilot.main import validate_cors_config

        result = validate_cors_config(type("S", (), {"cors_origins": "*"})())
        assert result["allow_credentials"] is False
        assert result["misconfigured"] is True

    def test_explicit_origins_keep_credentials(self):
        from homepilot.main import validate_cors_config

        result = validate_cors_config(type("S", (), {"cors_origins": "http://localhost:5173"})())
        assert result["allow_credentials"] is True
        assert result["misconfigured"] is False


class TestRootRedirect:
    def test_bare_root_redirects_to_ui(self, client):
        # GET / used to 404 behind the proxy (#284); now it sends the operator
        # to the web UI regardless of proxy root_path.
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/ui/"


class TestMetricPathTemplating:
    def test_metric_path_uses_route_template(self):
        from unittest.mock import MagicMock

        from homepilot.main import _metric_path

        req = MagicMock()
        req.scope = {"route": MagicMock(path="/agents/{agent_id}")}
        assert _metric_path(req) == "/agents/{agent_id}"

    def test_metric_path_buckets_unmatched(self):
        from unittest.mock import MagicMock

        from homepilot.main import _metric_path

        req = MagicMock()
        req.scope = {}
        assert _metric_path(req) == "<unmatched>"


class TestLivenessMeansServing:
    """/health is the liveness probe, so it answers one question: can this
    process serve requests (#470).

    The bug these gates forbid: any check in {error, unreachable, locked,
    misconfigured} made the whole instance report `down` with HTTP 503. The
    compose healthcheck calls this endpoint every 30s, so a fully serving
    HomePilot was advertised as unhealthy over a subsystem no restart can
    repair - a vault waiting to be unlocked, an unreachable embedding service,
    or a hub that refused its transport. #468 stopped the control plane dying;
    this stopped it looking dead to everything that reads the probe.

    Teeth: restore `has_down = any(v in degraded_statuses ...)` in main.health
    and every test here fails with 503.
    """

    def _serving_but_unhappy(self, client, **checks):
        from homepilot.vault import VaultError

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(side_effect=VaultError("locked"))
        mock_vault.ensure_master_identity = AsyncMock(side_effect=VaultError("locked"))
        mock_settings = MagicMock(
            agent_hub_enabled=checks.get("hub_enabled", False),
            cors_origins="http://localhost:5173",
        )
        mock_settings.proxmox_host = ""
        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        return client.get("/health")

    def test_a_subsystem_failure_never_fails_the_probe(self, client):
        """The OUTCOME: whatever else is wrong, a serving process answers 200."""
        resp = self._serving_but_unhappy(client)
        assert resp.status_code == 200, (
            "a serving instance reported unhealthy to the liveness probe - "
            "orchestrators restart on this, and a restart fixes none of it"
        )
        assert resp.json()["status"] == "degraded"

    def test_a_refused_agent_hub_does_not_fail_the_probe(self, client):
        """The #468 case specifically: the hub refused its transport, the rest of
        the product is fine. That is degraded, not dead."""
        resp = self._serving_but_unhappy(client, hub_enabled=True)
        data = resp.json()
        assert data["checks"]["agent_hub"] == "error"
        assert resp.status_code == 200
        assert data["status"] == "degraded"

    def test_the_database_is_still_fatal(self, client):
        """The one thing that genuinely means "cannot serve" must still say so,
        or this change would have traded a false alarm for a missing one."""
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=Exception("connection lost"))
        mock_db.fetchall = AsyncMock(side_effect=Exception("connection lost"))
        mock_settings = MagicMock(agent_hub_enabled=False, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""
        _setup_state(client, db=mock_db, proxmox=None, vault=None, settings=mock_settings)

        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "down"

    def test_trouble_is_still_reported_not_hidden(self, client):
        """Degraded must not become a synonym for fine: the per-check map still
        names what is wrong, which is what the UI and /admin/selfcheck read."""
        data = self._serving_but_unhappy(client).json()
        assert data["checks"]["vault"] == "locked"
        assert data["status"] != "ok"


class TestInformationalFieldsAreNotStatuses:
    """A count in the check map must not be read as a status (#470 follow-up).

    `agents_connected` is a number that lives alongside the statuses, and it only
    appears when the hub is running. Turning the hub on by default (ADR-004 S3)
    therefore made every healthy instance report `degraded`: "0" is neither "ok"
    nor "not_configured", so it fell through to the catch-all. Nothing in the
    suite noticed, because these tests all build states with the hub disabled -
    it took the public E2E run, against a default install, to surface it.

    Teeth: compute the verdict over `checks.values()` instead of the filtered
    statuses and both tests here fail with 'degraded'.
    """

    def _hub_up(self, client, connected: str):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=None)
        mock_db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
        mock_vault = MagicMock()
        mock_vault.list_secrets = AsyncMock(return_value=[])
        mock_vault.ensure_master_identity = AsyncMock(return_value="age1x")
        mock_settings = MagicMock(agent_hub_enabled=True, cors_origins="http://localhost:5173")
        mock_settings.proxmox_host = ""
        registry = MagicMock()
        registry.list_connected = MagicMock(return_value=[{"agent_id": "a"}] * int(connected))
        # "hub up" means LISTENING, not merely "a registry object exists" - the
        # registry is built either way, so a hub that never bound would
        # otherwise satisfy this fixture and these tests would be asserting a
        # healthy verdict over a hub nothing can reach.
        hub = MagicMock()
        hub.is_listening = MagicMock(return_value=True)
        _setup_state(client, db=mock_db, proxmox=None, vault=mock_vault, settings=mock_settings)
        client.app.state.agent_registry = registry
        client.app.state.agent_hub = hub
        return client.get("/health")

    def test_a_healthy_instance_with_no_agents_is_ok(self, client):
        """The default install: hub running, nothing enrolled yet. That is a
        brand-new HomePilot, and it must not describe itself as degraded."""
        resp = self._hub_up(client, "0")
        data = resp.json()
        assert data["checks"]["agents_connected"] == "0"
        assert data["status"] == "ok", (
            "a healthy instance reported degraded because an agent COUNT was read "
            "as if it were a status"
        )
        assert resp.status_code == 200

    def test_the_count_still_appears_for_the_ui(self, client):
        """Excluding it from the verdict must not remove it from the payload."""
        data = self._hub_up(client, "2").json()
        assert data["checks"]["agents_connected"] == "2"
        assert data["status"] == "ok"
