from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..reconciler.base import Reconciler, ReconcilerResult
from .repository import MetricsRepository

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400


class MetricsPruner(Reconciler):
    """Drops raw samples past the retention horizon (default 7 days).

    Deliberately NO rollup: ADR-004 S5 says measure a week of real data before
    deciding whether downsampling earns its complexity. Retention is therefore
    the ONLY thing bounding the table, which is why it runs on a schedule rather
    than at ingest - an instance whose agents all went quiet still has to shed
    its old data."""

    def __init__(
        self,
        metrics_repo: MetricsRepository,
        retention_days: int,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._metrics = metrics_repo
        self._retention_days = max(1, int(retention_days))
        self._now = now

    @property
    def cutoff_ts(self) -> int:
        return int(self._now() - self._retention_days * SECONDS_PER_DAY)

    async def run(self) -> ReconcilerResult:
        cutoff = self.cutoff_ts
        deleted = await self._metrics.prune(cutoff)
        if deleted:
            logger.info(
                "metrics retention: pruned %d sample(s) older than %d days",
                deleted,
                self._retention_days,
            )
        return ReconcilerResult(
            name="metrics_pruner",
            success=True,
            details={"deleted": deleted, "retention_days": self._retention_days},
        )
