"""An orderly shutdown checkpoints the WAL explicitly, off the loop, BEFORE close.

Closing the last connection to a WAL database folds the WAL into the database
file implicitly - unbounded disk I/O inside `Database.close()`, which is what
froze the loop for 16-22 s in #795. main.lifespan now runs
`Database.checkpoint()` (`PRAGMA wal_checkpoint(TRUNCATE)` on a daemon thread,
bounded) once every writer has drained, so by the time close() runs the WAL is
empty and the close is cheap.

TEETH (verified by mutation for the PR that introduced this file):
* delete the `state.database.checkpoint()` call from main.lifespan -> the
  journey sees a multi-megabyte WAL when close() starts;
* run `_checkpoint_file` on the loop thread inside `checkpoint()` -> the stall
  detector reports the full slow checkpoint;
* drop the bounded wait (await the thread unconditionally) -> the stuck
  checkpoint case outlasts its budget.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from tests.test_background_shutdown import _LoopStallDetector


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wal_size(db_path: str) -> int:
    wal = Path(db_path + "-wal")
    return wal.stat().st_size if wal.exists() else 0


@pytest.fixture
def hp_dir() -> Iterator[str]:
    """Must live under $HOME: create_app_state refuses an artifacts_dir in /tmp."""
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-walcheckpoint-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _write_rows(db: Database, count: int) -> None:
    for i in range(count):
        await db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (f"walcheck.{i}", "x" * 500, "2026-01-01T00:00:00Z"),
        )
    await db.conn.commit()


class TestTheRealShutdownCheckpointsBeforeClose:
    async def test_close_finds_the_wal_already_folded_in(
        self, hp_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import homepilot.main as main_mod
        from homepilot.config import Settings

        settings = Settings(
            data_dir=hp_dir,
            artifacts_dir=os.path.join(hp_dir, "artifacts"),
            agent_hub_port=_free_port(),
        )
        monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

        events: list[tuple[str, int]] = []
        real_checkpoint = Database.checkpoint
        real_close = Database.close

        async def checkpoint(self: Database) -> bool:
            events.append(("checkpoint", _wal_size(self.db_path)))
            return await real_checkpoint(self)

        async def close(self: Database) -> None:
            events.append(("close", _wal_size(self.db_path)))
            await real_close(self)

        monkeypatch.setattr(Database, "checkpoint", checkpoint)
        monkeypatch.setattr(Database, "close", close)

        async with main_mod.lifespan(main_mod.app):
            db = main_mod.app.state.db
            db_path = db.db_path
            await _write_rows(db, 2000)
            assert _wal_size(db_path) > 512 * 1024, "the scenario needs a sizeable WAL"

        closes = [size for name, size in events if name == "close"]
        assert closes, "the shutdown never closed the database"
        assert closes[0] == 0, (
            f"close() started with a {closes[0]}-byte WAL: the shutdown did not fold it "
            "in first, so the close pays for the checkpoint (#795)"
        )
        names = [name for name, _ in events]
        assert "checkpoint" in names, "the shutdown never checkpointed the WAL"
        assert names.index("checkpoint") < names.index("close"), (
            f"checkpoint ran after close: {events}"
        )

        # And the rows are in the database file itself, not only in a WAL.
        with sqlite3.connect(db_path) as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM settings WHERE key LIKE 'walcheck.%'"
            ).fetchone()
        assert count == 2000

    async def test_work_left_on_the_pools_finishes_before_the_checkpoint(
        self, hp_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#800's checkpoint meets #801's pools (3.9.0): the pools stop FIRST.

        A job still running on the `io` pool when the shutdown starts - the
        integrity check is one: its own sqlite connection walking every page -
        holds a read transaction, and `wal_checkpoint(TRUNCATE)` cannot reset
        the WAL under a reader. The shutdown lets the pools finish (bounded)
        before it checkpoints, so the close still finds the WAL folded in.
        """
        import homepilot.main as main_mod
        from homepilot.config import Settings
        from homepilot.runtime.blocking import run_blocking

        settings = Settings(
            data_dir=hp_dir,
            artifacts_dir=os.path.join(hp_dir, "artifacts"),
            agent_hub_port=_free_port(),
        )
        monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
        # The checkpoint's own budget, short so the reader outlasts it.
        monkeypatch.setattr(Database, "_CHECKPOINT_SECONDS", 0.5)

        events: list[tuple[str, int]] = []
        real_close = Database.close

        async def close(self: Database) -> None:
            events.append(("close", _wal_size(self.db_path)))
            await real_close(self)

        monkeypatch.setattr(Database, "close", close)

        reading = threading.Event()

        def read_for_a_while(path: str) -> None:
            conn = sqlite3.connect(path, isolation_level=None)
            try:
                conn.execute("BEGIN")
                conn.execute("SELECT COUNT(*) FROM settings").fetchone()
                reading.set()
                time.sleep(1.5)
                conn.execute("COMMIT")
            finally:
                conn.close()

        async with main_mod.lifespan(main_mod.app):
            db = main_mod.app.state.db
            db_path = db.db_path
            await _write_rows(db, 2000)
            assert _wal_size(db_path) > 512 * 1024, "the scenario needs a sizeable WAL"
            job = asyncio.ensure_future(run_blocking("io", read_for_a_while, db_path))
            assert await asyncio.to_thread(reading.wait, 5.0), "the reader never started"
        await job

        closes = [size for name, size in events if name == "close"]
        assert closes, "the shutdown never closed the database"
        assert closes[0] == 0, (
            f"close() started with a {closes[0]}-byte WAL: the checkpoint ran while a "
            "pool job still held a read transaction"
        )


class _SlowCheckpoint:
    """Stands in for a checkpoint on a busy disk: blocks its thread, not the GIL."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.release = threading.Event()
        self.real = Database._checkpoint_file

    def __call__(self, db_path: str, busy_seconds: float) -> tuple[int, int, int]:
        self.release.wait(self.seconds)
        return self.real(db_path, busy_seconds)


class TestTheCheckpointIsOffTheLoopAndBounded:
    _STALL_LIMIT = 0.5

    @pytest.fixture
    async def db(self, tmp_path: Path):
        database = Database(str(tmp_path / "homepilot.db"))
        await database.connect()
        await run_migrations(database)
        await _write_rows(database, 300)
        yield database
        await database.close()

    async def test_it_truncates_the_wal(self, db: Database) -> None:
        assert _wal_size(db.db_path) > 0
        assert await db.checkpoint() is True
        assert _wal_size(db.db_path) == 0

    async def test_a_slow_checkpoint_does_not_stall_the_loop(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Database, "_checkpoint_file", staticmethod(_SlowCheckpoint(1.5)))
        async with _LoopStallDetector() as stalls:
            assert await db.checkpoint() is True
        assert stalls.worst < self._STALL_LIMIT, (
            f"the event loop was blocked for {stalls.worst:.1f}s by the WAL checkpoint"
        )
        assert _wal_size(db.db_path) == 0

    async def test_a_stuck_checkpoint_is_abandoned_within_its_budget(
        self, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stuck = _SlowCheckpoint(60)
        monkeypatch.setattr(Database, "_checkpoint_file", staticmethod(stuck))
        monkeypatch.setattr(Database, "_CHECKPOINT_SECONDS", 0.5)
        try:
            started = time.monotonic()
            assert await asyncio.wait_for(db.checkpoint(), timeout=10) is False
            assert time.monotonic() - started < 2.0
            # The close is not held up by the checkpoint still running.
            started = time.monotonic()
            await db.close()
            assert time.monotonic() - started < Database._CLOSE_TIMEOUT_SECONDS
        finally:
            stuck.release.set()

    async def test_an_in_memory_database_has_nothing_to_checkpoint(self) -> None:
        database = Database(":memory:")
        await database.connect()
        try:
            assert await database.checkpoint() is False
        finally:
            await database.close()
