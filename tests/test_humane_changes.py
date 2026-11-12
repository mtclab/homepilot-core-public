"""The humane Changes queue, end to end (owner feedback 2026-11-08, #796, #797).

The owner approved a change, it left the review queue, and nothing said it still
had to be applied: the queue looked empty and the change looked lost. Four
approved destroys then sat unapplied for the same reason; one failed at apply
because its DELETE options went to PVE as a JSON body (PVE 501), after an earlier
step had already run, and its approved-but-broken siblings could not be taken
back (`Invalid transition: approved -> rejected`).

These are journeys through the REAL pieces - the artifacts router, lifecycle,
store, task runner, executor, the database, and the real ``ProxmoxClient`` -
with ONE fake: Proxmox itself, at the HTTP boundary (an ``httpx.MockTransport``
under the client), so what is asserted is what PVE would have received. Each
asserts what the operator SEES (the artifact detail, the task outcome, the
summary counts the sidebar shows), not that a function returned.

Teeth (each proven by reverting the named code and watching the named test
fail; evidence in the PR):
  * restore the guest-power-only kind check in ``approve_and_apply_artifact`` ->
    ``test_approve_and_apply_takes_a_proposal_to_applied_in_one_action`` fails;
  * drop the ``changes`` split in ``build_summary`` ->
    ``test_approve_only_leaves_it_ready_to_run`` fails;
  * drop ``WITHDRAWN`` from ``VALID_TRANSITIONS[APPROVED]`` ->
    ``test_withdraw_takes_back_an_approved_change_and_runs_nothing`` fails;
  * drop the DELETE rule in ``wire_parameters`` ->
    ``test_a_delete_steps_options_reach_pve_as_query_parameters`` fails;
  * drop the config re-read in the executor's failed-step branch ->
    ``test_a_config_write_pve_took_in_part_is_partly_applied_naming_the_key`` fails;
  * stop recording completed steps (``done``) ->
    ``test_a_failure_after_completed_steps_says_partly_applied`` fails;
  * drop the never-ran check in ``ArtifactExecutor.revoke`` ->
    ``test_revoking_a_never_run_change_runs_no_rollback`` fails;
  * stop reading the task log in ``_await_pve_task`` ->
    the warnings assertions in the approve-and-apply journey fail.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.adapters.proxmox import ProxmoxClient, wire_parameters
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.artifacts.summary import summarize
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.executor.pve_warnings import explain_warning
from homepilot.mcp.server import _handle_tool
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner

pytestmark = pytest.mark.asyncio

NODE = "pve1"
ADMIN = {"scope": "*", "role": "admin", "label": "owner", "prefix": "hp_owner000001"}
ASSISTANT = {"scope": "read,write", "label": "assistant", "prefix": "hp_asst0000001"}
OWNER_ACTOR = "owner (hp_owner000001)"
EFI_WARNING = (
    "EFI disk without 'ms-cert=2023k' option, suggesting that not all UEFI 2023 "
    "certificates from Microsoft are enrolled yet."
)


class FakePve:
    """Proxmox at the HTTP boundary: records every request, answers from state.

    ``config[vmid]`` is the guest config; a PUT .../config applies its keys in
    order and refuses any key in ``refuse_keys`` with a 500 - AFTER the keys
    before it took effect, which is how PVE behaves (#796). Tasks finish on
    their first status read unless ``hold`` is set, which keeps them running
    until ``release()``.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.config: dict[int, dict[str, Any]] = {}
        self.refuse_keys: set[str] = set()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.task_logs: dict[str, list[str]] = {}
        self.hold = False
        self._n = 0

    @property
    def writes(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] != "GET"]

    def release(self) -> None:
        self.hold = False
        for task in self.tasks.values():
            if task["status"] == "running":
                task["status"] = "stopped"

    def _task(self, kind: str, vmid: Any, exitstatus: str, log: list[str] | None = None) -> str:
        self._n += 1
        upid = f"UPID:{NODE}:0000{self._n:04X}:00000000:00000000:{kind}:{vmid}:root@pam:"
        self.tasks[upid] = {
            "status": "running" if self.hold else "stopped",
            "exitstatus": exitstatus,
        }
        self.task_logs[upid] = log or []
        return upid

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        params = dict(request.url.params)
        content = request.content
        body = json.loads(content) if content else None
        self.requests.append(
            {"method": request.method, "path": path, "params": params, "body": body}
        )

        def ok(data: Any) -> httpx.Response:
            return httpx.Response(200, json={"data": data})

        parts = path.strip("/").split("/")
        if request.method == "GET":
            if path == "/cluster/resources":
                # Who is at each VMID (#813): the guests this fake holds.
                return ok(
                    [
                        {"vmid": v, "node": NODE, "type": "qemu", "name": c.get("name")}
                        for v, c in sorted(self.config.items())
                    ]
                )
            if len(parts) == 5 and parts[2] == "tasks":
                upid = unquote(parts[3])
                if parts[4] == "status":
                    task = self.tasks[upid]
                    out = {"status": task["status"], "upid": upid}
                    if task["status"] == "stopped":
                        out["exitstatus"] = task["exitstatus"]
                    return ok(out)
                if parts[4] == "log":
                    lines = ["INFO: starting new backup job"] + [
                        f"WARN: {w}" for w in self.task_logs[upid]
                    ]
                    return ok([{"n": i + 1, "t": t} for i, t in enumerate(lines)])
            if len(parts) == 5 and parts[4] == "config":
                return ok(dict(self.config[int(parts[3])]))
            return httpx.Response(404, text=f"no such read {path}")

        if path == f"/nodes/{NODE}/vzdump":
            return ok(self._task("vzdump", (body or {}).get("vmid"), "WARNINGS: 1", [EFI_WARNING]))
        if request.method == "DELETE" and len(parts) == 4 and parts[2] == "qemu":
            if content:
                # What PVE really answers (#797).
                return httpx.Response(501, text="Unexpected content for method 'DELETE'")
            self.config.pop(int(parts[3]), None)
            return ok(self._task("qmdestroy", parts[3], "OK"))
        if request.method == "PUT" and len(parts) == 5 and parts[4] == "config":
            config = self.config[int(parts[3])]
            fields = dict(body or {})
            for key in [k for k in str(fields.pop("delete", "")).split(",") if k]:
                if key in self.refuse_keys:
                    return httpx.Response(500, text=f"only root can set '{key}'")
                config.pop(key, None)
            for key, value in fields.items():
                if key in self.refuse_keys:
                    return httpx.Response(500, text=f"only root can set '{key}'")
                config[key] = value
            return ok(None)
        if request.method == "POST" and len(parts) == 6 and parts[4] == "status":
            return ok(self._task(f"qm{parts[5]}", parts[3], "OK"))
        raise AssertionError(f"unexpected write {request.method} {path}")


class _FastPollClient(ProxmoxClient):
    """The real client; only its task-poll cadence is shortened for the test."""

    async def wait_until_stopped(
        self, node: str, upid: str, timeout_s: float = 600.0, poll_interval: float = 2.0
    ) -> dict[str, Any]:
        return await super().wait_until_stopped(node, upid, timeout_s, 0.02)


def _client(pve: FakePve) -> ProxmoxClient:
    client = _FastPollClient("https://pve.example:8006", "root@pam!hp=00000000")
    client._client = httpx.AsyncClient(
        base_url="https://pve.example:8006/api2/json",
        transport=httpx.MockTransport(pve.handle),
    )
    client._write_client = client._client
    return client


def _spec(intent: str, steps: str, **extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "kind": "proxmox-api-sequence",
        "intent": intent,
        "body": f"{intent}.\n\n```yaml proxmox-api-spec\nsteps:\n{steps}```\n",
        "target": {"kind": "vm", "vmid": 104, "node": NODE, "host": "retire-104"},
        "idempotence": "declared-natural",
        "requires_snapshot": False,
    }
    spec.update(extra)
    return spec


BACKUP_STEPS = """  - id: backup
    method: POST
    path: /nodes/{{ target.node }}/vzdump
    body:
      vmid: "{{ target.vmid }}"
      storage: fast1
"""

DESTROY_STEPS = """  - id: destroy
    method: DELETE
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}
    body:
      purge: 1
      destroy-unreferenced-disks: 0
"""


@pytest.fixture
async def world(tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.dashboard.router import router as dashboard_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = FakePve()
    pve.config[104] = {"name": "retire-104", "memory": "8192"}
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store, repository=repo)
    executor = ArtifactExecutor(
        store=store, lifecycle=lifecycle, repo=repo, proxmox=_client(pve), vault=None
    )
    # Propose records which machine a change is for (#813), as the app wires it.
    lifecycle._proxmox = executor.proxmox
    task_repo = TaskRepository(db)
    runner = TaskRunner(
        repo=task_repo, lifecycle=lifecycle, executor=executor, apply_reconciler=None, store=store
    )
    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.include_router(dashboard_router)
    app.state.repo = repo
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield client, token, pve, ctx
        finally:
            pve.release()
            await runner.drain(timeout=5.0)
            await db.close()


async def _propose(client: AsyncClient, spec: dict[str, Any]) -> str:
    resp = await client.post("/artifacts", json=spec)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


async def _detail(client: AsyncClient, aid: str) -> dict[str, Any]:
    resp = await client.get(f"/artifacts/{aid}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def _waiting(client: AsyncClient) -> dict[str, int]:
    resp = await client.get("/dashboard/summary")
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["changes"])


async def _until(client: AsyncClient, aid: str, status: str) -> dict[str, Any]:
    for _ in range(300):
        detail = await _detail(client, aid)
        if detail["frontmatter"]["status"] == status and detail["active_task"] is None:
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(f"{aid} never reached {status}: {detail['frontmatter']['status']}")


async def _audit_actions(ctx: dict[str, Any], aid: str) -> list[str]:
    rows = await ctx["repo"].db.fetchall(
        "SELECT action FROM audit_log WHERE artifact_id = ? ORDER BY id", (aid,)
    )
    return [r["action"] for r in rows]


# ── the journeys ──────────────────────────────────────────────────────────────


class TestTheQueueNeverLosesAChange:
    async def test_approve_and_apply_takes_a_proposal_to_applied_in_one_action(self, world):
        client, _token, pve, _ctx = world
        aid = await _propose(client, _spec("Back up VM 104", BACKUP_STEPS))
        before = await _detail(client, aid)
        # The plain-words summary is there before anyone decides.
        assert before["summary"]["steps"] == ["Back up retire-104 (VM 104 on pve1) to fast1"]
        assert before["summary"]["two_step"] is False
        assert await _waiting(client) == {"proposed": 1, "ready": 0, "running": 0, "expired": 0}

        pve.hold = True
        resp = await client.post(f"/artifacts/{aid}/approve-and-apply", json={})
        assert resp.status_code == 202, resp.text
        task_id = resp.json()["task_id"]

        # Running: approved, a live task, counted as running - never gone.
        running = await _detail(client, aid)
        assert running["frontmatter"]["status"] == "approved"
        assert running["active_task"] is not None
        assert running["active_task"]["id"] == task_id
        assert await _waiting(client) == {"proposed": 0, "ready": 0, "running": 1, "expired": 0}

        pve.release()
        done = await _until(client, aid, "applied")
        assert await _waiting(client) == {"proposed": 0, "ready": 0, "running": 0, "expired": 0}
        last = done["last_task"]
        assert last["id"] == task_id and last["status"] == "succeeded"
        # The PVE warning is shown, explained and judged - never "WARNINGS: 1".
        (warning,) = last["result"]["warnings"]
        assert "ms-cert=2023k" in warning["text"]
        assert warning["harmless"] is True
        assert warning["explanation"]
        assert "WARN: EFI disk" in done["body"]
        assert [w["path"] for w in pve.writes] == [f"/nodes/{NODE}/vzdump"]
        assert _audit_includes(await _audit_actions(_ctx, aid), ["approve", "apply"])

    async def test_approve_only_leaves_it_ready_to_run(self, world):
        client, _token, pve, _ctx = world
        aid = await _propose(client, _spec("Back up VM 104", BACKUP_STEPS))

        resp = await client.post(f"/artifacts/{aid}/approve", json={})
        assert resp.status_code == 200, resp.text

        # Still in the queue: listed as approved, counted as ready to run, with
        # no run in flight - the Apply press is the one thing it waits for.
        listed = (await client.get("/artifacts", params={"status": "approved"})).json()
        assert [a["id"] for a in listed["items"]] == [aid]
        assert await _waiting(client) == {"proposed": 0, "ready": 1, "running": 0, "expired": 0}
        detail = await _detail(client, aid)
        assert detail["frontmatter"]["status"] == "approved"
        assert detail["active_task"] is None
        assert pve.writes == []

        resp = await client.post(f"/artifacts/{aid}/apply", json={})
        assert resp.status_code == 202, resp.text
        await _until(client, aid, "applied")
        assert await _waiting(client) == {"proposed": 0, "ready": 0, "running": 0, "expired": 0}

    async def test_withdraw_takes_back_an_approved_change_and_runs_nothing(self, world):
        client, _token, pve, ctx = world
        aid = await _propose(client, _spec("Destroy VM 104", DESTROY_STEPS))
        assert (await client.post(f"/artifacts/{aid}/approve", json={})).status_code == 200
        # The old dead end: an approved change cannot be rejected.
        refused = await client.post(f"/artifacts/{aid}/reject", json={})
        assert refused.status_code == 409

        resp = await client.post(f"/artifacts/{aid}/withdraw", json={"reason": "wrong VM"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "withdrawn"
        detail = await _detail(client, aid)
        assert detail["frontmatter"]["status"] == "withdrawn"
        assert detail["frontmatter"]["withdrawn_by"]["user"] == OWNER_ACTOR
        assert detail["frontmatter"]["withdrawn_by"]["reason"] == "wrong VM"
        assert detail["last_task"]["action"] == "withdraw"
        assert detail["last_task"]["status"] == "succeeded"
        assert await _waiting(client) == {"proposed": 0, "ready": 0, "running": 0, "expired": 0}
        assert pve.writes == [], "withdrawing must not touch Proxmox"
        assert "withdraw" in await _audit_actions(ctx, aid)
        # Terminal: it cannot be run any more.
        assert (await client.post(f"/artifacts/{aid}/apply", json={})).status_code == 409
        assert pve.writes == []


def _audit_includes(actions: list[str], wanted: list[str]) -> bool:
    return all(w in actions for w in wanted)


class TestApproveAndApplyRules:
    async def test_two_step_kinds_keep_two_steps(self, world):
        client, _token, pve, _ctx = world
        aid = await _propose(client, _spec("Back up VM 104 once", BACKUP_STEPS, replay_safe=False))
        resp = await client.post(f"/artifacts/{aid}/approve-and-apply", json={})
        assert resp.status_code == 400
        assert "replay_safe" in resp.json()["detail"]
        assert (await _detail(client, aid))["frontmatter"]["status"] == "proposed"
        assert pve.writes == []

    async def test_only_an_operator_approves_and_applies(self, world):
        client, token, pve, _ctx = world
        aid = await _propose(client, _spec("Back up VM 104", BACKUP_STEPS))
        token.clear()
        token.update(ASSISTANT)
        resp = await client.post(f"/artifacts/{aid}/approve-and-apply", json={})
        assert resp.status_code == 403
        token.clear()
        token.update(ADMIN)
        assert (await _detail(client, aid))["frontmatter"]["status"] == "proposed"
        assert pve.writes == []

    async def test_a_change_that_is_not_proposed_is_refused_plainly(self, world):
        client, _token, _pve, _ctx = world
        aid = await _propose(client, _spec("Back up VM 104", BACKUP_STEPS))
        await client.post(f"/artifacts/{aid}/approve", json={})
        resp = await client.post(f"/artifacts/{aid}/approve-and-apply", json={})
        assert resp.status_code == 409
        assert "not proposed" in resp.json()["detail"]


class TestWithdrawRules:
    async def test_only_an_approved_change_can_be_withdrawn(self, world):
        client, _token, _pve, _ctx = world
        aid = await _propose(client, _spec("Back up VM 104", BACKUP_STEPS))
        resp = await client.post(f"/artifacts/{aid}/withdraw", json={})
        assert resp.status_code == 409
        assert "proposed" in resp.json()["detail"]
        assert (await _detail(client, aid))["frontmatter"]["status"] == "proposed"

    async def test_a_running_change_cannot_be_withdrawn(self, world):
        client, _token, pve, _ctx = world
        aid = await _propose(client, _spec("Back up VM 104", BACKUP_STEPS))
        pve.hold = True
        started = await client.post(f"/artifacts/{aid}/approve-and-apply", json={})
        task_id = started.json()["task_id"]

        resp = await client.post(f"/artifacts/{aid}/withdraw", json={})

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["task_id"] == task_id, "the refusal names the run holding it"
        assert task_id in detail["message"]
        pve.release()
        await _until(client, aid, "applied")

    async def test_an_unsettled_run_cannot_be_withdrawn(self, world):
        client, _token, _pve, ctx = world
        aid = await _propose(client, _spec("Back up VM 104", BACKUP_STEPS))
        await client.post(f"/artifacts/{aid}/approve", json={})
        await ctx["lifecycle"].mark_unsettled(aid, {"node": NODE, "upid": "UPID:pve1:X"})

        resp = await client.post(f"/artifacts/{aid}/withdraw", json={})

        assert resp.status_code == 409
        assert "UPID:pve1:X" in resp.json()["detail"]
        assert (await _detail(client, aid))["frontmatter"]["status"] == "approved"
        # The slot was handed back: a refused withdraw strands no task.
        assert (await _detail(client, aid))["active_task"] is None

    async def test_the_assistant_withdraws_over_mcp(self, world):
        client, _token, pve, ctx = world
        aid = await _propose(client, _spec("Destroy VM 104", DESTROY_STEPS))
        await client.post(f"/artifacts/{aid}/approve", json={})

        out = await _handle_tool("withdraw_artifact", {"artifact_id": aid}, ctx)

        assert out["status"] == "withdrawn"
        detail = await _detail(client, aid)
        assert detail["frontmatter"]["status"] == "withdrawn"
        assert detail["frontmatter"]["withdrawn_by"]["user"] == "assistant (hp_asst0000001)"
        assert pve.writes == []


# ── #797: DELETE options travel as query parameters ──────────────────────────


class TestDeleteOptionsReachPveAsQuery:
    async def test_a_delete_steps_options_reach_pve_as_query_parameters(self, world):
        client, _token, pve, _ctx = world
        aid = await _propose(client, _spec("Destroy VM 104", DESTROY_STEPS))

        resp = await client.post(f"/artifacts/{aid}/approve-and-apply?sync=true", json={})

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "succeeded", resp.json()
        (delete,) = [r for r in pve.writes if r["method"] == "DELETE"]
        assert delete["path"] == f"/nodes/{NODE}/qemu/104"
        assert delete["params"] == {"purge": "1", "destroy-unreferenced-disks": "0"}
        assert delete["body"] is None
        assert 104 not in pve.config
        assert (await _detail(client, aid))["frontmatter"]["status"] == "applied"

    async def test_options_in_the_path_and_the_body_are_both_kept(self, world):
        """The workaround the owner used (options in the path) keeps working, and
        mixing both never drops one: httpx replaces a URL's query when given one."""
        client, _token, pve, _ctx = world
        steps = """  - id: destroy
    method: DELETE
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}?purge=1
    body:
      destroy-unreferenced-disks: 0
"""
        aid = await _propose(client, _spec("Destroy VM 104", steps))
        resp = await client.post(f"/artifacts/{aid}/approve-and-apply?sync=true", json={})
        assert resp.json()["status"] == "succeeded", resp.json()
        (delete,) = [r for r in pve.writes if r["method"] == "DELETE"]
        assert delete["params"] == {"purge": "1", "destroy-unreferenced-disks": "0"}

    async def test_the_wire_rule(self):
        assert wire_parameters("DELETE", "nodes/n/qemu/1", {"purge": True}, None) == (
            "nodes/n/qemu/1",
            None,
            {"purge": 1},
        )
        # A POST keeps its JSON body; nothing moves.
        assert wire_parameters("POST", "nodes/n/vzdump", {"vmid": 1}, None) == (
            "nodes/n/vzdump",
            {"vmid": 1},
            None,
        )
        # A GET with a query keeps the path's own parameters too.
        assert wire_parameters("GET", "x?a=1", None, {"b": 2}) == ("x", None, {"a": "1", "b": 2})


# ── #796: a partial apply is said, not hidden behind "failed" ─────────────────


class TestPartialApply:
    async def test_a_config_write_pve_took_in_part_is_partly_applied_naming_the_key(self, world):
        client, _token, pve, _ctx = world
        pve.config[104].update({"scsi2": "fast1:vm-104-disk-2", "hostpci0": "0000:01:00"})
        pve.config[104]["ipconfig0"] = "ip=dhcp"
        pve.refuse_keys = {"hostpci0"}
        steps = """  - id: strip
    method: PUT
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/config
    body:
      delete: scsi2,hostpci0,ipconfig0
"""
        aid = await _propose(client, _spec("Strip VM 104", steps))

        resp = await client.post(f"/artifacts/{aid}/approve-and-apply?sync=true", json={})

        assert resp.json()["status"] == "failed"
        detail = await _detail(client, aid)
        fm = detail["frontmatter"]
        assert fm["status"] == "failed"
        assert fm["failure_reason"].startswith("partly applied: step strip changed scsi2")
        assert "not applied: hostpci0, ipconfig0" in fm["failure_reason"]
        assert fm["partial_apply"]["keys_applied"] == ["scsi2"]
        assert fm["partial_apply"]["keys_not_applied"] == ["hostpci0", "ipconfig0"]
        # The task says the same thing the artifact does.
        assert detail["last_task"]["error"] == fm["failure_reason"]
        assert detail["last_task"]["result"]["partial_apply"]["keys_applied"] == ["scsi2"]
        assert "scsi2" not in pve.config[104], "the fake applied scsi2's removal, like PVE"

    async def test_a_failure_after_completed_steps_says_partly_applied(self, world):
        client, _token, pve, _ctx = world
        pve.refuse_keys = {"memory"}
        steps = (
            BACKUP_STEPS
            + """  - id: shrink
    method: PUT
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/config
    body:
      memory: 4096
  - id: start
    method: POST
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/status/start
"""
        )
        aid = await _propose(client, _spec("Back up, shrink and start VM 104", steps))

        await client.post(f"/artifacts/{aid}/approve-and-apply?sync=true", json={})

        fm = (await _detail(client, aid))["frontmatter"]
        assert fm["status"] == "failed"
        assert fm["failure_reason"].startswith("partly applied: step backup done; step shrink")
        assert fm["failure_reason"].endswith("; not run: start")
        assert fm["partial_apply"]["steps_done"] == ["backup"]
        assert fm["partial_apply"]["steps_failed"] == ["shrink"]
        assert fm["partial_apply"]["steps_not_run"] == ["start"]

    async def test_a_failure_that_changed_nothing_stays_a_plain_failure(self, world):
        client, _token, pve, _ctx = world
        pve.refuse_keys = {"memory"}
        steps = """  - id: shrink
    method: PUT
    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/config
    body:
      memory: 4096
"""
        aid = await _propose(client, _spec("Shrink VM 104", steps))
        await client.post(f"/artifacts/{aid}/approve-and-apply?sync=true", json={})
        fm = (await _detail(client, aid))["frontmatter"]
        assert fm["status"] == "failed"
        assert not fm["failure_reason"].startswith("partly applied")
        assert "partial_apply" not in fm


# ── revoke of a change that never ran ────────────────────────────────────────


class TestRevokeNeverRan:
    async def test_revoking_a_never_run_change_runs_no_rollback(self, world):
        client, _token, pve, _ctx = world
        spec = _spec("Stop VM 104", "")
        spec["body"] = (
            "Stop VM 104.\n\n```yaml proxmox-api-spec\nsteps:\n  - id: stop\n    method: POST\n"
            "    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/status/stop\n```\n\n"
            "## Rollback\n\n```yaml proxmox-api-rollback\nsteps:\n  - id: start\n"
            "    method: POST\n    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}"
            "/status/start\n```\n"
        )
        aid = await _propose(client, spec)
        await client.post(f"/artifacts/{aid}/approve", json={})

        resp = await client.delete(f"/artifacts/{aid}?sync=true")

        assert resp.status_code == 200, resp.text
        assert resp.json()["frontmatter"]["status"] == "revoked"
        assert pve.writes == [], "a rollback ran against a host the change never touched"


# ── the words ────────────────────────────────────────────────────────────────


class TestPlainWords:
    async def test_a_destroy_is_named_and_needs_the_name_typed(self):
        fm = _spec("Retire VM 104", DESTROY_STEPS)
        summary = summarize(fm, fm["body"])
        assert summary["steps"] == ["Destroy VM 104 on pve1, with its disks"]
        assert summary["danger"] == "destroy"
        assert summary["confirm_name"] == "retire-104"
        assert summary["target"] == "retire-104 (VM 104 on pve1)"

    async def test_an_unknown_call_is_said_as_the_call_it_is(self):
        steps = "  - id: x\n    method: POST\n    path: /cluster/sdn\n"
        fm = _spec("Reload SDN", steps)
        assert summarize(fm, fm["body"])["steps"] == ["POST /cluster/sdn"]
        assert summarize(fm, fm["body"])["danger"] is None

    async def test_a_composite_keeps_two_steps(self):
        summary = summarize({"kind": "composite", "intent": "x", "target": {}}, "")
        assert summary["two_step"] is True

    async def test_known_and_unknown_warnings(self):
        known = explain_warning(EFI_WARNING)
        assert known["harmless"] is True and known["explanation"]
        unknown = explain_warning("disk 'scsi1' not backed up")
        assert unknown == {
            "text": "disk 'scsi1' not backed up",
            "explanation": None,
            "harmless": None,
        }


async def test_the_partial_sentence_names_every_part() -> None:
    from homepilot.executor.proxmox_api import describe_partial

    text = describe_partial(
        {
            "steps_done": ["a", "b"],
            "steps_failed": ["c"],
            "steps_not_run": ["d"],
            "error": "500 nope",
        }
    )
    assert text == "partly applied: steps a, b done; step c failed: 500 nope; not run: d"
    assert re.match(r"^partly applied: ", text)
