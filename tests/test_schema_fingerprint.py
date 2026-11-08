"""The schema the migration chain produces is the one `schema.lock` records.

`src/homepilot/db/schema.lock` is a committed, generated description of the
final schema (tables, columns with type/NOT NULL/default/key, foreign keys,
indexes, normalised CREATE text) plus a content hash of every migration
version. This file rebuilds the schema two ways and holds both to the lock:

* a fresh install: an empty database migrated 1..N;
* an upgrade from EVERY older version k (0..N-1): migrated to k by the chain as
  it stood, then upgraded to N in a second start, the way an install that last
  ran the build with schema k does. Each upgrade also proves the pre-migration
  backup still lands at `backups/pre-migration-v<k>.db`, at version k.

A schema change that nobody regenerated the lock for fails here, and so does an
edit to a version the lock already records (installs that ran it never run it
again, so the edit would split fresh installs from upgraded ones).

After an intended schema change:  make schema-lock
"""

from __future__ import annotations

import difflib
import sqlite3
from pathlib import Path

import pytest

from homepilot.db import schema_fingerprint as fp
from homepilot.db.migrations import MIGRATIONS

LATEST = max(MIGRATIONS)


def _lock() -> tuple[dict[int, str], list[str], str]:
    return fp.parse_lock(fp.LOCK_PATH.read_text(encoding="utf-8"))


def _assert_matches_lock(schema: list[str], how: str) -> None:
    _history, recorded, _sha = _lock()
    if schema != recorded:
        diff = "\n".join(
            difflib.unified_diff(recorded, schema, "schema.lock", how, lineterm="", n=1)
        )
        pytest.fail(
            f"The schema from {how} differs from schema.lock - an unrecorded schema "
            f"change. If it is intended, run `{fp.UPDATE_COMMAND}` and commit the lock.\n"
            f"{diff}"
        )


def test_the_lock_is_internally_consistent():
    """A hand edit to the schema section without the header (or the reverse)."""
    history, schema, recorded_sha = _lock()
    assert recorded_sha == fp.schema_sha256(schema)
    assert fp.LOCK_PATH.read_text(encoding="utf-8") == fp.render_lock(schema, history)


async def test_a_fresh_install_has_the_recorded_schema(tmp_path: Path):
    schema = await fp.migrate_and_describe(str(tmp_path / "homepilot.db"))
    _assert_matches_lock(schema, "a fresh migrate")


@pytest.mark.parametrize("start", range(LATEST))
async def test_an_upgrade_from_every_older_version_has_the_recorded_schema(
    tmp_path: Path, start: int
):
    db_path = tmp_path / "homepilot.db"
    schema = await fp.migrate_and_describe(str(db_path), upto=start)
    _assert_matches_lock(schema, f"an upgrade from version {start}")

    # The second start backed the version-`start` database up before any DDL.
    backup = tmp_path / "backups" / f"pre-migration-v{start}.db"
    if start == 0:
        # Nothing to lose: a database with no tables is not backed up.
        assert not backup.exists()
        return
    assert backup.exists(), f"no pre-migration backup for the upgrade from {start}"
    with sqlite3.connect(backup) as conn:
        (value,) = conn.execute(
            "SELECT value FROM settings WHERE key = 'schema_version'"
        ).fetchone()
        (journal,) = conn.execute("PRAGMA journal_mode").fetchone()
    assert int(value) == start
    assert journal == "delete", "the backup must be a rollback-journal file (VACUUM INTO)"


def test_every_recorded_version_is_unchanged_and_every_version_is_recorded():
    recorded, _schema, _sha = _lock()
    current = fp.history_digests(MIGRATIONS)
    edited = sorted(v for v in recorded if v in current and current[v] != recorded[v])
    assert edited == [], (
        f"released schema version(s) {edited} were edited. Installs that already "
        "applied them never run them again - add a NEW version instead."
    )
    assert sorted(recorded) == sorted(current), (
        f"schema.lock records versions 1..{max(recorded)} but the chain has 1..{LATEST}: "
        f"run `{fp.UPDATE_COMMAND}` and commit the lock"
    )


def test_the_update_command_refuses_to_rewrite_released_history(tmp_path, monkeypatch, capsys):
    """`make schema-lock` is the one way to update the lock; it must not be a
    way to launder an edit to a released version."""
    lock = tmp_path / "schema.lock"
    history, schema, _sha = _lock()
    history[1] = "0" * 64  # as if version 1's content had changed since
    lock.write_text(fp.render_lock(schema, history), encoding="utf-8")
    monkeypatch.setattr(fp, "LOCK_PATH", lock)
    before = lock.read_text(encoding="utf-8")

    assert fp.main(["--write"]) == 2
    assert "refusing to rewrite" in capsys.readouterr().err
    assert lock.read_text(encoding="utf-8") == before

    assert fp.main(["--write", "--rewrite-history"]) == 0
    assert fp.main([]) == 0
