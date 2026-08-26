from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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


@dataclass
class ReconcilerResult:
    name: str
    success: bool
    details: dict[str, Any] = field(default_factory=dict)


class Reconciler(ABC):
    @abstractmethod
    async def run(self) -> ReconcilerResult: ...


@dataclass
class _RegisteredReconciler:
    reconciler: Reconciler
    interval: IntervalSource
    startup_delay: float
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
        self._registered.append(
            _RegisteredReconciler(
                reconciler=reconciler,
                interval=interval,
                startup_delay=startup_delay,
            )
        )

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
            try:
                result = await reg.reconciler.run()
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
                logger.exception(
                    "Reconciler %s crashed: %s",
                    reg.reconciler.__class__.__name__,
                    exc,
                )
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
