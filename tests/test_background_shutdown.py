"""Background work must not outlive the database it writes to (#496).

THE BUG THESE FORBID, in the order it actually happens:

1. A database operation is in flight when the event loop that submitted it goes
   away - a starlette TestClient's portal loop closing, an app torn down under a
   background job that nothing awaits.
2. aiosqlite's worker thread resolves that operation on the loop that submitted
   it. The loop is closed, so `call_soon_threadsafe` raises - and raises again
   inside the handler that tries to report the failure, which is uncaught. THE
   WORKER THREAD DIES.
3. Every later operation queues onto a thread that will never pick it up. That
   includes `close()`, and `close()` cannot be waited out either: cancelling it
   lands the CancelledError inside aiosqlite's own `finally: await future`, on a
   second future nobody will resolve, so the cancelled task never finishes and
   `wait_for` waits forever.

The suite hung there, in an async fixture teardown, about one run in four, and
took `make gate` with it. The fix is on both sides: nothing may be left running
when the loop goes (drain), and a close must survive it having happened anyway.

TEETH (verified by reverting):

* Remove the dead-worker branch in `Database.close` and
  `test_a_close_survives_a_dead_worker_thread` hangs until its own `wait_for`
  fires - the exact failure it exists to forbid.
* Remove `drain_tasks`' cancellation and `test_a_stuck_job_is_cancelled_not_
  waited_on_forever` never returns.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from homepilot.background import drain_tasks
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.asyncio

# The timeout main.lifespan allows `agent_hub.stop()`. Kept here so the budget
# gate below fails loudly if either side is changed without the other.
_LIFESPAN_HUB_STOP_BUDGET = 20.0


def _assert_handle_is_closed(raw: sqlite3.Connection) -> None:
    """Prove the sqlite3 handle itself was closed, not merely dropped.

    The write-lock probe cannot see this on its own: a connection sitting idle
    holds no write lock, so abandoning the close looks identical to doing it.
    Asking the handle to work is the difference.
    """
    with pytest.raises(sqlite3.ProgrammingError) as caught:
        raw.execute("SELECT 1")
    # The message matters. Without check_same_thread=False the handle cannot be
    # closed from this thread at all - and asking it to work then raises
    # ProgrammingError too, about THREADS, so a bare `raises` passes while the
    # connection is still wide open. (It did. That is how this gate first
    # green-lit a close that closed nothing.)
    assert "closed" in str(caught.value).lower(), (
        f"the handle is not closed, it is unreachable: {caught.value}"
    )


def _assert_writable_by_someone_else(db_path: str) -> None:
    """Prove the sqlite3 handle is really shut, by taking the write lock.

    A connection that is still open holds it, and this raises "database is
    locked". Reverting to a close that only drops its own reference - or to one
    whose `raw.close()` silently fails because the handle was opened with
    check_same_thread=True - fails here, which is how a close that closed
    nothing passed for a fix once already.
    """
    other = sqlite3.connect(db_path, timeout=3)
    try:
        other.execute("CREATE TABLE IF NOT EXISTS _lock_probe (x)")
        other.execute("INSERT INTO _lock_probe VALUES (1)")
        other.commit()
    finally:
        other.close()


def _kill_worker_by_closing_its_loop(conn) -> None:
    """Do to the connection exactly what a TestClient teardown does: run one
    operation on a second event loop, and close that loop before the result
    lands. Runs in its own thread, like the portal loop it stands in for."""

    def other_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # `_execute` runs any callable on the worker thread; a sleep is the
        # cheapest way to guarantee the result lands after the loop is gone.
        doomed = loop.create_task(conn._execute(functools.partial(time.sleep, 1.0)))
        assert doomed is not None  # the reference RUF006 wants; the task is meant to die
        loop.run_until_complete(asyncio.sleep(0.05))
        loop.close()

    thread = threading.Thread(target=other_loop)
    thread.start()
    thread.join()
    # Let the worker thread try to resolve into the closed loop and die.
    deadline = time.monotonic() + 10
    while conn._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)


class TestACloseAlwaysReturns:
    async def test_a_close_survives_a_dead_worker_thread(self, tmp_path: Path):
        """The headline: the teardown finishes. Before the fix this waited
        forever - through `Database.close`'s own 10s timeout, which could not
        help because cancelling aiosqlite's close deadlocks inside it."""
        db = Database(str(tmp_path / "dead-worker.db"))
        await db.connect()
        await run_migrations(db)

        raw = db._connection._connection
        _kill_worker_by_closing_its_loop(db._connection)
        assert not db._connection._thread.is_alive(), (
            "the worker thread survived - this test is no longer reproducing the bug "
            "it exists to forbid, so its green means nothing"
        )

        await asyncio.wait_for(db.close(), timeout=Database._CLOSE_TIMEOUT_SECONDS + 10)

        # NOT `db._connection is None`: close() sets that before it does any
        # work, so it is true even when nothing was closed at all. The question
        # that matters is whether the sqlite3 handle actually went away, and the
        # database can answer it - a handle still open holds the write lock.
        _assert_writable_by_someone_else(db.db_path)
        _assert_handle_is_closed(raw)

    async def test_a_close_outlasts_a_worker_that_is_alive_but_stuck(self, tmp_path: Path):
        """The other half of the deadlock, and the one the first fix missed: the
        worker thread is ALIVE, just wedged in a statement that will not end (a
        query blocked behind busy_timeout, a long ANALYZE, a lock held by another
        process). Waiting it out is not an option and cancelling deadlocks, so
        the handle is interrupted and closed from here."""
        db = Database(str(tmp_path / "wedged.db"))
        await db.connect()
        await run_migrations(db)

        raw = db._connection._connection
        released = threading.Event()
        # Queued straight onto the worker: it blocks there, alive, holding the
        # connection, exactly like a statement that will not finish.
        db._connection._tx.put_nowait((None, lambda: released.wait(120)))
        await asyncio.sleep(0.2)
        assert db._connection._thread.is_alive(), "the worker died - wrong scenario"

        started = time.monotonic()
        try:
            await asyncio.wait_for(db.close(), timeout=60)
        finally:
            released.set()
        elapsed = time.monotonic() - started

        budget = Database._CLOSE_TIMEOUT_SECONDS + Database._FORCE_GRACE_SECONDS + 5
        assert elapsed < budget, (
            f"close took {elapsed:.1f}s: it waited for the wedged worker instead of interrupting it"
        )
        # Returning quickly is not enough - abandoning the close does that too,
        # and leaves the handle open for the life of the process.
        _assert_handle_is_closed(raw)

    async def test_an_ordinary_close_still_closes(self, tmp_path: Path):
        """The other half: the rescue path must not have replaced closing."""
        db = Database(str(tmp_path / "ordinary.db"))
        await db.connect()
        await run_migrations(db)
        await db.execute("SELECT 1")

        await asyncio.wait_for(db.close(), timeout=15)

        _assert_writable_by_someone_else(db.db_path)
        with pytest.raises(RuntimeError):
            _ = db.conn


class TestTheProcessCanStillExit:
    """A close that abandons work must not abandon it ON THE CALLER'S LOOP.

    `asyncio.Runner` cancels leftover tasks at loop close and then awaits them,
    and an aiosqlite close cancelled inside its own `finally: await future`
    never finishes - so an abandoned close does not remove the hang, it moves it
    to interpreter exit. That is not a hypothetical: it made pytest report
    "1933 passed" and then sit there until it was killed, which is what made
    `make gate` look like a frozen background job for hours.

    This has to be a subprocess, because the thing under test is whether the
    process can exit at all.
    """

    def test_a_forced_close_does_not_stop_the_process_exiting(self, tmp_path: Path):
        script = textwrap.dedent(
            f"""
            import asyncio, threading
            from homepilot.db.connection import Database

            async def main():
                db = Database({str(tmp_path / "exit.db")!r})
                await db.connect()
                # Wedge the worker in a REAL long statement - the case
                # interrupt() exists for, and the one an operator actually hits
                # (a query behind busy_timeout, a long ANALYZE). Nothing here
                # releases it: the close has to end this by itself.
                raw = db._connection._connection
                db._connection._tx.put_nowait(
                    (
                        None,
                        lambda: raw.execute(
                            "WITH RECURSIVE c(x) AS ("
                            "  SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 2000000000"
                            ") SELECT count(*) FROM c"
                        ),
                    )
                )
                await asyncio.sleep(0.3)
                await db.close()

            asyncio.run(main())
            leftover = [
                t for t in threading.enumerate()
                if not t.daemon and t is not threading.main_thread()
            ]
            print("LEFTOVER", [t.name for t in leftover])
            print("EXITED-CLEANLY")
            """
        )
        budget = Database._CLOSE_TIMEOUT_SECONDS + Database._FORCE_GRACE_SECONDS + 20
        try:
            done = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=budget,
                cwd=str(REPO_ROOT),
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"the process could not exit within {budget:.0f}s after a forced close - "
                "something abandoned on the caller's loop, or a non-daemon thread, is "
                "holding interpreter exit"
            )
        assert "EXITED-CLEANLY" in done.stdout, done.stderr[-2000:]
        assert "LEFTOVER []" in done.stdout, (
            "a non-daemon worker thread outlived the close: "
            f"{[ln for ln in done.stdout.splitlines() if ln.startswith('LEFTOVER')]}"
        )


class TestDrainingBackgroundWork:
    async def test_an_in_flight_job_is_waited_for(self):
        finished: list[str] = []

        async def job() -> None:
            await asyncio.sleep(0.05)
            finished.append("done")

        task = asyncio.create_task(job())
        cancelled = await drain_tasks([task], "test job(s)", timeout=5.0)

        assert cancelled == 0
        assert finished == ["done"], "the drain returned before the job it was draining"

    async def test_a_stuck_job_is_cancelled_not_waited_on_forever(self):
        """A job that cannot finish must not hold the shutdown. It is the
        database close that comes next, and that is where a straggler kills the
        worker thread."""

        async def stuck() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(stuck())
        cancelled = await asyncio.wait_for(
            drain_tasks([task], "test job(s)", timeout=0.2), timeout=10
        )

        assert cancelled == 1
        assert task.cancelled()

    async def test_every_service_that_runs_jobs_is_drained_by_the_real_shutdown(self):
        """Not "does the class have a drain method" - that stays green when the
        shutdown stops calling it. This asserts the SHUTDOWN drains what is on
        app.state: rename `provision_service` and main.lifespan's `getattr`
        quietly skips it, and this fails.
        """
        import inspect

        from homepilot import main as main_mod

        source = inspect.getsource(main_mod.lifespan)
        shutdown = source[source.index("finally:") :]

        for attr in (
            "task_runner",
            "provision_service",
            "agent_enroll_service",
            "agent_registry",
        ):
            assert f'"{attr}"' in shutdown, (
                f"main.lifespan's shutdown never looks up app.state.{attr}, so the "
                "background jobs it starts are never drained"
            )
            # And the name must still be the one startup sets. A rename on one
            # side only is the whole failure mode: the getattr in the shutdown
            # returns None and the drain is skipped in silence.
            assert f"app.state.{attr} =" in source or f"app.state.{attr}=" in source, (
                f"nothing in main.lifespan assigns app.state.{attr}, so the shutdown's "
                "lookup of it can only ever return None"
            )
        # ... and the lookups must actually be drained, before the database closes.
        assert shutdown.index("drain") < shutdown.index("database.close"), (
            "the database is closed before the background jobs are drained - which "
            "is the #496 precondition, not a fix for it"
        )


class TestTheHubShutsDownCleanly:
    """`stop()` grew an await between closing the listener and waiting on it,
    which opened two holes the old code did not have."""

    async def test_two_concurrent_stops_do_not_raise(self):
        """serve_forever's signal handler can race the lifespan shutdown. The
        loser used to find `self._server` already None and raise AttributeError
        - out of a lifespan `finally`, which skips the reconciler stop, every
        drain, the database close and the MCP teardown."""
        from homepilot.agent_hub.registry import AgentRegistry
        from homepilot.agent_hub.server import AgentHubServer

        srv = AgentHubServer(host="127.0.0.1", port=0, auth_token="t", registry=AgentRegistry())
        await srv.start()
        port = srv._server.sockets[0].getsockname()[1]
        # A live connection is what makes stop() actually AWAIT between closing
        # the listener and waiting on it. Without one it never yields, the two
        # calls cannot interleave, and this gate passes on the broken code too
        # (it did, the first time it was written).
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)

        results = await asyncio.gather(srv.stop(), srv.stop(), return_exceptions=True)
        writer.close()

        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a concurrent stop() raised: {raised!r}"

    async def test_stop_fits_inside_the_budget_its_caller_allows_it(self):
        """main.lifespan guards `agent_hub.stop()` with a timeout. If stop() can
        outlast that guard, the guard cancels it midway and the
        `registry.drain()` at the end of stop() never runs - so the writes it
        exists to drain are still in flight when the database closes."""
        from homepilot.agent_hub.server import AgentHubServer

        worst_case = (
            AgentHubServer._STOP_TIMEOUT_SECONDS  # hang-up wait + cancellation unwind
            + AgentHubServer._STOP_TIMEOUT_SECONDS  # wait_closed
        )
        assert worst_case < _LIFESPAN_HUB_STOP_BUDGET, (
            f"stop() can take {worst_case}s but its caller cancels it at "
            f"{_LIFESPAN_HUB_STOP_BUDGET}s"
        )

    async def test_a_rejected_connection_is_not_tracked_forever(self):
        """The pre-auth guards close the socket and return. If the peer has
        already reset, `wait_closed()` raises on the way out - and an entry left
        behind then leaks for the life of the process, under exactly the flood
        those guards exist for."""
        from unittest.mock import patch

        from homepilot.agent_hub.registry import AgentRegistry
        from homepilot.agent_hub.server import AgentHubServer

        srv = AgentHubServer(host="127.0.0.1", port=0, auth_token="t", registry=AgentRegistry())
        # Ban the loopback peer so every connection takes the reject path.
        srv._auth_bans["127.0.0.1"] = asyncio.get_running_loop().time() + 300
        await srv.start()
        port = srv._server.sockets[0].getsockname()[1]

        async def boom(self=None):
            raise ConnectionResetError("peer went away")

        try:
            with patch("asyncio.StreamWriter.wait_closed", boom):
                for _ in range(3):
                    _r, w = await asyncio.open_connection("127.0.0.1", port)
                    w.close()
                await asyncio.sleep(0.3)

            assert not srv._connections, (
                f"{len(srv._connections)} rejected connection(s) left tracked forever"
            )
        finally:
            await srv.stop()
