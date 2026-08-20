"""Consistent snapshots of a live SQLite database, and the checks around them.

The database runs in WAL mode (`PRAGMA journal_mode=WAL` in connection.py), so
committed rows live partly in `homepilot.db-wal` until a checkpoint folds them
back. Copying `homepilot.db` alone - what `hp export` used to do - therefore
produces a stale or torn snapshot (#421).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec


class DatabaseLockedError(RuntimeError):
    """Another process (normally the running backend) has the database open."""


class SnapshotError(RuntimeError):
    """A consistent copy of the database could not be produced."""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    # The schema contains a vec0 virtual table; VACUUM re-creates the schema in
    # the destination, so the module has to be resolvable on this connection.
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enable_load_extension(False)
    return conn


def snapshot_database(db_path: Path, dest_path: Path) -> None:
    """Write a consistent, WAL-free copy of `db_path` to `dest_path`.

    VACUUM INTO rather than the backup API: both read a consistent snapshot, but
    VACUUM INTO emits a single compacted file in rollback-journal mode, so the
    result carries no `-wal`/`-shm` sidecar that an archive would have to keep in
    step. Fails if `dest_path` already exists - SQLite refuses to overwrite.
    """
    if dest_path.exists():
        raise SnapshotError(f"Snapshot destination already exists: {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        raise SnapshotError(f"Could not open {db_path} for snapshot: {exc}") from exc
    try:
        conn.execute("VACUUM INTO ?", (str(dest_path),))
    except sqlite3.Error as exc:
        dest_path.unlink(missing_ok=True)
        raise SnapshotError(f"Snapshot of {db_path} failed: {exc}") from exc
    finally:
        conn.close()


def read_schema_version(db_path: Path) -> int:
    """Return the recorded schema_version, or 0 for an unmigrated/absent table."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def ensure_not_locked(db_path: Path) -> None:
    """Raise DatabaseLockedError if any other connection holds the database open.

    Honest detection, not a pidfile: `PRAGMA locking_mode=EXCLUSIVE` followed by
    a write transaction takes an exclusive lock on the WAL index, which SQLite
    grants only when no other connection has the database open. A plain BEGIN
    IMMEDIATE is not enough - in WAL mode an idle backend holds no write lock and
    would be missed.
    """
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=0)
    try:
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise DatabaseLockedError(
                f"{db_path} is open in another process. Stop the HomePilot backend "
                "(docker compose stop backend) and retry."
            ) from exc
        raise
    finally:
        conn.close()
