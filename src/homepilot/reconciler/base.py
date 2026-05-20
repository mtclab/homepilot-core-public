from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

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
    interval: float
    startup_delay: float
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class ReconcilerScheduler:
    def __init__(self) -> None:
        self._registered: list[_RegisteredReconciler] = []

    def register(
        self,
        reconciler: Reconciler,
        interval: float,
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
                "Reconciler registered: %s interval=%.0fs startup_delay=%.0fs",
                reg.reconciler.__class__.__name__,
                reg.interval,
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
            await asyncio.sleep(reg.interval)
