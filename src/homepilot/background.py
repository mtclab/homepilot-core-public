"""Draining background work before the things it writes to go away (#496).

Several services do work in fire-and-forget `asyncio.Task`s: the agent registry
persists connection state, the provision and enrolment services run a whole job
behind an accepted HTTP request, the task runner applies an artifact. All of
them write to the database, and none of them is awaited by whoever started it.

A shutdown that closes the database under one of those tasks is not merely
untidy. aiosqlite resolves each operation's future on the loop that submitted
it; if that loop is gone when the result lands, its worker thread dies, and
every later operation - including the close itself - queues onto a thread that
will never pick it up. That is the deadlock behind #496.

So there is exactly one way to end background work here, and this is it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT = 5.0


# How long a cancelled task gets to unwind, per round, before the next round
# cancels whatever it is still waiting on.
CANCEL_GRACE = 1.0
# Rounds of cancel-then-grace. One for the work itself, one for the unwind that
# cancelling it started (a cancelled provision spawns the cleanup that takes its
# guest back), and one spare. Bounded so a task that spawns a new task every
# time it is cancelled still cannot hold the shutdown.
CANCEL_ROUNDS = 3


def _pending(tasks: Collection[asyncio.Task[Any]]) -> list[asyncio.Task[Any]]:
    return [t for t in tasks if not t.done()]


async def drain_tasks(
    tasks: Collection[asyncio.Task[Any]],
    what: str,
    timeout: float = DEFAULT_DRAIN_TIMEOUT,
) -> int:
    """Let outstanding tasks finish, then cancel what is left. Returns the
    number that had to be cancelled.

    `tasks` is the service's LIVE tracking collection and is re-read on every
    step, never snapshotted: work can start more work while it winds down. A
    provision cancelled here spawns - and tracks - the cleanup that destroys the
    half-made guest and records the outcome. A drain that only waited for the
    tasks it saw on entry returned with that cleanup still writing, and the
    caller's next step closed the database under it: the #496 class again,
    reached through the door the drain itself opened.

    Bounded on purpose: work that cannot finish must not stop a shutdown, and
    the caller is already tearing down. `what` names the work in the log, so an
    operator reading a slow shutdown can tell which subsystem held it.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while pending := _pending(tasks):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.wait(pending, timeout=remaining)

    cancelled: set[asyncio.Task[Any]] = set()
    for _round in range(CANCEL_ROUNDS):
        pending = _pending(tasks)
        if not pending:
            break
        for task in pending:
            task.cancel()
        cancelled.update(pending)
        # Give the cancellations a turn to unwind, so the caller's next step -
        # usually closing the database - is not racing a half-cancelled write.
        # An unwind they start is picked up by the next round's re-read.
        await asyncio.wait(pending, timeout=CANCEL_GRACE)
    if cancelled:
        logger.warning(
            "%d %s did not finish within %.1fs of shutdown and were cancelled",
            len(cancelled),
            what,
            timeout,
        )
    left = _pending(tasks)
    if left:
        logger.error(
            "%d %s still running after %d cancel rounds - the database may close under them",
            len(left),
            what,
            CANCEL_ROUNDS,
        )
    return len(cancelled)
