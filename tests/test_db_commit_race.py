"""One shared connection, many coroutines: no statement outlives its await, one row per machine.

The public mirror's e2e (3.10.0) caught the fleet server dying at startup:
`Failed to refresh proxmox inventory: cannot commit transaction - SQL statements
in progress`, then two inventory rows for the same guest. Two sweeps ran at
once - the inventory reconciler's first cycle (startup_delay 0) and a second
refresh - over the ONE aiosqlite connection every repository shares. Three
defects, each pinned here:

* A write statement left IN PROGRESS across an await. `mark_hosts_absent_ids`
  ran `UPDATE ... RETURNING` as `execute()` and fetched its rows in a SECOND
  queued operation. Between the two, any other coroutine's `commit()` reaches
  the worker thread, and SQLite refuses to commit while a write statement is
  unfinished. The cure is at the connection: `Database.fetchall` executes and
  fetches in one worker operation, and a RETURNING statement goes only through
  it (a source gate below).
* Two sweeps interleaving their check-then-insert: both look a new guest up,
  both find nothing, both insert. Sweeps of one inventory are serialised, and
  the database itself refuses a second LIVE row for a VMID or a node name
  (migration 41), so no other writer - provisioning, a CLI sweep in another
  process - can make one either.
* A write transaction left open by a coroutine interrupted mid-method (the
  failed commit above; a reconciler cancelled at shutdown) blocks the shutdown
  `wal_checkpoint(TRUNCATE)` for its whole 3s budget. The checkpoint now rolls
  that orphan back first - which is what closing the connection does anyway.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.service import InventoryService
from tests.test_power_controls import NODE, OTHER_NODE, FakeCluster, lxc_net, qemu_net

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"


def _fleet(cls: type[FakeCluster] = FakeCluster) -> FakeCluster:
    pve = cls()
    pve.add(101, "atlas", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
    pve.add(112, "relay", "running", qemu_net("192.0.2.20", "BC:24:11:00:00:12"))
    pve.add(103, "forge-dev", "stopped", qemu_net("192.0.2.13", "BC:24:11:00:00:03"))
    pve.add(130, "alice-web", "stopped", qemu_net("192.0.2.30", "BC:24:11:00:00:30"))
    pve.add(142, "batch-2", "stopped", qemu_net("192.0.2.42", "BC:24:11:00:00:42"), node=OTHER_NODE)
    pve.add(110, "dns", "running", lxc_net("192.0.2.3", "BC:24:11:00:00:10"), guest_type="lxc")
    return pve


GUESTS = {
    101: "atlas",
    112: "relay",
    103: "forge-dev",
    130: "alice-web",
    142: "batch-2",
    110: "dns",
}


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    await run_migrations(database)
    try:
        yield database
    finally:
        await database.close()


async def _live_rows(db: Database) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT id, hostname, host_type, proxmox_id FROM hosts WHERE absent_since IS NULL"
    )


async def _assert_one_live_row_per_machine(db: Database) -> None:
    rows = await _live_rows(db)
    names = sorted(r["hostname"] for r in rows)
    assert names == sorted([*GUESTS.values(), NODE, OTHER_NODE]), names
    vmids = [r["proxmox_id"] for r in rows if r["proxmox_id"] is not None]
    assert sorted(vmids) == sorted(GUESTS), vmids


class _CommitAfterEveryStatement:
    """The worst interleaving one shared connection allows, made deterministic.

    Wraps the connection's `execute`: after EVERY statement it returns, a
    different writer inserts a row and commits before the caller gets its
    cursor back - exactly what another coroutine may do at any `await`. Each
    of those commits is recorded; one SQLite refuses is the defect.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.conn = db.conn
        self.original = self.conn.execute
        self.commits = 0
        self.refused: list[str] = []
        self._inside = False

    async def _other_writer(self) -> None:
        self._inside = True
        try:
            await self.original(
                "INSERT INTO audit_log (user_id, source, action) VALUES ('other', 'test', 'tick')"
            )
            await self.conn.commit()
            self.commits += 1
        except sqlite3.OperationalError as exc:
            self.refused.append(str(exc))
        finally:
            self._inside = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def execute(sql: str, parameters: Any = None) -> aiosqlite.Cursor:
            cursor = await self.original(sql, parameters)
            if not self._inside:
                await self._other_writer()
            return cursor

        monkeypatch.setattr(self.conn, "execute", execute)


class TestNoWriteStatementOutlivesItsAwait:
    async def test_another_writers_commit_lands_between_the_absent_stamp_and_its_rows(
        self, db, monkeypatch
    ):
        """The CI failure, isolated: a commit queued inside the RETURNING window."""
        repo = Repository(db)
        gone = await repo.create_host(hostname="gone", host_type="qemu", proxmox_id=900)
        kept = await repo.create_host(hostname="kept", host_type="qemu", proxmox_id=901)
        writer = _CommitAfterEveryStatement(db)
        writer.install(monkeypatch)

        newly_absent = await repo.mark_hosts_absent_ids({kept}, ("discovered",))

        assert writer.refused == [], writer.refused
        assert writer.commits > 0
        assert newly_absent == [gone]
        row = await db.fetchone("SELECT absent_since FROM hosts WHERE id = ?", (gone,))
        assert row is not None and row["absent_since"] is not None

    async def test_a_full_sweep_completes_with_a_commit_after_every_statement(
        self, db, monkeypatch
    ):
        """The goal: the sweep completes and the inventory is whole, whoever else commits."""
        repo = Repository(db)
        # A guest Proxmox no longer has: the absent stamp RETURNS a row, so its
        # statement is still in progress after `execute()` (one with no rows to
        # return finishes in its first step and opens no window).
        destroyed = await repo.create_host(hostname="destroyed", host_type="qemu", proxmox_id=999)
        svc = InventoryService(repo, _fleet())
        writer = _CommitAfterEveryStatement(db)
        writer.install(monkeypatch)

        result = await svc.refresh_inventory()

        assert writer.refused == [], writer.refused
        assert result["complete"], result
        assert result["absent_host_ids"] == [destroyed]
        await _assert_one_live_row_per_machine(db)

    def test_every_returning_statement_is_executed_and_read_in_one_step(self):
        """Source gate: RETURNING only through `fetchall`/`execute_fetchall`.

        A RETURNING statement is a WRITE that stays in progress until its last
        row is read. Executed with `execute()` and read with a later
        `fetchall()`, the gap between the two is a window in which any other
        coroutine's commit fails. `Database.fetchall` does both in one worker
        operation, so no window exists.
        """
        allowed = {"fetchall", "execute_fetchall"}
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and re.search(r"\bRETURNING\b", node.value)
                    # A docstring talks about SQL; it is not a statement.
                    and not isinstance(parents.get(node), ast.Expr)
                ):
                    continue
                call: ast.AST | None = node
                while call is not None and not isinstance(call, ast.Call):
                    call = parents.get(call)
                func = call.func if isinstance(call, ast.Call) else None
                name = func.attr if isinstance(func, ast.Attribute) else None
                if name not in allowed:
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} via {name}()")
        assert offenders == [], offenders


class _Rendezvous(FakeCluster):
    """Holds the first guest-config read until a SECOND sweep reaches it too.

    Both sweeps then look the guest up before either inserts it: the
    interleaving that recorded alice-web twice, forced instead of hoped for.
    Waits at most `patience` seconds, so a fix that serialises the sweeps
    (the second never arrives while the first waits) still finishes.
    """

    patience = 1.0

    def __init__(self) -> None:
        super().__init__()
        self.arrived = 0
        self.both_here = asyncio.Event()

    async def call(self, method, path, body=None, query=None):
        if path.endswith("/qemu/101/config") and self.arrived < 2:
            self.arrived += 1
            if self.arrived == 2:
                self.both_here.set()
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.both_here.wait(), self.patience)
        return await super().call(method, path, body, query)


class TestOneLiveRowPerMachine:
    async def test_two_concurrent_sweeps_record_every_machine_once(self, db):
        pve = _fleet(_Rendezvous)
        repo = Repository(db)
        # Two services over one database, as the app has: the HTTP app's and
        # the MCP server's. Serialising per INSTANCE would not be enough.
        first, second = InventoryService(repo, pve), InventoryService(repo, pve)

        results = await asyncio.gather(first.refresh_inventory(), second.refresh_inventory())

        assert all(r["complete"] for r in results), results
        await _assert_one_live_row_per_machine(db)
        listed = [h["hostname"] for h in await first.query_inventory()]
        assert sorted(listed) == sorted(set(listed)), listed

    async def test_the_database_refuses_a_second_live_row_for_a_vmid(self, db):
        repo = Repository(db)
        first = await repo.create_host(hostname="alice-web", host_type="qemu", proxmox_id=130)
        with pytest.raises(sqlite3.IntegrityError):
            await repo.create_host(hostname="alice-web", host_type="qemu", proxmox_id=130)
        # A retired occupant (#706) is history, not a live claim: the next
        # machine at that VMID is recorded beside it.
        await repo.retire_host(first)
        await repo.create_host(hostname="mirror", host_type="qemu", proxmox_id=130)

    async def test_the_database_refuses_a_second_live_row_for_a_node(self, db):
        repo = Repository(db)
        first = await repo.create_host(hostname=NODE, host_type="node", role="node")
        with pytest.raises(sqlite3.IntegrityError):
            await repo.create_host(hostname=NODE, host_type="node", role="node")
        await repo.retire_host(first)
        await repo.create_host(hostname=NODE, host_type="node", role="node")

    async def test_a_sweep_keeps_using_the_live_row_when_a_retired_one_shares_its_name(self, db):
        """After migration 41 retires a duplicate, the sweep must not revive it.

        The sweep picks "the row that IS this machine" by name. With a retired
        duplicate of the same name beside the live row, picking the newest by
        creation could pick the RETIRED one, and the absent sweep would then
        bring it back while stamping the live one gone - moving the operator's
        adoption onto a row nobody chose.
        """
        repo = Repository(db)
        svc = InventoryService(repo, _fleet())
        assert (await svc.refresh_inventory())["complete"]
        live = await repo.get_host_by_proxmox_id(130)
        assert live is not None
        await repo.update_host(str(live["id"]), owner="alice")
        # A newer row of the same machine, already retired (what the migration
        # leaves when the row it kept is the older one).
        await repo.retire_host(str(live["id"]))
        ghost = await repo.create_host(hostname="alice-web", host_type="qemu", proxmox_id=130)
        await repo.retire_host(ghost)
        # The live row is the OLDER one, so "newest row with this name" is the ghost.
        await db.execute(
            "UPDATE hosts SET absent_since = NULL, created_at = '2026-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (str(live["id"]),),
        )
        await db.conn.commit()

        assert (await svc.refresh_inventory())["complete"]

        (row,) = [r for r in await _live_rows(db) if r["proxmox_id"] == 130]
        assert row["id"] == live["id"]


class TestMigration41RetiresExistingDuplicates:
    async def test_an_install_with_duplicates_keeps_the_operators_row_and_retires_the_rest(
        self, tmp_path, monkeypatch
    ):
        database = Database(str(tmp_path / "homepilot.db"))
        await database.connect()
        try:
            for version in [v for v in MIGRATIONS if v > 40]:
                monkeypatch.delitem(MIGRATIONS, version)
            await run_migrations(database)
            repo = Repository(database)
            # What the race left: two rows for one guest (the operator adopted
            # and assigned the OLDER one), three for a node, one clean guest.
            adopted = await repo.create_host(hostname="alice-web", host_type="qemu", proxmox_id=130)
            await repo.update_host(adopted, owner="alice", import_state="adopted", managed=1)
            twin = await repo.create_host(hostname="alice-web", host_type="qemu", proxmox_id=130)
            nodes = [
                await repo.create_host(hostname=NODE, host_type="node", role="node")
                for _ in range(3)
            ]
            clean = await repo.create_host(hostname="relay", host_type="qemu", proxmox_id=112)
            monkeypatch.undo()

            await run_migrations(database)

            live = {r["id"] for r in await _live_rows(database)}
            assert adopted in live and twin not in live
            assert nodes[0] in live and not live & set(nodes[1:])
            assert clean in live
            retired = await database.fetchone(
                "SELECT absent_since, status FROM hosts WHERE id = ?", (twin,)
            )
            assert retired is not None and retired["absent_since"] is not None
            # Retired, never deleted: the row and its history stay.
            assert await database.fetchone("SELECT 1 FROM hosts WHERE id = ?", (nodes[2],))
            with pytest.raises(sqlite3.IntegrityError):
                await repo.create_host(hostname="relay", host_type="qemu", proxmox_id=112)
        finally:
            await database.close()


class TestProvisioningAfterTheSweepFoundTheGuest:
    async def test_the_provisioned_guest_takes_over_the_discovered_row(self, db):
        """Provisioning records its guest minutes after the clone; a sweep may see it first.

        That used to leave two live rows (the sweep's "discovered", pending
        adoption, and provisioning's own). With one live row per VMID it would
        fail the provision instead. The provisioned facts land on the one row.
        """
        repo = Repository(db)
        pve = FakeCluster()
        pve.add(150, "alice-box", "running", qemu_net("192.0.2.50", "BC:24:11:00:01:50"))
        assert (await InventoryService(repo, pve).refresh_inventory())["complete"]
        discovered = await repo.get_host_by_proxmox_id(150)
        assert discovered is not None and discovered["import_state"] == "pending"

        host_id = await repo.record_provisioned_guest(
            hostname="alice-box",
            host_type="qemu",
            proxmox_id=150,
            node=NODE,
            ip_address="192.0.2.50",
            owner="alice",
            cpu_cores=2,
            memory_mb=2048,
            disk_gb=20,
        )

        assert host_id == discovered["id"]
        (row,) = [r for r in await _live_rows(db) if r["proxmox_id"] == 150]
        full = await repo.get_host(row["id"])
        assert full is not None
        assert full["owner"] == "alice"
        assert full["source"] == "hp_created"
        assert full["managed_by"] == "provisioned"
        assert full["import_state"] is None

    async def test_a_live_row_of_an_earlier_occupant_is_retired_not_reused(self, db):
        repo = Repository(db)
        old = await repo.create_host(hostname="old-guest", host_type="qemu", proxmox_id=151)
        await repo.update_host(old, owner="bob")

        host_id = await repo.record_provisioned_guest(
            hostname="alice-box", host_type="qemu", proxmox_id=151, node=NODE, owner="alice"
        )

        assert host_id != old
        old_row = await repo.get_host(old)
        assert old_row is not None and old_row["absent_since"] is not None
        assert old_row["owner"] == "bob"


class TestShutdownCheckpoint:
    async def test_an_orphaned_write_transaction_does_not_block_the_checkpoint(
        self, db, caplog, monkeypatch
    ):
        """A writer interrupted mid-method left BEGIN open; the checkpoint waited 3s on it."""
        # A roomy budget, so a slow disk under a loaded gate is never mistaken
        # for the defect. The defect is not slowness: an open writer blocks the
        # TRUNCATE checkpoint for the WHOLE budget, however long it is.
        monkeypatch.setattr(Database, "_CHECKPOINT_SECONDS", 10.0)
        await db.execute(
            "INSERT INTO audit_log (user_id, source, action) VALUES ('orphan', 'test', 'cut')"
        )
        assert db.conn.in_transaction

        with caplog.at_level(logging.WARNING, logger="homepilot.db.connection"):
            # True only when the checkpoint ran to the end and was not blocked:
            # with the orphan left open it times out and answers False. (No
            # wall-clock bound: a loaded box makes that a flake, not a finding.)
            assert await db.checkpoint() is True

        assert "rolled it back" in caplog.text
        assert not db.conn.in_transaction
        assert await db.fetchone("SELECT 1 FROM audit_log WHERE user_id = 'orphan'") is None
