from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# An interval is either a fixed number of seconds or something that ANSWERS the
# question each cycle. The callable form is what makes a persisted interval
# (#553 C2) real: a loop that read its period once at registration would keep the
# boot value forever and the UI's "saved" would be a lie.
IntervalSource = float | Callable[[], float | Awaitable[float]]

# How often a callable interval is re-asked while a loop is waiting. It bounds
# how long a shortened interval keeps waiting on the old one, and it is the only
# extra work the polling costs: one settings read per slice, per loop that opted
# into a callable.
INTERVAL_POLL_SECONDS = 5.0

logger = logging.getLogger(__name__)


def _stamp() -> str:
    """UTC, in the same shape every other timestamp in the product uses."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ReconcilerResult:
    name: str
    success: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcilerStatus:
    """What one scheduled loop has actually been doing (#648 tranche 8).

    Eight loops run the estate - the inventory sync, drift, retention, the
    database integrity check, alert evaluation, metrics pruning, the archive
    push, auto-apply - and until this existed, NOTHING anywhere could say
    whether any of them was still running. `_run_loop` catches every exception
    and writes a log line, so a loop that crashes on every cycle looks exactly
    like a healthy one from every surface the product offers; a dead drift loop
    looks BETTER than a live one, because with no new checks the fleet stays
    green. Two subsystems (`archive_push`, `db_integrity`) already dodged this
    by writing their outcome into the settings table for the self-check to read
    - which is the pattern, generalised here so it holds for all of them.

    In memory on purpose: the question is "is the automation alive in THIS
    process", and a process that has just started has honestly run nothing yet.
    """

    cls: str
    name: str
    interval_seconds: float | None
    startup_delay: float
    registered_at: str
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_ok: bool | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    runs: int = 0


class Reconciler(ABC):
    @abstractmethod
    async def run(self) -> ReconcilerResult: ...


@dataclass
class _RegisteredReconciler:
    reconciler: Reconciler
    interval: IntervalSource
    startup_delay: float
    status: ReconcilerStatus
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class ReconcilerScheduler:
    def __init__(self) -> None:
        self._registered: list[_RegisteredReconciler] = []

    def register(
        self,
        reconciler: Reconciler,
        interval: IntervalSource,
        startup_delay: float = 0.0,
    ) -> None:
        cls = reconciler.__class__.__name__
        self._registered.append(
            _RegisteredReconciler(
                reconciler=reconciler,
                interval=interval,
                startup_delay=startup_delay,
                status=ReconcilerStatus(
                    cls=cls,
                    # Until a cycle answers, the class name is the only name we
                    # have; the first result replaces it with the loop's own.
                    name=cls,
                    interval_seconds=None if callable(interval) else float(interval),
                    startup_delay=startup_delay,
                    registered_at=_stamp(),
                ),
            )
        )

    def status(self) -> list[ReconcilerStatus]:
        """What every registered loop has been doing, for the self-check."""
        return [reg.status for reg in self._registered]

    async def start(self) -> None:
        for reg in self._registered:
            reg.task = asyncio.create_task(
                self._run_loop(reg), name=f"reconciler-{reg.reconciler.__class__.__name__}"
            )
            logger.info(
                "Reconciler registered: %s interval=%s startup_delay=%.0fs",
                reg.reconciler.__class__.__name__,
                "resolved per cycle" if callable(reg.interval) else f"{reg.interval:.0f}s",
                reg.startup_delay,
            )

    async def stop(self) -> None:
        for reg in self._registered:
            if reg.task and not reg.task.done():
                reg.task.cancel()
        for reg in self._registered:
            if reg.task and not reg.task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await reg.task
        logger.info("All reconcilers stopped")

    async def _run_loop(self, reg: _RegisteredReconciler) -> None:
        if reg.startup_delay > 0:
            await asyncio.sleep(reg.startup_delay)
        while True:
            reg.status.last_started_at = _stamp()
            try:
                result = await reg.reconciler.run()
                reg.status.last_finished_at = _stamp()
                reg.status.runs += 1
                reg.status.name = result.name or reg.status.name
                reg.status.last_ok = result.success
                if result.success:
                    reg.status.consecutive_failures = 0
                    reg.status.last_error = ""
                else:
                    reg.status.consecutive_failures += 1
                    reg.status.last_error = str(result.details)[:500]
                if not result.success:
                    logger.warning(
                        "Reconciler %s failed: %s",
                        result.name,
                        result.details,
                    )
                else:
                    logger.info(
                        "Reconciler %s succeeded: %s",
                        result.name,
                        result.details,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A crash is a cycle that produced no result at all, so the
                # status has to be written HERE too - otherwise a loop that
                # throws on every pass keeps whatever `last_ok` its final
                # successful cycle left, which is the exact reading this record
                # exists to prevent.
                reg.status.last_finished_at = _stamp()
                reg.status.runs += 1
                reg.status.last_ok = False
                reg.status.consecutive_failures += 1
                reg.status.last_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.exception(
                    "Reconciler %s crashed: %s",
                    reg.reconciler.__class__.__name__,
                    exc,
                )
            if callable(reg.interval):
                with contextlib.suppress(Exception):
                    reg.status.interval_seconds = await _interval_seconds(reg.interval)
            await _wait_for_next_cycle(reg.interval)


async def _interval_seconds(interval: IntervalSource) -> float:
    """Seconds to wait before the next cycle, asked fresh every time."""
    if not callable(interval):
        return float(interval)
    value = interval()
    if inspect.isawaitable(value):
        value = await value
    return float(value)


async def _wait_for_next_cycle(interval: IntervalSource) -> None:
    """Wait out the interval, re-asking a callable one as the wait goes on.

    A single `sleep(interval)` would make a SHORTENED interval take effect only
    after the OLD one elapsed - an operator who cuts the archive push from an
    hour to a minute would wait the hour first, and the UI's "saved" would be a
    lie for that hour. So a callable interval is waited in slices and compared
    against the freshly resolved target each slice. A fixed interval keeps the
    plain sleep: every reconciler that does not take a callable behaves exactly
    as before.
    """
    if not callable(interval):
        await asyncio.sleep(float(interval))
        return
    waited = 0.0
    while True:
        target = await _interval_seconds(interval)
        remaining = target - waited
        if remaining <= 0:
            return
        slice_seconds = min(INTERVAL_POLL_SECONDS, remaining)
        await asyncio.sleep(slice_seconds)
        waited += slice_seconds
