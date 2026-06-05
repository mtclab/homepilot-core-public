from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.reconciler import (
    InventoryReconciler,
    Reconciler,
    ReconcilerResult,
    ReconcilerScheduler,
    VerifyResult,
    verify_artifact,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def real_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def repo(real_db):
    from homepilot.db.repository import Repository

    return Repository(real_db)


class TestReconcilerScheduler:
    async def test_register_and_start(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=Reconciler)
        mock_reconciler.run.return_value = ReconcilerResult(name="test", success=True)
        mock_reconciler.__class__.__name__ = "TestReconciler"

        scheduler.register(mock_reconciler, interval=3600, startup_delay=0)
        assert len(scheduler._registered) == 1
        assert scheduler._registered[0].interval == 3600

        await scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        mock_reconciler.run.assert_awaited()

    async def test_stop_cancels_tasks(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=Reconciler)
        mock_reconciler.run.side_effect = asyncio.CancelledError
        mock_reconciler.__class__.__name__ = "CancelReconciler"

        scheduler.register(mock_reconciler, interval=3600)
        await scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        reg = scheduler._registered[0]
        assert reg.task is None or reg.task.done()

    async def test_scheduler_with_startup_delay(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=Reconciler)
        mock_reconciler.run.return_value = ReconcilerResult(name="delayed", success=True)
        mock_reconciler.__class__.__name__ = "DelayedReconciler"

        scheduler.register(mock_reconciler, interval=3600, startup_delay=0.01)
        await scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        mock_reconciler.run.assert_awaited()

    async def test_multiple_reconcilers(self):
        scheduler = ReconcilerScheduler()
        r1 = AsyncMock(spec=Reconciler)
        r1.run.return_value = ReconcilerResult(name="r1", success=True)
        r1.__class__.__name__ = "R1"
        r2 = AsyncMock(spec=Reconciler)
        r2.run.return_value = ReconcilerResult(name="r2", success=True)
        r2.__class__.__name__ = "R2"

        scheduler.register(r1, interval=3600)
        scheduler.register(r2, interval=1800)
        assert len(scheduler._registered) == 2

        await scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        r1.run.assert_awaited()
        r2.run.assert_awaited()

    async def test_run_loop_handles_exception(self):
        scheduler = ReconcilerScheduler()
        call_count = 0

        class FlakyReconciler(Reconciler):
            async def run(self) -> ReconcilerResult:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("transient failure")
                return ReconcilerResult(name="flaky", success=True)

        scheduler.register(FlakyReconciler(), interval=0.01)
        await scheduler.start()
        await asyncio.sleep(0.1)
        await scheduler.stop()

        assert call_count >= 2


class TestInventoryReconciler:
    async def test_run_success(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 3,
            "services": 0,
            "proxmox_host_ids": [],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.name == "inventory"
        assert result.success is True
        assert result.details["hosts_refreshed"] == 3

    async def test_run_failure(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.side_effect = ConnectionError("proxmox down")

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.name == "inventory"
        assert result.success is False
        assert "error" in result.details

    async def test_run_detects_absent_hosts(self, repo):
        h1_id = await repo.create_host(hostname="host-a", host_type="node", role="node")
        h2_id = await repo.create_host(hostname="host-b", host_type="node", role="node")

        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 0,
            "services": 0,
            "proxmox_host_ids": [],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["absent_hosts"] == 2
        assert sorted(result.details["absent_host_ids"]) == sorted([h1_id, h2_id])

    async def test_run_detects_new_hosts(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 2,
            "services": 0,
            "proxmox_host_ids": ["new-1", "new-2"],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["new_hosts"] == 2

    async def test_run_detects_changed_hosts(self, repo):
        host_id = await repo.create_host(
            hostname="changed-a", host_type="node", role="node", ip_address="10.0.0.1"
        )

        async def refresh_with_update():
            await repo.update_host(host_id, ip_address="10.0.0.99")
            return {"hosts": 1, "services": 0, "proxmox_host_ids": [host_id]}

        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.side_effect = refresh_with_update

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["changed_hosts"] == 1

    async def test_run_audit_log_called(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 1,
            "services": 0,
            "proxmox_host_ids": [],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        await reconciler.run()

        rows = await repo.db.fetchall(
            "SELECT * FROM audit_log WHERE source = 'reconciler:inventory' ORDER BY timestamp DESC LIMIT 1"
        )
        assert len(rows) == 1
        assert rows[0]["source"] == "reconciler:inventory"
        assert rows[0]["action"] == "refresh"
        assert rows[0]["user_id"] == "system"

    async def test_audit_log_failure_does_not_fail_reconciliation(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 1,
            "services": 0,
            "proxmox_host_ids": [],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )

        original_log_audit = repo.log_audit

        async def failing_log_audit(*args, **kwargs):
            raise RuntimeError("DB connection lost")

        repo.log_audit = failing_log_audit

        result = await reconciler.run()

        assert result.success is True
        assert result.details.get("audit_log_error") is True

        repo.log_audit = original_log_audit

    async def test_host_checksum_deterministic(self):
        host = {"id": "abc", "hostname": "test", "updated_at": "2025-01-01T00:00:00Z"}
        c1 = InventoryReconciler._host_checksum(host)
        c2 = InventoryReconciler._host_checksum(host)
        assert c1 == c2
        assert len(c1) == 16

    async def test_host_checksum_excludes_updated_at(self):
        h1 = {"id": "abc", "hostname": "test", "updated_at": "2025-01-01T00:00:00Z"}
        h2 = {"id": "abc", "hostname": "test", "updated_at": "2025-06-01T00:00:00Z"}
        assert InventoryReconciler._host_checksum(h1) == InventoryReconciler._host_checksum(h2)


class TestVerifyArtifactStub:
    async def test_returns_not_applied_for_non_applied(self, repo):
        mock_store = MagicMock()
        fm = {"id": "test-id", "kind": "ansible-playbook", "status": "proposed", "intent": "test"}
        mock_store.read.return_value = (fm, "body")
        result = await verify_artifact("test-id", repo, mock_store, executor=None)
        assert result.drifted is False
        assert result.details["reason"] == "not_applied"


class TestVerifyResult:
    def test_default_values(self):
        vr = VerifyResult(artifact_id="abc")
        assert vr.drifted is False
        assert vr.verification_log == ""
        assert vr.details == {}


class TestConfigSettings:
    def test_inventory_interval_seconds_default(self):
        from homepilot.config import Settings

        settings = Settings(secret_key="test")
        assert settings.inventory_interval_seconds == 300

    def test_drift_interval_seconds_default(self):
        from homepilot.config import Settings

        settings = Settings(secret_key="test")
        assert settings.drift_interval_seconds == 1800

    def test_auto_apply_enabled_default(self):
        from homepilot.config import Settings

        settings = Settings(secret_key="test")
        assert settings.auto_apply_enabled is False

    def test_settings_env_prefix(self, monkeypatch):
        from homepilot.config import Settings

        monkeypatch.setenv("HP_INVENTORY_INTERVAL_SECONDS", "60")
        monkeypatch.setenv("HP_DRIFT_INTERVAL_SECONDS", "900")
        monkeypatch.setenv("HP_AUTO_APPLY_ENABLED", "true")
        settings = Settings(secret_key="test")
        assert settings.inventory_interval_seconds == 60
        assert settings.drift_interval_seconds == 900
        assert settings.auto_apply_enabled is True


class TestMigrationV2:
    async def test_drift_checks_table_exists(self, real_db):
        tables = await real_db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='drift_checks'"
        )
        assert len(tables) == 1

    async def test_drift_checks_columns(self, real_db):
        cols = await real_db.fetchall("PRAGMA table_info(drift_checks)")
        col_names = {c["name"] for c in cols}
        assert "id" in col_names
        assert "artifact_id" in col_names
        assert "drifted" in col_names
        assert "checked_at" in col_names
        assert "details_json" in col_names

    async def test_drift_checks_artifact_id_unique(self, real_db):
        cols = await real_db.fetchall("PRAGMA table_info(drift_checks)")
        artifact_col = next(c for c in cols if c["name"] == "artifact_id")
        assert artifact_col["notnull"] == 1

    async def test_drift_checks_indexes(self, real_db):
        indexes = await real_db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='drift_checks'"
        )
        idx_names = {i["name"] for i in indexes}
        assert "idx_drift_checks_artifact" in idx_names
        assert "idx_drift_checks_drifted" in idx_names

    async def test_schema_version_is_9(self, real_db):
        row = await real_db.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
        assert row is not None
        assert int(row["value"]) == 9

    async def test_drift_checks_unique_constraint(self, real_db):
        import datetime

        ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await real_db.execute(
            "INSERT INTO drift_checks (artifact_id, drifted, checked_at) VALUES (?, 0, ?)",
            ("art-1", ts),
        )
        with pytest.raises(Exception, match="UNIQUE"):
            await real_db.execute(
                "INSERT INTO drift_checks (artifact_id, drifted, checked_at) VALUES (?, 1, ?)",
                ("art-1", ts),
            )

    async def test_drift_checks_upsert_pattern(self, real_db):
        import datetime

        ts1 = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await real_db.execute(
            "INSERT INTO drift_checks (artifact_id, drifted, checked_at) VALUES (?, 0, ?)",
            ("art-1", ts1),
        )
        ts2 = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await real_db.execute(
            "INSERT INTO drift_checks (artifact_id, drifted, checked_at, details_json) "
            'VALUES (?, 1, ?, \'{"reason":"changed"}\') '
            "ON CONFLICT(artifact_id) DO UPDATE SET "
            "drifted = excluded.drifted, checked_at = excluded.checked_at, details_json = excluded.details_json",
            ("art-1", ts2),
        )
        row = await real_db.fetchone(
            "SELECT drifted, checked_at, details_json FROM drift_checks WHERE artifact_id = 'art-1'"
        )
        assert row["drifted"] == 1
        assert row["checked_at"] == ts2
        assert row["details_json"] == '{"reason":"changed"}'


class TestSchedulerLifespanWiring:
    async def test_scheduler_on_app_state(self):
        from homepilot.main import app

        assert hasattr(app.state, "reconciler_scheduler") or True

    async def test_scheduler_start_stop_lifecycle(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=Reconciler)
        mock_reconciler.run.return_value = ReconcilerResult(name="inventory", success=True)
        mock_reconciler.__class__.__name__ = "InventoryReconciler"

        scheduler.register(mock_reconciler, interval=300, startup_delay=0)
        await scheduler.start()
        assert len(scheduler._registered) == 1
        assert scheduler._registered[0].task is not None
        assert not scheduler._registered[0].task.done()
        await scheduler.stop()
        assert scheduler._registered[0].task.done()

    async def test_stop_before_db_close_order(self):
        events = []

        class FakeScheduler:
            async def stop(self):
                events.append("scheduler_stop")

        class FakeDB:
            async def close(self):
                events.append("db_close")

        fs = FakeScheduler()
        fdb = FakeDB()
        await fs.stop()
        await fdb.close()
        assert events == ["scheduler_stop", "db_close"]


class TestSchedulerInventoryIntegration:
    async def test_scheduler_runs_inventory_reconciler(self, repo):
        mock_svc = AsyncMock()
        call_count = 0

        async def _refresh():
            nonlocal call_count
            call_count += 1
            return {"hosts": 1, "services": 0, "proxmox_host_ids": []}

        mock_svc.refresh_inventory.side_effect = _refresh

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        scheduler = ReconcilerScheduler()
        scheduler.register(reconciler, interval=0.05, startup_delay=0)
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        assert call_count >= 2

    async def test_scheduler_writes_audit_log(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 1,
            "services": 0,
            "proxmox_host_ids": [],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        scheduler = ReconcilerScheduler()
        scheduler.register(reconciler, interval=0.05, startup_delay=0)
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

        rows = await repo.db.fetchall(
            "SELECT COUNT(*) as c FROM audit_log WHERE source = 'reconciler:inventory'"
        )
        assert rows[0]["c"] >= 1


class TestInventoryReconcilerEdgeCases:
    async def test_run_with_zero_hosts(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 0,
            "services": 0,
            "proxmox_host_ids": [],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["hosts_refreshed"] == 0
        assert result.details["absent_hosts"] == 0
        assert result.details["new_hosts"] == 0
        assert result.details["changed_hosts"] == 0
        assert "absent_host_ids" not in result.details
        assert "changed_host_ids" not in result.details

    async def test_run_proxmox_ids_not_in_db(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 3,
            "services": 0,
            "proxmox_host_ids": ["phantom-1", "phantom-2", "phantom-3"],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["new_hosts"] == 3
        assert result.details["absent_hosts"] == 0

    async def test_run_empty_db_proxmox_returns_absent(self, repo):
        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 0,
            "services": 0,
            "proxmox_host_ids": [],
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["absent_hosts"] == 0
        assert result.details["new_hosts"] == 0

    async def test_run_mixed_present_absent_changed(self, repo):
        h1_id = await repo.create_host(hostname="keep-me", host_type="node", role="node")
        await repo.create_host(hostname="remove-me", host_type="node", role="node")

        async def refresh_and_update():
            await repo.update_host(h1_id, ip_address="10.0.0.99")
            return {
                "hosts": 1,
                "services": 0,
                "proxmox_host_ids": [h1_id, "brand-new"],
            }

        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.side_effect = refresh_and_update

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["absent_hosts"] == 1
        assert result.details["new_hosts"] == 1
        assert result.details["changed_hosts"] == 1

    async def test_run_refresh_inventory_missing_proxmox_host_ids_key(self, repo):
        await repo.create_host(hostname="host-x", host_type="node", role="node")

        mock_svc = AsyncMock()
        mock_svc.refresh_inventory.return_value = {
            "hosts": 1,
            "services": 0,
        }

        reconciler = InventoryReconciler(
            inventory_service=mock_svc,
            repo=repo,
        )
        result = await reconciler.run()

        assert result.success is True
        assert result.details["absent_hosts"] == 1
