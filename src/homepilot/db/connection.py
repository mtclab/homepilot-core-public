from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

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
        from here - which is only possible because `connect()` opens it with
        `check_same_thread=False`. Forcing the handle shut also unblocks the
        polite close: the worker's next operation fails against a closed
        database instead of waiting forever.
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
            self._force_close(conn, raw)
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
        self._force_close(conn, raw)
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

    @staticmethod
    def _force_close(conn: Any, raw: Any) -> None:
        """Interrupt and close the sqlite3 handle from this thread.

        `interrupt()` is documented as safe to call from another thread and
        aborts a statement in progress, which is what lets a wedged worker's
        `close()` return instead of blocking. Both calls are best-effort: a
        handle we cannot close is still better reported than waited on.
        """
        conn._running = False
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
        cursor = await self.conn.execute(query, params or ())
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
