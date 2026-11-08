"""Loop-stall watchdog: say WHAT froze the event loop, while it is frozen.

#795 was found by its symptom - every endpoint hung for 16 s - and the cause had
to be reasoned out afterwards. This watchdog makes the next one name itself:

* a heartbeat task on the loop stamps the time every ``tick`` seconds;
* a daemon thread wakes every ``tick`` seconds and compares. When the last
  stamp is older than ``tick + threshold``, the loop has been stuck in ONE
  callback for longer than the threshold, and the thread logs the loop
  thread's current Python stack (``sys._current_frames``) - the blocking call
  itself, caught in the act - once per stall;
* when the loop comes back, the heartbeat measures how late it was and records
  the stall (count, worst, last) for the self-check.

Idle cost is one short sleep-and-stamp on the loop and one timed wait on the
thread per tick (4 per second at the default 500 ms threshold).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _StallStats:
    stalls: int = 0
    worst_ms: float = 0.0
    last_ms: float = 0.0
    last_at: float = 0.0  # wall clock, for the self-check


class LoopStallWatchdog:
    def __init__(self, threshold_ms: int) -> None:
        if threshold_ms <= 0:
            raise ValueError("threshold_ms must be positive; 0 disables the watchdog upstream")
        self.threshold_ms = threshold_ms
        self._threshold = threshold_ms / 1000.0
        # Half the threshold, kept within [50 ms, 1 s]: often enough to catch a
        # stall while it is still happening, rare enough to cost nothing idle.
        self._tick = min(1.0, max(0.05, self._threshold / 2))
        self._beat = time.monotonic()
        self._loop_thread_id: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reported_beat: float | None = None
        self._stats = _StallStats()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start on the RUNNING loop (call from inside the app's lifespan)."""
        if self.running:
            return
        self._loop_thread_id = threading.get_ident()
        self._beat = time.monotonic()
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(
            self._heartbeat(), name="hp-loop-watchdog"
        )
        self._thread = threading.Thread(target=self._monitor, name="hp-loop-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._thread is not None:
            self._thread.join(timeout=2 * self._tick + 1)
            self._thread = None

    async def _heartbeat(self) -> None:
        while True:
            before = time.monotonic()
            self._beat = before
            await asyncio.sleep(self._tick)
            late = time.monotonic() - before - self._tick
            if late > self._threshold:
                self._record(late)

    def _record(self, late: float) -> None:
        ms = round(late * 1000.0, 1)
        with self._lock:
            self._stats.stalls += 1
            self._stats.worst_ms = max(self._stats.worst_ms, ms)
            self._stats.last_ms = ms
            self._stats.last_at = time.time()
        logger.warning(
            "Event loop was blocked for %.0f ms (threshold %d ms); see the stack logged "
            "while it was blocked",
            ms,
            self.threshold_ms,
        )

    def _monitor(self) -> None:
        while not self._stop.wait(self._tick):
            beat = self._beat
            stuck_for = time.monotonic() - beat - self._tick
            if stuck_for <= self._threshold or self._reported_beat == beat:
                continue
            self._reported_beat = beat
            frame = sys._current_frames().get(self._loop_thread_id or -1)
            stack = "".join(traceback.format_stack(frame)) if frame is not None else "?"
            logger.warning(
                "Event loop blocked for %.0f ms and counting; loop thread stack:\n%s",
                stuck_for * 1000.0,
                stack,
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stats = _StallStats(**vars(self._stats))
        return {
            "running": self.running,
            "threshold_ms": self.threshold_ms,
            "stalls": stats.stalls,
            "worst_ms": stats.worst_ms,
            "last_ms": stats.last_ms,
            "last_at": stats.last_at or None,
        }
