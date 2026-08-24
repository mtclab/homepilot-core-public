"""Push the artifact store to its off-box remote, on a schedule (#442 follow-up).

`artifacts_remote` existed as a setting and the self-check even promised "the
next sync" - but nothing ever synced. An operator who configured the remote
had an off-box copy of NOTHING, discovered exactly when the volume was lost,
which is the worst possible time. This reconciler is the sync: every interval
(and shortly after boot) the artifacts git repository is pushed to the remote,
and the outcome - success or the actual error - is recorded where the
self-check and the Settings page read it. "Configured" and "working" stop
being the same claim.

The push itself is the store's own (`ArtifactStore.push`): same lock, same
ssh-key handling as the CLI's `hp artifacts push`. Subprocess git is
synchronous, so it runs in a worker thread rather than blocking the loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homepilot.db.repository import now

from .base import Reconciler, ReconcilerResult

logger = logging.getLogger(__name__)

# Settings-table keys the self-check reads. One WRITER (this file), any reader.
LAST_PUSH_AT = "archive_last_push_at"
LAST_PUSH_OK = "archive_last_push_ok"
LAST_PUSH_ERROR = "archive_last_push_error"


class ArchivePushReconciler(Reconciler):
    def __init__(self, store: Any, repo: Any) -> None:
        self._store = store
        self._repo = repo

    async def run(self) -> ReconcilerResult:
        try:
            output = await asyncio.to_thread(self._store.push, "origin")
            await self._repo.set_setting(LAST_PUSH_AT, now())
            await self._repo.set_setting(LAST_PUSH_OK, "1")
            await self._repo.set_setting(LAST_PUSH_ERROR, "")
            lines = (output or "").strip().splitlines()
            return ReconcilerResult(
                name="archive_push",
                success=True,
                details={"pushed": True, "git": lines[-1] if lines else "up to date"},
            )
        except Exception as exc:
            # The error is the point: it is what turns the Settings panel's
            # "artifacts remote" from a comforting green into an honest fault.
            await self._repo.set_setting(LAST_PUSH_AT, now())
            await self._repo.set_setting(LAST_PUSH_OK, "0")
            await self._repo.set_setting(LAST_PUSH_ERROR, str(exc)[:500])
            return ReconcilerResult(
                name="archive_push",
                success=False,
                details={"pushed": False, "error": str(exc)[:500]},
            )
