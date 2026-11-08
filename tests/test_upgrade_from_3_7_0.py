"""A 3.7.0 database upgrades to 3.8.0 in ONE start (release 3.8.0, Phase B lanes).

3.8.0 was built in parallel lanes, and two of them (B1 the fleet model, B2
addresses and clashes) each wrote "schema version 37". The release renumbered
them into one sequence in merge order:

* 37 - the fleet model (B1): ``hosts.is_template``, ``pool``, ``pve_tags``,
  ``maxcpu``, ``maxmem_mb``, ``maxdisk_gb``, ``env``, ``env_source``,
  ``is_self`` and the one-self partial unique index
* 38 - addresses and clashes (B2): ``host_addresses``

Lane A+ (updates available) keeps its state in ``settings`` and adds none.

An operator upgrading from 3.7.0 has a ``hosts`` table full of rows. What they
need is that the one ``run_migrations`` the backend makes at start walks that
database through both versions, keeps every row as it was, gives the new
columns their "nothing decided yet" values, and leaves a schema both lanes'
code can write to and read from - including the cross-lane read, B1's
self-guest match over B2's address table.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.addresses import (
    SOURCE_QEMU_IPCONFIG,
    NicAddress,
    find_conflicts,
)
from homepilot.inventory.self_guest import ControlPlaneAddress, self_guest_status

SCHEMA_3_7_0 = 36
SCHEMA_3_8_0 = 38

_NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# (id, proxmox_id, hostname, node, host_type, ip_address, ip_source, source,
#  import_state, pve_status, managed, pinned_fields)
_ROWS_3_7_0 = [
    (
        "h-hp",
        100,
        "hp-server",
        "pve",
        "qemu",
        "10.0.0.100",
        "dhcp",
        "discovered",
        "adopted",
        "running",
        1,
        None,
    ),
    (
        "h-web",
        101,
        "web-01",
        "pve",
        "qemu",
        "10.0.0.51",
        "dhcp",
        "discovered",
        "pending",
        "stopped",
        0,
        '["description"]',
    ),
    ("h-ct", 201, "ct-db", "pve", "lxc", None, None, "discovered", "ignored", "running", 0, None),
    (
        "h-nas",
        None,
        "nas",
        None,
        "manual",
        "10.0.0.51",
        "user",
        "manual",
        None,
        None,
        0,
        '["ip_address"]',
    ),
]


async def _version(db: Database) -> int:
    row = await db.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
    return int(row["value"])


async def _columns(db: Database, table: str) -> set[str]:
    return {r["name"] for r in await db.fetchall(f"PRAGMA table_info({table})")}


async def _tables(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r["name"] for r in rows}


@pytest.fixture
async def db_3_7_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A database at 3.7.0's schema, holding the hosts a 3.7.0 install has."""
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    for version in [v for v in MIGRATIONS if v > SCHEMA_3_7_0]:
        monkeypatch.delitem(MIGRATIONS, version)
    await run_migrations(database)
    monkeypatch.undo()
    assert await _version(database) == SCHEMA_3_7_0
    assert "is_template" not in await _columns(database, "hosts")
    assert "host_addresses" not in await _tables(database)

    for row in _ROWS_3_7_0:
        await database.execute(
            "INSERT INTO hosts (id, proxmox_id, hostname, node, host_type, ip_address, "
            "ip_source, source, import_state, pve_status, managed, pinned_fields, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*row, _NOW, _NOW),
        )
    await database.conn.commit()
    yield database
    await database.close()


class TestADatabaseFrom370UpgradesInOneStart:
    async def test_one_start_reaches_the_release_schema_and_keeps_every_row(self, db_3_7_0):
        assert max(MIGRATIONS) == SCHEMA_3_8_0, (
            "a migration was added after this test was last extended: extend it to cover it"
        )
        before = [dict(r) for r in await db_3_7_0.fetchall("SELECT * FROM hosts ORDER BY id")]

        await run_migrations(db_3_7_0)

        assert await _version(db_3_7_0) == SCHEMA_3_8_0
        new_columns = {
            "is_template", "pool", "pve_tags", "maxcpu", "maxmem_mb", "maxdisk_gb",
            "env", "env_source", "is_self",
        }  # fmt: skip
        assert new_columns <= await _columns(db_3_7_0, "hosts")
        assert "host_addresses" in await _tables(db_3_7_0)
        indexes = {
            r["name"]
            for r in await db_3_7_0.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {
            "idx_hosts_env",
            "idx_hosts_one_self",
            "idx_host_addresses_address",
            "idx_host_addresses_host",
        } <= indexes

        after = [dict(r) for r in await db_3_7_0.fetchall("SELECT * FROM hosts ORDER BY id")]
        # Every 3.7.0 row and every 3.7.0 value survives untouched.
        assert [{k: r[k] for k in before[0]} for r in after] == before
        # The new columns say "nothing decided yet": no host is a template or
        # HomePilot's own until a sync or an operator says so.
        for r in after:
            assert (r["is_template"], r["is_self"], r["env"], r["env_source"]) == (0, 0, None, None)
        assert await db_3_7_0.fetchall("SELECT * FROM host_addresses") == []

        # The one restore an operator is told about takes them back to 3.7.0.
        backup = Path(db_3_7_0.db_path).parent / "backups" / f"pre-migration-v{SCHEMA_3_7_0}.db"
        conn = sqlite3.connect(str(backup))
        try:
            value = conn.execute(
                "SELECT value FROM settings WHERE key = 'schema_version'"
            ).fetchone()[0]
            hosts = conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0]
        finally:
            conn.close()
        assert (int(value), hosts) == (SCHEMA_3_7_0, len(_ROWS_3_7_0))

    async def test_both_lanes_write_and_read_on_the_upgraded_database(self, db_3_7_0, monkeypatch):
        await run_migrations(db_3_7_0)
        repo = Repository(db_3_7_0)
        reader_db = Database(db_3_7_0.db_path)
        await reader_db.connect()
        reader = Repository(reader_db)
        try:
            # B2: the reconciler's per-cycle sync backfills the operator-typed
            # address of a host that existed before the table did.
            await repo.sync_operator_addresses()
            nas = await reader.list_host_addresses("h-nas")
            assert [(a["address"], a["source"]) for a in nas] == [("10.0.0.51", "manual")]

            # B2: a pre-existing guest's config claim lands in the table and
            # clashes with the NAS that has held that address since 3.7.0.
            await repo.replace_host_addresses(
                "h-web",
                (SOURCE_QEMU_IPCONFIG,),
                [NicAddress("10.0.0.51", SOURCE_QEMU_IPCONFIG, iface="net0", bridge="vmbr0")],
            )
            await repo.replace_host_addresses(
                "h-hp",
                (SOURCE_QEMU_IPCONFIG,),
                [NicAddress("10.0.0.100", SOURCE_QEMU_IPCONFIG, iface="net0", bridge="vmbr0")],
            )
            clashes = find_conflicts(await reader.list_address_rows())
            assert [(c.address, sorted(c.host_ids)) for c in clashes] == [
                ("10.0.0.51", ["h-nas", "h-web"])
            ]

            # B1 over B2: the self-guest proposal on a 3.7.0 install comes from
            # the address table, not the row's old `ip_address` guess alone.
            cp = [ControlPlaneAddress("10.0.0.100", "the hub advertise address")]
            status = await self_guest_status(reader, cp)
            assert status["proposal"]["host_id"] == "h-hp"
            assert "net0 (qemu-ipconfig) = 10.0.0.100" in status["proposal"]["evidence"]

            # B1: the mark is written, read back through a second connection,
            # and the database refuses a second one.
            await repo.set_self_host("h-hp")
            assert (await reader.get_self_host())["hostname"] == "hp-server"
            with pytest.raises(sqlite3.IntegrityError):
                await repo.db.execute("UPDATE hosts SET is_self = 1 WHERE id = 'h-web'")
            await repo.db.conn.rollback()

            # B1: env is writable within its vocabulary on a 3.7.0 row.
            await repo.update_host("h-web", env="dev", env_source="user")
            assert (await reader.get_host("h-web"))["env"] == "dev"
        finally:
            await reader_db.close()

    async def test_a_second_start_changes_nothing(self, db_3_7_0):
        await run_migrations(db_3_7_0)
        snapshot = [dict(r) for r in await db_3_7_0.fetchall("SELECT * FROM hosts ORDER BY id")]
        await run_migrations(db_3_7_0)
        assert await _version(db_3_7_0) == SCHEMA_3_8_0
        again = [dict(r) for r in await db_3_7_0.fetchall("SELECT * FROM hosts ORDER BY id")]
        assert again == snapshot
