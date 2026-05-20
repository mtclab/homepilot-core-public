from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        self._connection = await aiosqlite.connect(self.db_path)
        await self._connection.enable_load_extension(True)
        await self._connection.load_extension(sqlite_vec.loadable_path())
        await self._connection.enable_load_extension(False)
        await self._connection.execute("PRAGMA journal_mode=WAL")
        busy_timeout = int(os.environ.get("HP_BUSY_TIMEOUT", "10000"))
        await self._connection.execute(f"PRAGMA busy_timeout={busy_timeout}")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.execute("PRAGMA cache_size=-64000")
        self._connection.row_factory = aiosqlite.Row
        return self._connection

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            self._connection = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database not connected")
        return self._connection

    async def execute(self, query: str, params: Any = None) -> aiosqlite.Cursor:
        return await self.conn.execute(query, params or ())

    async def fetchone(self, query: str, params: Any = None) -> dict[str, Any] | None:
        cursor = await self.conn.execute(query, params or ())
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(query, params or ())
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
