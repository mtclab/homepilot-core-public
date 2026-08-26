"""Prune the operational tables that grow forever (#431).

There was no retention `DELETE` anywhere in the repository. `audit_log`,
`agent_audit`, `tasks` and `webhook_deliveries` gain a row per operation, per
fleet command and per event per configured webhook, and nothing ever removed one.
A year on a homelab VM is a multi-GB SQLite file and a backup too big to move -
the failure mode is slow, silent, and arrives when someone tries to restore.

What this does NOT prune is as deliberate as what it does:

* **artifacts** are the product's record of intent and are kept forever;
* **hosts**, **services** and **agents** describe the estate, not its history;
* **drift_checks** holds ONE row per artifact (an upsert), so it is bounded by
  the artifact count already;
* **metrics** has its own pruner with its own horizon (ADR-004 S5), because the
  right retention for a time series is not the right retention for an audit log.

`incremental_vacuum` runs after a prune that actually deleted something: SQLite
does not return freed pages to the filesystem on its own, so a delete-only
retention policy shrinks nothing an operator can see.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homepilot.db.repository import Repository

from .base import Reconciler, ReconcilerResult

logger = logging.getLogger(__name__)

# (table, timestamp column). One tuple per table so adding a table is one line
# and cannot forget the column, which is the shape of the bug that produced an
# unbounded table in the first place.
PRUNABLE_TABLES: tuple[tuple[str, str], ...] = (
    ("audit_log", "timestamp"),
    ("agent_audit", "ts"),
    ("webhook_deliveries", "created_at"),
)

# Tasks are pruned only when FINISHED: a pending or running task older than the
# horizon is a stuck task, and deleting it would hide the problem and strand
# whatever is waiting on it.
_FINISHED_TASK_STATES = ("succeeded", "failed", "cancelled")


class RetentionReconciler(Reconciler):
    """Delete operational history older than the retention horizon."""

    def __init__(self, repo: Repository, retention_days: int, resolver: Any = None) -> None:
        self._repo = repo
        # A horizon of zero would delete everything written this instant, which
        # is never what an operator means; one day is the floor.
        self._retention_days = max(1, int(retention_days))
        # The horizon is an operator setting (#553 C2): with a resolver it is
        # read at the start of every run, so a shortened window prunes on the
        # next cycle rather than at the next restart.
        self._resolver = resolver

    @property
    def cutoff(self) -> str:
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _resolve_days(self) -> int:
        if self._resolver is None:
            return self._retention_days
        try:
            return max(1, int(await self._resolver.value("retention_days")))
        except Exception:
            logger.warning("Could not resolve retention_days; keeping the boot value")
            return self._retention_days

    async def run(self) -> ReconcilerResult:
        self._retention_days = await self._resolve_days()
        cutoff = self.cutoff
        deleted: dict[str, int] = {}
        total = 0

        for table, column in PRUNABLE_TABLES:
            try:
                count = await self._repo.prune_before(table, column, cutoff)
            except Exception as exc:
                # One unprunable table must not stop the others: the point of
                # this reconciler is that the database stays bounded.
                logger.warning("retention: could not prune %s: %s", table, exc)
                continue
            if count:
                deleted[table] = count
                total += count

        try:
            count = await self._repo.prune_finished_tasks(cutoff, _FINISHED_TASK_STATES)
        except Exception as exc:
            logger.warning("retention: could not prune tasks: %s", exc)
        else:
            if count:
                deleted["tasks"] = count
                total += count

        if total:
            logger.info(
                "retention: deleted %d row(s) older than %d days: %s",
                total,
                self._retention_days,
                ", ".join(f"{t}={n}" for t, n in sorted(deleted.items())),
            )
            # Freed pages are not returned to the filesystem otherwise, so the
            # file an operator has to back up never actually shrinks.
            await self._repo.reclaim_free_pages()

        return ReconcilerResult(
            name="retention",
            success=True,
            details={"deleted": deleted, "total": total, "retention_days": self._retention_days},
        )
