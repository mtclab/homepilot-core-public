"""Gates for #648 tranche 4: backup, restore, migrations and data durability.

Every case here was reproduced on dev 3.6.15 before it was written, and the
2827-test suite was green throughout. Each one asserts the OUTCOME an operator
needs - a vault that opens, a database that reads, a claim that is true - never
that a call returned success.

The four things that were wrong:

1. `hp export --include-secrets` archived the vault identity and NOT the
   passphrase on every deployment shape the docs describe, while printing "It
   holds the vault identity and passphrase". The restore then could not start.
2. Restoring the product's own `backups/pre-migration-vNN.db` by hand - the
   remedy `run_migrations` itself recommends - corrupted the database, because
   the file was WAL-mode and the stale `-wal` beside it got replayed.
3. `/health` answered `database: ok` from `SELECT 1`, which reads no page, so a
   corrupted database reported healthy while real reads returned 500.
4. The vault probe on `/health` and `/admin/selfcheck` was `glob("*.age")`, so
   "The vault is unlocked" was established by filenames existing.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest

from homepilot.db.backup import (
    integrity_problems,
    remove_wal_sidecars,
    snapshot_database,
    wal_sidecars,
)
from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations
from homepilot.selfcheck import (
    STATE_OK,
    STATE_UNKNOWN,
    STATE_UNREACHABLE,
    ProbeVerdict,
    Subsystem,
    _artifacts_remote_subsystem,
    _evaluate,
    vault_unlocked,
)
from homepilot.vault import VaultError, VaultManager

PASSPHRASE = "gate-passphrase-not-for-production"


async def _seeded_db(db_path: Path, rows: int = 40) -> None:
    db = Database(str(db_path))
    await db.connect()
    try:
        await run_migrations(db)
        for index in range(rows):
            await db.execute(
                "INSERT INTO hosts (id, hostname, host_type, role, status, created_at, "
                "updated_at) VALUES (?, ?, 'vm', 'guest', 'running', '2026-08-30T00:00:00Z', "
                "'2026-08-30T00:00:00Z')",
                (f"host-{index}", f"h{index}.lan"),
            )
        await db.conn.commit()
    finally:
        await db.close()


# ── 1. A backup the product writes must survive being restored ──────────────


class TestPreMigrationBackupIsSafeToRestore:
    """The 2026-08-29 incident, as a standing assertion.

    `run_migrations` refuses a database newer than the build and names
    `backups/pre-migration-v<N>.db` as the thing to restore. Restoring it the
    obvious way destroyed the database: the file was written with the sqlite
    backup API, which copies pages and therefore inherits the source's WAL
    header, and SQLite replayed the stale `-wal` sitting beside `homepilot.db`
    onto it. `database disk image is malformed`, both copies gone.
    """

    def test_the_snapshot_is_not_in_wal_mode(self, tmp_path):
        db_path = tmp_path / "homepilot.db"
        asyncio.run(_seeded_db(db_path))

        # Force a migration so the pre-migration backup is taken for real.
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE settings SET value = ? WHERE key = 'schema_version'", ("1",))
        conn.commit()
        conn.close()

        async def _migrate() -> None:
            db = Database(str(db_path))
            await db.connect()
            try:
                await run_migrations(db)
            finally:
                await db.close()

        asyncio.run(_migrate())

        backup = tmp_path / "backups" / "pre-migration-v1.db"
        assert backup.exists(), "the pre-migration backup was not taken"

        probe = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            mode = probe.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            probe.close()
        # A WAL-mode backup is the loaded gun: dropped over homepilot.db beside
        # a stale journal, SQLite replays that journal into it.
        assert mode.lower() != "wal", (
            "the pre-migration backup is WAL-mode again - restoring it beside a "
            "stale -wal will corrupt it"
        )
        assert integrity_problems(backup) == []

    def test_a_stale_wal_replayed_onto_a_restored_backup_corrupts_it(self, tmp_path):
        """The mechanism itself, so the fix above cannot be undone quietly.

        The dev incident in miniature: `homepilot.db` was at schema v30 with an
        uncheckpointed journal beside it, and `pre-migration-v29.db` - a
        WAL-MODE file describing a different generation of the same database -
        was copied over it. SQLite binds a WAL to no particular main file, so it
        replayed one database's journal into another's pages.
        """
        db_path = tmp_path / "homepilot.db"
        asyncio.run(_seeded_db(db_path, rows=600))

        # The journal of the database that is being replaced, captured while it
        # is still uncheckpointed - what an OOM kill leaves on disk.
        live = sqlite3.connect(str(db_path))
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        for index in range(600):
            live.execute(
                "INSERT INTO hosts (id, hostname, host_type, role, status, created_at, "
                "updated_at) VALUES (?, ?, 'vm', 'guest', 'running', 'x', 'x')",
                (f"late-{index}", f"late{index}.lan"),
            )
        live.commit()
        wal, shm = wal_sidecars(db_path)
        assert wal.exists() and wal.stat().st_size > 0
        stale_wal = tmp_path / "stale-wal"
        stale_shm = tmp_path / "stale-shm"
        shutil.copyfile(str(wal), str(stale_wal))
        shutil.copyfile(str(shm), str(stale_shm))
        live.close()

        # A different generation of the database, in WAL mode - what the sqlite
        # backup API produces, and what `pre-migration-vNN.db` used to be.
        other = tmp_path / "pre-migration-vNN.db"
        asyncio.run(_seeded_db(other, rows=3))
        wal_backup = tmp_path / "wal-mode-backup.db"
        source = sqlite3.connect(str(other))
        target = sqlite3.connect(str(wal_backup))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        probe = sqlite3.connect(f"file:{wal_backup}?mode=ro", uri=True)
        try:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            probe.close()
        assert integrity_problems(wal_backup) == []

        # The restore an operator would perform, by hand, as told.
        remove_wal_sidecars(db_path)
        shutil.copyfile(str(wal_backup), str(db_path))
        shutil.copyfile(str(stale_wal), str(wal))
        shutil.copyfile(str(stale_shm), str(shm))
        assert integrity_problems(db_path) != [], (
            "a WAL-mode backup copied over a database with a stale -wal was "
            "expected to be corrupted by the replay"
        )

        # And the supported path - clearing the sidecars first - does not.
        remove_wal_sidecars(db_path)
        shutil.copyfile(str(wal_backup), str(db_path))
        assert integrity_problems(db_path) == []

    def test_the_refusal_names_the_remedy_not_just_the_file(self, tmp_path):
        db_path = tmp_path / "homepilot.db"
        asyncio.run(_seeded_db(db_path))
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'schema_version'",
            (str(max(MIGRATIONS.keys()) + 5),),
        )
        conn.commit()
        conn.close()

        async def _migrate() -> None:
            db = Database(str(db_path))
            await db.connect()
            try:
                await run_migrations(db)
            finally:
                await db.close()

        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(_migrate())
        message = str(excinfo.value)
        # Naming the file and stopping there is what sent an operator to `cp`.
        assert "hp db restore" in message
        assert "-wal" in message
        assert "by hand" in message


class TestRestoreClearsTheSidecars:
    def test_remove_wal_sidecars_reports_what_it_removed(self, tmp_path):
        db_path = tmp_path / "homepilot.db"
        db_path.write_bytes(b"")
        wal, shm = wal_sidecars(db_path)
        wal.write_bytes(b"stale")
        shm.write_bytes(b"stale")
        assert sorted(p.name for p in remove_wal_sidecars(db_path)) == [
            "homepilot.db-shm",
            "homepilot.db-wal",
        ]
        assert not wal.exists() and not shm.exists()
        # Idempotent: nothing to remove is not an error.
        assert remove_wal_sidecars(db_path) == []


# ── 2. A backup taken from a LIVE backend must be consistent ────────────────


class TestSnapshotOfALiveDatabase:
    def test_vacuum_into_snapshot_is_sound_and_has_no_sidecars(self, tmp_path):
        db_path = tmp_path / "homepilot.db"
        asyncio.run(_seeded_db(db_path))

        # A second connection open and mid-transaction, standing in for the
        # running backend: the snapshot must not be a torn read.
        writer = sqlite3.connect(str(db_path))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN")
        writer.execute(
            "INSERT INTO hosts (id, hostname, host_type, role, status, created_at, updated_at) "
            "VALUES ('uncommitted', 'nope.lan', 'vm', 'guest', 'running', 'x', 'x')"
        )
        try:
            dest = tmp_path / "snapshot.db"
            snapshot_database(db_path, dest)
        finally:
            writer.rollback()
            writer.close()

        assert integrity_problems(dest) == []
        wal, shm = wal_sidecars(dest)
        assert not wal.exists() and not shm.exists()
        probe = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
            names = [r[0] for r in probe.execute("SELECT hostname FROM hosts")]
        finally:
            probe.close()
        assert "nope.lan" not in names, "an uncommitted row reached the snapshot"


class TestIntegrityProblems:
    def test_a_sound_database_has_none_and_a_broken_one_says_so(self, tmp_path):
        db_path = tmp_path / "homepilot.db"
        asyncio.run(_seeded_db(db_path))
        assert integrity_problems(db_path) == []

        broken = tmp_path / "broken.db"
        broken.write_bytes(db_path.read_bytes()[:2048] + b"\x00" * 4096)
        assert integrity_problems(broken) != []

    def test_reading_the_schema_version_of_a_corrupt_file_answers_zero(self, tmp_path):
        """`hp db restore` asks this about the database it is REPLACING.

        Found on the real product: the narrower `except OperationalError` let
        `DatabaseError: database disk image is malformed` escape, so the restore
        traceback'd before restoring anything - failing in exactly the case the
        command exists for.
        """
        from homepilot.db.backup import read_schema_version

        db_path = tmp_path / "homepilot.db"
        asyncio.run(_seeded_db(db_path))
        assert read_schema_version(db_path) == max(MIGRATIONS.keys())

        corrupt = tmp_path / "corrupt.db"
        corrupt.write_bytes(db_path.read_bytes()[:2048] + b"\xff" * 8192)
        assert read_schema_version(corrupt) == 0
        assert read_schema_version(tmp_path / "absent.db") == 0


# ── 3. `database: ok` must mean the database can be READ ────────────────────


class TestHealthReadsTheDatabase:
    """`SELECT 1` opens no table. On 2026-08-29 an OOM kill corrupted the file
    mid-write and `/health` reported `database: ok` throughout, while
    `list_tasks` returned 500. Reproduced on 3.6.15: with the file replaced
    under a running backend, `/health` stayed `{"database":"ok"}` and HTTP 200
    while `/inventory` and `/admin/selfcheck` both 500'd.
    """

    @staticmethod
    async def _health(db) -> dict:
        """Call the real handler against a real database, no TestClient portal."""
        import json
        from types import SimpleNamespace

        from homepilot.main import health

        settings = SimpleNamespace(
            proxmox_host="",
            agent_hub_enabled=False,
            cors_origins="http://localhost:5173",
        )
        state = SimpleNamespace(db=db, settings=settings)
        response = await health(SimpleNamespace(app=SimpleNamespace(state=state)))
        return json.loads(bytes(response.body))

    @pytest.mark.asyncio
    async def test_a_database_that_cannot_be_read_is_not_ok(self, tmp_path):
        db_path = tmp_path / "homepilot.db"
        await _seeded_db(db_path)
        db = Database(str(db_path))
        await db.connect()
        try:
            sound = await self._health(db)
            assert sound["checks"]["database"] == "ok"

            # The database the process is holding stops being readable. This is
            # the shape of the incident: the connection is fine, the FILE is
            # not, and `SELECT 1` cannot tell the difference.
            cursor = await db.execute("SELECT 1")
            assert await cursor.fetchone() is not None, "SELECT 1 still answers - that is the bug"

            await db.execute("ALTER TABLE settings RENAME TO settings_moved")
            await db.conn.commit()

            cursor = await db.execute("SELECT 1")
            assert await cursor.fetchone() is not None, (
                "SELECT 1 STILL answers on a database that can no longer serve "
                "the product - which is why it must not be the probe"
            )
            broken = await self._health(db)
            assert broken["checks"]["database"] == "error"
            assert broken["status"] == "down"
        finally:
            await db.close()


class TestIntegrityIsCheckedWhereItCanBeSeen:
    """The page cache is why a row read on the app's connection is not enough.

    Verified on a scratch copy of dev 3.6.15: with `homepilot.db` replaced under
    the running backend, `/health`, `/inventory`, `/artifacts`,
    `/artifacts/drift` and `/admin/selfcheck` ALL returned 200 - served out of
    cached pages - while a fresh connection to the same file answered `database
    disk image is malformed`. The check has to own its connection.
    """

    @pytest.mark.asyncio
    async def test_the_reconciler_records_a_corrupt_file_and_health_reports_it(self, tmp_path):
        from homepilot.db.repository import Repository
        from homepilot.reconciler.db_integrity import (
            LAST_CHECK_OK,
            LAST_CHECK_PROBLEMS,
            DatabaseIntegrityReconciler,
        )

        db_path = tmp_path / "homepilot.db"
        await _seeded_db(db_path, rows=200)
        db = Database(str(db_path))
        await db.connect()
        try:
            repo = Repository(db)
            reconciler = DatabaseIntegrityReconciler(db_path, repo)

            result = await reconciler.run()
            assert result.details["ok"] is True
            healthy = await TestHealthReadsTheDatabase._health(db)
            assert healthy["checks"]["database"] == "ok"

            # Damage the FILE under the live connection. Cached pages keep
            # answering - that is the whole point.
            payload = bytearray(db_path.read_bytes())
            page_size = 4096
            for offset in range(page_size * 3, min(len(payload), page_size * 12)):
                payload[offset] = 0xFF
            db_path.write_bytes(bytes(payload))

            result = await reconciler.run()
            assert result.details["ok"] is False, "quick_check did not see the damage"
            assert result.details["problems"]

            row = await repo.get_setting(LAST_CHECK_OK)
            assert row is not None and row["value"] == "0"
            problems = await repo.get_setting(LAST_CHECK_PROBLEMS)
            assert problems is not None and problems["value"]

            broken = await TestHealthReadsTheDatabase._health(db)
            assert broken["checks"]["database"] == "corrupt", (
                "the file is corrupt, the recorded verdict says so, and /health still calls it ok"
            )
            assert broken["status"] == "down"
        finally:
            await db.close()


# ── 4. `vault: ok` must mean the vault OPENS ────────────────────────────────


class TestVaultProbeOpensTheVault:
    @pytest.mark.asyncio
    async def test_a_listable_but_unopenable_vault_is_not_ok(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        vault = VaultManager(data_dir, PASSPHRASE)
        await vault.ensure_master_identity()
        await vault.store_secret("pve-token", {"token": "t"})

        assert await vault_unlocked(vault) is True

        # The same files, the wrong passphrase: exactly what a restore without
        # the source host's key leaves behind.
        wrong = VaultManager(data_dir, "a-different-passphrase")
        assert await wrong.list_secrets() == ["pve-token"], (
            "list_secrets is a directory listing - that is why it could not be the probe"
        )
        with pytest.raises(VaultError):
            await wrong.ensure_master_identity()
        assert await vault_unlocked(wrong) is False


class TestLockedVaultDoesNotKillTheBackend:
    @pytest.mark.asyncio
    async def test_create_app_state_survives_a_vault_it_cannot_open(self, tmp_path, monkeypatch):
        """A wrong passphrase used to end the lifespan: exit 3, crash loop.

        /health's own comment says a vault that needs unlocking must not mark
        the container unhealthy "over something no restart can repair". The
        lifespan had not learned it, so the ordinary result of restoring a
        backup was a container that would not start.
        """
        import tempfile

        from homepilot.app_state import create_app_state

        # Under the HOME directory, not /tmp: `create_app_state` refuses an
        # artifacts dir on a protected path.
        home_tmp = Path(tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-648-"))
        data_dir = home_tmp / "hp"
        data_dir.mkdir()
        vault = VaultManager(data_dir, PASSPHRASE)
        await vault.ensure_master_identity()

        monkeypatch.setenv("HP_DATA_DIR", str(data_dir))
        monkeypatch.setenv("HP_ARTIFACTS_DIR", str(data_dir / "artifacts"))
        monkeypatch.setenv("HP_VAULT_PASSPHRASE", "the-wrong-passphrase")
        monkeypatch.setenv("HP_AGENT_HUB_ENABLED", "false")
        from homepilot.config import get_settings

        # Every Database `create_app_state` opens, so that a REGRESSION here
        # fails instead of hanging: the old code raised after connecting and
        # left an aiosqlite worker thread - non-daemon - with nobody to close
        # it, which wedges the interpreter at exit (#496).
        opened: list[Database] = []
        real_database = Database

        class _Recording(Database):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)

        monkeypatch.setattr("homepilot.app_state.Database", _Recording)
        assert _Recording is not real_database

        get_settings.cache_clear()
        state = None
        try:
            state = await create_app_state(get_settings())
            assert state.vault is not None, "the vault must stay on the state, locked"
            assert await vault_unlocked(state.vault) is False
        finally:
            get_settings.cache_clear()
            for database in opened:
                await database.close()
            shutil.rmtree(home_tmp, ignore_errors=True)


# ── 5. The artifacts remote must not report an off-box copy it has not made ──


class _Repo:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get_setting(self, key: str):
        if key not in self._values:
            return None
        return {"value": self._values[key]}


class _State:
    def __init__(self, repo) -> None:
        self.repo = repo


class _Settings:
    artifacts_remote = "git@example.com:org/artifacts.git"
    artifacts_push_interval_seconds = 3600


async def _remote_entry(values: dict[str, str]) -> dict:
    subsystem = _artifacts_remote_subsystem(_State(_Repo(values)), _Settings())
    return await _evaluate(subsystem, timeout=2.0)


class TestArtifactsRemoteReportsWhatItKnows:
    @pytest.mark.asyncio
    async def test_never_pushed_is_not_ok(self):
        """ "(or the first one has not run yet)" was doing all the work."""
        entry = await _remote_entry({})
        assert entry["state"] == STATE_UNKNOWN, (
            "an instance that has never pushed has no off-box copy; it must not "
            "report the most recent push as successful"
        )
        assert "no off-box copy" in entry["consequence"]

    @pytest.mark.asyncio
    async def test_a_push_that_stopped_running_goes_stale(self):
        entry = await _remote_entry(
            {"archive_last_push_ok": "1", "archive_last_push_at": "2026-01-01T00:00:00Z"}
        )
        assert entry["state"] == STATE_UNREACHABLE
        assert "stopped running" in entry["consequence"]

    @pytest.mark.asyncio
    async def test_a_recent_success_is_ok_and_says_when(self):
        from homepilot.db.repository import now

        entry = await _remote_entry({"archive_last_push_ok": "1", "archive_last_push_at": now()})
        assert entry["state"] == STATE_OK
        assert "ago" in entry["consequence"]

    @pytest.mark.asyncio
    async def test_a_failed_push_is_still_broken(self):
        from homepilot.db.repository import now

        entry = await _remote_entry({"archive_last_push_ok": "0", "archive_last_push_at": now()})
        assert entry["state"] == STATE_UNREACHABLE
        assert "FAILED" in entry["consequence"]


class TestProbeVerdictIsHonoured:
    @pytest.mark.asyncio
    async def test_a_verdict_overrides_the_boolean_consequences(self):
        async def probe():
            return ProbeVerdict(STATE_UNKNOWN, "not established")

        entry = await _evaluate(
            Subsystem(
                name="x",
                label="x",
                configured=True,
                target="",
                off="off",
                ok="ok",
                broken="broken",
                probe=probe,
            ),
            timeout=1.0,
        )
        assert entry["state"] == STATE_UNKNOWN
        assert entry["consequence"] == "not established"


# ── 6. Retention deletes what it claims, and claims only what it does ───────


class TestRetentionClaims:
    @pytest.mark.asyncio
    async def test_reclaim_reports_zero_rather_than_promising_a_shrink(self, tmp_path):
        """`incremental_vacuum` is a no-op without auto_vacuum=INCREMENTAL, and
        HomePilot never sets it (dev 3.6.15: auto_vacuum=0). The docstring
        promised a `VACUUM` fallback that does not exist and README told
        operators freed pages were returned to the filesystem."""
        from homepilot.db.repository import Repository

        db_path = tmp_path / "homepilot.db"
        await _seeded_db(db_path, rows=500)
        db = Database(str(db_path))
        await db.connect()
        try:
            repo = Repository(db)
            await db.execute("DELETE FROM hosts")
            await db.conn.commit()
            freed = await repo.reclaim_free_pages()
            assert isinstance(freed, int)
            mode = await db.fetchone("PRAGMA auto_vacuum")
            assert int(next(iter(mode.values()))) == 0
            assert freed == 0, (
                "reclaim_free_pages reported a shrink under auto_vacuum=NONE - "
                "either the pragma changed or the count is wrong"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_prune_only_touches_rows_older_than_the_cutoff(self, tmp_path):
        from homepilot.db.repository import Repository

        db_path = tmp_path / "homepilot.db"
        await _seeded_db(db_path, rows=0)
        db = Database(str(db_path))
        await db.connect()
        try:
            repo = Repository(db)
            for stamp in ("2026-01-01T00:00:00Z", "2026-08-30T00:00:00Z"):
                await db.execute(
                    "INSERT INTO audit_log (timestamp, user_id, source, action) "
                    "VALUES (?, 'u', 'test', 'act')",
                    (stamp,),
                )
            await db.conn.commit()
            deleted = await repo.prune_before("audit_log", "timestamp", "2026-06-01T00:00:00Z")
            assert deleted == 1
            rows = await db.fetchall("SELECT timestamp FROM audit_log")
            assert [r["timestamp"] for r in rows] == ["2026-08-30T00:00:00Z"]

            # The allowlist is the only thing standing between a table name and
            # an interpolated DELETE.
            with pytest.raises(ValueError):
                await repo.prune_before("hosts", "created_at", "2026-06-01T00:00:00Z")
            with pytest.raises(ValueError):
                await repo.prune_before("audit_log", "created_at", "2026-06-01T00:00:00Z")
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_horizon_of_zero_cannot_delete_everything(self, tmp_path):
        from homepilot.reconciler.retention import RetentionReconciler

        class _Resolver:
            def __init__(self, value):
                self._value = value

            async def value(self, _key):
                return self._value

        reconciler = RetentionReconciler(repo=None, retention_days=0, resolver=_Resolver(0))
        assert await reconciler._resolve_days() == 1
        reconciler_negative = RetentionReconciler(
            repo=None, retention_days=-5000, resolver=_Resolver(-5000)
        )
        assert await reconciler_negative._resolve_days() == 1
