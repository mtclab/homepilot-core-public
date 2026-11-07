"""The Overview says what is true about the estate (review 01, steps 1-6).

Each class is one way the first screen misled an operator on dev or prod, and
each asserts what the operator reads - the `/dashboard/summary` payload and the
inventory rows the Overview is built from - not that some helper ran.

Teeth, each verified by reverting the fix (recorded in the PR):

* drop the `JOIN artifacts ... status IN (...)` in `dashboard/service.py` and
  ``test_a_frozen_verdict_of_a_revoked_artifact_is_not_counted`` fails 1 != 0;
* drop `_forget_drift_verdict` from `ArtifactTransitionManager.revoke` and
  ``test_revoke_takes_the_verdict_with_it`` fails;
* change ACTIVE_DRIFT_STATUSES or drift.ts ACTIVE_STATUSES alone and
  ``test_the_overview_and_the_drift_page_count_the_same_set`` fails;
* drop `offline_alarm` (always False) and the fleet tests fail; drop the
  `listening_since` term and the restart-grace test fails;
* read `agents.connected` again in `inventory/router.py` and
  ``test_a_stale_connected_column_is_not_believed_per_host`` fails;
* drop the heal UPDATE in `mark_hosts_absent_ids` and
  ``test_a_host_gone_since_before_613_stops_counting_online`` fails;
* drop the `test_connection` branch in `_verify_host_provision` and
  ``test_a_host_with_no_agent_is_unverified_for_that_reason`` fails;
* drop `unanswered_steps` from `_sequence_verdict` and
  ``test_a_failed_probe_is_not_describe_only`` fails.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.deps import require_token
from homepilot.auth.principal import Principal
from homepilot.dashboard.service import ACTIVE_DRIFT_STATUSES
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.reconciler.drift import DriftReconciler

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]

PROXMOX_BODY = (
    "## Plan\nCreate\n## Spec\n```yaml proxmox-api-spec\n"
    "steps:\n  - id: s1\n    method: POST\n    path: /nodes/pve1/qemu\n```\n"
)
HOST_PROVISION_BODY = (
    "## Plan\nProvision\n## Spec\n```yaml host-provision-spec\npackages:\n  - nginx\n```\n"
)


def _ts(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_ago(iso: str | None) -> float:
    """How long ago `iso` was, so a clock tick between write and read is no flake."""
    assert iso, "no timestamp where one was due"
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(UTC) - parsed).total_seconds() / 60


class _FakeRegistry:
    """The live hub registry: who is dialled in, and since when it listens."""

    def __init__(self, connected: list[str] | None = None, started_minutes_ago: float = 60) -> None:
        self._connected = connected or []
        self.started_at = datetime.now(UTC) - timedelta(minutes=started_minutes_ago)

    def list_connected(self) -> list[dict[str, Any]]:
        return [{"agent_id": a, "hostname": f"host-{a}"} for a in self._connected]


@pytest.fixture
async def estate(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    yield db, repo, store, lifecycle
    await db.close()


@pytest.fixture
def app(estate):
    from homepilot.dashboard.router import router as dashboard_router
    from homepilot.inventory.router import router as inventory_router

    _db, repo, _store, _lifecycle = estate
    application = FastAPI()
    application.include_router(dashboard_router)
    application.include_router(inventory_router, prefix="/inventory")
    application.state.repo = repo
    application.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    return application


async def _get(app: FastAPI, path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def _spec(artifact_id: str, kind: str, body: str, host: str = "web1") -> dict[str, Any]:
    return {
        "id": artifact_id,
        "kind": kind,
        "intent": "Attention honesty",
        "body": body,
        "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": host},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }


async def _applied(lifecycle: ArtifactLifecycle, artifact_id: str, kind: str, body: str) -> None:
    await lifecycle.propose(_spec(artifact_id, kind, body))
    await lifecycle.approve(artifact_id, Principal.operator("admin"))
    await lifecycle.mark_applied(artifact_id, execution_log="applied")


# ── 1. The drift count is the ACTIVE set ──────────────────────────────────────


class TestDriftCountIsTheActiveSet:
    async def test_revoking_a_drifted_artifact_takes_it_off_the_overview(self, estate, app):
        """Dev 2026-11-07: "2 artifacts disagree" on the Overview, one of them
        revoked; the drift page it links to listed one."""
        _db, repo, _store, lifecycle = estate
        await _applied(lifecycle, "2026-11-07-real", "proxmox-api-sequence", PROXMOX_BODY)
        await _applied(lifecycle, "2026-11-07-gone", "proxmox-api-sequence", PROXMOX_BODY)
        for aid in ("2026-11-07-real", "2026-11-07-gone"):
            await repo.upsert_drift_check(aid, drifted=True, state="drifted")
        assert (await _get(app, "/dashboard/summary"))["drift"]["drifted"] == 2

        await lifecycle.revoke("2026-11-07-gone", "admin")

        drift = (await _get(app, "/dashboard/summary"))["drift"]
        assert drift["drifted"] == 1, "a revoked artifact's verdict is still counted"
        assert drift["total"] == 1

    async def test_revoke_takes_the_verdict_with_it(self, estate):
        """The stored table is read directly too (MCP `get_fleet_drift`), so the
        row itself must go, not only be filtered by the summary."""
        _db, repo, _store, lifecycle = estate
        await _applied(lifecycle, "2026-11-07-rev", "proxmox-api-sequence", PROXMOX_BODY)
        await repo.upsert_drift_check("2026-11-07-rev", drifted=True, state="drifted")

        await lifecycle.revoke("2026-11-07-rev", "admin")

        assert await repo.get_drift_check("2026-11-07-rev") is None
        assert [r["artifact_id"] for r in await repo.get_drift_checks()] == []

    async def test_a_frozen_verdict_of_a_revoked_artifact_is_not_counted(self, estate, app):
        """Rows frozen by releases before this fix stay in the table until
        something deletes them; the count must not wait for that."""
        db, repo, _store, lifecycle = estate
        await _applied(lifecycle, "2026-08-29-old-revoked", "proxmox-api-sequence", PROXMOX_BODY)
        await db.execute(
            "UPDATE artifacts SET status = 'revoked' WHERE id = '2026-08-29-old-revoked'"
        )
        await db.conn.commit()
        await repo.upsert_drift_check("2026-08-29-old-revoked", drifted=True, state="drifted")
        await repo.upsert_drift_check(
            "2026-08-29-orphan",
            drifted=False,
            state="unknown",
            details_json=json.dumps({"reason": "nothing_evaluated"}),
        )

        drift = (await _get(app, "/dashboard/summary"))["drift"]

        assert drift["drifted"] == 0
        assert drift["unknown"] == 0, "an orphan or revoked row is still 'not established'"
        assert drift["total"] == 0

    async def test_the_overview_and_the_drift_page_count_the_same_set(self):
        """The drift page filters to drift.ts ACTIVE_STATUSES; the summary to
        ACTIVE_DRIFT_STATUSES. Two lists in two languages: pinned equal here."""
        source = (REPO_ROOT / "web" / "src" / "lib" / "drift.ts").read_text(encoding="utf-8")
        match = re.search(r"export const ACTIVE_STATUSES = \[([^\]]*)\]", source)
        assert match is not None, "drift.ts no longer declares ACTIVE_STATUSES"
        page = sorted(re.findall(r"'([a-z_]+)'", match.group(1)))
        assert page == sorted(ACTIVE_DRIFT_STATUSES)


# ── 2. One fleet line when every agent is gone ─────────────────────────────────


async def _agent_row(
    db: Database,
    agent_id: str,
    *,
    heartbeat_minutes_ago: float,
    connected: int = 1,
    revoked: bool = False,
) -> None:
    await db.execute(
        "INSERT INTO agents (agent_id, hostname, connected, last_heartbeat, connected_at, "
        "revoked_at, credential_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            agent_id,
            f"host-{agent_id}",
            connected,
            _ts(heartbeat_minutes_ago),
            _ts(heartbeat_minutes_ago + 60),
            _ts(1) if revoked else None,
            "h" if revoked else None,
        ),
    )
    await db.conn.commit()


class TestFleetWithNoAgent:
    async def test_every_agent_gone_for_a_while_raises_the_fleet_alarm(
        self, estate, app, monkeypatch
    ):
        """Prod: 0 connected for a week, said only as one warning per host. The
        stale `connected = 1` column is what a crash leaves behind."""
        db, *_ = estate
        await _agent_row(db, "a", heartbeat_minutes_ago=30)
        await _agent_row(db, "b", heartbeat_minutes_ago=20)
        monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: _FakeRegistry([], 60))

        agents = (await _get(app, "/dashboard/summary"))["agents"]

        assert agents["enrolled"] == 2
        assert agents["connected"] == 0
        assert agents["offline_alarm"] is True
        assert round(_minutes_ago(agents["offline_since"])) == 20, (
            "since = the last time ANY agent was heard from"
        )

    async def test_a_reconnect_blip_is_not_an_alarm(self, estate, app, monkeypatch):
        db, *_ = estate
        await _agent_row(db, "a", heartbeat_minutes_ago=1)
        monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: _FakeRegistry([], 60))

        agents = (await _get(app, "/dashboard/summary"))["agents"]

        assert agents["offline_alarm"] is False
        assert round(_minutes_ago(agents["offline_since"])) == 1, (
            "the fact is still stated, just not alarmed"
        )

    async def test_a_backend_restart_gives_the_agents_time_to_dial_back(
        self, estate, app, monkeypatch
    ):
        """After an upgrade the last heartbeat is as old as the downtime; the
        agents have not had their five minutes yet."""
        db, *_ = estate
        await _agent_row(db, "a", heartbeat_minutes_ago=30)
        monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: _FakeRegistry([], 1))

        assert (await _get(app, "/dashboard/summary"))["agents"]["offline_alarm"] is False

    async def test_one_connected_agent_is_not_a_dark_fleet(self, estate, app, monkeypatch):
        db, *_ = estate
        await _agent_row(db, "a", heartbeat_minutes_ago=30)
        await _agent_row(db, "b", heartbeat_minutes_ago=0)
        monkeypatch.setattr(
            "homepilot.app_state.get_agent_registry", lambda: _FakeRegistry(["b"], 60)
        )

        agents = (await _get(app, "/dashboard/summary"))["agents"]

        assert agents["offline_alarm"] is False
        assert agents["offline_since"] is None

    async def test_a_revoked_agent_is_not_expected_back(self, estate, app, monkeypatch):
        db, *_ = estate
        await _agent_row(db, "a", heartbeat_minutes_ago=600, revoked=True)
        monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: _FakeRegistry([], 60))

        agents = (await _get(app, "/dashboard/summary"))["agents"]

        assert agents["enrolled"] == 0
        assert agents["offline_alarm"] is False

    async def test_a_stale_connected_column_is_not_believed_per_host(
        self, estate, app, monkeypatch
    ):
        """The per-host "agent offline" lines read inventory. Inventory read the
        best-effort column, so after a restart they said "connected" under a
        fleet line saying none is (#469 class, second endpoint)."""
        db, repo, *_ = estate
        host_id = await repo.create_host(hostname="host-a", host_type="qemu", managed=True)
        await _agent_row(db, "a", heartbeat_minutes_ago=90, connected=1)
        await db.execute("UPDATE hosts SET agent_id = 'a' WHERE id = ?", (host_id,))
        await db.conn.commit()
        monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: _FakeRegistry([], 60))

        rows = (await _get(app, "/inventory"))["items"]

        row = next(r for r in rows if r["id"] == host_id)
        assert row["agent_connected"] is False
        assert round(_minutes_ago(row["agent_offline_since"])) == 90, (
            "the line cannot say since when"
        )


# ── 5. A gone host is not online ───────────────────────────────────────────────


class TestGoneHostsAreNotOnline:
    async def test_a_host_gone_since_before_613_stops_counting_online(self, estate, app):
        """Dev: three hosts absent since August still read `online`, and the
        Overview's donut counted them online. The absence stamp ran once, before
        it learned to blank the status, and never revisits a stamped row."""
        db, repo, *_ = estate
        here = await repo.create_host(
            hostname="still-here", host_type="qemu", proxmox_id=101, source="discovered"
        )
        gone = await repo.create_host(
            hostname="gone-in-august", host_type="qemu", proxmox_id=102, source="hp_created"
        )
        await db.execute(
            "UPDATE hosts SET status = 'online', pve_status = 'running', "
            "absent_since = '2026-08-29T18:00:00Z' WHERE id = ?",
            (gone,),
        )
        await db.execute("UPDATE hosts SET status = 'online' WHERE id = ?", (here,))
        await db.conn.commit()

        await repo.mark_hosts_absent_ids({here}, ("discovered", "hp_created", "imported"))

        by_status = (await _get(app, "/dashboard/summary"))["inventory"]["by_status"]
        assert by_status == {"online": 1, "unknown": 1}, by_status
        row = await repo.get_host(gone)
        assert row is not None
        assert row["absent_since"] == "2026-08-29T18:00:00Z", "the date it went is kept"

    async def test_a_newly_absent_host_reads_unknown(self, estate, app):
        db, repo, *_ = estate
        host = await repo.create_host(hostname="vm-1", host_type="qemu", proxmox_id=103)
        await db.execute("UPDATE hosts SET status = 'online' WHERE id = ?", (host,))
        await db.conn.commit()

        await repo.mark_hosts_absent_ids(set(), ("discovered",))

        assert (await _get(app, "/dashboard/summary"))["inventory"]["by_status"] == {"unknown": 1}

    async def test_a_gone_host_whose_agent_still_answers_keeps_its_status(self, estate):
        """Proxmox no longer lists it, but its agent is dialled in: the machine
        exists and answers, so its agent-reported status is a fact."""
        db, repo, *_ = estate
        host = await repo.create_host(hostname="moved", host_type="qemu", proxmox_id=104)
        await _agent_row(db, "m", heartbeat_minutes_ago=0, connected=1)
        await db.execute(
            "UPDATE hosts SET status = 'online', agent_id = 'm', "
            "absent_since = '2026-10-01T00:00:00Z' WHERE id = ?",
            (host,),
        )
        await db.conn.commit()

        await repo.mark_hosts_absent_ids(set(), ("discovered",))

        row = await repo.get_host(host)
        assert row is not None
        assert row["status"] == "online"


# ── 6. Unknown drift, by reason ────────────────────────────────────────────────


class _Adapter:
    """A host adapter whose agent is or is not dialled in."""

    def __init__(self, connected: bool) -> None:
        self.connected = connected

    async def test_connection(self, host: str) -> bool:
        return self.connected


class TestUnknownDriftByReason:
    async def test_the_summary_splits_unknown_by_what_to_do_next(self, estate, app):
        _db, repo, _store, lifecycle = estate
        reasons = {
            "2026-11-07-noagent": "no_agent",
            "2026-11-07-failed": "check_failed",
            "2026-11-07-probe": "probe_failed",
            "2026-11-07-describe": "nothing_evaluated",
            "2026-11-07-shell": "shell_script_unverifiable",
            "2026-11-07-noexec": "no_executor",
            "2026-11-07-nospec": "no_spec",
        }
        for aid, reason in reasons.items():
            await _applied(lifecycle, aid, "proxmox-api-sequence", PROXMOX_BODY)
            await repo.upsert_drift_check(
                aid, drifted=False, state="unknown", details_json=json.dumps({"reason": reason})
            )
        await _applied(lifecycle, "2026-11-07-ok", "proxmox-api-sequence", PROXMOX_BODY)
        await repo.upsert_drift_check("2026-11-07-ok", drifted=False, state="in_spec")

        drift = (await _get(app, "/dashboard/summary"))["drift"]

        assert drift["unknown"] == 7
        assert drift["unknown_by_reason"] == {
            "no_agent": 1,
            "check_failed": 2,
            "no_proxmox": 1,
            "by_design": 2,
            "other": 1,
        }
        assert sum(drift["unknown_by_reason"].values()) == drift["unknown"]

    async def test_a_host_with_no_agent_is_unverified_for_that_reason(self, estate, app):
        """Prod-shaped: the host's agent is gone, so a host-provision artifact
        cannot be read. The reason has to say so, end to end through the drift
        loop into the summary - not a generic "items unanswered"."""
        _db, repo, store, lifecycle = estate
        await _applied(lifecycle, "2026-11-07-hp", "host-provision", HOST_PROVISION_BODY)
        executor = MagicMock(pve_nodes=[])
        executor.host_adapter = _Adapter(connected=False)

        await DriftReconciler(store, repo, executor, inter_check_delay=0).check_single(
            "2026-11-07-hp"
        )

        row = await repo.get_drift_check("2026-11-07-hp")
        assert row is not None and row["state"] == "unknown"
        assert json.loads(row["details_json"])["reason"] == "no_agent"
        why = (await _get(app, "/dashboard/summary"))["drift"]["unknown_by_reason"]
        assert why["no_agent"] == 1

    async def test_a_failed_probe_is_not_describe_only(self, estate, app):
        """`nothing_evaluated` reads "describe-only (fine)" on the Overview, so it
        must never cover a precheck that was asked and went unanswered."""
        _db, repo, store, lifecycle = estate
        body = (
            "## Plan\nCreate\n## Spec\n```yaml proxmox-api-spec\n"
            "steps:\n  - id: s1\n    method: POST\n    path: /nodes/pve1/qemu\n"
            "    precheck:\n      method: GET\n      path: /nodes/pve1/qemu/100/status/current\n"
            '      skip_if: "response.status == 200"\n```\n'
        )
        await _applied(lifecycle, "2026-11-07-probe", "proxmox-api-sequence", body)
        executor = MagicMock(pve_nodes=[])
        executor.proxmox.call = AsyncMock(side_effect=TimeoutError())

        await DriftReconciler(store, repo, executor, inter_check_delay=0).check_single(
            "2026-11-07-probe"
        )

        row = await repo.get_drift_check("2026-11-07-probe")
        assert row is not None
        assert json.loads(row["details_json"])["reason"] == "probe_failed"
        why = (await _get(app, "/dashboard/summary"))["drift"]["unknown_by_reason"]
        assert why["check_failed"] == 1
        assert why["by_design"] == 0
