from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from ..db.connection import Database


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid4() -> str:
    return str(uuid.uuid4())


VALID_ACTIONS = {"apply", "revoke", "replay"}


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create_task(self, artifact_id: str, action: str) -> str:
        if action not in VALID_ACTIONS:
            msg = f"Invalid action: {action!r}. Must be one of {VALID_ACTIONS}"
            raise ValueError(msg)
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
        if action not in VALID_ACTIONS:
            msg = f"Invalid action: {action!r}. Must be one of {VALID_ACTIONS}"
            raise ValueError(msg)
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

    async def get_active_task(self, artifact_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """SELECT id, status, action FROM tasks
               WHERE artifact_id = ? AND status IN ('pending', 'running')
               ORDER BY created_at DESC LIMIT 1""",
            (artifact_id,),
        )
        return dict(row) if row is not None else None

    async def list_tasks(
        self, artifact_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT * FROM tasks WHERE artifact_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (artifact_id, limit, offset),
        )
        return [dict(r) for r in rows]

    async def count_tasks(self, artifact_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM tasks WHERE artifact_id = ?",
            (artifact_id,),
        )
        return row["cnt"] if row else 0

    async def fail_orphaned_tasks(self) -> int:
        ts = _now()
        cursor = await self.db.execute(
            """UPDATE tasks SET status = 'failed', error = 'orphaned_after_restart', finished_at = ?
               WHERE status = 'running'""",
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
