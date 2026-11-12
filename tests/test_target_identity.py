"""An approved change never acts on a different machine than it was written for (#813).

Found on prod 3.10.0: a one-step ``DELETE /nodes/<node>/qemu/<vmid>`` approved
two months earlier for a throw-away probe guest sat in the Inbox as "Ready to
run" while a friend's live machine held that VMID; the typed confirmation asked
for the number both machines shared. Three more destroys pointed at VMIDs that
were free - they would have deleted whatever was created there next.

These are journeys through the real lifecycle, task runner, executor and REST
surface over a fake cluster (``FakeCluster`` at the ProxmoxClient boundary):
the outcome asserted is what an operator or the friend gets - the machine still
there, nothing sent to Proxmox, a refusal that names who is at the VMID now -
never merely that a call returned.

Teeth (each verified by reverting the guard; see the PR body):
  * drop the ``check_run`` call from ``ArtifactExecutor.apply`` -> auto-apply,
    the every-entry-point test (direct apply, composite steps) and the
    structural test fail; drop the task runner's preflight instead -> every
    REST / MCP / CLI journey fails (the run starts and the 409 is gone);
  * drop the name comparison in ``target_identity._problem`` -> the reused-VMID,
    free-VMID, approve, rollback and confirm tests fail; drop the ctime
    comparison -> the same-name journey fails;
  * drop the stale check from ``check_run`` -> every stale path test fails;
  * treat "nothing recorded" as fine -> the three legacy journeys fail;
  * drop the replay guard or the rollback guard -> the entry-point, revoke and
    structural tests fail;
  * make ``confirm_name`` fall back to the VMID again -> the summary test fails.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts.file_store import ArtifactFileStore
from homepilot.artifacts.lifecycle import ArtifactLifecycle, LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.artifacts.summary import summarize
from homepilot.auth.deps import require_token
from homepilot.auth.principal import Principal
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.inventory.service import InventoryService
from homepilot.mcp.server import _handle_tool
from homepilot.reconciler.apply import ApplyReconciler
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner
from tests.test_power_controls import ADMIN, NODE, FakeCluster

pytestmark = pytest.mark.asyncio

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"
PROBE_CTIME = 1_700_000_000
FRIEND_CTIME = 1_705_000_000


def _meta(ctime: int) -> dict[str, Any]:
    return {"meta": f"creation-qemu=9.0.2,ctime={ctime}", "cores": 1}


class Cluster(FakeCluster):
    """FakeCluster that also destroys and creates guests, as PVE does.

    Every write lands in ``posts`` BEFORE it is acted on, so "nothing was sent"
    is ``writes == []`` and nothing else.
    """

    @property
    def writes(self) -> list[str]:
        return [p for p, _ in self.posts]

    async def call(self, method, path, body=None, query=None):  # type: ignore[override]
        p = "/" + path.lstrip("/")
        parts = p.strip("/").split("/")
        if method == "DELETE" and len(parts) == 4 and parts[2] in ("qemu", "lxc"):
            self.posts.append((p, body))
            self.guests.pop(int(parts[3]))
            return {"data": self._task(parts[1], "OK")}
        if method == "POST" and len(parts) == 3 and parts[2] in ("qemu", "lxc"):
            self.posts.append((p, body))
            data = dict(body or {})
            self.add(
                int(data["vmid"]),
                str(data.get("name") or "new"),
                "stopped",
                _meta(1_710_000_000),
                guest_type=parts[2],
                node=parts[1],
            )
            return {"data": self._task(parts[1], "OK")}
        if method == "PUT" and len(parts) == 5 and parts[4] == "config":
            self.posts.append((p, body))
            self.guests[int(parts[3])]["config"].update(dict(body or {}))
            return {"data": None}
        return await super().call(method, path, body, query)


def _spec(intent: str, steps: str, *, vmid: int = 150, host: str | None = "probe-1", **extra):
    target: dict[str, Any] = {"kind": "vm", "vmid": vmid, "node": NODE}
    if host:
        target["host"] = host
    spec: dict[str, Any] = {
        "kind": "proxmox-api-sequence",
        "intent": intent,
        "body": f"{intent}.\n\n```yaml proxmox-api-spec\nsteps:\n{steps}```\n",
        "target": target,
        "idempotence": "declared-natural",
        "requires_snapshot": False,
    }
    spec.update(extra)
    return spec


DESTROY = """  - id: destroy
    method: DELETE
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}
"""
SET_CORES = """  - id: cores
    method: PUT
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/config
    body:
      cores: 2
"""
CORES_ROLLBACK = """
```yaml proxmox-api-rollback
steps:
  - id: cores-back
    method: PUT
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/config
    body:
      cores: 1
```
"""
CREATE_300 = """  - id: create
    method: POST
    path: /nodes/{{ target.node }}/qemu
    body:
      vmid: 300
      name: fresh-300
"""


@pytest.fixture
async def world(tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = Cluster()
    pve.add(150, "probe-1", "stopped", _meta(PROBE_CTIME))
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
    token: dict[str, Any] = dict(ADMIN)
    app.dependency_overrides[require_token] = lambda: token
    ctx = {
        "repo": repo,
        "lifecycle": lifecycle,
        "store": store,
        "task_runner": runner,
        "task_repo": task_repo,
        "_mcp_caller_id": "assistant (hp_asst0000001)",
        "_mcp_token_scope": "full",
    }
    w = {
        "repo": repo,
        "pve": pve,
        "store": store,
        "lifecycle": lifecycle,
        "executor": executor,
        "runner": runner,
        "ctx": ctx,
        "inventory": InventoryService(repo, pve),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        w["client"] = client
        try:
            yield w
        finally:
            await runner.drain(timeout=5.0)
            await db.close()


async def _propose(w: dict[str, Any], spec: dict[str, Any]) -> str:
    resp = await w["client"].post("/artifacts", json=spec)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


async def _approved(w: dict[str, Any], spec: dict[str, Any]) -> str:
    aid = await _propose(w, spec)
    resp = await w["client"].post(f"/artifacts/{aid}/approve", json={})
    assert resp.status_code == 200, resp.text
    return aid


def _fm(w: dict[str, Any], aid: str) -> dict[str, Any]:
    return w["store"].read(aid)[0]


async def _backdate(w: dict[str, Any], aid: str, days: int) -> None:
    """Age the approval, as two months in the Inbox would (off the loop: a git commit)."""
    store = w["store"]

    def _write() -> None:
        fm, body = store.read(aid)
        at = (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        fm["approved_by"]["at"] = at
        store.write(aid, ArtifactFileStore.serialize_frontmatter(fm), body, "backdate")

    await asyncio.to_thread(_write)


def _swap_in_friend(pve: Cluster, *, name: str = "alice-web", ctime: int = FRIEND_CTIME) -> None:
    """PVE reuses VMID 150: the probe is gone and another machine has the number."""
    pve.guests.pop(150)
    pve.add(150, name, "stopped", _meta(ctime))


async def _settled_task(w: dict[str, Any], aid: str, task_id: str | None = None) -> dict[str, Any]:
    """The artifact's run, once finished: the task named, else its newest."""
    for _ in range(300):
        if task_id:
            row = await w["runner"].repo.get_task(task_id)
            rows = [row] if row else []
        else:
            rows = await w["runner"].repo.list_tasks(artifact_id=aid, limit=1)
        if rows and rows[0]["status"] not in ("pending", "running"):
            return dict(rows[0])
        await asyncio.sleep(0.02)
    raise AssertionError(f"no finished task for {aid}")


async def _audit(w: dict[str, Any], aid: str) -> list[str]:
    rows = await w["repo"].db.fetchall(
        "SELECT action FROM audit_log WHERE artifact_id = ? ORDER BY id", (aid,)
    )
    return [r["action"] for r in rows]


# ── who the change is for is recorded when it is written ─────────────────────


class TestIdentityIsRecordedAtPropose:
    async def test_the_guest_name_type_and_creation_time_are_recorded(self, world):
        aid = await _propose(world, _spec("Destroy the probe", DESTROY))
        recorded = _fm(world, aid)["target_identity"]["guests"]["150"]
        assert recorded == {
            "vmid": 150,
            "node": NODE,
            "type": "qemu",
            "name": "probe-1",
            "ctime": PROBE_CTIME,
        }

    async def test_an_edit_cannot_rewrite_the_recorded_machine(self, world):
        aid = await _propose(world, _spec("Destroy the probe", DESTROY))
        fm, body = world["store"].read(aid)
        fm["target_identity"]["guests"]["150"]["name"] = "alice-web"
        with pytest.raises(LifecycleError, match="target_identity"):
            await world["lifecycle"].edit_content(aid, fm, body, "someone")


# ── (a)/(b) the machine at the VMID is not the one the change was written for ─


class TestARunForAnotherMachineIsRefused:
    async def test_a_destroy_for_a_reused_vmid_is_refused_naming_who_is_there(self, world):
        pve = world["pve"]
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        _swap_in_friend(pve)

        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "alice-web" in detail and "probe-1" in detail, detail
        assert "Nothing was sent" in detail
        # The friend's machine is there, untouched; nothing reached Proxmox.
        assert pve.guests[150]["name"] == "alice-web"
        assert pve.writes == []
        fm = _fm(world, aid)
        assert fm["status"] == "proposed", "a refused approval stays Ready to run"
        assert fm["returned_for_review"]["code"] == "changed"
        assert "alice-web" in fm["returned_for_review"]["reason"]
        assert "apply_refused" in await _audit(world, aid)
        # The audit row says THAT it went back to review, as a bool - not a
        # copy of the artifact's frontmatter.
        row = await world["repo"].db.fetchone(
            "SELECT details_json FROM audit_log WHERE artifact_id = ? AND action = 'apply_refused'",
            (aid,),
        )
        details = json.loads(row["details_json"])
        assert details["returned_for_review"] is True, details
        assert set(details) == {"code", "reason", "returned_for_review"}, details

    async def test_a_different_machine_under_the_same_name_is_refused(self, world):
        pve = world["pve"]
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        _swap_in_friend(pve, name="probe-1", ctime=FRIEND_CTIME)

        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})

        assert resp.status_code == 409, resp.text
        assert "created at a different time" in resp.json()["detail"]
        assert pve.writes == [] and 150 in pve.guests

    async def test_a_destroy_for_a_vmid_that_is_free_now_is_refused(self, world):
        pve = world["pve"]
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        pve.guests.pop(150)

        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})

        assert resp.status_code == 409, resp.text
        assert "no longer exists" in resp.json()["detail"]
        assert pve.writes == []
        # And the next machine given the number is never touched by it.
        pve.add(150, "next-guest", "running", _meta(FRIEND_CTIME))
        resp = await world["client"].post(f"/artifacts/{aid}/approve", json={})
        assert resp.status_code == 400 and "next-guest" in resp.text, resp.text
        assert pve.writes == []

    async def test_the_same_machine_runs(self, world):
        pve = world["pve"]
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 202, resp.text
        task = await _settled_task(world, aid, resp.json().get("task_id"))
        assert task["status"] == "succeeded", task
        assert pve.writes == [f"/nodes/{NODE}/qemu/150"]
        assert 150 not in pve.guests

    async def test_approving_a_change_whose_machine_changed_is_refused(self, world):
        aid = await _propose(world, _spec("Destroy the probe", DESTROY))
        _swap_in_friend(world["pve"])
        resp = await world["client"].post(f"/artifacts/{aid}/approve", json={})
        assert resp.status_code == 400, resp.text
        assert "alice-web" in resp.json()["detail"]
        assert _fm(world, aid)["status"] == "proposed"

    async def test_a_backup_of_a_guest_named_only_in_its_body_is_checked(self, world):
        pve = world["pve"]
        pve.add(160, "alice-files", "running", _meta(FRIEND_CTIME))
        steps = (
            "  - id: backup\n    method: POST\n    path: /nodes/{{ target.node }}/vzdump\n"
            '    body: {vmid: "150,160", storage: fast1}\n'
        )
        aid = await _approved(world, _spec("Back up two guests", steps))
        assert set(_fm(world, aid)["target_identity"]["guests"]) == {"150", "160"}
        pve.guests.pop(160)
        pve.add(160, "someone-else", "running", _meta(FRIEND_CTIME + 1))

        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})

        assert resp.status_code == 409, resp.text
        assert "someone-else" in resp.json()["detail"]
        assert pve.writes == []

    async def test_a_change_that_creates_a_guest_is_refused_when_the_vmid_is_taken(self, world):
        pve = world["pve"]
        spec = _spec("Make a guest", CREATE_300, vmid=300, host=None)
        aid = await _approved(world, spec)
        pve.add(300, "alice-db", "running", _meta(FRIEND_CTIME))

        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})

        assert resp.status_code == 409, resp.text
        assert "taken by 'alice-db'" in resp.json()["detail"]
        assert pve.writes == []

    async def test_a_create_then_its_rollback_find_the_guest_it_made(self, world):
        pve = world["pve"]
        rollback = (
            "\n```yaml proxmox-api-rollback\nsteps:\n"
            + DESTROY.replace("{{ target.vmid }}", "300")
            + "```\n"
        )
        spec = _spec("Make a guest", CREATE_300, vmid=300, host=None)
        spec["body"] += rollback
        aid = await _approved(world, spec)
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 202, resp.text
        assert (await _settled_task(world, aid, resp.json().get("task_id")))[
            "status"
        ] == "succeeded"
        # What the run LEFT is what a rollback is checked against.
        assert _fm(world, aid)["target_identity"]["guests"]["300"]["name"] == "fresh-300"
        outcome = await world["executor"].revoke(aid, "owner")
        assert outcome.rolled_back, outcome.reason
        assert 300 not in pve.guests

    async def test_a_rollback_for_a_machine_that_changed_is_not_run(self, world):
        pve = world["pve"]
        spec = _spec("More cores", SET_CORES)
        spec["body"] += CORES_ROLLBACK
        aid = await _approved(world, spec)
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 202, resp.text
        assert (await _settled_task(world, aid, resp.json().get("task_id")))[
            "status"
        ] == "succeeded"
        _swap_in_friend(pve)
        sent = list(pve.writes)

        outcome = await world["executor"].revoke(aid, "owner")

        assert outcome.rolled_back is False
        assert "alice-web" in outcome.reason and "not run" in outcome.reason
        assert pve.writes == sent, "the rollback reached the friend's machine"
        assert _fm(world, aid)["status"] == "revoked"


# ── (e) a change proposed before identities were recorded ────────────────────


class TestALegacyChangeIsNotRunSilently:
    async def _legacy(self, world, spec):
        lifecycle = world["lifecycle"]
        lifecycle._proxmox = None  # what 3.10 did: nothing recorded
        try:
            aid = await _approved(world, spec)
        finally:
            lifecycle._proxmox = world["pve"]
        assert "target_identity" not in _fm(world, aid)
        return aid

    async def test_it_goes_back_to_review_naming_the_machine_there_now(self, world):
        pve = world["pve"]
        aid = await self._legacy(world, _spec("Destroy the probe", DESTROY, host=None))
        _swap_in_friend(pve)

        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "nothing records which machine VMID 150 was" in detail
        assert "alice-web" in detail
        assert pve.writes == []
        assert _fm(world, aid)["returned_for_review"]["code"] == "unconfirmed"

    async def test_approving_it_again_confirms_the_live_machine_and_then_it_runs(self, world):
        pve = world["pve"]
        aid = await self._legacy(world, _spec("Destroy the probe", DESTROY))
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 409 and pve.writes == []

        resp = await world["client"].post(f"/artifacts/{aid}/approve", json={})
        assert resp.status_code == 200, resp.text
        assert _fm(world, aid)["target_identity"]["guests"]["150"]["name"] == "probe-1"
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 202, resp.text
        assert (await _settled_task(world, aid, resp.json().get("task_id")))[
            "status"
        ] == "succeeded"
        assert pve.writes == [f"/nodes/{NODE}/qemu/150"]

    async def test_a_legacy_change_naming_another_machine_cannot_be_reapproved(self, world):
        pve = world["pve"]
        aid = await self._legacy(world, _spec("Destroy the probe", DESTROY))
        _swap_in_friend(pve)
        await world["client"].post(f"/artifacts/{aid}/apply", json={})

        resp = await world["client"].post(f"/artifacts/{aid}/approve", json={})

        assert resp.status_code == 400, resp.text
        assert "names 'probe-1'" in resp.json()["detail"] and "alice-web" in resp.json()["detail"]
        assert pve.writes == []


# ── (c) a stale approval cannot be run by any path ───────────────────────────


class TestAStaleApprovalDoesNotRun:
    async def _stale(self, world) -> str:
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        await _backdate(world, aid, 74)
        return aid

    async def _assert_refused_and_back_in_review(self, world, aid):
        assert world["pve"].writes == []
        assert 150 in world["pve"].guests
        fm = _fm(world, aid)
        assert fm["status"] == "proposed"
        assert fm["returned_for_review"]["code"] == "stale"
        assert "approved 74 days ago" in fm["returned_for_review"]["reason"]

    async def test_rest_apply(self, world):
        aid = await self._stale(world)
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 409, resp.text
        assert "approved 74 days ago" in resp.json()["detail"]
        await self._assert_refused_and_back_in_review(world, aid)

    async def test_rest_replay_of_an_approved_change(self, world):
        aid = await self._stale(world)
        resp = await world["client"].post(f"/artifacts/{aid}/replay")
        assert resp.status_code == 409, resp.text
        await self._assert_refused_and_back_in_review(world, aid)

    async def test_replay_of_an_applied_change_with_an_old_approval(self, world):
        pve = world["pve"]
        aid = await _approved(world, _spec("More cores", SET_CORES))
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert (await _settled_task(world, aid, resp.json().get("task_id")))[
            "status"
        ] == "succeeded", resp.text
        await _backdate(world, aid, 30)
        sent = list(pve.writes)
        resp = await world["client"].post(f"/artifacts/{aid}/replay")
        assert resp.status_code == 409, resp.text
        assert "Propose the change again" in resp.json()["detail"]
        assert pve.writes == sent
        assert _fm(world, aid)["status"] == "applied"

    async def test_mcp_apply_and_replay(self, world):
        for tool in ("apply_artifact", "replay_artifact"):
            aid = await self._stale(world)
            with pytest.raises(ValueError, match="approved 74 days ago"):
                await _handle_tool(tool, {"artifact_id": aid}, world["ctx"])
            await self._assert_refused_and_back_in_review(world, aid)

    async def test_cli_apply(self, world):
        """`hp artifacts apply` is a REST client of /apply; its refusal is that 409."""
        from homepilot.cli.main import BackendRefusedError, _follow_task

        aid = await self._stale(world)
        client = world["client"]

        async def call(method: str, path: str, body: Any = None) -> Any:
            resp = await client.request(method, path, json=body)
            if resp.status_code >= 400:
                raise BackendRefusedError(resp.status_code, path, resp.json()["detail"])
            return resp.json()

        with pytest.raises(BackendRefusedError, match="approved 74 days ago"):
            await _follow_task(call, f"/artifacts/{aid}/apply", "POST", {})
        await self._assert_refused_and_back_in_review(world, aid)

    async def test_auto_apply(self, world):
        aid = await self._stale(world)
        reconciler = ApplyReconciler(
            world["store"], world["repo"], world["executor"], auto_apply_enabled=True
        )
        result = await reconciler.run()
        assert result.details["failed"] == 1, result.details
        await self._assert_refused_and_back_in_review(world, aid)
        # Back in review, auto-apply leaves it alone on the next tick.
        again = await reconciler.run()
        assert again.details.get("reason") == "no_approved_artifacts", again.details

    async def test_approving_again_is_a_fresh_decision_that_runs(self, world):
        aid = await self._stale(world)
        await world["client"].post(f"/artifacts/{aid}/apply", json={})
        resp = await world["client"].post(f"/artifacts/{aid}/approve", json={})
        assert resp.status_code == 200, resp.text
        assert "returned_for_review" not in _fm(world, aid)
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 202, resp.text
        assert (await _settled_task(world, aid, resp.json().get("task_id")))[
            "status"
        ] == "succeeded"

    async def test_zero_days_means_approvals_never_expire(self, world, monkeypatch):
        from homepilot.config import get_settings

        monkeypatch.setattr(get_settings(), "approval_max_age_days", 0)
        aid = await self._stale(world)
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 202, resp.text
        assert (await _settled_task(world, aid, resp.json().get("task_id")))[
            "status"
        ] == "succeeded"

    async def test_recheck_sends_it_back_without_running_anything(self, world):
        aid = await self._stale(world)
        resp = await world["client"].post(f"/artifacts/{aid}/recheck")
        assert resp.status_code == 200, resp.text
        assert resp.json()["may_run"] is False and resp.json()["status"] == "proposed"
        await self._assert_refused_and_back_in_review(world, aid)
        fresh = await _approved(world, _spec("Destroy the probe", DESTROY))
        resp = await world["client"].post(f"/artifacts/{fresh}/recheck")
        assert resp.json() == {"id": fresh, "may_run": True, "status": "approved"}


# ── (d) the confirm dialog's facts ───────────────────────────────────────────


class TestTheConfirmAsksForTheLiveName:
    async def test_a_matching_machine_is_confirmed_by_its_live_name_with_its_state(self, world):
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        await world["inventory"].refresh_inventory()
        row = await world["repo"].get_host_by_hostname("probe-1")
        await world["repo"].update_host(str(row["id"]), owner="alice")

        body = (await world["client"].get(f"/artifacts/{aid}/target")).json()

        assert body["may_run"] is True
        assert body["confirm"]["name"] == "probe-1"
        assert body["confirm"]["status"] == "stopped"
        assert body["confirm"]["owner"] == "alice"
        assert body["approval"]["limit_days"] == 7
        assert body["approval"]["age_days"] is not None and body["approval"]["stale"] is None

    async def test_another_machine_at_the_vmid_offers_nothing_to_confirm(self, world):
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        _swap_in_friend(world["pve"])

        body = (await world["client"].get(f"/artifacts/{aid}/target")).json()

        assert body["may_run"] is False
        assert body["confirm"] is None
        assert body["identity"]["code"] == "changed"
        assert "alice-web" in body["identity"]["message"]
        assert world["pve"].writes == []

    async def test_a_stale_approval_says_its_age(self, world):
        aid = await _approved(world, _spec("Destroy the probe", DESTROY))
        await _backdate(world, aid, 74)
        body = (await world["client"].get(f"/artifacts/{aid}/target")).json()
        assert body["may_run"] is False
        assert int(body["approval"]["age_days"]) == 74
        assert "approved 74 days ago" in body["approval"]["stale"]
        # A read: it is still approved.
        assert _fm(world, aid)["status"] == "approved"

    async def test_the_summary_never_asks_for_a_vmid(self, world):
        fm = _spec("Destroy", DESTROY, host=None)
        summary = summarize(fm, fm["body"])
        assert summary["danger"] == "destroy"
        assert summary["confirm_name"] is None
        assert summary["danger_vmid"] == 150


# ── the guard sits on EVERY execution path ───────────────────────────────────


class TestEveryExecutionPathAsksTheGuard:
    """Each way a change can execute, driven for real, meets ``check_run``.

    A new entry point that reaches a kind executor without passing the guard
    fails the structural test below; one that goes through apply/replay/revoke
    is covered by construction.
    """

    @pytest.fixture
    def refused(self, monkeypatch):
        asked: list[str] = []

        async def _refuse(self, fm, body, action):
            asked.append(action)
            if fm.get("kind") == "composite":
                return None  # let the composite run, so its steps meet the guard
            return "changed", "VMID 150 is now 'sentinel-guest'"

        monkeypatch.setattr(ArtifactExecutor, "check_run", _refuse)
        return asked

    async def test_each_entry_point_is_refused_and_sends_nothing(self, world, refused):
        client, ctx, executor, runner = (
            world["client"],
            world["ctx"],
            world["executor"],
            world["runner"],
        )
        spec = _spec("Destroy the probe", DESTROY)

        async def rest_apply(aid):
            return (await client.post(f"/artifacts/{aid}/apply", json={})).status_code

        async def rest_replay(aid):
            return (await client.post(f"/artifacts/{aid}/replay")).status_code

        async def mcp(tool):
            async def go(aid):
                with pytest.raises(ValueError, match="sentinel-guest"):
                    await _handle_tool(tool, {"artifact_id": aid}, ctx)
                return 409

            return go

        async def direct(aid):
            return 409 if (await executor.apply(aid, "owner")).refused else 202

        async def direct_replay(aid):
            return 409 if (await executor.replay(aid)).refused else 202

        async def auto(aid):
            task = await runner.run_apply_now(aid, "auto-apply")
            return 409 if "sentinel-guest" in str(task.get("error")) else 202

        paths = {
            "rest apply (UI, CLI)": rest_apply,
            "rest replay": rest_replay,
            "mcp apply_artifact": await mcp("apply_artifact"),
            "mcp replay_artifact": await mcp("replay_artifact"),
            "executor.apply (composite steps)": direct,
            "executor.replay": direct_replay,
            "auto-apply": auto,
        }
        for name, run in paths.items():
            aid = await _approved(world, spec)
            assert await run(aid) == 409, name
            assert world["pve"].writes == [], f"{name} sent something"
            assert _fm(world, aid)["status"] == "proposed", name

        # approve-and-apply: approves, then the run is refused before it starts.
        aid = await _propose(world, spec)
        resp = await client.post(f"/artifacts/{aid}/approve-and-apply", json={})
        assert resp.status_code == 409 and "sentinel-guest" in resp.text, resp.text
        assert "ready to run" not in resp.text, "it is back in review, not ready"

        # A composite runs its steps through executor.apply.
        sub = await _approved(world, spec)
        comp_body = (
            f"Two steps.\n\n```yaml composite-spec\nsteps:\n  - id: one\n    artifact: {sub}\n```\n"
        )
        comp = await _approved(
            world,
            {
                "kind": "composite",
                "intent": "Composite",
                "body": comp_body,
                "target": {"kind": "global"},
                "idempotence": "declared-natural",
            },
        )
        result = await executor.apply(comp, "owner")
        assert not result.success, result
        assert _fm(world, sub)["status"] == "proposed", "the composite's step bypassed the guard"
        assert world["pve"].writes == []
        assert {"apply", "replay"} <= set(refused)

    async def test_revoke_asks_before_its_rollback(self, world, monkeypatch):
        spec = _spec("More cores", SET_CORES)
        spec["body"] += CORES_ROLLBACK
        aid = await _approved(world, spec)
        resp = await world["client"].post(f"/artifacts/{aid}/apply", json={})
        assert (await _settled_task(world, aid, resp.json().get("task_id")))[
            "status"
        ] == "succeeded", resp.text
        sent = list(world["pve"].writes)

        async def _refuse(self, fm, body, action):
            return "changed", f"sentinel for {action}"

        monkeypatch.setattr(ArtifactExecutor, "check_run", _refuse)
        resp = await world["client"].request("DELETE", f"/artifacts/{aid}", json={})
        assert resp.status_code in (200, 202), resp.text
        task = await _settled_task(world, aid, resp.json()["task_id"])
        assert "sentinel for revoke" in task["result_json"], task
        assert world["pve"].writes == sent

    async def test_kind_executors_are_called_only_behind_the_guard(self):
        """Structural: the kind executors are reached only from the orchestrator,
        and every orchestrator method that reaches them asks ``check_run`` first."""
        kind_calls = re.compile(
            r"\b(ansible|proxmox_api|http_sequence|shell_script|host_provision|"
            r"guest_network|guest_power)_execute\("
        )
        outside = [
            str(p.relative_to(SRC))
            for p in SRC.rglob("*.py")
            if p.name not in ("orchestrator.py", "__init__.py") and kind_calls.search(p.read_text())
        ]
        assert outside == [], f"a kind executor is called outside the orchestrator: {outside}"

        tree = ast.parse((SRC / "executor" / "orchestrator.py").read_text())
        cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ArtifactExecutor"
        )
        unguarded = []
        for fn in cls.body:
            if not isinstance(fn, ast.AsyncFunctionDef) or fn.name in (
                "_dispatch",
                "_run_rollback",
            ):
                continue
            calls: dict[str, int] = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                    calls[n.func.attr] = min(n.lineno, calls.get(n.func.attr, n.lineno))
            reaches = [calls[c] for c in ("_dispatch", "_run_rollback") if c in calls]
            if reaches and not ("check_run" in calls and calls["check_run"] < min(reaches)):
                unguarded.append(fn.name)
        assert unguarded == [], f"these reach a kind executor without check_run: {unguarded}"


class TestTheOverviewSaysWhichApprovalsExpired:
    async def test_an_expired_approval_is_counted_apart(self, world):
        import json

        from homepilot.dashboard.service import build_summary

        old = await _approved(world, _spec("Destroy the probe", DESTROY))
        await _approved(world, _spec("More cores", SET_CORES))
        at = (datetime.now(UTC) - timedelta(days=74)).isoformat()
        await world["repo"].db.execute(
            "UPDATE artifacts SET approved_by_json = ? WHERE id = ?",
            (json.dumps({"user": "owner", "at": at}), old),
        )
        await world["repo"].db.conn.commit()

        changes = (await build_summary(world["repo"]))["changes"]

        assert changes["ready"] == 2 and changes["expired"] == 1, changes


class TestWhichGuestsAChangeActsOn:
    """``guest_refs``: the set the guard checks. A guest it misses is unguarded."""

    def _refs(self, steps: str, fence: str = "proxmox-api-spec", **target: Any):
        from homepilot.artifacts.target_identity import guest_refs

        fm = {
            "kind": "proxmox-api-sequence",
            "target": target or {"kind": "node", "node": NODE},
        }
        body = f"```yaml {fence}\nsteps:\n{steps}```\n"
        return {(r.vmid, r.creates) for r in guest_refs(fm, body)}

    async def test_writes_to_guests_are_named_and_reads_are_not(self):
        steps = (
            "  - id: a\n    method: GET\n    path: /nodes/pve1/qemu/101/config\n"
            "  - id: b\n    method: POST\n    path: /nodes/pve1/lxc/102/status/stop\n"
        )
        assert self._refs(steps) == {(102, False)}

    async def test_a_backup_names_its_guests_in_the_body(self):
        steps = (
            "  - id: c\n    method: POST\n    path: /nodes/pve1/vzdump\n"
            '    body: {vmid: "103,104", storage: fast1}\n'
        )
        assert self._refs(steps) == {(103, False), (104, False)}

    async def test_a_create_a_restore_and_a_clone_target_name_numbers_that_must_be_free(self):
        steps = (
            "  - id: a\n    method: POST\n    path: /nodes/pve1/qemu\n    body: {vmid: 300}\n"
            "  - id: b\n    method: POST\n    path: /nodes/pve1/lxc\n"
            "    body: {vmid: 301, restore: 1, force: 1}\n"
            "  - id: c\n    method: POST\n    path: /nodes/pve1/qemu/9000/clone\n"
            "    body: {newid: 302}\n"
            "  - id: d\n    method: PUT\n    path: /nodes/pve1/qemu/300/config\n"
            "    body: {cores: 2}\n"
        )
        assert self._refs(steps) == {(300, True), (301, True), (302, True), (9000, False)}

    async def test_the_rollback_and_the_target_are_named_too(self):
        steps = "  - id: a\n    method: DELETE\n    path: /nodes/pve1/qemu/{{ target.vmid }}\n"
        assert self._refs(steps, "proxmox-api-rollback", kind="vm", vmid=150, node=NODE) == {
            (150, False)
        }
        from homepilot.artifacts.target_identity import guest_refs

        fm = {"kind": "shell-script", "target": {"kind": "lxc", "vmid": 110, "node": NODE}}
        assert [(r.vmid, r.guest_type) for r in guest_refs(fm, "")] == [(110, "lxc")]


class TestAnAgentOnlyChangeWithNoProxmox:
    async def test_it_applies_with_no_proxmox_configured(self, tmp_path):
        """A shell script on a VM target runs through the agent; with no Proxmox
        there is nobody to ask who is at the VMID, and it does not address one."""
        from unittest.mock import AsyncMock, MagicMock

        store = ArtifactStore(tmp_path / "artifacts")
        lifecycle = ArtifactLifecycle(store=store)
        agent = AsyncMock()
        agent.exec = AsyncMock(return_value=(0, "done", ""))
        agent._validate_guest_only = MagicMock()
        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=AsyncMock(),
            proxmox=None,
            vault=AsyncMock(),
            agent=agent,
            pve_nodes=[NODE],
        )
        aid = await lifecycle.propose(
            {
                "id": "2026-11-12-agent-only-813",
                "kind": "shell-script",
                "intent": "Say done",
                "body": "## Plan\nx\n\n## Idempotence preamble\nIdempotent.\n\n"
                "```bash shell-spec\n#!/bin/bash\nset -euo pipefail\necho done\n```\n",
                "target": {"kind": "vm", "vmid": 150, "node": NODE, "host": "probe-1"},
                "idempotence": "via-precheck",
                "requires_snapshot": False,
                "produced_by": {"session": "s", "agent": "a", "user": "u"},
            }
        )
        await lifecycle.approve(aid, Principal.operator("owner"))

        result = await executor.apply(aid, "owner")

        assert result.success, result
        agent.exec.assert_called()

    async def test_a_proxmox_change_with_no_proxmox_is_still_refused(self):
        from homepilot.artifacts.target_identity import check

        fm = _spec("Destroy the probe", DESTROY)
        verdict = await check(None, fm, fm["body"])
        assert verdict.ok is False and verdict.code == "unreadable"
