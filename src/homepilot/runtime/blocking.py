"""Blocking work runs OFF the event loop, through one helper.

One asyncio loop carries the API, the SSE stream, the MCP transport, the agent
hub's heartbeats and every reconciler. A synchronous call made on it - a git
subprocess, a `time.sleep` lock spin, a blocking `flock`, a sync sqlite
statement, a DNS lookup, PBKDF2 - freezes all of them at once. That is how a
WAL checkpoint inside a sqlite close froze the whole app (#795), and the
architecture review found the same shape in every git call of the artifact
store, a router's `git diff`, startup and more.

So blocking work goes through ``run_blocking(pool, fn, *args, **kwargs)``,
which runs it on a NAMED, BOUNDED thread pool:

* ``git`` - ONE thread. Every operation that touches the artifacts repository
  (write + add + commit + push, push/pull/remote changes, and the lifecycle's
  read-modify-write transitions under the file lock) runs here. One thread is
  the serialisation: jobs run in submission order and never interleave, which
  is what the old on-loop code got for free and what git's own index lock
  demands. A slow push therefore delays the next commit - and nothing else.
* ``io`` - local disk and CPU-heavy work: artifact reads and listings, KB file
  walks, vault key derivation and writes, sqlite integrity checks, TLS
  material. Kept apart from ``git`` so a 60 s push cannot starve a read.
* ``net`` - name resolution and socket probes. A broken resolver can hang
  each lookup for many seconds; on its own pool it cannot starve disk reads.

``assert_off_loop`` is the other half: the blocking primitives themselves call
it, so a new caller that forgets ``run_blocking`` is reported - with its stack -
however FAST the call happens to be on a quiet test box. The test session turns
that report into a failure (tests/loop_block_detector.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

Pool = Literal["git", "io", "net"]

# git: 1 is the ordering guarantee, not a tuning knob.
POOL_SIZES: dict[str, int] = {"git": 1, "io": 4, "net": 4}

_P = ParamSpec("_P")
_T = TypeVar("_T")

_pools: dict[str, ThreadPoolExecutor] = {}
_pools_lock = threading.Lock()


def _executor(pool: Pool) -> ThreadPoolExecutor:
    executor = _pools.get(pool)
    if executor is not None:
        return executor
    if pool not in POOL_SIZES:
        raise ValueError(f"unknown blocking pool: {pool!r}")
    with _pools_lock:
        executor = _pools.get(pool)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=POOL_SIZES[pool], thread_name_prefix=f"hp-{pool}"
            )
            _pools[pool] = executor
    return executor


async def run_blocking(
    pool: Pool, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Run ``fn(*args, **kwargs)`` on the named pool and await its result.

    The caller's context variables travel with the call (request ids, actor
    attribution). Cancelling the awaiting task does NOT stop the thread: the
    job runs to completion, which for a git write is the safe outcome - a
    half-cancelled commit is worse than a finished one nobody waited for.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(_executor(pool), call)


async def shutdown_pools(timeout: float) -> list[str]:
    """Let the work already on the pools finish, at most ``timeout``; retire the pools.

    Called once by the shutdown, after every service that submits work has
    drained and BEFORE the WAL checkpoint: a job still running here can hold
    the repository mid-commit, or a sqlite read transaction (the integrity
    check walks every page on ``io``) that leaves ``wal_checkpoint(TRUNCATE)``
    busy. Queued jobs are not cancelled - a git job is a commit a change already
    rests on, and finishing it is the safe outcome (see ``run_blocking``).

    The wait runs on a daemon thread, never on the loop. Returns the pools still
    busy at the deadline; their threads carry on in the background. The pools
    are forgotten either way, so a later ``run_blocking`` (another app in the
    same process, as the test suite does) gets fresh ones.
    """
    with _pools_lock:
        retiring = dict(_pools)
        _pools.clear()
    if not retiring:
        return []
    loop = asyncio.get_running_loop()
    waits: dict[asyncio.Future[None], str] = {}
    for name, executor in retiring.items():
        finished: asyncio.Future[None] = loop.create_future()

        def wait_then_resolve(
            executor: ThreadPoolExecutor = executor, finished: asyncio.Future[None] = finished
        ) -> None:
            try:
                executor.shutdown(wait=True)
            finally:

                def resolve() -> None:
                    if not finished.done():
                        finished.set_result(None)

                # The loop may be gone by the time a slow job returns.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(resolve)

        threading.Thread(target=wait_then_resolve, name=f"hp-{name}-shutdown", daemon=True).start()
        waits[finished] = name
    _done, pending = await asyncio.wait(waits, timeout=timeout)
    busy = sorted(waits[f] for f in pending)
    if busy:
        logger.warning(
            "Blocking pool(s) %s still busy after %.0fs at shutdown - proceeding",
            ", ".join(busy),
            timeout,
        )
    return busy


Reporter = Callable[[str, traceback.StackSummary], None]


def _default_reporter(what: str, stack: traceback.StackSummary) -> None:
    logger.warning(
        "Blocking call on the event loop: %s. Route it through run_blocking(). Stack:\n%s",
        what,
        "".join(stack.format()),
    )


_reporter: Reporter = _default_reporter


def set_on_loop_reporter(reporter: Reporter | None) -> None:
    """Install what ``assert_off_loop`` does when it finds a running loop.

    The default logs a warning: in production a forgotten offload must stay a
    slow call, not become an outage. The test session installs a recorder that
    fails the test instead.
    """
    global _reporter
    _reporter = reporter or _default_reporter


def assert_off_loop(what: str) -> None:
    """Report ``what`` if it is about to block a running event loop.

    Cheap: one C call that raises when this thread runs no loop (the normal
    case for a pool thread and for the CLI)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    stack = traceback.extract_stack(limit=16)
    stack.pop()  # this frame
    _reporter(what, stack)
