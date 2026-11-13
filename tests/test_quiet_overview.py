"""The quiet Overview's server rules, as journeys on the real backend (3.12).

Owner, on prod 3.11.0: "the overview is a bit noisy" - 14 attention lines, 3 of
them decisions. These assert what the operator (and an assistant reading
`get_dashboard_summary`) is ASKED about after the fix, over a real database,
through the real routes:

* a failed run a later run of the same change superseded is not asked about,
  nor is one older than a day - both are counted as left out;
* seven gone machines are ONE line, and forgetting the machines that line names
  forgets exactly those seven and nothing else;
* a machine HomePilot destroyed through an applied change is forgotten by the
  inventory sweep, with a journal row naming the change; one that vanished any
  other way is not;
* "Not now, 7 days" hides a line for 7 days, survives a restart, and ends early
  when the line becomes more serious.

Teeth (each verified by reverting the code - the evidence table is in the PR):
  * drop the later-success check in ``attention._settled`` -> the superseded test;
  * drop the 24 h window in ``attention._runs`` -> the 25-hour test;
  * drop the adoption/template filter in ``attention._gone`` -> the seven-machines test;
  * drop the ``absent_since >= ran_at`` or the name check in ``inventory/retired.py``
    -> the vanished-machine tests; drop the reconciler call -> the destroy test;
  * drop the severity comparison in ``snooze_holds`` -> the severity-rise test;
    drop the ``put_off`` filter -> the snooze test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

ADMIN = {"scope": "*", "role": "admin", "label": "owner", "prefix": "hp_owner000001"}
READER = {"scope": "read", "label": "viewer", "prefix": "hp_viewer00001"}


def _ts(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Registry:
    def __init__(self, connected: list[dict[str, Any]] | None = None) -> None:
        self.connected = connected or []

    def list_connected(self) -> list[dict[str, Any]]:
        return list(self.connected)


def _app(repo: Repository, token: dict[str, Any] | None = None) -> FastAPI:
    from homepilot.agent_hub.router import router as agents_router
    from homepilot.audit.router import router as audit_router
    from homepilot.dashboard.router import router as dashboard_router
    from homepilot.inventory.router import router as inventory_router

    app = FastAPI()
    app.include_router(dashboard_router)
    app.include_router(agents_router)
    app.include_router(audit_router, prefix="/audit")
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = repo
    who = dict(token or ADMIN)
    app.dependency_overrides[require_token] = lambda: who
    return app


async def _call(app: FastAPI, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.request(method, path, json=body)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text


async def _attention(app: FastAPI) -> dict[str, Any]:
    status, body = await _call(app, "GET", "/dashboard/summary")
    assert status == 200, body
    return dict(body["attention"])


@pytest.fixture
async def world(tmp_path: Path, monkeypatch):
    path = str(tmp_path / "homepilot.db")
    db = Database(path)
    await db.connect()
    await run_migrations(db)
    registry = _Registry()
    monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: registry)
    repo = Repository(db)
    w: dict[str, Any] = {"db": db, "repo": repo, "app": _app(repo), "path": path}
    w["registry"] = registry
    try:
        yield w
    finally:
        await w["db"].close()


async def _task(
    db: Database,
    task_id: str,
    artifact_id: str | None,
    status: str,
    age: timedelta,
    action: str = "apply",
    error: str | None = None,
) -> None:
    created = _ts(-age)
    await db.execute(
        "INSERT INTO tasks (id, artifact_id, action, status, created_at, finished_at, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            artifact_id,
            action,
            status,
            created,
            created if status in ("failed", "succeeded") else None,
            error,
        ),
    )
    await db.conn.commit()


async def _host(repo: Repository, name: str, **over: Any) -> str:
    gone = over.pop("gone", None)
    template = over.pop("template", False)
    kwargs: dict[str, Any] = {
        "source": "imported",
        "import_state": "adopted",
        "pve_status": "running",
        "status": "online",
    }
    kwargs.update(over)
    host_id = await repo.create_host(hostname=name, host_type="qemu", **kwargs)
    if gone or template:
        await repo.db.execute(
            "UPDATE hosts SET absent_since = ?, is_template = ? WHERE id = ?",
            (_ts(-gone) if gone else None, int(template), host_id),
        )
        await repo.db.conn.commit()
    return host_id


class TestFailedRuns:
    async def test_a_failure_a_later_success_of_the_same_change_superseded_is_not_asked_about(
        self, world
    ):
        db = world["db"]
        await _task(db, "t-fail-a", "nginx-conf", "failed", timedelta(hours=3), error="exit 1")
        await _task(db, "t-ok-a", "nginx-conf", "succeeded", timedelta(hours=2))
        # A different change's failure, unsettled: still asked about.
        await _task(db, "t-fail-b", "dns-zone", "failed", timedelta(minutes=30), error="refused")
        # A success of ANOTHER change settles nothing.
        await _task(db, "t-ok-c", "other-change", "succeeded", timedelta(minutes=10))

        attention = await _attention(world["app"])

        assert [r["task_id"] for r in attention["failed_runs"]] == ["t-fail-b"]
        line = attention["failed_runs"][0]
        assert line["key"] == "task:t-fail-b" and line["severity"] == "critical"
        assert line["artifact_id"] == "dns-zone" and line["error"] == "refused"
        assert attention["left_out"]["superseded"] == 1
        assert attention["left_out"]["older"] == 0

    async def test_a_25_hour_old_failure_is_not_asked_about_but_is_counted(self, world):
        db = world["db"]
        await _task(db, "t-old", "backup-104", "failed", timedelta(hours=25))
        await _task(db, "t-new", "backup-105", "failed", timedelta(hours=23))

        attention = await _attention(world["app"])

        assert [r["task_id"] for r in attention["failed_runs"]] == ["t-new"]
        assert attention["left_out"] == {
            "superseded": 0,
            "older": 1,
            "since": attention["left_out"]["since"],
        }

    async def test_an_agent_install_is_settled_by_the_agent_now_connected(self, world):
        db, repo = world["db"], world["repo"]
        host_id = await _host(repo, "relay")
        await repo.db.execute("UPDATE hosts SET agent_id = 'ag-relay' WHERE id = ?", (host_id,))
        await db.execute(
            "INSERT INTO tasks (id, artifact_id, action, status, result_json, created_at) "
            "VALUES ('t-inst', NULL, 'install_agent', 'failed', ?, ?)",
            (json.dumps({"host_id": host_id, "hostname": "relay"}), _ts(-timedelta(hours=1))),
        )
        await db.conn.commit()
        assert [r["task_id"] for r in (await _attention(world["app"]))["failed_runs"]] == ["t-inst"]
        world["registry"].connected = [{"agent_id": "ag-relay", "hostname": "relay"}]
        attention = await _attention(world["app"])
        assert attention["failed_runs"] == []
        assert attention["left_out"]["superseded"] == 1

    async def test_a_run_stuck_for_an_hour_is_critical(self, world):
        db = world["db"]
        await _task(db, "t-busy", "a", "running", timedelta(minutes=5))
        await _task(db, "t-stuck", "b", "running", timedelta(minutes=20))
        await _task(db, "t-long", "c", "pending", timedelta(minutes=90))
        stuck = {
            r["task_id"]: r["severity"] for r in (await _attention(world["app"]))["stuck_runs"]
        }
        assert stuck == {"t-stuck": "warning", "t-long": "critical"}

    async def test_the_assistant_reads_the_same_attention_as_the_console(self, world):
        from homepilot.mcp.tools.ops_tools import handle_get_dashboard_summary

        await _task(world["db"], "t-fail", "x", "failed", timedelta(hours=1))
        await _host(world["repo"], "old-1", gone=timedelta(days=2))
        console = await _attention(world["app"])
        assistant = (await handle_get_dashboard_summary({}, {"repo": world["repo"]}))["attention"]
        console.pop("checked_at")
        assistant.pop("checked_at")
        assert assistant == console


SEVEN = ["old-web", "old-db", "old-cache", "probe-1", "probe-2", "retired-104", "lab-x"]


class TestGoneMachines:
    async def test_seven_gone_machines_are_one_line_and_forget_all_forgets_exactly_them(
        self, world
    ):
        repo, app = world["repo"], world["app"]
        for i, name in enumerate(SEVEN):
            await _host(repo, name, gone=timedelta(days=i + 1))
        # Not news, and must survive "Forget all": a live machine, a gone guest
        # nobody adopted, a gone template, and a hand-added device.
        await _host(repo, "llm")
        await _host(repo, "stray-guest", import_state="pending", gone=timedelta(days=1))
        await _host(repo, "tpl-debian", template=True, gone=timedelta(days=1))
        await _host(repo, "nas", source="manual", pve_status=None)

        attention = await _attention(app)
        gone = attention["gone"]
        assert gone["key"] == "gone:machines" and gone["severity"] == "warning"
        assert sorted(h["hostname"] for h in gone["hosts"]) == sorted(SEVEN)

        # "Forget all 7": the Overview runs the Hosts page's forget, per host,
        # over exactly the ids the line names.
        for h in gone["hosts"]:
            status, body = await _call(app, "DELETE", f"/inventory/{h['id']}")
            assert status == 200, body

        left = await repo.db.fetchall("SELECT hostname FROM hosts ORDER BY hostname")
        assert [r["hostname"] for r in left] == ["llm", "nas", "stray-guest", "tpl-debian"]
        assert (await _attention(app))["gone"] is None
        journal = await repo.db.fetchall(
            "SELECT target_host, user_id, source FROM audit_log WHERE action = 'host_forgotten'"
        )
        assert sorted(r["target_host"] for r in journal) == sorted(SEVEN)
        # The row says who forgot them and from where (it used to say the
        # HOST's source, "imported").
        assert {(r["user_id"], r["source"]) for r in journal} == {("owner (hp_owner000001)", "api")}

    async def test_review_lists_only_the_gone_machines(self, world):
        repo, app = world["repo"], world["app"]
        await _host(repo, "old-web", gone=timedelta(days=1))
        await _host(repo, "llm")
        status, body = await _call(app, "GET", "/inventory?absent=true")
        assert status == 200, body
        assert [h["hostname"] for h in body["items"]] == ["old-web"] and body["total"] == 1
        status, body = await _call(app, "GET", "/inventory?absent=false")
        assert [h["hostname"] for h in body["items"]] == ["llm"]


class TestSnooze:
    async def test_not_now_hides_a_line_for_seven_days_and_brings_it_back(self, world):
        app = world["app"]
        await _host(world["repo"], "old-web", gone=timedelta(days=1))
        status, row = await _call(
            app,
            "POST",
            "/dashboard/attention/snoozes",
            {"key": "gone:machines", "severity": "warning"},
        )
        assert status == 200, row
        until = datetime.fromisoformat(row["until"].replace("Z", "+00:00"))
        assert timedelta(days=6, hours=23) < until - datetime.now(UTC) <= timedelta(days=7)

        attention = await _attention(app)
        assert attention["gone"] is None
        (held,) = attention["put_off"]
        assert held["kind"] == "gone" and held["line"]["hosts"][0]["hostname"] == "old-web"
        assert held["snooze"]["until"] == row["until"]
        assert held["snooze"]["by"] == "owner (hp_owner000001)"

        # Seven days later it is back on its own.
        await world["db"].execute(
            "UPDATE attention_snoozes SET until = ? WHERE key = 'gone:machines'",
            (_ts(-timedelta(seconds=1)),),
        )
        await world["db"].conn.commit()
        attention = await _attention(app)
        assert attention["gone"] is not None and attention["put_off"] == []

    async def test_a_snooze_survives_a_restart(self, world):
        await _host(world["repo"], "old-web", gone=timedelta(days=1))
        status, _ = await _call(
            world["app"],
            "POST",
            "/dashboard/attention/snoozes",
            {"key": "gone:machines", "severity": "warning"},
        )
        assert status == 200
        # The process goes away; a new one opens the same database file.
        await world["db"].close()
        db = Database(world["path"])
        await db.connect()
        await run_migrations(db)
        world["db"] = db
        app = _app(Repository(db))
        attention = await _attention(app)
        assert attention["gone"] is None and [p["kind"] for p in attention["put_off"]] == ["gone"]

    async def test_a_snooze_ends_early_when_the_line_becomes_more_serious(self, world):
        app, db = world["app"], world["db"]
        await _task(db, "t-stuck", "b", "running", timedelta(minutes=20))
        status, _ = await _call(
            app,
            "POST",
            "/dashboard/attention/snoozes",
            {"key": "stuck:t-stuck", "severity": "warning"},
        )
        assert status == 200
        assert (await _attention(app))["stuck_runs"] == []
        # Forty minutes later the run has been stuck for over an hour: critical.
        await db.execute(
            "UPDATE tasks SET created_at = ? WHERE id = 't-stuck'", (_ts(-timedelta(minutes=70)),)
        )
        await db.conn.commit()
        attention = await _attention(app)
        assert [(r["task_id"], r["severity"]) for r in attention["stuck_runs"]] == [
            ("t-stuck", "critical")
        ]
        assert attention["put_off"] == []

    async def test_bringing_a_line_back_and_who_may(self, world):
        app = world["app"]
        await _host(world["repo"], "old-web", gone=timedelta(days=1))
        body = {"key": "gone:machines", "severity": "warning"}
        reader = _app(world["repo"], READER)
        status, _ = await _call(reader, "POST", "/dashboard/attention/snoozes", body)
        assert status == 403
        assert (await _call(app, "POST", "/dashboard/attention/snoozes", body))[0] == 200
        assert (await _call(reader, "DELETE", "/dashboard/attention/snoozes/gone:machines"))[
            0
        ] == 403
        status, out = await _call(app, "DELETE", "/dashboard/attention/snoozes/gone:machines")
        assert status == 200 and out == {"key": "gone:machines", "snoozed": False}
        assert (await _attention(app))["gone"] is not None
        status, _ = await _call(app, "DELETE", "/dashboard/attention/snoozes/gone:machines")
        assert status == 404
        actions = [
            r["action"]
            for r in await world["db"].fetchall(
                "SELECT action FROM audit_log WHERE action LIKE 'attention_%' ORDER BY id"
            )
        ]
        assert actions == ["attention_snoozed", "attention_unsnoozed"]

    async def test_a_snooze_with_no_severity_is_refused(self, world):
        status, _ = await _call(
            world["app"],
            "POST",
            "/dashboard/attention/snoozes",
            {"key": "drift", "severity": "loud"},
        )
        assert status == 422


class TestAgentsAndEstate:
    async def test_older_agents_are_one_line_with_the_upgrades_list(self, world):
        from homepilot import __version__

        world["registry"].connected = [
            {"agent_id": "a1", "hostname": "relay", "system_info": {"agent_version": "v3.0.0"}},
            {"agent_id": "a2", "hostname": "dns", "system_info": {}},
            {"agent_id": "a3", "hostname": "llm", "system_info": {"agent_version": __version__}},
        ]
        line = (await _attention(world["app"]))["agents_behind"]
        assert line["key"] == f"agents:behind:{__version__}"
        assert sorted(a["hostname"] for a in line["agents"]) == ["dns", "relay"]
        status, upgrades = await _call(world["app"], "GET", "/agents/upgrades")
        assert status == 200
        assert upgrades == line["agents"]

    async def test_the_estate_counts_running_stopped_and_watched_never_gone(self, world):
        repo = world["repo"]
        a = await _host(repo, "llm")
        await _host(repo, "atlas")
        await _host(repo, "forge-dev", pve_status="stopped", status="offline")
        await _host(repo, "old-web", gone=timedelta(days=1), pve_status="unknown")
        await _host(repo, "nas", source="manual", pve_status=None, status="online")
        await repo.db.execute("UPDATE hosts SET agent_id = 'ag-llm' WHERE id = ?", (a,))
        await repo.db.conn.commit()
        world["registry"].connected = [{"agent_id": "ag-llm", "hostname": "llm"}]
        status, summary = await _call(world["app"], "GET", "/dashboard/summary")
        assert status == 200
        assert summary["inventory"]["machines"] == {
            "running": 3,
            "stopped": 1,
            "gone": 1,
            "other": 0,
            "watched": 1,
        }


class TestJournalSplit:
    async def test_people_and_system_are_two_sets_that_add_up(self, world):
        repo, app = world["repo"], world["app"]
        await repo.log_audit(user_id="system", source="reconciler:drift", action="check_drift")
        await repo.log_audit(user_id="system", source="reconciler:inventory", action="refresh")
        await repo.log_audit(
            user_id="system", source="migration:41", action="duplicate_host_retired"
        )
        await repo.log_audit(user_id="owner (hp_owner000001)", source="api", action="host_added")
        await repo.log_audit(
            user_id="claude (hp_asst0000001)", source="lifecycle", action="withdraw"
        )
        status, people = await _call(app, "GET", "/audit?actor=people")
        assert status == 200
        assert [r["action"] for r in people["items"]] == ["withdraw", "host_added"]
        status, system = await _call(app, "GET", "/audit?actor=system")
        assert system["total"] == 3
        status, everything = await _call(app, "GET", "/audit")
        assert everything["total"] == people["total"] + system["total"]
        status, _ = await _call(app, "GET", "/audit?actor=robots")
        assert status == 422

    async def test_the_assistant_filters_the_journal_the_same_way(self, world):
        from homepilot.mcp.tools.ops_tools import handle_get_audit_log

        repo = world["repo"]
        await repo.log_audit(user_id="system", source="reconciler:drift", action="check_drift")
        await repo.log_audit(user_id="owner (hp_owner000001)", source="api", action="host_added")
        out = await handle_get_audit_log({"actor": "people"}, {"repo": repo})
        _status, rest = await _call(world["app"], "GET", "/audit?actor=people")
        assert out == rest


# ── HomePilot's own destroys are forgotten by the sweep ──────────────────────


@pytest.fixture
async def pve_world(tmp_path: Path):
    """The real chain: REST propose/approve/apply over a fake Proxmox that
    destroys what it is told to, and the real inventory sweep."""
    from homepilot.artifacts.lifecycle import ArtifactLifecycle
    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.artifacts.store import ArtifactStore
    from homepilot.executor.orchestrator import ArtifactExecutor
    from homepilot.inventory.service import InventoryService
    from homepilot.reconciler.inventory import InventoryReconciler
    from homepilot.tasks.repository import TaskRepository
    from homepilot.tasks.runner import TaskRunner
    from tests.test_target_identity import Cluster, _meta

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = Cluster()
    for vmid, name in ((150, "probe-1"), (151, "stray-1"), (152, "keeper"), (153, "pending-1")):
        pve.add(vmid, name, "stopped", _meta(1_700_000_000 + vmid))
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store, repository=repo)
    lifecycle._proxmox = pve
    executor = ArtifactExecutor(
        store=store, lifecycle=lifecycle, repo=repo, proxmox=pve, vault=None
    )
    task_repo = TaskRepository(db)
    runner = TaskRunner(
        repo=task_repo, lifecycle=lifecycle, executor=executor, apply_reconciler=None, store=store
    )
    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.repo = repo
    app.state.proxmox = pve
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
    app.state.artifact_executor = executor
    app.state.task_runner = runner
    app.state.task_repo = task_repo
    app.dependency_overrides[require_token] = lambda: dict(ADMIN)
    inventory = InventoryService(repo, pve)
    assert (await inventory.refresh_inventory())["complete"]
    await db.execute("UPDATE hosts SET import_state = 'adopted', managed = 1")
    await db.conn.commit()
    w = {
        "repo": repo,
        "pve": pve,
        "store": store,
        "runner": runner,
        "sweep": InventoryReconciler(inventory_service=inventory, repo=repo, artifact_store=store),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        w["client"] = client
        try:
            yield w
        finally:
            await runner.drain(timeout=5.0)
            await db.close()


def _destroy_spec(name: str, vmid: int) -> dict[str, Any]:
    return {
        "kind": "proxmox-api-sequence",
        "intent": f"Retire {name}",
        "body": (
            f"Retire {name}.\n\n```yaml proxmox-api-spec\nsteps:\n  - id: destroy\n"
            "    method: DELETE\n    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}\n```\n"
        ),
        "target": {"kind": "vm", "vmid": vmid, "node": "pve1", "host": name},
        "idempotence": "declared-natural",
        "requires_snapshot": False,
    }


async def _approved(w: dict[str, Any], spec: dict[str, Any]) -> str:
    resp = await w["client"].post("/artifacts", json=spec)
    assert resp.status_code == 200, resp.text
    aid = str(resp.json()["id"])
    resp = await w["client"].post(f"/artifacts/{aid}/approve", json={})
    assert resp.status_code == 200, resp.text
    return aid


async def _applied(w: dict[str, Any], spec: dict[str, Any]) -> str:
    import asyncio

    aid = await _approved(w, spec)
    resp = await w["client"].post(f"/artifacts/{aid}/apply", json={})
    assert resp.status_code == 202, resp.text
    for _ in range(300):
        task = await w["runner"].repo.get_task(resp.json()["task_id"])
        if task and task["status"] not in ("pending", "running"):
            assert task["status"] == "succeeded", task
            return aid
        await asyncio.sleep(0.02)
    raise AssertionError("the destroy never finished")


async def _names(repo: Repository) -> dict[str, bool]:
    """Every guest row left, and whether it is marked gone."""
    rows = await repo.db.fetchall(
        "SELECT hostname, absent_since FROM hosts WHERE host_type != 'node'"
    )
    return {r["hostname"]: r["absent_since"] is not None for r in rows}


class TestHomePilotsOwnDestroys:
    async def test_a_machine_homepilot_destroyed_is_forgotten_and_a_vanished_one_is_not(
        self, pve_world
    ):
        w = pve_world
        aid = await _applied(w, _destroy_spec("probe-1", 150))
        assert 150 not in w["pve"].guests
        # stray-1 is deleted in the Proxmox UI: nothing of HomePilot's did it.
        w["pve"].guests.pop(151)
        # pending-1 has an approved destroy whose only run FAILED, and then vanished.
        failed = await _approved(w, _destroy_spec("pending-1", 153))
        await w["repo"].db.execute(
            "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
            "VALUES ('t-failed', ?, 'apply', 'failed', ?)",
            (failed, _ts(-timedelta(minutes=1))),
        )
        await w["repo"].db.conn.commit()
        w["pve"].guests.pop(153)

        result = await w["sweep"].run()

        assert result.success, result.details
        assert result.details["forgotten_destroyed"] == ["probe-1"]
        assert await _names(w["repo"]) == {"stray-1": True, "keeper": False, "pending-1": True}
        (row,) = await w["repo"].db.fetchall(
            "SELECT user_id, source, artifact_id, details_json FROM audit_log "
            "WHERE action = 'host_forgotten' AND target_host = 'probe-1'"
        )
        assert (row["user_id"], row["source"], row["artifact_id"]) == (
            "system",
            "reconciler:inventory",
            aid,
        )
        assert json.loads(row["details_json"])["reason"] == "destroyed by an applied change"
        # A second sweep changes nothing: the vanished machines stay for the operator.
        assert "forgotten_destroyed" not in (await w["sweep"].run()).details
        assert await _names(w["repo"]) == {"stray-1": True, "keeper": False, "pending-1": True}

    async def test_a_reused_vmid_does_not_make_someone_elses_machine_ours(self, pve_world):
        """HomePilot destroyed probe-1 at 150 and PVE gave 150 to a new guest,
        which then ALSO vanished. The destroy explains probe-1, never the new one."""
        w = pve_world
        from tests.test_target_identity import _meta

        await _applied(w, _destroy_spec("probe-1", 150))
        await w["sweep"].run()
        w["pve"].add(150, "alice-db", "running", _meta(1_705_000_000))
        await w["sweep"].run()
        assert (await _names(w["repo"]))["alice-db"] is False
        w["pve"].guests.pop(150)
        await w["sweep"].run()
        names = await _names(w["repo"])
        assert names.get("alice-db") is True and "probe-1" not in names

    async def test_a_machine_that_was_already_gone_before_the_destroy_ran_is_kept(self, pve_world):
        """The run did not explain an absence that came first; keeper, whose run
        came before its absence, is the control."""
        w = pve_world
        stray = await _approved(w, _destroy_spec("stray-1", 151))
        keeper = await _approved(w, _destroy_spec("keeper", 152))
        # stray-1 vanished two hours ago, outside HomePilot.
        w["pve"].guests.pop(151)
        await w["sweep"].run()
        await w["repo"].db.execute(
            "UPDATE hosts SET absent_since = ? WHERE hostname = 'stray-1'",
            (_ts(-timedelta(hours=2)),),
        )
        # Both destroys "succeeded" an hour ago (seeded, to isolate the timing
        # rule: PVE would have refused the one for a machine already gone).
        for i, aid in enumerate((stray, keeper)):
            await w["repo"].db.execute(
                "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
                "VALUES (?, ?, 'apply', 'succeeded', ?)",
                (f"t-{i}", aid, _ts(-timedelta(hours=1))),
            )
        await w["repo"].db.conn.commit()
        w["pve"].guests.pop(152)
        await w["sweep"].run()
        names = await _names(w["repo"])
        assert names["stray-1"] is True
        assert "keeper" not in names
