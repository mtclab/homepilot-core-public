from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

from ..runtime.blocking import assert_off_loop

try:  # the value aiosqlite's worker thread breaks its own loop on
    from aiosqlite.core import _STOP_RUNNING_SENTINEL
except ImportError:  # pragma: no cover - a rename costs us only the wake
    _STOP_RUNNING_SENTINEL = object()

logger = logging.getLogger(__name__)

_closer_lock = threading.Lock()
_closer_loop: asyncio.AbstractEventLoop | None = None


def _closer() -> asyncio.AbstractEventLoop:
    """A DAEMON event loop that owns every aiosqlite close.

    A close we may have to abandon must not be a task on the caller's loop:
    `asyncio.Runner` cancels leftover tasks at loop close and then AWAITS them,
    and an aiosqlite close cancelled inside its own `finally: await future`
    never finishes - so the abandonment turns into a hang at interpreter exit
    instead of a hang at shutdown (#496). Run it here and the worst case costs a
    daemon thread, which the process is free to leave behind.
    """
    global _closer_loop
    with _closer_lock:
        if _closer_loop is None or _closer_loop.is_closed():
            _closer_loop = asyncio.new_event_loop()
            threading.Thread(
                target=_closer_loop.run_forever, name="hp-db-closer", daemon=True
            ).start()
        return _closer_loop


_CONNECT_RETRIES = int(os.environ.get("HP_DB_CONNECT_RETRIES", "3"))
_CONNECT_RETRY_DELAY = float(os.environ.get("HP_DB_CONNECT_RETRY_DELAY", "1.0"))


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        last_exc: Exception | None = None
        for attempt in range(1, _CONNECT_RETRIES + 1):
            try:
                # check_same_thread=False so the sqlite3 handle can be reached
                # from the event-loop thread as well as aiosqlite's worker. Only
                # `close()` uses that, and only to interrupt and shut a handle
                # whose worker can no longer do it (#496); every ordinary
                # statement still goes through aiosqlite's single worker thread,
                # so nothing becomes concurrent by allowing this.
                self._connection = await aiosqlite.connect(self.db_path, check_same_thread=False)
                await self._connection.enable_load_extension(True)
                await self._connection.load_extension(sqlite_vec.loadable_path())
                await self._connection.enable_load_extension(False)
                await self._connection.execute("PRAGMA journal_mode=WAL")
                busy_timeout = int(os.environ.get("HP_BUSY_TIMEOUT", "10000"))
                await self._connection.execute(f"PRAGMA busy_timeout={busy_timeout}")
                await self._connection.execute("PRAGMA synchronous=NORMAL")
                await self._connection.execute("PRAGMA foreign_keys=ON")
                await self._connection.execute("PRAGMA cache_size=-64000")
                self._connection.row_factory = aiosqlite.Row
                if attempt > 1:
                    logger.info("Database connected on attempt %d", attempt)
                return self._connection
            except (aiosqlite.OperationalError, aiosqlite.DatabaseError) as exc:
                last_exc = exc
                if self._connection is not None:
                    with contextlib.suppress(Exception):
                        await self._connection.close()
                    self._connection = None
                if attempt < _CONNECT_RETRIES:
                    logger.warning(
                        "Database connect attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt,
                        _CONNECT_RETRIES,
                        exc,
                        _CONNECT_RETRY_DELAY,
                    )
                    await asyncio.sleep(_CONNECT_RETRY_DELAY)
                else:
                    logger.error(
                        "Database connect failed after %d attempts: %s",
                        _CONNECT_RETRIES,
                        exc,
                    )
        raise last_exc  # type: ignore[misc]

    # A close that cannot complete must not wedge the process (#496). Sized well
    # under the 10s budget main.lifespan allows the whole shutdown step.
    _CLOSE_TIMEOUT_SECONDS = 5.0
    # After forcing the handle shut, how long aiosqlite's own close is given to
    # notice and unwind before we stop caring about it.
    _FORCE_GRACE_SECONDS = 1.0
    # How long the caller waits for a forced handle close before it stops
    # caring. Closing the last connection to a WAL database checkpoints the WAL
    # into the database file and fsyncs it: disk I/O with no upper bound under
    # I/O pressure. So the forced close runs on its own daemon thread, never on
    # the event loop (it blocked the loop for 16s once), and this bounds the
    # wait. Worst case of the whole close: 5 + 3 + 1 = 9s, inside main.lifespan's
    # 10s.
    _HANDLE_CLOSE_SECONDS = 3.0

    # How long an orderly shutdown waits for the WAL checkpoint before it closes
    # anyway. Not part of close()'s own budget: main.lifespan runs the checkpoint
    # first, outside the guard it puts around close().
    _CHECKPOINT_SECONDS = 3.0

    async def checkpoint(self) -> bool:
        """Fold the WAL into the database file and truncate it: off the loop, bounded.

        Closing the last connection to a WAL database checkpoints it implicitly,
        which is unbounded disk I/O inside close() - the cost #795 measured
        (checkpoint first, and the close takes about a millisecond). An orderly
        shutdown calls this once nothing writes any more, so the checkpoint is
        explicit, timed and logged, and close() has nothing left to do.

        Runs `PRAGMA wal_checkpoint(TRUNCATE)` on a SEPARATE short-lived sqlite3
        connection, on its own daemon thread - the #795 pattern: never on the
        event loop, never on the default executor (joined at exit), and never
        through aiosqlite's worker, where a slow checkpoint would queue close()
        behind it. The aiosqlite handle is not shared with another thread.
        Waits at most `_CHECKPOINT_SECONDS`; a checkpoint still running then
        finishes in the background, and one that never finishes is safe - the
        WAL stays and the next start (or the close) folds it in.

        Returns True when the WAL was fully checkpointed and truncated.
        """
        if self._connection is None or self._is_memory():
            return False
        await self._roll_back_orphaned_transaction()
        path = self.db_path
        outcome: dict[str, Any] = {}

        def work() -> None:
            try:
                outcome["row"] = self._checkpoint_file(path, self._CHECKPOINT_SECONDS)
            except Exception as exc:
                outcome["error"] = exc

        if not await self._on_daemon_thread(work, "hp-db-checkpoint", self._CHECKPOINT_SECONDS):
            logger.warning(
                "WAL checkpoint still running after %.0fs (slow disk). Closing anyway; "
                "it finishes in the background.",
                self._CHECKPOINT_SECONDS,
            )
            return False
        if "error" in outcome:
            logger.warning("WAL checkpoint before close failed: %s", outcome["error"])
            return False
        busy, log_frames, checkpointed = outcome["row"]
        if busy:
            # Something still holds a read or write transaction: the WAL is only
            # partly folded in. The close does the rest.
            logger.warning(
                "WAL checkpoint before close was blocked (%d of %d frames written back)",
                checkpointed,
                log_frames,
            )
            return False
        logger.info("WAL checkpointed and truncated before close")
        return True

    async def _roll_back_orphaned_transaction(self) -> None:
        """Roll back a write transaction nobody will ever finish. Shutdown only.

        Every repository shares this connection, and SQLite opens a transaction
        implicitly at the first write. A coroutine cut off between that write
        and its commit - a reconciler cancelled at shutdown, a method whose
        commit failed - leaves the transaction OPEN, and
        `wal_checkpoint(TRUNCATE)` waits on an open writer for its whole budget
        and then gives up: the "WAL checkpoint still running after 3s" the e2e
        fleet server logged as it shut down. `checkpoint()` runs once nothing
        writes any more, so an open transaction then has no owner left to
        commit it, and closing the connection would roll it back anyway; doing
        that first lets the checkpoint run.

        Through aiosqlite's worker, on the daemon closer loop, bounded and never
        cancelled - the pattern `close()` uses, for the same reason (#496).
        """
        conn = self._connection
        if conn is None or not conn.in_transaction:
            return
        bridge = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(conn.rollback(), _closer()))
        _done, pending = await asyncio.wait({bridge}, timeout=self._CHECKPOINT_SECONDS)
        if pending:
            logger.warning(
                "A write transaction was still open at shutdown and rolling it back did "
                "not finish in %.0fs; the close rolls it back.",
                self._CHECKPOINT_SECONDS,
            )
            return
        exc = bridge.exception()
        if exc is not None:
            logger.warning(
                "A write transaction was still open at shutdown; rolling it back failed: %s", exc
            )
            return
        logger.warning(
            "A write transaction was still open at shutdown (a writer was interrupted "
            "before its commit); rolled it back so the WAL checkpoint can run."
        )

    def _is_memory(self) -> bool:
        return self.db_path == ":memory:" or "mode=memory" in self.db_path

    @staticmethod
    def _checkpoint_file(db_path: str, busy_seconds: float) -> tuple[int, int, int]:
        """`PRAGMA wal_checkpoint(TRUNCATE)` on its own connection. Blocking: never on the loop.

        A second connection, so the aiosqlite handle is never touched from this
        thread; it is not the last one to the file, so closing it does no
        checkpoint of its own.
        """
        # Reported (a test failure in the suite) if a caller ever runs it on a
        # loop instead of through `checkpoint`'s daemon thread (#801).
        assert_off_loop("sqlite WAL checkpoint")
        conn = sqlite3.connect(db_path, timeout=busy_seconds, isolation_level=None)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        return int(row[0]), int(row[1]), int(row[2])

    @staticmethod
    async def _on_daemon_thread(work: Callable[[], None], name: str, timeout: float) -> bool:
        """Run blocking `work` on a dedicated daemon thread; wait for it at most `timeout`.

        Not the default executor: its threads are joined at interpreter exit,
        so work still blocked in disk I/O would hold the process up. Returns
        True when the work finished within `timeout`; otherwise it carries on
        in the background and the caller stops waiting.
        """
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def resolve() -> None:
            if not finished.done():
                finished.set_result(None)

        def run() -> None:
            try:
                work()
            finally:
                # The loop may be gone by the time slow work returns.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(resolve)

        threading.Thread(target=run, name=name, daemon=True).start()
        _done, pending = await asyncio.wait({finished}, timeout=timeout)
        return not pending

    async def close(self) -> None:
        """Close the connection. This never hangs, and it always shuts the handle.

        aiosqlite runs the real sqlite3 connection on a worker thread and
        resolves each operation on the loop that submitted it. Two things go
        wrong, and both end in a shutdown that never finishes:

        * If the submitting loop is gone when a result lands - a starlette
          TestClient's portal loop, an app torn down under an in-flight
          background write - the worker raises `Event loop is closed`, and
          raises AGAIN inside the handler meant to report that, uncaught. THE
          WORKER THREAD DIES, and every later operation, `close()` included,
          queues to a thread that will never pick it up.
        * If the worker is alive but stuck in a long or blocked statement,
          `close()` waits behind it for as long as that takes.

        Neither can be waited out with `wait_for`: cancelling aiosqlite's
        `close()` lands the CancelledError inside its own `finally: await
        future`, on a second future nobody will resolve, so the cancelled task
        never finishes and the "timeout" waits forever. That deadlock is what
        hung a fixture teardown and took `make gate` with it (#496).

        So the close is never cancelled. It is given a bounded chance to finish
        politely, and if it cannot, the sqlite3 handle is interrupted and closed
        directly, on a daemon thread - which is only possible because
        `connect()` opens it with `check_same_thread=False`. Forcing the handle
        shut also unblocks the polite close: the worker's next operation fails
        against a closed database instead of waiting forever.
        """
        conn = self._connection
        if conn is None:
            return
        self._connection = None
        raw = getattr(conn, "_connection", None)
        worker = getattr(conn, "_thread", None)

        if worker is not None and not worker.is_alive():
            # Nothing we await can ever complete: the queue has no reader.
            logger.warning(
                "Database close: aiosqlite's worker thread is gone (an operation's "
                "event loop was closed under it), so its queue will never drain. "
                "Closing the sqlite3 handle directly."
            )
            await self._force_close(conn, raw)
            return

        bridge = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(conn.close(), _closer()))
        _done, pending = await asyncio.wait({bridge}, timeout=self._CLOSE_TIMEOUT_SECONDS)
        if not pending:
            exc = bridge.exception()
            if exc is not None:
                logger.warning("Error closing database connection: %s", exc)
            return

        logger.warning(
            "Database close did not finish in %.0fs - something is still running "
            "through the connection. Interrupting it and closing the handle.",
            self._CLOSE_TIMEOUT_SECONDS,
        )
        await self._force_close(conn, raw)
        # The forced close makes the polite one fail rather than wait, so it can
        # usually finish now - and finishing is what ends aiosqlite's worker
        # thread, which is NON-DAEMON and would otherwise hold up interpreter
        # exit on its own.
        await asyncio.wait({bridge}, timeout=self._FORCE_GRACE_SECONDS)
        if not bridge.done():
            # Cancelling the BRIDGE is safe - it is only a future relay. The
            # close itself stays on the daemon loop, where being stuck costs
            # nothing.
            bridge.cancel()
            # NOW end the worker thread. It is non-daemon, so CPython joins it
            # at interpreter exit and one left behind stops the process exiting
            # at all - a container that ignores SIGTERM, and a pytest run that
            # says "1934 passed" and then hangs. The sentinel is queued only
            # HERE, after we have stopped waiting on aiosqlite's own close:
            # sending it earlier breaks the worker's loop before that close can
            # resolve, which is the same hang by another route.
            with contextlib.suppress(Exception):
                conn._tx.put_nowait((None, lambda: _STOP_RUNNING_SENTINEL))

    async def _force_close(self, conn: Any, raw: Any) -> None:
        """Interrupt and close the sqlite3 handle - on a daemon thread, never here.

        `raw.close()` is not a cheap call. Closing the last connection to a WAL
        database checkpoints the WAL into the database file, fsyncs it and
        removes the WAL: synchronous disk I/O that runs as long as the disk
        makes it. Run on the event loop, that I/O freezes everything the loop
        serves, and with it every timeout meant to bound this close - which is
        how a forced close budgeted at 6s took 22s under a busy disk.

        So the handle work goes to a dedicated daemon thread and this waits for
        it for at most `_HANDLE_CLOSE_SECONDS`. Not the default executor: its
        threads are joined at interpreter exit, so a close still in its fsync
        would hold the process up. A close that outlasts the wait carries on
        in the background; sqlite's WAL makes an exit in the middle of it safe.
        """
        conn._running = False
        if raw is None:
            return
        if not await self._on_daemon_thread(
            lambda: self._close_handle(conn, raw), "hp-db-force-close", self._HANDLE_CLOSE_SECONDS
        ):
            logger.warning(
                "Closing the sqlite3 handle is still running after %.0fs (slow disk "
                "I/O in its WAL checkpoint). It finishes in the background.",
                self._HANDLE_CLOSE_SECONDS,
            )

    @staticmethod
    def _close_handle(conn: Any, raw: Any) -> None:
        """Interrupt and close the sqlite3 handle. Blocking: never call on the loop.

        `interrupt()` is documented as safe to call from another thread and
        aborts a statement in progress, which is what lets a wedged worker's
        `close()` return instead of blocking. Both calls are best-effort: a
        handle we cannot close is still better reported than waited on.
        """
        if raw is not None:
            with contextlib.suppress(Exception):
                raw.interrupt()
            try:
                raw.close()
            except Exception as exc:
                logger.warning("Could not close the sqlite3 handle directly: %s", exc)
            else:
                conn._connection = None
        # NOT woken with a stop sentinel here: aiosqlite's own close(), still
        # running on the closer loop, ends the worker itself once the forced
        # handle-close makes its pending operation fail. Queueing our own
        # sentinel breaks the worker's loop FIRST, so that close never resolves
        # and the thread outlives the process's willingness to exit.

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database not connected")
        return self._connection

    async def execute(self, query: str, params: Any = None) -> aiosqlite.Cursor:
        return await self.conn.execute(query, params or ())

    async def fetchone(self, query: str, params: Any = None) -> dict[str, Any] | None:
        cursor = await self.conn.execute(query, params or ())
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        """Execute and read every row in ONE operation on aiosqlite's worker thread.

        Every repository shares this one connection, so any `await` between a
        statement's execute and its last row is a point where another coroutine's
        `commit()` reaches the worker. For a write that returns rows
        (`UPDATE ... RETURNING`) the statement is still IN PROGRESS until its
        last row is read, and SQLite refuses that commit: "cannot commit
        transaction - SQL statements in progress". That is how two inventory
        sweeps at once failed the e2e fleet server's startup. Executed and read
        in a single worker operation, there is no gap for anything to land in,
        so a RETURNING statement goes only through here
        (tests/test_db_commit_race.py holds the source to that).
        """
        rows = await self.conn.execute_fetchall(query, params or ())
        return [dict(row) for row in rows]
