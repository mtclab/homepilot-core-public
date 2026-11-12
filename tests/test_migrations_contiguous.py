"""The migration chain is 1, 2, 3, ... N - each version exactly once, in order.

Two lanes that each add "the next" version must not silently become one. The
chain used to be a `{version: [...]}` dict literal, where a repeated key keeps
only the LAST value: a merge of two lanes that both wrote `37: [...]` imported
cleanly, numbered without a gap, and dropped the first lane's DDL (it nearly
lost a migration in 3.7.0). It is now a tuple of `Migration(version, ...)`, and
`_index_chain` refuses a repeat, a gap or a reorder when the module is
imported - so a broken chain stops the backend before it can touch a database.

Teeth (each proven by mutation for the PR that introduced this file):
* duplicate a `Migration(38, ...)` entry in migrations.py -> the module fails to
  import, and `test_the_real_module_refuses_a_repeated_version` fails if that
  refusal is removed;
* replace `_index_chain`'s body with a plain dict comprehension -> every
  `TestTheChainIsChecked` case fails.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

import homepilot.db.migrations as mig
from homepilot.db.connection import Database
from homepilot.db.migrations import (
    MIGRATION_CHAIN,
    MIGRATIONS,
    Migration,
    MigrationChainError,
    _index_chain,
    run_migrations,
)

SOURCE = Path(mig.__file__)


def _m(version: int) -> Migration:
    return Migration(version, [f"CREATE TABLE IF NOT EXISTS t{version} (id INTEGER)"])


class TestTheChainIsChecked:
    def test_the_shipped_chain_is_one_to_n(self):
        versions = [m.version for m in MIGRATION_CHAIN]
        assert versions == list(range(1, len(versions) + 1))
        assert {m.version: m.statements for m in MIGRATION_CHAIN} == MIGRATIONS

    def test_a_repeated_version_is_refused(self):
        with pytest.raises(MigrationChainError, match="version 2 is defined twice"):
            _index_chain([_m(1), _m(2), _m(2)])

    def test_a_gap_is_refused(self):
        with pytest.raises(
            MigrationChainError, match="entry 2 of the migration chain is version 3"
        ):
            _index_chain([_m(1), _m(3)])

    def test_out_of_order_is_refused(self):
        with pytest.raises(MigrationChainError, match="expected 1"):
            _index_chain([_m(2), _m(1)])

    def test_the_chain_starts_at_one(self):
        with pytest.raises(MigrationChainError, match="expected 1"):
            _index_chain([_m(0), _m(1)])

    def test_an_empty_version_is_refused(self):
        with pytest.raises(MigrationChainError, match="version 2 has no statements"):
            _index_chain([_m(1), Migration(2, [])])


class TestTheRealModuleRefusesABrokenChainAtImport:
    """The checks above are only worth anything if the module runs them on
    ITS chain at import. Load the real source with one entry duplicated - the
    exact shape of a bad two-lane merge - and the import itself must fail."""

    def _load(self, source: str, tmp_path: Path) -> None:
        path = tmp_path / "migrations_mutant.py"
        path.write_text(source, encoding="utf-8")
        name = "homepilot.db._migrations_mutant"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)

    def _last_entry(self, source: str) -> str:
        tree = ast.parse(source)
        for node in tree.body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "MIGRATION_CHAIN"
            ):
                assert isinstance(node.value, ast.Tuple)
                segment = ast.get_source_segment(source, node.value.elts[-1])
                assert segment is not None
                return segment
        raise AssertionError("MIGRATION_CHAIN is no longer a tuple literal: update this gate")

    def test_the_unmodified_source_loads(self, tmp_path):
        self._load(SOURCE.read_text(encoding="utf-8"), tmp_path)

    def test_the_real_module_refuses_a_repeated_version(self, tmp_path):
        source = SOURCE.read_text(encoding="utf-8")
        last = self._last_entry(source)
        mutant = source.replace(last + ",\n)", last + ",\n    " + last + ",\n)", 1)
        assert mutant != source, "could not build the duplicated-entry mutant"
        # The mutant module defines its OWN MigrationChainError class, so match
        # by name rather than by identity.
        with pytest.raises(Exception, match="defined twice") as excinfo:
            self._load(mutant, tmp_path)
        assert type(excinfo.value).__name__ == MigrationChainError.__name__

    def test_every_entry_names_its_version_as_a_literal(self):
        """The persisted number is visible at the entry, never computed."""
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        chain = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "MIGRATION_CHAIN"
        )
        assert isinstance(chain, ast.Tuple)
        literal = []
        for elt in chain.elts:
            assert isinstance(elt, ast.Call) and getattr(elt.func, "id", None) == "Migration"
            assert isinstance(elt.args[0], ast.Constant), "a version is not a literal number"
            literal.append(elt.args[0].value)
        assert literal == sorted(MIGRATIONS)


class TestTheRunnerRefusesAGap:
    """MIGRATIONS is a mutable dict. Whatever it holds when the runner starts
    must still be contiguous, or the runner must refuse before any DDL."""

    async def test_a_missing_middle_version_stops_the_run(self, tmp_path, monkeypatch):
        database = Database(str(tmp_path / "homepilot.db"))
        await database.connect()
        try:
            monkeypatch.delitem(MIGRATIONS, 5)
            with pytest.raises(MigrationChainError, match="expected 5"):
                await run_migrations(database)
            rows = await database.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
            assert rows == [], "the runner touched the database before refusing"
        finally:
            await database.close()

    async def test_a_full_run_still_reaches_the_last_version(self, tmp_path):
        database = Database(str(tmp_path / "homepilot.db"))
        await database.connect()
        try:
            await run_migrations(database)
            row = await database.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
            assert row is not None and int(row["value"]) == max(MIGRATIONS)
        finally:
            await database.close()
