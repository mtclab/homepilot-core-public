"""One noun: Host (#514 S1).

THE WALK'S HEADLINE DEFECT, as a gate: a live 2.9.0 install had a connected
agent while Inventory said "No hosts in inventory yet" and Coverage read 0%.
The machine an agent runs on was not a host in the product's own model unless
Proxmox also said so - the two tabs described the same machines with no link.

These assert the OPERATOR'S OUTCOME through the real hub + real migrated DB +
real routers: enroll an agent on an empty install, and the fleet pages know
about the machine. No Proxmox anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.server import AgentHubServer
from homepilot.dashboard.router import router as dashboard_router
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.router import router as inventory_router

pytestmark = pytest.mark.asyncio

AUTH = "one-noun-test-token"
HEADER_LEN = 4


def _encode(msg: dict[str, Any]) -> bytes:
    body = json.dumps(msg).encode()
    return struct.pack("!I", len(body)) + body


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    hdr = await reader.readexactly(HEADER_LEN)
    (length,) = struct.unpack("!I", hdr)
    return dict(json.loads(await reader.readexactly(length)))


SYSTEM_INFO = {
    "os": "Linux",
    "os_version": "6.8.0-test",
    "arch": "amd64",
    "cpu_count": 8,
    "memory": {"total_gb": 32.0, "free_gb": 20.0},
    "agent_version": "v2.9.9-test",
}


@contextlib.asynccontextmanager
async def _stack(tmp_path: Path):
    """Real DB + real hub + the real inventory/dashboard routers."""
    db = Database(str(tmp_path / "one-noun.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    registry = AgentRegistry(repo=repo)
    srv = AgentHubServer(host="127.0.0.1", port=0, auth_token=AUTH, registry=registry)
    await srv.start()
    assert srv._server is not None
    port = srv._server.sockets[0].getsockname()[1]

    app = FastAPI()
    app.include_router(inventory_router, prefix="/hosts")
    app.include_router(dashboard_router)  # carries its own /dashboard prefix
    app.state.repo = repo
    app.state.database = db
    app.state.agent_registry = registry

    # Several tests here enrol a SECOND, different hostname with the shared
    # token, which since #537 needs an operator-opened enrolment window. This
    # stack is about hosts and agents being one noun, not about who may join, so
    # it enrols with the window open rather than relaxing the rule.
    from homepilot.agent_hub.enrolment_window import open_window

    await open_window(repo, 60)

    async def register(agent_id: str, hostname: str):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            _encode(
                {
                    "action": "register",
                    "auth_token": AUTH,
                    "agent_id": agent_id,
                    "hostname": hostname,
                    "system_info": SYSTEM_INFO,
                    "request_id": f"reg-{agent_id}",
                }
            )
        )
        await writer.drain()
        ack = await _recv(reader)
        assert ack["action"] == "register_ack", ack
        # The write is fire-and-forget behind the ack; drain makes it a fact.
        await registry.drain()
        return reader, writer

    try:
        yield app, repo, register
    finally:
        await srv.stop()
        await db.close()


def _auth_bypass(app: FastAPI) -> None:
    # require_scope() manufactures a new dependency per call, so overriding a
    # fresh instance matches nothing; the underlying token dependency is the
    # single seam every scope check flows through.
    from homepilot.auth.deps import require_token

    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}


class TestAnEnrolledAgentIsAHost:
    async def test_enrolment_on_an_empty_install_creates_the_host_row(self, tmp_path: Path):
        """The gate the walk failed: agent connected, zero Proxmox, and the
        machine EXISTS in inventory with the facts the agent reported."""
        async with _stack(tmp_path) as (_app, repo, register):
            _reader, writer = await register("agent-nn-1", "workshop-box")

            hosts = await repo.list_hosts()
            assert len(hosts) == 1, "an enrolled agent's machine is missing from inventory"
            h = hosts[0]
            assert h["hostname"] == "workshop-box"
            assert h["source"] == "agent"
            assert h["agent_id"] == "agent-nn-1"
            assert h["cpu_cores"] == 8
            assert h["memory_mb"] == 32 * 1024
            assert "Linux" in (h["os_info"] or "")
            writer.close()

    async def test_coverage_counts_the_agent_managed_host(self, tmp_path: Path):
        """Coverage 0% next to a connected agent was a lie. Through the REAL
        dashboard endpoint: one agent-carried host = coverage above zero."""
        async with _stack(tmp_path) as (app, _repo, register):
            _reader, writer = await register("agent-nn-2", "coverage-box")
            _auth_bypass(app)

            with TestClient(app) as client:
                summary = client.get("/dashboard/summary").json()

            assert summary["inventory"]["total"] == 1
            assert summary["inventory"]["coverage_pct"] > 0, (
                "a host with a live agent channel still counts as uncovered"
            )
            writer.close()

    async def test_the_hosts_list_says_the_agent_is_connected(self, tmp_path: Path):
        """The fleet page must answer 'does this machine have a live channel'
        itself - needing a second tab for that is the two-noun split."""
        async with _stack(tmp_path) as (app, _repo, register):
            _reader, writer = await register("agent-nn-3", "listed-box")
            _auth_bypass(app)

            with TestClient(app) as client:
                items = client.get("/hosts").json()["items"]

            assert len(items) == 1
            assert items[0]["agent_connected"] is True
            assert items[0]["agent_version"] == "v2.9.9-test"
            writer.close()

    async def test_reenrolment_links_not_duplicates(self, tmp_path: Path):
        """An agent that reconnects (same id) or is re-enrolled on a machine
        already in inventory by hostname must link to the existing row -
        a duplicate host per reconnect would be the old defect inverted."""
        async with _stack(tmp_path) as (_app, repo, register):
            _r1, w1 = await register("agent-nn-4", "steady-box")
            w1.close()
            _r2, w2 = await register("agent-nn-4", "steady-box")

            hosts = await repo.list_hosts()
            assert len(hosts) == 1, f"reconnect duplicated the host: {len(hosts)} rows"
            w2.close()

    async def test_a_manual_host_gains_the_agent_link_by_hostname(self, tmp_path: Path):
        """Operator adds the machine by hand FIRST, installs the agent second -
        the enrolment must claim that row, not shadow it with a second one.
        And it must not touch what the operator set (#424)."""
        async with _stack(tmp_path) as (_app, repo, register):
            await repo.create_host(
                hostname="hand-added",
                host_type="physical",
                source="manual",
                role="nas",
                role_source="user",
                description="the NAS under the stairs",
            )

            _reader, writer = await register("agent-nn-5", "hand-added")

            hosts = await repo.list_hosts()
            assert len(hosts) == 1
            h = hosts[0]
            assert h["agent_id"] == "agent-nn-5"
            assert h["source"] == "manual", "linking must not rewrite the host's origin"
            assert h["role"] == "nas", "linking overwrote an operator-set role"
            assert h["description"] == "the NAS under the stairs"
            writer.close()

    async def test_two_hosts_same_hostname_link_nothing(self, tmp_path: Path):
        """Ambiguity refuses to guess: two inventory rows with one hostname
        (it happens - a rebuilt machine kept as a tombstone) get NO automatic
        link, and enrolment still creates no third row."""
        async with _stack(tmp_path) as (_app, repo, register):
            await repo.create_host(hostname="twin", host_type="vm", source="manual")
            await repo.create_host(hostname="twin", host_type="vm", source="manual")

            _reader, writer = await register("agent-nn-6", "twin")

            hosts = await repo.list_hosts()
            assert len(hosts) == 2, "ambiguous hostname produced a new row anyway"
            assert all(h["agent_id"] is None for h in hosts), (
                "the link guessed between two identically named hosts"
            )
            writer.close()


class TestAgentFactsAndStatusFlowToTheHost:
    async def test_a_backfilled_host_gains_facts_on_the_next_register(self, tmp_path: Path):
        """The migration backfill knows only the hostname; the facts arrive with
        the agent's next registration. Live proof: the first S2 walk showed a
        facts card of em-dashes over a host whose agent was connected."""
        async with _stack(tmp_path) as (_app, repo, register):
            # A host the backfill would have created: link only, no facts.
            host_id = await repo.create_host(
                hostname="bare-box", host_type="physical", source="agent"
            )
            await repo.db.execute(
                "UPDATE hosts SET agent_id = 'agent-ff-1' WHERE id = ?", (host_id,)
            )
            await repo.db.conn.commit()

            _reader, writer = await register("agent-ff-1", "bare-box")

            h = dict(await repo.get_host(host_id))
            assert h["cpu_cores"] == 8, "the agent's cpu_count did not fill the NULL column"
            assert h["memory_mb"] == 32 * 1024
            assert "Linux" in (h["os_info"] or "")
            writer.close()

    async def test_status_follows_the_channel_unless_pinned(self, tmp_path: Path):
        """ "unknown" in grey next to "agent connected" in green is a lie. The
        linked host reads online while the channel is up - except when an
        operator pinned status, which automation never overwrites (#424)."""
        async with _stack(tmp_path) as (_app, repo, register):
            _reader, writer = await register("agent-ff-2", "status-box")
            hosts = await repo.list_hosts()
            assert hosts[0]["status"] == "online", "a connected agent left the host 'unknown'"

            pinned_id = await repo.create_host(
                hostname="pinned-box", host_type="physical", source="manual", status="offline"
            )
            await repo.db.execute(
                "UPDATE hosts SET pinned_fields = '[\"status\"]' WHERE id = ?", (pinned_id,)
            )
            await repo.db.conn.commit()
            _r2, w2 = await register("agent-ff-3", "pinned-box")
            pinned = dict(await repo.get_host(pinned_id))
            assert pinned["status"] == "offline", "automation overwrote an operator-pinned status"
            writer.close()
            w2.close()

    async def test_disconnect_marks_the_host_offline(self, tmp_path: Path):
        async with _stack(tmp_path) as (_app, repo, register):
            _reader, writer = await register("agent-ff-4", "drop-box")
            await repo.mark_agent_disconnected("agent-ff-4", "test disconnect")
            hosts = await repo.list_hosts()
            assert hosts[0]["status"] == "offline", (
                "the channel dropped and the host still claims its old status"
            )
            writer.close()


class TestForgettingAnAgentUnlinksTheHost:
    async def test_forget_clears_the_link_and_the_borrowed_status(self, tmp_path: Path):
        """Found on the live S3 walk: forget the agent and the host keeps a
        dangling agent_id - the fleet list then claims "agent enrolled, not
        connected" about a credential that no longer exists, and the host
        stays 'online' on the strength of a deleted channel."""
        async with _stack(tmp_path) as (_app, repo, register):
            _reader, writer = await register("agent-ul-1", "unlink-box")
            writer.close()

            assert await repo.delete_agent("agent-ul-1") is True

            h = (await repo.list_hosts())[0]
            assert h["agent_id"] is None, "the host still points at a deleted agent"
            assert h["status"] != "online", (
                "the host claims online through a channel that no longer exists"
            )


class TestTheBackfillSurvivesRealData:
    async def test_migration_25_backfills_an_agent_with_no_first_seen(self, tmp_path: Path):
        """Found by running the migration against the REAL dev database, not by
        the suite: agents.first_seen can be NULL, and the backfill INSERT used
        it for hosts.created_at (NOT NULL) - the whole migration rolled back
        and the backend refused to start. The suite only ever migrated fresh
        databases with no agents, which is exactly the hole live data walks
        through."""
        db = Database(str(tmp_path / "backfill.db"))
        await db.connect()
        await run_migrations(db)
        # Rewind to the pre-25 world: strip the link column state and plant an
        # agent row the way an old install would have it - first_seen NULL.
        await db.execute("DELETE FROM hosts")
        await db.execute(
            """INSERT INTO agents (agent_id, hostname, system_info, state, connected,
                                   first_seen, connected_at, last_heartbeat)
               VALUES ('old-agent', 'veteran-box', '{}', '{}', 0, NULL, NULL, NULL)"""
        )
        await db.conn.commit()
        try:
            # Re-run JUST the backfill INSERT of migration 25 against this state
            # ("INSERT INTO hosts (" - NOT the hosts_new copy of the rebuild).
            from homepilot.db.migrations import MIGRATIONS

            backfill = [
                m for m in MIGRATIONS[25] if isinstance(m, str) and "INSERT INTO hosts (" in m
            ]
            assert len(backfill) == 1, "migration 25 lost its backfill INSERT"
            await db.execute(backfill[0])
            await db.conn.commit()

            row = await db.fetchone("SELECT * FROM hosts WHERE hostname = 'veteran-box'")
            assert row is not None, "the NULL-first_seen agent produced no host row"
            assert row["agent_id"] == "old-agent"
            assert row["created_at"], "created_at must be filled even when first_seen is NULL"
        finally:
            await db.close()
