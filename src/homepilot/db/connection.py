from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

logger = logging.getLogger(__name__)

_CONNECT_RETRIES = int(os.environ.get("HP_DB_CONNECT_RETRIES", "3"))
_CONNECT_RETRY_DELAY = float(os.environ.get("HP_DB_CONNECT_RETRY_DELAY", "1.0"))


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        last_exc: Exception | None = None
        for attempt in range(1, _CONNECT_RETRIES + 1):
            try:
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
                if attempt > 1:
                    logger.info("Database connected on attempt %d", attempt)
                return self._connection
            except (aiosqlite.OperationalError, aiosqlite.DatabaseError) as exc:
                last_exc = exc
                if self._connection is not None:
                    with contextlib.suppress(Exception):
                        await self._connection.close()
                    self._connection = None
                if attempt < _CONNECT_RETRIES:
                    logger.warning(
                        "Database connect attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt,
                        _CONNECT_RETRIES,
                        exc,
                        _CONNECT_RETRY_DELAY,
                    )
                    await asyncio.sleep(_CONNECT_RETRY_DELAY)
                else:
                    logger.error(
                        "Database connect failed after %d attempts: %s",
                        _CONNECT_RETRIES,
                        exc,
                    )
        raise last_exc  # type: ignore[misc]

    async def close(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception as exc:
                logger.warning("Error closing database connection: %s", exc)
            finally:
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
