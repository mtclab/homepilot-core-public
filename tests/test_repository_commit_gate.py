"""Gate against the "repo methods commit by convention" data-loss class (#383).

`Database.execute()` never commits. Repository write methods must therefore
commit their own writes explicitly; a write left uncommitted lives only in the
shared aiosqlite connection's implicit transaction — visible in-process but
rolled back on close and invisible to a second connection (the CLI). This has
already caused real data loss (PR #335, `hp kb reindex` wiping the index).

Two guards:

1. A STATIC guard (`test_every_writing_method_commits`) parses
   ``src/homepilot/db/repository.py`` with ``ast`` and asserts that every
   ``async def`` on ``Repository`` whose body issues an INSERT/UPDATE/DELETE
   also contains a ``commit()`` call. This keeps the whole class closed: it
   fails the moment a commit is dropped from any writing method.

2. A BEHAVIORAL guard (`test_create_doc_metadata_visible_to_second_connection`)
   proves the fix end-to-end for ``create_doc_metadata`` — it writes through one
   ``Database`` connection and reads the row back through a SECOND connection to
   the same file, mirroring the real CLI-vs-server bug.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from homepilot.db import Database, Repository, run_migrations

_REPO_SOURCE = Path(__file__).resolve().parent.parent / "src" / "homepilot" / "db" / "repository.py"

# Whole-word DML keywords. ``\bUPDATE\b`` deliberately does NOT match the column
# name ``updated_at`` (no word boundary after "UPDATE"), while it DOES match the
# upsert clause ``DO UPDATE SET``.
_DML_RE = re.compile(r"\b(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def _string_constants(node: ast.AST) -> list[str]:
    """All string literal constants anywhere under ``node`` (incl. f-strings)."""
    out: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            out.append(child.value)
    return out


def _issues_write(method: ast.AsyncFunctionDef) -> bool:
    """True if the method body contains a SQL string literal with an INSERT/UPDATE/DELETE."""
    return any(_DML_RE.search(s) for s in _string_constants(method))


def _has_commit_call(method: ast.AsyncFunctionDef) -> bool:
    """True if the method body contains any ``*.commit(...)`` call (e.g. self.db.conn.commit())."""
    for child in ast.walk(method):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "commit"
        ):
            return True
    return False


def _repository_methods() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(_REPO_SOURCE.read_text(encoding="utf-8"))
    repo_cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Repository"
    )
    return [n for n in repo_cls.body if isinstance(n, ast.AsyncFunctionDef)]


def test_every_writing_method_commits() -> None:
    """Every Repository async method that writes must also commit — no exceptions.

    Has teeth: delete any ``await self.db.conn.commit()`` from a fixed writing
    method (e.g. create_doc_metadata, touch_token_last_used) and this fails,
    naming the offender.
    """
    offenders = [
        m.name for m in _repository_methods() if _issues_write(m) and not _has_commit_call(m)
    ]
    assert not offenders, (
        "Repository methods issue a write (INSERT/UPDATE/DELETE) but never call "
        "commit(), so the write is left in the shared connection's implicit "
        "transaction and is rolled back on close / invisible to other "
        f"connections (data-loss class #383): {sorted(offenders)}"
    )


def test_gate_detects_a_missing_commit() -> None:
    """Meta-test: the static detector actually flags an uncommitted writer.

    Guards the gate itself from silently rotting into a no-op (e.g. if the
    keyword regex or commit detector broke and matched everything).
    """
    src = (
        "class Repository:\n"
        "    async def bad_writer(self):\n"
        "        await self.db.execute('DELETE FROM x WHERE id = ?', (1,))\n"
    )
    tree = ast.parse(src)
    repo_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    method = next(n for n in repo_cls.body if isinstance(n, ast.AsyncFunctionDef))
    assert _issues_write(method)
    assert not _has_commit_call(method)


async def test_create_doc_metadata_visible_to_second_connection(tmp_path: Path) -> None:
    """A doc written via one connection must be readable from a SECOND connection.

    This is the real CLI-vs-server bug: the server writes but never commits, so
    the CLI (a separate connection) sees nothing and the write is lost on close.
    """
    db_path = tmp_path / "gate.db"

    writer = Database(db_path)
    await writer.connect()
    try:
        await run_migrations(writer)
        repo = Repository(writer)
        doc_id = await repo.create_doc_metadata(
            source="test:commit-gate",
            title="Commit gate doc",
            content="body",
            kind="note",
        )
        assert doc_id is not None
    finally:
        await writer.close()

    # A fresh, independent connection to the SAME file — only committed rows show.
    reader = Database(db_path)
    await reader.connect()
    try:
        row = await reader.fetchone(
            "SELECT id, title FROM doc_metadata WHERE source = ?",
            ("test:commit-gate",),
        )
    finally:
        await reader.close()

    assert row is not None, (
        "create_doc_metadata write was not committed — invisible to a second "
        "connection (data-loss class #383)"
    )
    assert row["title"] == "Commit gate doc"
