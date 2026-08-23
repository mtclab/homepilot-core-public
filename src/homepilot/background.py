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
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT = 5.0


async def drain_tasks(
    tasks: Iterable[asyncio.Task[Any]],
    what: str,
    timeout: float = DEFAULT_DRAIN_TIMEOUT,
) -> int:
    """Let outstanding tasks finish, then cancel what is left. Returns the
    number that had to be cancelled.

    Bounded on purpose: work that cannot finish must not stop a shutdown, and
    the caller is already tearing down. `what` names the work in the log, so an
    operator reading a slow shutdown can tell which subsystem held it.
    """
    pending = [t for t in tasks if not t.done()]
    if not pending:
        return 0
    _done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        # Give the cancellations a turn to unwind, so the caller's next step -
        # usually closing the database - is not racing a half-cancelled write.
        await asyncio.wait(still_pending, timeout=1.0)
        logger.warning(
            "%d %s did not finish within %.0fs of shutdown and were cancelled",
            len(still_pending),
            what,
            timeout,
        )
    return len(still_pending)
