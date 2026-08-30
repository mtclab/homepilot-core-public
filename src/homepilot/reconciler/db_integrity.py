"""Ask the database FILE whether it is still sound, on a schedule (#648 tranche 4).

Nothing ever asked. `/health` reported `database: ok` from `SELECT 1`, which is
answered by the expression evaluator without opening a table, so on 2026-08-29 an
OOM kill corrupted a database mid-write and the liveness probe stayed green while
`list_tasks` returned 500 and `PRAGMA integrity_check` failed. Data was gone
before anyone looked.

Reading a real row on the request path - which `/health` now does - is strictly
better but still not enough, and the reason is worth stating: the backend's
connection has a PAGE CACHE. A file damaged underneath a running process goes on
serving cached rows perfectly. Verified on a scratch copy of dev 3.6.15: with
`homepilot.db` overwritten under the running backend, `/health`, `/inventory`,
`/artifacts`, `/artifacts/drift` and `/admin/selfcheck` all returned 200 while a
fresh connection to the same file answered `database disk image is malformed`.

So the check runs where it can see the truth: its own read-only connection,
`PRAGMA quick_check`, off the request path, on a long interval. The outcome goes
into the settings table - one writer, this file - and `/health` reports it, so
the probe an orchestrator acts on can finally say `database: corrupt` instead of
`ok`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homepilot.db.backup import integrity_problems
from homepilot.db.repository import now

from .base import Reconciler, ReconcilerResult

logger = logging.getLogger(__name__)

# Settings-table keys `/health` reads. One WRITER (this file), any reader.
LAST_CHECK_AT = "db_integrity_checked_at"
LAST_CHECK_OK = "db_integrity_ok"
LAST_CHECK_PROBLEMS = "db_integrity_problems"

# quick_check stops after this many problems. One is already conclusive; a few
# more make the log line diagnosable without walking a damaged file to the end.
_PROBLEM_LIMIT = 5


class DatabaseIntegrityReconciler(Reconciler):
    def __init__(self, db_path: str | Path, repo: Any) -> None:
        self._db_path = Path(db_path)
        self._repo = repo

    async def run(self) -> ReconcilerResult:
        if not self._db_path.exists():
            return ReconcilerResult(
                name="db_integrity",
                success=True,
                details={"checked": False, "skipped": "no database file"},
            )
        # Its own connection, in a worker thread: quick_check walks every page,
        # which is a blocking read and must not sit on the event loop.
        problems = await asyncio.to_thread(integrity_problems, self._db_path, _PROBLEM_LIMIT)
        joined = "; ".join(problems)[:500]
        try:
            await self._repo.set_setting(LAST_CHECK_AT, now())
            await self._repo.set_setting(LAST_CHECK_OK, "0" if problems else "1")
            await self._repo.set_setting(LAST_CHECK_PROBLEMS, joined)
        except Exception as exc:
            # A database too damaged to record the verdict is itself the verdict,
            # and the log is the only place left to put it.
            logger.error(
                "DATABASE INTEGRITY: %s could not even record the result: %s. Problems found: %s",
                self._db_path,
                exc,
                joined or "none",
            )
            return ReconcilerResult(
                name="db_integrity",
                success=False,
                details={"checked": True, "ok": not problems, "problems": problems[:3]},
            )

        if problems:
            logger.error(
                "DATABASE CORRUPT: PRAGMA quick_check on %s reports %s. Stop the backend "
                "and restore a backup: `hp db restore <data_dir>/backups/<file>.db` "
                "(never copy one into place by hand - the stale -wal is replayed onto "
                "it). /health now reports `database: corrupt`.",
                self._db_path,
                joined,
            )
        return ReconcilerResult(
            name="db_integrity",
            success=True,
            details={"checked": True, "ok": not problems, "problems": problems[:3]},
        )
