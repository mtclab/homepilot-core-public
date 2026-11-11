"""A 3.6.25 database upgrades to 3.7.0 in ONE start (release 3.7.0, Phase A).

Phase A was built in three parallel lanes, and two of them (alerts are heard,
agents can join) each added "schema version 35". The release renumbered them
into one sequence in merge order:

* 35 - alerts are heard: ``alert_rules.kind``, ``alert_events``,
  ``notification_channels``
* 36 - agents can join: ``enrolment_attempts``, ``bootstrap_tokens.created_at``
  and ``bootstrap_tokens.minted_by``

What an operator upgrading from 3.6.25 needs is not "the dict has two keys": it
is that the one ``run_migrations`` call the backend makes at start walks a
3.6.25 database through both, keeps the rows it already had, and leaves a schema
the new features can actually write to and read from. So this builds a real
3.6.25-shaped database with real 3.6.25-shaped rows (an alert rule from before
rules had a kind, a bootstrap token from before tokens had a minter), runs the
startup migration exactly once, and then drives the two lanes' own code over it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from homepilot.agent_hub import join_requests
from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations
from homepilot.db.repository import Repository
from homepilot.metrics.defaults import HOST_WENT_DARK_RULE, seed_default_alert_rules
from homepilot.metrics.repository import MetricsRepository
from homepilot.notifications.service import ChannelStore

# The last schema version 3.6.25 shipped. Versions 1..34 of this release are
# byte-for-byte the ones 3.6.25 shipped (the release only appended), so applying
# exactly those builds the database a 3.6.25 install has on disk.
SCHEMA_3_6_25 = 34
SCHEMA_3_7_0 = 36
# Appended after 3.7.0 (release 3.8.0), and walked in the same one start:
# * 37 - the fleet model (lane B1): template flag, pool, PVE tags, allocation,
#   env and the self-guest mark on ``hosts``
# * 38 - addresses and clashes (lane B2): ``host_addresses``
# Unreleased:
# * 39 - operator power controls (lane B3): ``hosts.protected``
# * 40 - the `withdraw` task action (#797): ``tasks`` rebuilt, rows kept
# * 41 - one live ``hosts`` row per VMID and per node (duplicates retired)
# tests/test_upgrade_from_3_7_0.py drives a 3.7.0 database with real rows
# through both.
SCHEMA_LATEST = 41


async def _version(db: Database) -> int:
    row = await db.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
    return int(row["value"])


async def _columns(db: Database, table: str) -> set[str]:
    return {r["name"] for r in await db.fetchall(f"PRAGMA table_info({table})")}


async def _tables(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r["name"] for r in rows}


@pytest.fixture
async def db_3_6_25(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A database at 3.6.25's schema, holding rows 3.6.25 could have written."""
    db_file = tmp_path / "homepilot.db"
    database = Database(str(db_file))
    await database.connect()
    for version in [v for v in MIGRATIONS if v > SCHEMA_3_6_25]:
        monkeypatch.delitem(MIGRATIONS, version)
    await run_migrations(database)
    monkeypatch.undo()
    assert await _version(database) == SCHEMA_3_6_25
    assert "alert_events" not in await _tables(database)
    assert "kind" not in await _columns(database, "alert_rules")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await database.execute(
        "INSERT INTO alert_rules (id, name, host_filter, metric, comparison, threshold, "
        "for_seconds, enabled, created_at, updated_at) "
        "VALUES ('r-disk', 'Disk almost full', '*', 'disk.free_gb', 'lt', 1.0, 600, 1, ?, ?)",
        (now, now),
    )
    await database.execute(
        "INSERT INTO bootstrap_tokens (token_hash, expires_at) VALUES (?, ?)",
        ("ab" * 32, time.time() + 600),
    )
    await database.execute(
        "INSERT INTO tasks (id, artifact_id, action, status, result_json, created_at, "
        "finished_at, error) VALUES ('t-old', '2026-01-01-old-aaaaaa', 'apply', 'succeeded', "
        '\'{"execution_log": "ok"}\', ?, ?, NULL)',
        (now, now),
    )
    await database.conn.commit()
    yield database
    await database.close()


class TestADatabaseFrom3625UpgradesInOneStart:
    async def test_one_start_reaches_the_release_schema(self, db_3_6_25):
        assert max(MIGRATIONS) == SCHEMA_LATEST, (
            "a migration was added after this test was last extended: extend it to cover it"
        )
        # One sequence, no gaps: run_migrations walks range(current + 1, max + 1).
        assert sorted(MIGRATIONS) == list(range(1, SCHEMA_LATEST + 1))
        await run_migrations(db_3_6_25)
        assert await _version(db_3_6_25) == SCHEMA_LATEST
        assert {
            "is_template",
            "pool",
            "pve_tags",
            "maxcpu",
            "maxmem_mb",
            "maxdisk_gb",
            "env",
            "env_source",
            "is_self",
            "protected",
        } <= await _columns(db_3_6_25, "hosts")
        assert "host_addresses" in await _tables(db_3_6_25)

        tables = await _tables(db_3_6_25)
        assert {"alert_events", "notification_channels", "enrolment_attempts"} <= tables
        assert "kind" in await _columns(db_3_6_25, "alert_rules")
        assert {"created_at", "minted_by"} <= await _columns(db_3_6_25, "bootstrap_tokens")

        # 40 rebuilt `tasks` for the `withdraw` action (#797): the old run is
        # kept as it was, and the one-run-per-artifact index came back with it.
        (old,) = await db_3_6_25.fetchall("SELECT * FROM tasks")
        assert (old["id"], old["action"], old["status"]) == ("t-old", "apply", "succeeded")
        assert old["result_json"] == '{"execution_log": "ok"}'
        indexes = {
            r["name"]
            for r in await db_3_6_25.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_tasks_one_active_per_artifact" in indexes
        # 41: one live row per machine.
        assert {"idx_hosts_one_live_per_vmid", "idx_hosts_one_live_node"} <= indexes

        # The pre-migration snapshot is of the 3.6.25 database, so the one
        # restore an operator is told about takes them back to what they had.
        backup = Path(db_3_6_25.db_path).parent / "backups" / "pre-migration-v34.db"
        conn = sqlite3.connect(str(backup))
        try:
            value = conn.execute(
                "SELECT value FROM settings WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert int(value) == SCHEMA_3_6_25

    async def test_existing_rules_and_tokens_survive_and_read_as_before(self, db_3_6_25):
        await run_migrations(db_3_6_25)
        metrics = MetricsRepository(db_3_6_25)
        rules = {r["id"]: r for r in await metrics.list_rules()}
        assert rules["r-disk"]["kind"] == "threshold"
        assert rules["r-disk"]["threshold"] == 1.0

        # A token minted by 3.6.25 has no minter or mint time; it is still
        # listed as pending, not hidden or crashed on.
        pending = await join_requests.pending_tokens(Repository(db_3_6_25))
        assert len(pending) == 1
        assert pending[0]["id"] == "ab" * 6
        assert pending[0]["minted_by"] is None
        assert pending[0]["created_at"] is None
        assert pending[0]["expired"] is False

    async def test_the_new_features_work_on_the_upgraded_database(self, db_3_6_25):
        await run_migrations(db_3_6_25)
        repo = Repository(db_3_6_25)
        metrics = MetricsRepository(db_3_6_25)

        # Version 35: the "Host went dark" rule is seeded onto an install that
        # already had a policy, and its history can be written and read.
        assert await seed_default_alert_rules(repo, metrics) == 1
        dark = [r for r in await metrics.list_rules() if r["kind"] == "nodata"]
        assert [r["name"] for r in dark] == [HOST_WENT_DARK_RULE["name"]]
        # Every write below is read back at once through a SECOND connection to
        # the same file, before anything else can commit it: a lane write left
        # uncommitted on the writer's connection reads as missing (the 3.7.0
        # dev walk asked this of every new write path).
        reader = Database(db_3_6_25.db_path)
        await reader.connect()
        try:
            at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            event_id = await metrics.record_alert_event(
                rule=dark[0], hostname="web-1", event="firing", value=300.0, since=at, at=at
            )
            await metrics.set_alert_event_delivery(event_id, ["ntfy:1"])
            events = await MetricsRepository(reader).list_alert_events("1970-01-01T00:00:00Z")
            assert [
                (e["hostname"], e["event"], e["rule_kind"], e["delivered_to"]) for e in events
            ] == [("web-1", "firing", "nodata", ["ntfy:1"])]

            channels = ChannelStore(db_3_6_25)
            channel_id = await channels.insert(
                kind="ntfy", name="phone", endpoint="https://ntfy.example.com", has_token=False
            )
            await channels.record_attempt(channel_id, ok=True, error=None)
            channel = await ChannelStore(reader).get(channel_id)
            assert channel is not None and channel["last_result"] == "ok"

            # Version 36: a refused host is recorded and shows as wanting to join.
            await join_requests.record_attempt(
                repo,
                hostname="new-host",
                peer="192.0.2.10",
                code=join_requests.DENY_WINDOW_CLOSED,
                reason="not accepting new hosts right now",
            )
            body = await join_requests.payload(Repository(reader))
            assert [(a["hostname"], a["wants_to_join"]) for a in body["attempts"]] == [
                ("new-host", True)
            ]
        finally:
            await reader.close()
