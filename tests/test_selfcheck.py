"""Gates for the startup self-check (ADR-004 S6, epic #458).

The report exists so an operator can tell "off by choice" from "configured but
broken" - those need opposite actions. Every assertion here is about the OUTCOME
an operator or a user gets, not about a function returning success:

  * a stock install reports every optional subsystem honestly, and nothing claims
    to be configured that is not;
  * a configured-but-dead subsystem reports "unreachable", never "off";
  * with no embedding service a KB search still RETURNS RESULTS, and the report
    says the search is keyword-only;
  * a hanging probe cannot delay startup or the report past its stated bound;
  * no secret reaches the report, including one hidden in a URL's userinfo,
    path or query.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.common import redact_endpoint
from homepilot.selfcheck import (
    PROBE_TIMEOUT_SECONDS,
    STATE_OFF,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_UNREACHABLE,
    Subsystem,
    build_subsystems,
    run_selfcheck,
    schedule_boot_selfcheck,
    selfcheck_report,
)

# A port nothing listens on, used to prove the "configured but unreachable" arm.
# 127.0.0.1 keeps the probe local, so the assertion never depends on the network.
DEAD_ADDR = "127.0.0.1:9"


def _stock_settings(**overrides):
    """Settings as a stock install resolves them: no optional service set."""
    base = {
        "proxmox_host": "",
        "proxmox_port": 8006,
        "agent_hub_enabled": False,
        "agent_hub_host": "0.0.0.0",
        "agent_hub_port": 8443,
        "embedding_service_url": "",
        "embedding_model": "bge-m3",
        "embedding_fallback_url": "",
        "embedding_fallback_model": "nomic-embed-text",
        "events_webhook_url": None,
        "artifacts_remote": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _stock_state(**overrides):
    base = {"proxmox": None, "vault": None, "agent_hub": None, "mcp_app": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def _by_name(report):
    return {entry["name"]: entry for entry in report["subsystems"]}


class TestStockInstallIsHonest:
    async def test_nothing_optional_claims_to_be_configured(self, monkeypatch):
        monkeypatch.delenv("HP_MCP_TOKEN", raising=False)

        report = await selfcheck_report(_stock_state(), _stock_settings())
        entries = _by_name(report)

        # Every optional subsystem is present and every one of them is off.
        assert set(entries) == {
            "proxmox",
            "agent_hub",
            "vault",
            "embeddings",
            "events_webhook",
            "mcp",
            "artifacts_remote",
        }
        for name, entry in entries.items():
            assert entry["configured"] is False, (
                f"{name} claims to be configured on a stock install"
            )
            assert entry["state"] == STATE_OFF, f"{name} is {entry['state']}, expected off"
            assert entry["consequence"].strip(), f"{name} states no consequence"
            # An "off" subsystem has no address to show, so it cannot imply one.
            assert entry["target"] == ""

    async def test_off_entries_say_what_is_lost(self, monkeypatch):
        monkeypatch.delenv("HP_MCP_TOKEN", raising=False)
        entries = _by_name(await selfcheck_report(_stock_state(), _stock_settings()))

        assert "keyword-only" in entries["embeddings"]["consequence"]
        assert "not forwarded anywhere" in entries["events_webhook"]["consequence"]
        assert "inventory" in entries["proxmox"]["consequence"]

    async def test_stock_install_reports_nothing_broken(self, monkeypatch):
        """Off is not a fault: a stock install must produce zero red lines."""
        monkeypatch.delenv("HP_MCP_TOKEN", raising=False)
        report = await selfcheck_report(_stock_state(), _stock_settings())

        assert report["counts"][STATE_UNREACHABLE] == 0
        assert report["counts"][STATE_UNKNOWN] == 0
        assert report["counts"][STATE_OFF] == len(report["subsystems"])


class TestOffIsNotBroken:
    """The distinction the whole feature exists for."""

    async def test_dead_embedding_service_is_unreachable_not_off(self):
        settings = _stock_settings(embedding_service_url=f"http://{DEAD_ADDR}/v1/embeddings")
        entry = _by_name(await selfcheck_report(_stock_state(), settings))["embeddings"]

        assert entry["state"] == STATE_UNREACHABLE
        assert entry["state"] != STATE_OFF
        assert entry["configured"] is True
        assert "did not return an embedding" in entry["consequence"]

    async def test_dead_webhook_is_unreachable_not_off(self):
        settings = _stock_settings(events_webhook_url=f"http://{DEAD_ADDR}/webhook/x")
        entry = _by_name(await selfcheck_report(_stock_state(), settings))["events_webhook"]

        assert entry["state"] == STATE_UNREACHABLE
        assert entry["configured"] is True
        assert "dropped" in entry["consequence"]

    async def test_proxmox_host_without_client_is_unreachable_not_off(self):
        settings = _stock_settings(proxmox_host="pve.example.com")
        entry = _by_name(await selfcheck_report(_stock_state(proxmox=None), settings))["proxmox"]

        assert entry["state"] == STATE_UNREACHABLE
        assert entry["configured"] is True

    async def test_enabled_hub_that_never_bound_is_unreachable_not_off(self):
        hub = MagicMock()
        hub.is_listening.return_value = False
        settings = _stock_settings(agent_hub_enabled=True)
        entry = _by_name(await selfcheck_report(_stock_state(agent_hub=hub), settings))["agent_hub"]

        assert entry["state"] == STATE_UNREACHABLE
        assert entry["configured"] is True

    async def test_locked_vault_is_unreachable_not_off(self):
        vault = MagicMock()
        vault.list_secrets = AsyncMock(side_effect=RuntimeError("locked"))
        entry = _by_name(await selfcheck_report(_stock_state(vault=vault), _stock_settings()))[
            "vault"
        ]

        assert entry["state"] == STATE_UNREACHABLE
        assert entry["configured"] is True

    async def test_working_subsystem_reports_ok(self):
        proxmox = MagicMock()
        proxmox.test_connection = AsyncMock(return_value=True)
        vault = MagicMock()
        vault.list_secrets = AsyncMock(return_value=[])
        settings = _stock_settings(proxmox_host="pve.example.com")

        entries = _by_name(
            await selfcheck_report(_stock_state(proxmox=proxmox, vault=vault), settings)
        )
        assert entries["proxmox"]["state"] == STATE_OK
        assert entries["vault"]["state"] == STATE_OK


class TestKbStaysUsableWithoutEmbeddings:
    """The KB path, asserted as a journey: search must still RETURN RESULTS."""

    @pytest.fixture
    async def kb(self, tmp_path: Path):
        from homepilot.artifacts.lifecycle import ArtifactLifecycle
        from homepilot.artifacts.store import ArtifactStore
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository
        from homepilot.kb.service import KBService

        db = Database(str(tmp_path / "kb.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        store_dir = tmp_path / "artifacts"
        store_dir.mkdir()
        store = ArtifactStore(store_dir)
        service = KBService(repo=repo, store=store, lifecycle=ArtifactLifecycle(store, repo))
        yield service, repo
        await db.close()

    async def test_search_returns_results_with_no_embedding_service(
        self, kb, tmp_path: Path, monkeypatch
    ):
        from homepilot.config import Settings

        service, repo = kb
        # The REAL defaults, not a mock: a stock install configures no embedding
        # service, and this asserts that the resulting search still works.
        settings = Settings(data_dir=str(tmp_path), vault_passphrase="test-passphrase")
        assert settings.embedding_service_url == ""
        assert settings.embedding_fallback_url == ""
        monkeypatch.setattr("homepilot.kb.service.get_settings", lambda: settings)

        await repo.create_doc_metadata(
            source="test:keyword-only",
            title="Redis config",
            content="redis maxmemory 2gb",
            kind="note",
            target="redis",
        )

        results = await service.search("redis")

        assert results, "KB search returned nothing with no embedding service configured"
        assert any("redis" in r["content"].lower() for r in results)

    async def test_report_says_keyword_only_when_search_is_keyword_only(self, monkeypatch):
        monkeypatch.delenv("HP_MCP_TOKEN", raising=False)
        entry = _by_name(await selfcheck_report(_stock_state(), _stock_settings()))["embeddings"]
        assert entry["state"] == STATE_OFF
        assert "keyword-only" in entry["consequence"]

    async def test_embedding_status_distinguishes_off_from_down(
        self, kb, tmp_path: Path, monkeypatch
    ):
        from homepilot.config import Settings

        service, _repo = kb
        settings = Settings(data_dir=str(tmp_path), vault_passphrase="test-passphrase")
        monkeypatch.setattr("homepilot.kb.service.get_settings", lambda: settings)
        status = await service.embedding_status()
        assert status["search_mode"] == "keyword"
        assert status["configured"] is False


class TestProbesAreBounded:
    async def test_hanging_probe_degrades_to_unknown_within_the_bound(self):
        # Sleeps far past the bound rather than blocking forever, so REMOVING the
        # bound makes this test go red instead of hanging the suite - a gate that
        # can only hang has no provable teeth.
        async def sleeps_past_the_bound() -> bool:
            await asyncio.sleep(5)
            return True

        hanging = Subsystem(
            name="hangs",
            label="the hanging subsystem",
            configured=True,
            target="tcp://nowhere",
            off="off",
            ok="ok",
            broken="broken",
            probe=sleeps_past_the_bound,
        )

        started = time.monotonic()
        report = await run_selfcheck([hanging], timeout=0.2)
        elapsed = time.monotonic() - started

        entry = _by_name(report)["hangs"]
        assert entry["state"] == STATE_UNKNOWN
        assert "unverified" in entry["consequence"]
        # Bounded, and it did NOT get reported as working.
        assert elapsed < 2.0, f"a hanging probe held the report open for {elapsed:.2f}s"
        assert entry["state"] != STATE_OK

    async def test_many_hanging_probes_still_finish_inside_one_timeout(self):
        """Probes run concurrently, so N hanging probes cost one timeout, not N."""

        async def sleeps_past_the_bound() -> bool:
            await asyncio.sleep(5)
            return True

        subsystems = [
            Subsystem(
                name=f"hangs{i}",
                label="a hanging subsystem",
                configured=True,
                target="",
                off="off",
                ok="ok",
                broken="broken",
                probe=sleeps_past_the_bound,
            )
            for i in range(6)
        ]

        started = time.monotonic()
        report = await run_selfcheck(subsystems, timeout=0.2)
        elapsed = time.monotonic() - started

        assert report["counts"][STATE_UNKNOWN] == 6
        assert elapsed < 0.2 * 3, f"probes ran serially: {elapsed:.2f}s for 6 x 0.2s"

    async def test_boot_selfcheck_does_not_delay_startup(self):
        """The boot report is scheduled, never awaited."""
        settings = _stock_settings(embedding_service_url=f"http://{DEAD_ADDR}/v1/embeddings")

        started = time.monotonic()
        task = schedule_boot_selfcheck(_stock_state(), settings)
        elapsed = time.monotonic() - started

        assert elapsed < 0.05, f"scheduling the boot self-check blocked for {elapsed:.3f}s"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_default_bound_is_short_enough_to_be_harmless(self):
        assert PROBE_TIMEOUT_SECONDS <= 5.0


class TestNoSecretsInTheReport:
    """Assert against a config where every optional secret is a marker value."""

    SECRET = "HPSECRETMARKER"

    def _loaded_settings(self):
        s = self.SECRET
        return SimpleNamespace(
            proxmox_host="pve.example.com",
            proxmox_port=8006,
            agent_hub_enabled=True,
            agent_hub_host="0.0.0.0",
            agent_hub_port=8443,
            agent_hub_auth_token=f"{s}-hub-token",
            embedding_service_url=f"https://user:{s}-embed@embed.example.com/v1/embeddings?key={s}-q",
            embedding_model="bge-m3",
            embedding_fallback_url="",
            embedding_fallback_model="nomic-embed-text",
            # An n8n webhook path IS the credential, so the path must not survive.
            events_webhook_url=f"http://n8n.example.com:5678/webhook/{s}-path",
            events_webhook_secret=f"{s}-webhook",
            n8n_api_key=f"{s}-n8n",
            vault_passphrase=f"{s}-vault",
            secret_key=f"{s}-key",
            admin_secret=f"{s}-admin",
            portal_proxy_secret=f"{s}-portal",
            artifacts_remote=f"https://oauth2:{s}-git@github.com/example/artifacts.git",
        )

    async def test_no_secret_appears_anywhere_in_the_report(self, monkeypatch):
        monkeypatch.setenv("HP_MCP_TOKEN", f"{self.SECRET}-mcp")
        proxmox = MagicMock()
        proxmox.test_connection = AsyncMock(return_value=True)
        vault = MagicMock()
        vault.list_secrets = AsyncMock(return_value=[])
        hub = MagicMock()
        hub.is_listening.return_value = True

        report = await selfcheck_report(
            _stock_state(proxmox=proxmox, vault=vault, agent_hub=hub),
            self._loaded_settings(),
        )

        serialized = json.dumps(report)
        assert self.SECRET not in serialized, (
            "a secret leaked into the self-check report: "
            f"{[e for e in report['subsystems'] if self.SECRET in json.dumps(e)]}"
        )

    async def test_no_secret_appears_in_the_logged_report(self, monkeypatch, caplog):
        import logging

        from homepilot.selfcheck import log_selfcheck

        monkeypatch.setenv("HP_MCP_TOKEN", f"{self.SECRET}-mcp")
        with caplog.at_level(logging.INFO, logger="homepilot.selfcheck"):
            await log_selfcheck(_stock_state(), self._loaded_settings())

        rendered = "\n".join(r.getMessage() for r in caplog.records)
        assert rendered.strip(), "log_selfcheck wrote nothing"
        assert self.SECRET not in rendered, f"a secret leaked into the boot log:\n{rendered}"

    def test_redaction_keeps_only_scheme_host_port(self):
        assert (
            redact_endpoint("http://user:pw@n8n.example.com:5678/webhook/abc?token=t")
            == "http://n8n.example.com:5678"
        )
        assert redact_endpoint("https://oauth2:tok@github.com/org/repo.git") == "https://github.com"
        assert redact_endpoint("git@github.com:org/repo.git") == "github.com"
        assert redact_endpoint("") == ""


class TestSelfcheckEndpoint:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from homepilot.admin.router import _require_admin_dep
        from homepilot.admin.router import router as admin_router

        monkeypatch.delenv("HP_MCP_TOKEN", raising=False)
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.state.settings = _stock_settings()
        app.state.repo = MagicMock()
        app.state.proxmox = None
        app.state.vault = None
        app.state.agent_hub = None
        app.state.mcp_app = None
        client = TestClient(app)
        app.dependency_overrides[_require_admin_dep.dependency] = lambda: {
            "user_id": 1,
            "token_id": 1,
            "scope": "*",
            "role": "admin",
            "display_name": "admin",
        }
        yield client
        app.dependency_overrides.clear()

    def test_endpoint_returns_the_report(self, client):
        resp = client.get("/admin/selfcheck")
        assert resp.status_code == 200
        body = resp.json()
        names = {s["name"] for s in body["subsystems"]}
        assert "embeddings" in names and "events_webhook" in names
        assert all(s["state"] == STATE_OFF for s in body["subsystems"])

    def test_endpoint_requires_admin(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from homepilot.admin.router import router as admin_router

        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.state.settings = _stock_settings()
        app.state.repo = MagicMock()
        resp = TestClient(app).get("/admin/selfcheck")
        assert resp.status_code == 401

    def test_endpoint_answers_within_the_probe_bound(self, client):
        client.app.state.settings = _stock_settings(
            embedding_service_url=f"http://{DEAD_ADDR}/v1/embeddings",
            events_webhook_url=f"http://{DEAD_ADDR}/webhook/x",
            proxmox_host="pve.example.invalid",
        )
        started = time.monotonic()
        resp = client.get("/admin/selfcheck")
        elapsed = time.monotonic() - started

        assert resp.status_code == 200
        assert elapsed < PROBE_TIMEOUT_SECONDS * 3, (
            f"/admin/selfcheck took {elapsed:.2f}s against dead endpoints"
        )


class TestBuildSubsystems:
    def test_every_subsystem_states_a_consequence_for_every_arm(self, monkeypatch):
        monkeypatch.setenv("HP_MCP_TOKEN", "t")
        configured = _stock_settings(
            proxmox_host="pve.example.com",
            agent_hub_enabled=True,
            embedding_service_url="http://embed.example.com/v1/embeddings",
            events_webhook_url="http://hooks.example.com/webhook/x",
            artifacts_remote="git@github.com:example/artifacts.git",
        )
        vault = MagicMock()
        vault.list_secrets = AsyncMock(return_value=[])
        subsystems = build_subsystems(_stock_state(vault=vault), configured)
        for sub in subsystems:
            assert sub.off.strip(), f"{sub.name} has no 'off' consequence"
            assert sub.ok.strip(), f"{sub.name} has no 'ok' consequence"
            # A subsystem with no probe can never be reported broken, so it needs
            # no 'broken' sentence - but one WITH a probe must have one.
            if sub.probe is not None:
                assert sub.broken.strip(), f"{sub.name} probes but has no 'broken' consequence"


class TestAgainstRealAppState:
    """Integration: the report over the objects create_app_state actually builds.

    Unit tests can be fooled by a mock that answers differently from the real
    thing (a mock kept a dead safety guard green once already), so the honest
    claim - "a stock install reports itself truthfully" - is checked here against
    real Settings, a real vault and a real hub server.
    """

    @pytest.fixture
    def hp_dir(self):
        import shutil
        import tempfile

        path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-selfcheck-")
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    async def _state(self, hp_dir: str, **overrides):
        import os

        from homepilot.app_state import create_app_state
        from homepilot.config import Settings

        settings = Settings(
            secret_key="test-secret-key-for-pytest-only-not-for-production",
            data_dir=hp_dir,
            artifacts_dir=os.path.join(hp_dir, "artifacts"),
            **overrides,
        )
        return await create_app_state(settings), settings

    async def test_stock_app_state_reports_embeddings_and_webhook_off(self, hp_dir, monkeypatch):
        monkeypatch.delenv("HP_MCP_TOKEN", raising=False)
        state, settings = await self._state(hp_dir, agent_hub_enabled=False)
        try:
            entries = _by_name(await selfcheck_report(state, settings))
        finally:
            await state.database.close()

        assert entries["embeddings"]["state"] == STATE_OFF
        assert entries["events_webhook"]["state"] == STATE_OFF
        assert entries["agent_hub"]["state"] == STATE_OFF
        # The vault DOES self-generate on first boot, so it must report ok - an
        # "off" here would mean the zero-touch install lost its secret store.
        assert entries["vault"]["state"] == STATE_OK

    async def test_a_started_hub_reports_ok_and_a_stopped_one_does_not(self, hp_dir):
        state, settings = await self._state(
            hp_dir, agent_hub_enabled=True, agent_hub_host="127.0.0.1", agent_hub_port=0
        )
        try:
            before = _by_name(await selfcheck_report(state, settings))["agent_hub"]
            assert before["state"] == STATE_UNREACHABLE, "an unbound hub must not read ok"

            await state.agent_hub.start()
            after = _by_name(await selfcheck_report(state, settings))["agent_hub"]
            assert after["state"] == STATE_OK

            await state.agent_hub.stop()
            stopped = _by_name(await selfcheck_report(state, settings))["agent_hub"]
            assert stopped["state"] == STATE_UNREACHABLE
        finally:
            await state.database.close()
