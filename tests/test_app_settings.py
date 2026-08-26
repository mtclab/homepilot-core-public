"""Settings persist, with env > db > default, and the product actually obeys them (#553 C2).

THE DEFECTS THIS FORBIDS:

* a setting saved from the product that the running process never reads - the
  UI says "saved" and the reconciler keeps the boot value forever;
* a UI edit written on top of an env var the operator set, so the stored value
  silently contradicts the environment at the next boot (the surprise
  `hub_tls_mode` exists to prevent);
* a secret reachable through the settings surface in either direction.

The journey gates drive REAL loops and REAL call paths - a scheduler that is
running, a webhook actually sent, a prune that actually deletes - because "the
PUT returned 200" is not evidence anything changed.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot import app_settings
from homepilot.admin.router import _require_admin_dep
from homepilot.admin.router import router as admin_router
from homepilot.app_settings import (
    DB_KEY_PREFIX,
    FORBIDDEN_KEYS,
    REGISTRY,
    EnvOverrideError,
    SettingError,
    SettingsResolver,
    bind_resolver,
    env_is_explicit,
)
from homepilot.config import Settings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio


def _settings(**overrides: Any) -> Settings:
    return Settings(
        data_dir="/tmp/hp-c2-test", artifacts_dir="/tmp/hp-c2-test/artifacts", **overrides
    )


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "settings.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield Repository(db)
    finally:
        await db.close()


@pytest.fixture
async def resolver(repo):
    return SettingsResolver(repo, _settings())


@pytest.fixture
async def api(repo):
    """The real admin routes over a real repository, in THIS event loop.

    TestClient would run the app on its own loop, which is exactly what a
    scheduler-plus-API journey must not do.
    """
    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    settings = _settings()
    app.state.repo = repo
    app.state.settings = settings
    app.state.settings_resolver = SettingsResolver(repo, settings)
    app.dependency_overrides[_require_admin_dep.dependency] = lambda: {
        "user_id": 1,
        "scope": "*",
        "role": "admin",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app


class TestPrecedence:
    async def test_nothing_set_is_the_code_default(self, resolver):
        resolved = await resolver.resolve("retention_days")
        assert resolved.source == "default"
        assert resolved.value == 90

    async def test_the_db_value_wins_over_the_default(self, resolver, repo):
        await resolver.set("retention_days", 14)
        resolved = await resolver.resolve("retention_days")
        assert (resolved.value, resolved.source) == (14, "db")
        row = await repo.get_setting(DB_KEY_PREFIX + "retention_days")
        assert row is not None and row["value"] == "14"

    async def test_an_explicit_env_var_wins_over_the_db(self, repo, monkeypatch):
        stored = SettingsResolver(repo, _settings())
        await stored.set("retention_days", 14)

        monkeypatch.setenv("HP_RETENTION_DAYS", "3")
        env_resolver = SettingsResolver(repo, _settings())
        resolved = await env_resolver.resolve("retention_days")

        assert (resolved.value, resolved.source) == (3, "env")

    async def test_an_env_set_key_refuses_the_write_and_records_nothing(self, repo, monkeypatch):
        monkeypatch.setenv("HP_RETENTION_DAYS", "3")
        env_resolver = SettingsResolver(repo, _settings())

        with pytest.raises(EnvOverrideError) as exc:
            await env_resolver.set("retention_days", 14)

        assert "HP_RETENTION_DAYS" in str(exc.value)
        assert "records nothing" in str(exc.value)
        assert await repo.get_setting(DB_KEY_PREFIX + "retention_days") is None, (
            "the refused write still left a row - the stored value would contradict "
            "the environment at the next boot"
        )

    async def test_clearing_returns_to_the_default(self, resolver):
        await resolver.set("retention_days", 14)
        cleared = await resolver.clear("retention_days")
        assert (cleared.value, cleared.source) == (90, "default")

    async def test_model_fields_set_distinguishes_env_from_default(self, monkeypatch):
        """The explicitness test's foundation, proven rather than assumed.

        `env_is_explicit` trusts pydantic to record a field as SET when the
        environment supplied it and NOT to record it when the code default was
        used. If that ever stops being true, the precedence silently inverts.
        """
        assert "retention_days" not in _settings().model_fields_set
        assert not env_is_explicit("retention_days", _settings())

        monkeypatch.setenv("HP_RETENTION_DAYS", "3")
        assert "retention_days" in _settings().model_fields_set

    async def test_an_env_file_value_counts_as_explicit_without_os_environ(self, monkeypatch):
        """A `.env` line is the operator speaking too.

        pydantic hands it over as a SET field with nothing in `os.environ`, and
        treating that as the code default would let the UI overwrite a
        documented deployment file.
        """
        monkeypatch.delenv("HP_RETENTION_DAYS", raising=False)
        from_env_file = _settings(retention_days=3)
        assert env_is_explicit("retention_days", from_env_file)

    async def test_a_bad_int_is_refused(self, resolver):
        for bad in ("not-a-number", 0, -5, True):
            with pytest.raises(SettingError):
                await resolver.set("artifacts_push_interval_seconds", bad)

    async def test_an_unknown_key_is_refused(self, resolver):
        with pytest.raises(SettingError):
            await resolver.set("proxmox_host", "pve.example")

    async def test_a_corrupt_stored_value_falls_back_to_the_default(self, repo, resolver):
        await repo.set_setting(DB_KEY_PREFIX + "retention_days", "banana")
        resolved = await resolver.resolve("retention_days")
        assert (resolved.value, resolved.source) == (90, "default")


class TestSecretDiscipline:
    async def test_no_secret_has_a_spec(self):
        for key in FORBIDDEN_KEYS:
            assert key not in REGISTRY, f"{key} is a secret and must never be editable here"

    async def test_the_webhook_secret_is_absent_from_the_report(self, api):
        client, app = api
        app.state.settings.events_webhook_secret = "s3cr3t-signing-key"

        resp = await client.get("/admin/settings/overrides")

        assert resp.status_code == 200
        body = resp.text
        assert "s3cr3t-signing-key" not in body
        keys = {entry["key"] for entry in resp.json()["settings"]}
        assert "events_webhook_secret" not in keys
        assert keys & {"events_webhook_url"}, "the non-secret half must still be there"

    async def test_the_webhook_secret_is_not_settable(self, api):
        client, _app = api
        resp = await client.put(
            "/admin/settings/overrides/events_webhook_secret", json={"value": "nope"}
        )
        assert resp.status_code == 400
        assert "unknown setting" in resp.json()["detail"]

    async def test_no_secret_field_is_settable(self, api):
        client, _app = api
        for key in FORBIDDEN_KEYS:
            resp = await client.put(f"/admin/settings/overrides/{key}", json={"value": "nope"})
            assert resp.status_code == 400, f"{key} was accepted"


class TestTheApi:
    async def test_the_report_describes_every_registry_setting(self, api):
        client, _app = api
        resp = await client.get("/admin/settings/overrides")
        assert resp.status_code == 200
        entries = {e["key"]: e for e in resp.json()["settings"]}
        assert set(entries) == set(REGISTRY)
        entry = entries["artifacts_push_interval_seconds"]
        assert entry["source"] == "default"
        assert entry["env_var"] == "HP_ARTIFACTS_PUSH_INTERVAL_SECONDS"
        assert entry["hot_reloadable"] is True
        assert entry["description"]
        assert entry["editable"] is True

    async def test_a_saved_value_comes_back_as_db(self, api):
        client, _app = api
        put = await client.put(
            "/admin/settings/overrides/artifacts_remote",
            json={"value": "git@example.com:me/archive.git"},
        )
        assert put.status_code == 200, put.text

        resp = await client.get("/admin/settings/overrides")
        entry = next(e for e in resp.json()["settings"] if e["key"] == "artifacts_remote")
        assert entry["value"] == "git@example.com:me/archive.git"
        assert entry["source"] == "db"

    async def test_an_env_locked_setting_is_refused_with_409(self, repo, monkeypatch):
        monkeypatch.setenv("HP_ARTIFACTS_REMOTE", "git@env.example:me/archive.git")
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        settings = _settings()
        app.state.repo = repo
        app.state.settings = settings
        app.state.settings_resolver = SettingsResolver(repo, settings)
        app.dependency_overrides[_require_admin_dep.dependency] = lambda: {"scope": "*"}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/admin/settings/overrides/artifacts_remote", json={"value": "git@ui.example:x"}
            )
            assert resp.status_code == 409
            detail = resp.json()["detail"]
            assert "HP_ARTIFACTS_REMOTE" in detail and "records nothing" in detail

            listing = await client.get("/admin/settings/overrides")
            entry = next(e for e in listing.json()["settings"] if e["key"] == "artifacts_remote")
            assert entry["source"] == "env"
            assert entry["editable"] is False

    async def test_a_bad_int_is_a_400(self, api):
        client, _app = api
        resp = await client.put(
            "/admin/settings/overrides/artifacts_push_interval_seconds",
            json={"value": "every hour"},
        )
        assert resp.status_code == 400
        assert "whole number" in resp.json()["detail"]

    async def test_delete_returns_the_default(self, api):
        client, _app = api
        await client.put("/admin/settings/overrides/retention_days", json={"value": 5})
        resp = await client.delete("/admin/settings/overrides/retention_days")
        assert resp.status_code == 200
        assert resp.json()["source"] == "default"
        assert resp.json()["value"] == 90

    async def test_the_routes_need_a_token(self, repo):
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.state.repo = repo
        app.state.settings = _settings()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/admin/settings/overrides")).status_code == 401
            assert (
                await client.put("/admin/settings/overrides/retention_days", json={"value": 5})
            ).status_code == 401
            assert (
                await client.delete("/admin/settings/overrides/retention_days")
            ).status_code == 401


class TestTheJourney:
    """Set the archive push interval through the API; the RUNNING scheduler
    uses the new one for its next cycle.

    This is the design's stated C2 gate. It drives the real ReconcilerScheduler
    with the same callable-interval registration main.py uses. No clock is
    faked and no exact arithmetic is asserted - the assertion is the SHAPE a
    working reschedule produces: a loop that was idle on a long interval starts
    cycling once the interval is short.
    """

    async def test_a_saved_interval_reschedules_the_running_loop(self, api, repo, monkeypatch):
        from homepilot.reconciler import base as base_mod
        from homepilot.reconciler.base import Reconciler, ReconcilerResult, ReconcilerScheduler

        # The poll slice bounds how long a waiting loop keeps honouring the OLD
        # interval. Shortened here so the test takes fractions of a second
        # rather than the shipped five.
        monkeypatch.setattr(base_mod, "INTERVAL_POLL_SECONDS", 0.02)

        client, app = api
        resolver = app.state.settings_resolver
        await resolver.set("artifacts_push_interval_seconds", 3600)

        runs = 0

        class Counting(Reconciler):
            async def run(self) -> ReconcilerResult:
                nonlocal runs
                runs += 1
                return ReconcilerResult(name="counting", success=True)

        scheduler = ReconcilerScheduler()
        scheduler.register(
            Counting(),
            interval=lambda: app_settings.resolve_interval(
                resolver, "artifacts_push_interval_seconds", 3600.0
            ),
        )
        await scheduler.start()
        try:
            await asyncio.sleep(0.2)
            assert runs == 1, (
                f"an hourly loop cycled {runs} times - it is not honouring its interval"
            )

            resp = await client.put(
                "/admin/settings/overrides/artifacts_push_interval_seconds", json={"value": 1}
            )
            assert resp.status_code == 200, resp.text

            # One second is the registry's floor, so the deadline is the honest
            # one: a rescheduled loop cycles again inside a few seconds, a loop
            # still waiting out its hour never does.
            deadline = time.monotonic() + 6.0
            while runs < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            assert runs >= 2, (
                "the scheduler never ran again after the interval was cut from an hour "
                "to a second - the saved value is not reaching the running loop"
            )
        finally:
            await scheduler.stop()

    async def test_a_fixed_interval_loop_is_unchanged(self, monkeypatch):
        """The callable is opt-in: every other reconciler still sleeps its
        number, with no settings read per cycle."""
        from homepilot.reconciler.base import Reconciler, ReconcilerResult, ReconcilerScheduler

        runs = 0

        class Counting(Reconciler):
            async def run(self) -> ReconcilerResult:
                nonlocal runs
                runs += 1
                return ReconcilerResult(name="counting", success=True)

        scheduler = ReconcilerScheduler()
        scheduler.register(Counting(), interval=0.05)
        await scheduler.start()
        try:
            await asyncio.sleep(0.3)
            assert runs >= 3
        finally:
            await scheduler.stop()


class TestCallTimeResolution:
    """Each consumer reads the setting AT USE TIME, so a DB value is obeyed with
    no restart. Each test would pass equally if the value came from the env -
    what it proves is that nothing cached the boot value."""

    async def test_the_archive_push_uses_the_saved_remote(self, tmp_path, repo):
        import subprocess

        from homepilot.artifacts.store import ArtifactStore
        from homepilot.reconciler.archive_push import ArchivePushReconciler

        bare = tmp_path / "late.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        # Built with NO remote, exactly like an instance that booted unconfigured.
        store = ArtifactStore(tmp_path / "artifacts", remote="")
        store.write("2026-08-c2-remote", "status: proposed", "content", event="test artifact")
        resolver = SettingsResolver(repo, _settings())
        reconciler = ArchivePushReconciler(store, repo, resolver=resolver)

        idle = await reconciler.run()
        assert idle.success and idle.details.get("skipped")

        await resolver.set("artifacts_remote", str(bare))
        result = await reconciler.run()

        assert result.success, result.details
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=bare,
            capture_output=True,
            text=True,
        ).stdout
        assert "2026-08-c2-remote" in listing, (
            "the reconciler reported success but the remote configured after boot holds nothing"
        )

    async def test_retention_prunes_on_the_saved_horizon(self, repo):
        from datetime import UTC, datetime, timedelta

        from homepilot.reconciler.retention import RetentionReconciler

        old = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await repo.db.execute(
            "INSERT INTO audit_log (user_id, timestamp, source, action) "
            "VALUES (1, ?, 'test', 'c2')",
            (old,),
        )
        await repo.db.conn.commit()

        resolver = SettingsResolver(repo, _settings())
        # Constructed with the 90-day default: a reconciler that read its horizon
        # once would keep that row forever.
        reconciler = RetentionReconciler(repo, 90, resolver=resolver)

        kept = await reconciler.run()
        assert kept.details["retention_days"] == 90
        row = await repo.db.fetchone("SELECT COUNT(*) c FROM audit_log")
        assert row["c"] == 1

        await resolver.set("retention_days", 1)
        pruned = await reconciler.run()

        assert pruned.details["retention_days"] == 1
        row = await repo.db.fetchone("SELECT COUNT(*) c FROM audit_log")
        assert row["c"] == 0, "the ten-day-old row survived a one-day horizon"

    async def test_metrics_retention_prunes_on_the_saved_horizon(self, repo):
        from homepilot.metrics.repository import MetricsRepository
        from homepilot.metrics.retention import MetricsPruner

        metrics = MetricsRepository(repo.db)
        old_ts = int(time.time()) - 5 * 86400
        await metrics.insert_samples("host-a", "agent-a", [("cpu", old_ts, 1.0)])

        resolver = SettingsResolver(repo, _settings())
        pruner = MetricsPruner(metrics, 7, resolver=resolver)

        kept = await pruner.run()
        assert kept.details == {"deleted": 0, "retention_days": 7}

        await resolver.set("metrics_retention_days", 1)
        pruned = await pruner.run()

        assert pruned.details == {"deleted": 1, "retention_days": 1}

    async def test_the_webhook_is_posted_to_the_saved_url(self, repo, monkeypatch):
        from homepilot.webhooks import send as send_mod

        posted: list[str] = []

        class _Resp:
            status_code = 200

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None: ...

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def post(self, url: str, **kwargs: Any) -> _Resp:
                posted.append(url)
                return _Resp()

        monkeypatch.setattr(send_mod.httpx, "AsyncClient", _Client)

        resolver = SettingsResolver(repo, _settings())
        bind_resolver(resolver)
        try:
            await send_mod.send_webhook("artifact.proposed", {"id": "a1"})
            assert posted == [], "an unconfigured webhook posted somewhere"

            await resolver.set("events_webhook_url", "https://hooks.example/c2")
            await send_mod.send_webhook("artifact.proposed", {"id": "a1"})
        finally:
            bind_resolver(None)

        assert posted == ["https://hooks.example/c2"], (
            "the webhook URL saved after boot was ignored by the sender"
        )

    async def test_the_embedding_call_uses_the_saved_url_and_model(self, repo, monkeypatch):
        from homepilot.kb import service as kb_service

        called: list[tuple[str, str]] = []

        async def _fake_embed(url: str, model: str, text: str, **kwargs: Any) -> list[float]:
            called.append((url, model))
            return [0.1, 0.2]

        monkeypatch.setattr(kb_service, "_call_embed_service", _fake_embed)

        resolver = SettingsResolver(repo, _settings())
        bind_resolver(resolver)
        try:
            assert await kb_service._get_embedding("hello") is None, (
                "an unconfigured embedding service was called anyway"
            )
            await resolver.set("embedding_service_url", "http://embed.example:8080/embed")
            await resolver.set("embedding_model", "bge-small")
            vector = await kb_service._get_embedding("hello")
        finally:
            bind_resolver(None)

        assert vector == [0.1, 0.2]
        assert called == [("http://embed.example:8080/embed", "bge-small")]


class TestTheReportTellsTheTruth:
    async def test_selfcheck_sees_a_setting_saved_after_boot(self, repo):
        from types import SimpleNamespace

        from homepilot.selfcheck import selfcheck_report

        settings = _settings()
        state = SimpleNamespace(
            repo=repo,
            settings=settings,
            settings_resolver=SettingsResolver(repo, settings),
            proxmox=None,
            vault=None,
            mcp_app=None,
        )

        before = await selfcheck_report(state, settings)
        events = next(s for s in before["subsystems"] if s["name"] == "events_webhook")
        assert events["state"] == "off"

        await state.settings_resolver.set("events_webhook_url", "https://hooks.example/c2")
        after = await selfcheck_report(state, settings)

        events = next(s for s in after["subsystems"] if s["name"] == "events_webhook")
        assert events["configured"] is True, (
            "the report still calls a webhook configured from the product 'off by choice'"
        )
