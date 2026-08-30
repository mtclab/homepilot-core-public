"""#648 tranche 8: is the estate's automation visible, and does it only claim
what it established?

Three findings, one belief each - the same belief the rest of this review keeps
finding:

1. **Nothing said whether the reconcilers were running.** Eight loops maintain
   the estate and `_run_loop` swallows every exception into a log line, so a
   loop that crashes on every cycle is indistinguishable from a healthy one on
   every surface the product has. The drift loop is worse than
   indistinguishable: dead, it leaves the whole fleet on its last green verdict.

2. **A hypervisor sweep that did not finish was returned as one that did.** The
   caller subtracts the ids it got back from what it already has to decide what
   is GONE, so a partial answer makes it declare live machines absent - and an
   install with no Proxmox at all returned the same shape on every cycle.

3. **`enriched` counted hosts LOOKED AT, not hosts changed**, because `status`
   was rewritten unconditionally so the update set was never empty.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homepilot.inventory.service import InventoryService
from homepilot.reconciler.base import (
    Reconciler,
    ReconcilerResult,
    ReconcilerScheduler,
)
from homepilot.selfcheck import STATE_OK, STATE_UNKNOWN, STATE_UNREACHABLE, selfcheck_report

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def real_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(tmp_path / "recon.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def repo(real_db):
    from homepilot.db.repository import Repository

    return Repository(real_db)


def _settings():
    return SimpleNamespace(
        proxmox_host="",
        agent_hub_enabled=False,
        agent_hub_port=0,
        vault_enabled=False,
        embeddings_url="",
        events_webhook_url="",
        artifacts_remote="",
        artifacts_push_interval_seconds=3600,
    )


class _Always(Reconciler):
    """A loop with a chosen outcome, so a cycle can be made to fail on demand."""

    def __init__(self, name: str, ok: bool = True, boom: bool = False) -> None:
        self._name = name
        self._ok = ok
        self._boom = boom
        self.calls = 0

    async def run(self) -> ReconcilerResult:
        self.calls += 1
        if self._boom:
            raise RuntimeError("the loop threw")
        return ReconcilerResult(name=self._name, success=self._ok, details={"n": self.calls})


async def _spin(scheduler: ReconcilerScheduler, cycles: int = 4) -> None:
    """Let the loops turn a few times, then stop them."""
    await scheduler.start()
    for _ in range(cycles):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    await scheduler.stop()


class TestTheSchedulerRecordsWhatItDid:
    """Teeth: drop the status writes in `_run_loop` and every test here fails."""

    async def test_a_healthy_loop_records_its_cycles(self):
        scheduler = ReconcilerScheduler()
        scheduler.register(_Always("drift"), interval=0.01)
        await _spin(scheduler)

        (status,) = scheduler.status()
        assert status.runs >= 1
        assert status.last_ok is True
        assert status.consecutive_failures == 0
        assert status.last_finished_at is not None
        # The loop's own name, not the class name, once a cycle has answered.
        assert status.name == "drift"

    async def test_a_loop_that_reports_failure_is_counted_as_failing(self):
        scheduler = ReconcilerScheduler()
        scheduler.register(_Always("inventory", ok=False), interval=0.01)
        await _spin(scheduler)

        (status,) = scheduler.status()
        assert status.last_ok is False
        assert status.consecutive_failures >= 1

    async def test_a_loop_that_crashes_is_counted_as_failing(self):
        """A crash produces no result at all, so the status must be written on
        the exception path too - otherwise a loop throwing every cycle keeps
        the `last_ok=True` its final successful pass left behind."""
        scheduler = ReconcilerScheduler()
        scheduler.register(_Always("retention", boom=True), interval=0.01)
        await _spin(scheduler)

        (status,) = scheduler.status()
        assert status.last_ok is False
        assert status.consecutive_failures >= 1
        assert "RuntimeError" in status.last_error


class TestTheSelfcheckReportsTheLoops:
    """The operator-facing half: the report has to SAY it, with the consequence.

    Teeth: delete `_reconcilers_subsystem` from `build_subsystems` and every
    test here fails on the missing entry; return plain `True` from its probe
    and the failing/stalled cases fail on the state.
    """

    async def _entry(self, state):
        report = await selfcheck_report(state, _settings())
        return {e["name"]: e for e in report["subsystems"]}["reconcilers"]

    async def test_a_failing_loop_is_reported_broken_with_its_consequence(self):
        scheduler = ReconcilerScheduler()
        scheduler.register(_Always("drift", ok=False), interval=0.01)
        await _spin(scheduler, cycles=8)

        entry = await self._entry(
            SimpleNamespace(
                proxmox=None,
                vault=None,
                agent_hub=None,
                mcp_app=None,
                reconciler_scheduler=scheduler,
            )
        )
        assert entry["state"] == STATE_UNREACHABLE
        # Not "a reconciler is failing" - what the operator LOST.
        assert "drift" in entry["consequence"]
        assert "as old as the last cycle" in entry["consequence"]

    async def test_healthy_loops_are_reported_ok(self):
        scheduler = ReconcilerScheduler()
        scheduler.register(_Always("inventory"), interval=30.0)
        await _spin(scheduler)

        entry = await self._entry(
            SimpleNamespace(
                proxmox=None,
                vault=None,
                agent_hub=None,
                mcp_app=None,
                reconciler_scheduler=scheduler,
            )
        )
        assert entry["state"] == STATE_OK

    async def test_an_instance_with_no_loops_registered_says_so(self):
        """ "Nothing registered" is not a healthy instance with nothing to do."""
        entry = await self._entry(
            SimpleNamespace(
                proxmox=None,
                vault=None,
                agent_hub=None,
                mcp_app=None,
                reconciler_scheduler=ReconcilerScheduler(),
            )
        )
        assert entry["configured"] is False
        assert "nothing maintains the estate" in entry["consequence"]

    async def test_a_loop_that_never_ran_is_unknown_not_ok(self):
        """Registered and long overdue, with no cycle ever completed. `ok` here
        would be the report asserting a health it has never observed."""
        scheduler = ReconcilerScheduler()
        # Never started, so nothing ever ran; a tiny interval makes it overdue
        # the moment the report is taken.
        scheduler.register(_Always("archive_push"), interval=0.001)
        await asyncio.sleep(0.05)

        entry = await self._entry(
            SimpleNamespace(
                proxmox=None,
                vault=None,
                agent_hub=None,
                mcp_app=None,
                reconciler_scheduler=scheduler,
            )
        )
        assert entry["state"] == STATE_UNKNOWN
        assert "never completed a cycle" in entry["consequence"]


class TestASweepThatDidNotFinishSaysSo:
    async def test_a_failed_node_list_is_not_a_complete_sweep(self, repo):
        proxmox = AsyncMock()
        proxmox.read = AsyncMock(side_effect=RuntimeError("pve is down"))
        svc = InventoryService(repo=repo, proxmox=proxmox)

        result = await svc.refresh_inventory()

        assert result["complete"] is False
        assert "pve is down" in result["error"]

    async def test_a_sweep_that_answered_is_complete(self, repo):
        """The honest verdict stays reachable, or the fix would only have
        swapped one wrong answer for another."""
        proxmox = AsyncMock()
        proxmox.read = AsyncMock(
            side_effect=lambda path: (
                {"data": [{"node": "pve1", "ip": "10.0.0.1", "status": "online"}]}
                if path == "/nodes"
                else {"data": []}
            )
        )
        svc = InventoryService(repo=repo, proxmox=proxmox)
        result = await svc.refresh_inventory()
        assert result["complete"] is True
        assert "error" not in result


class TestEnrichedCountsWhatChanged:
    async def test_a_host_that_needed_nothing_is_not_counted_as_enriched(self, repo):
        """Second pass over an already-enriched host writes nothing, so the
        count must not move. `status` used to be rewritten unconditionally, so
        the update set was never empty and every host examined was 'enriched'."""
        proxmox = AsyncMock()
        svc = InventoryService(repo=repo, proxmox=proxmox)
        await repo.create_host(
            hostname="plain-box",
            host_type="qemu",
            role="guest",
            ip_address="10.0.0.5",
            pve_status="running",
            status="online",
            ip_source="user",
            role_source="user",
        )

        first = await svc.enrich_inventory()
        second = await svc.enrich_inventory()

        assert first["enriched"] == 0, first
        assert first["unchanged"] == 1, first
        assert second["enriched"] == 0
        assert second["unchanged"] == 1

    async def test_a_host_that_did_change_is_counted(self, repo):
        proxmox = AsyncMock()
        svc = InventoryService(repo=repo, proxmox=proxmox)
        await repo.create_host(
            hostname="db-box",
            host_type="qemu",
            role="guest",
            ip_address="10.0.0.6",
            pve_status="running",
            status="unknown",
            ip_source="user",
            role_source="inferred",
        )

        result = await svc.enrich_inventory()

        # hostname matches the `database` role patterns, and status moves
        # unknown -> online, so this host genuinely changed.
        assert result["enriched"] == 1, result
        assert result["unchanged"] == 0
