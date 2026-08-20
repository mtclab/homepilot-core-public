from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from ..db.connection import Database


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid4() -> str:
    return str(uuid.uuid4())


VALID_ACTIONS = {"apply", "revoke", "replay", "provision", "install_agent"}

# Actions that create infrastructure rather than apply an artifact. They carry a
# NULL artifact_id BY CONTRACT (both ways: artifactless actions must not name an
# artifact, artifact actions must name one) so they can never be picked up by the
# artifact-scoped dedup/active-task queries — a provision task must not block, or
# be mistaken for, an apply/revoke on some artifact.
ARTIFACTLESS_ACTIONS = {"provision", "install_agent"}


def _validate_action(action: str, artifact_id: str | None) -> None:
    if action not in VALID_ACTIONS:
        msg = f"Invalid action: {action!r}. Must be one of {VALID_ACTIONS}"
        raise ValueError(msg)
    if action in ARTIFACTLESS_ACTIONS and artifact_id is not None:
        msg = f"Action {action!r} must not carry an artifact_id (got {artifact_id!r})"
        raise ValueError(msg)
    if action not in ARTIFACTLESS_ACTIONS and artifact_id is None:
        msg = f"Action {action!r} requires an artifact_id"
        raise ValueError(msg)


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create_task(self, artifact_id: str | None, action: str) -> str:
        _validate_action(action, artifact_id)
        task_id = _uuid4()
        ts = _now()
        await self.db.execute(
            """INSERT INTO tasks (id, artifact_id, action, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (task_id, artifact_id, action, ts),
        )
        await self.db.conn.commit()
        return task_id

    async def create_task_if_no_active(self, artifact_id: str, action: str) -> str | None:
        # Artifact-scoped dedup only: the NOT EXISTS guard is meaningless for an
        # artifactless action, so _validate_action rejects those here too.
        _validate_action(action, artifact_id)
        task_id = _uuid4()
        ts = _now()
        cursor = await self.db.execute(
            """INSERT INTO tasks (id, artifact_id, action, status, created_at)
               SELECT ?, ?, ?, 'pending', ?
               WHERE NOT EXISTS (
                   SELECT 1 FROM tasks WHERE artifact_id = ? AND status IN ('pending', 'running')
               )""",
            (task_id, artifact_id, action, ts, artifact_id),
        )
        if cursor.rowcount == 0:
            return None
        await self.db.conn.commit()
        return task_id

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return dict(row) if row is not None else None

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        error: str | None = None,
        result_json: str | None = None,
    ) -> None:
        ts = _now() if status in ("succeeded", "failed") else None
        await self.db.execute(
            """UPDATE tasks SET status = ?, finished_at = ?, error = ?, result_json = ?
               WHERE id = ?""",
            (status, ts, error, result_json, task_id),
        )
        await self.db.conn.commit()

    async def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        # Cancel is only valid from a live state (pending/running → cancelled).
        # A task in any terminal state (succeeded/failed/cancelled) is a no-op:
        # we return its unchanged record rather than error. The status guard in
        # the UPDATE's WHERE clause makes the transition atomic — a task that
        # finishes between our read and our write is NOT clobbered to cancelled.
        task = await self.get_task(task_id)
        if task is None:
            return None
        if task["status"] not in ("pending", "running"):
            return task
        ts = _now()
        await self.db.execute(
            """UPDATE tasks SET status = 'cancelled', finished_at = ?
               WHERE id = ? AND status IN ('pending', 'running')""",
            (ts, task_id),
        )
        await self.db.conn.commit()
        return await self.get_task(task_id)

    async def get_active_task(self, artifact_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """SELECT id, status, action FROM tasks
               WHERE artifact_id = ? AND status IN ('pending', 'running')
               ORDER BY created_at DESC LIMIT 1""",
            (artifact_id,),
        )
        return dict(row) if row is not None else None

    async def list_tasks(
        self, artifact_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        # artifact_id=None lists tasks across the whole system (the operator's
        # "what's running" view); a value scopes to one artifact.
        if artifact_id is None:
            rows = await self.db.fetchall(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        else:
            rows = await self.db.fetchall(
                """SELECT * FROM tasks WHERE artifact_id = ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (artifact_id, limit, offset),
            )
        return [dict(r) for r in rows]

    async def count_tasks(self, artifact_id: str | None = None) -> int:
        if artifact_id is None:
            row = await self.db.fetchone("SELECT COUNT(*) as cnt FROM tasks", ())
        else:
            row = await self.db.fetchone(
                "SELECT COUNT(*) as cnt FROM tasks WHERE artifact_id = ?",
                (artifact_id,),
            )
        return row["cnt"] if row else 0

    async def fail_orphaned_tasks(self) -> int:
        # Sweep BOTH 'running' and 'pending' (#386). A restart between task
        # creation (status 'pending') and the first status write ('running')
        # would otherwise strand the pending task forever, and every future
        # apply would keep returning 202 pointing at that dead task.
        ts = _now()
        cursor = await self.db.execute(
            """UPDATE tasks SET status = 'failed', error = 'orphaned_after_restart', finished_at = ?
               WHERE status IN ('running', 'pending')""",
            (ts,),
        )
        await self.db.conn.commit()
        return cursor.rowcount

    async def has_active_task(self, artifact_id: str) -> bool:
        row = await self.db.fetchone(
            """SELECT EXISTS(
               SELECT 1 FROM tasks WHERE artifact_id = ? AND status IN ('pending', 'running')
               ) as exists_flag""",
            (artifact_id,),
        )
        return bool(row["exists_flag"]) if row else False
