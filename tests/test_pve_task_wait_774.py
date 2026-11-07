"""A long PVE task is followed, and a wait that runs out is NOT a failure (#774).

What happened: a `proxmox-api-sequence` with one `POST /nodes/<node>/vzdump` of
three guests (~13 min) was marked failed with "did not finish within 600.0s".
PVE finished the task `OK` and every archive was on disk. The artifact record
said the backup had failed when it had succeeded.

These drive the REAL `ProxmoxClient.wait_for_task` against a fake cluster whose
tasks take real (fake-clock) time, through the real executor, artifact store and
task records, and assert what an operator is told:

- a backup that takes 13 minutes is reported succeeded on the defaults;
- a step's own `timeout_s` is honoured, and validated;
- a task still running when the wait ends leaves the outcome UNKNOWN - the
  artifact is not failed, the task reads `unknown`, the UPID is recorded - and
  the next apply settles it from PVE without starting the work a second time.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest

import homepilot.adapters.proxmox as proxmox_adapter
from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.artifacts.lifecycle import ArtifactLifecycle, LifecycleError
from homepilot.artifacts.models import ConflictError
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.principal import Principal
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.executor.proxmox_api import execute as proxmox_api_execute
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner

VZDUMP_UPID = "UPID:pve:00001234:0000ABCD:66000000:vzdump::root@pam:"
STOP_UPID = "UPID:pve:00001235:0000ABCE:66000001:qmstop:101:root@pam:"

MINUTE = 60.0


class FakeClock:
    """Time that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # Never zero, so a zero-interval poll still makes progress.
        self.now += max(float(seconds), 0.001)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePve(ProxmoxClient):
    """A cluster whose tasks RUN for a while, then stop with an exit status.

    Only `call` is replaced; `wait_for_task` and `upid_of` are the real ones.
    """

    def __init__(self, clock: FakeClock, durations: dict[str, float], exitstatus: str = "OK"):
        # No super().__init__: no HTTP client, nothing can leave the process.
        self.clock = clock
        self.durations = durations
        self.exitstatus = exitstatus
        self.calls: list[tuple[str, str]] = []
        self.tasks: dict[str, float] = {}

    def _start(self, upid: str) -> dict[str, Any]:
        self.tasks[upid] = self.clock.now + self.durations[upid]
        return {"data": upid}

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/vzdump"):
            return self._start(VZDUMP_UPID)
        if method == "POST" and path.endswith("/status/stop"):
            return self._start(STOP_UPID)
        if method == "GET" and "/tasks/" in path and path.endswith("/status"):
            upid = unquote(path.split("/tasks/", 1)[1].rsplit("/status", 1)[0])
            if self.clock.now < self.tasks[upid]:
                return {"data": {"status": "running", "upid": upid}}
            return {"data": {"status": "stopped", "exitstatus": self.exitstatus, "upid": upid}}
        return {"data": None}

    def posts(self, suffix: str) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith(suffix))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Drive the adapter's wait loop on fake time - only the adapter's."""
    c = FakeClock()
    monkeypatch.setattr(proxmox_adapter, "time", SimpleNamespace(monotonic=c.monotonic))
    monkeypatch.setattr(proxmox_adapter, "asyncio", SimpleNamespace(sleep=c.sleep))
    return c


def _vzdump_body(timeout_line: str = "", then_step: bool = False) -> str:
    body = f"""\
## Plan
Back up three guests

## Spec

```yaml proxmox-api-spec
steps:
  - id: backup
    method: POST
    path: /nodes/pve/vzdump
    body:
      vmid: "101,102,103"
      storage: local
      mode: snapshot
{timeout_line}"""
    if then_step:
        body += """\
  - id: after-backup
    method: POST
    path: /nodes/pve/qemu/101/status/stop
"""
    return body + "```\n"


FM = {"id": "2026-11-02-backup-774", "intent": "back up"}


# --- the executor: follow the task, and never call "still running" failed -----


async def test_a_13_minute_backup_succeeds_on_the_defaults(clock: FakeClock) -> None:
    """The incident itself: vzdump outlives 600 s and finishes OK."""
    pve = FakePve(clock, {VZDUMP_UPID: 13 * MINUTE})

    result = await proxmox_api_execute(FM, _vzdump_body(), {"node": "pve"}, pve)

    assert result["success"] is True, result.get("failure_reason")
    assert "task finished OK" in result["execution_log"]
    assert pve.posts("/vzdump") == 1


async def test_a_step_names_its_own_wait(clock: FakeClock) -> None:
    """`timeout_s` on a step is honoured: a 15-minute stop under a 20-minute wait."""
    pve = FakePve(clock, {STOP_UPID: 15 * MINUTE})
    body = """\
```yaml proxmox-api-spec
steps:
  - id: stop-101
    method: POST
    path: /nodes/pve/qemu/101/status/stop
    timeout_s: 1200
```
"""
    result = await proxmox_api_execute(FM, body, {"node": "pve"}, pve)

    assert result["success"] is True, result.get("failure_reason")


async def test_a_wait_that_runs_out_is_unknown_not_failed(clock: FakeClock) -> None:
    pve = FakePve(clock, {VZDUMP_UPID: 30 * MINUTE})

    result = await proxmox_api_execute(
        FM, _vzdump_body("    timeout_s: 60\n", then_step=True), {"node": "pve"}, pve
    )

    assert result["success"] is False
    unsettled = result.get("unsettled_task")
    assert unsettled, "a task still running was reported as a plain failure"
    assert unsettled["upid"] == VZDUMP_UPID
    assert unsettled["node"] == "pve"
    assert unsettled["step"] == "backup"
    assert unsettled["steps_not_run"] == ["after-backup"]
    assert result["failure_reason"].startswith("outcome unknown")
    assert VZDUMP_UPID in result["execution_log"]
    # The step behind a task still in flight must not race it.
    assert pve.posts("/status/stop") == 0


async def test_a_bad_timeout_is_refused_before_anything_runs(clock: FakeClock) -> None:
    pve = FakePve(clock, {VZDUMP_UPID: MINUTE})
    for bad in ("0", "-5", "true", '"600"', "999999"):
        result = await proxmox_api_execute(
            FM, _vzdump_body(f"    timeout_s: {bad}\n"), {"node": "pve"}, pve
        )
        assert result["success"] is False, bad
        assert "timeout_s" in result["failure_reason"], bad
    assert pve.calls == [], "a step ran before its wait was validated"


# --- the artifact: unknown is recorded as unknown, and settled from PVE -------


def _spec(body: str) -> dict[str, Any]:
    return {
        "id": "2026-11-02-backup-774",
        "kind": "proxmox-api-sequence",
        "intent": "Back up three guests",
        "body": body,
        "target": {"kind": "node", "node": "pve"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }


async def _approved(tmp_artifacts_dir: Path, pve: FakePve, body: str) -> tuple[Any, ...]:
    store = ArtifactStore(tmp_artifacts_dir)
    lifecycle = ArtifactLifecycle(store=store)
    executor = ArtifactExecutor(
        store=store,
        lifecycle=lifecycle,
        repo=AsyncMock(),
        proxmox=pve,
        vault=AsyncMock(),
        pve_nodes=["pve"],
    )
    aid = await lifecycle.propose(_spec(body))
    await lifecycle.approve(aid, Principal.operator("admin"))
    return store, lifecycle, executor, aid


async def test_an_unknown_outcome_leaves_the_artifact_unfailed_and_settles_without_rerunning(
    clock: FakeClock, tmp_artifacts_dir: Path
) -> None:
    pve = FakePve(clock, {VZDUMP_UPID: 30 * MINUTE})
    store, _, executor, aid = await _approved(
        tmp_artifacts_dir, pve, _vzdump_body("    timeout_s: 60\n")
    )

    first = await executor.apply(aid, approved_by="admin")

    fm, _ = store.read(aid)
    assert fm["status"] == "approved", "a backup still running was recorded as " + fm["status"]
    assert fm["unsettled_task"]["upid"] == VZDUMP_UPID
    assert first.unsettled_task and first.unsettled_task["upid"] == VZDUMP_UPID

    # Asked again while PVE still runs it: still unknown, and NO second vzdump.
    again = await executor.apply(aid, approved_by="admin")
    assert again.unsettled_task
    assert store.read(aid)[0]["status"] == "approved"
    assert pve.posts("/vzdump") == 1

    # PVE finishes it. The next apply settles - from PVE, without re-running.
    clock.advance(30 * MINUTE)
    settled = await executor.apply(aid, approved_by="admin")

    assert settled.success is True, settled.failure_reason
    fm, body = store.read(aid)
    assert fm["status"] == "applied"
    assert "unsettled_task" not in fm
    assert f"PVE task {VZDUMP_UPID} finished OK" in body
    assert pve.posts("/vzdump") == 1, "settling re-ran the backup"


async def test_a_task_that_ends_in_error_settles_to_failed(
    clock: FakeClock, tmp_artifacts_dir: Path
) -> None:
    pve = FakePve(clock, {VZDUMP_UPID: 30 * MINUTE}, exitstatus="job errors")
    store, _, executor, aid = await _approved(
        tmp_artifacts_dir, pve, _vzdump_body("    timeout_s: 60\n")
    )
    await executor.apply(aid, approved_by="admin")

    clock.advance(30 * MINUTE)
    settled = await executor.apply(aid, approved_by="admin")

    assert settled.success is False
    assert settled.unsettled_task is None
    fm, _ = store.read(aid)
    assert fm["status"] == "failed"
    assert "job errors" in fm["failure_reason"]


async def test_steps_never_run_keep_a_settled_task_from_reading_applied(
    clock: FakeClock, tmp_artifacts_dir: Path
) -> None:
    """The backup ended OK, but the step behind it never ran: not applied."""
    pve = FakePve(clock, {VZDUMP_UPID: 30 * MINUTE})
    store, _, executor, aid = await _approved(
        tmp_artifacts_dir, pve, _vzdump_body("    timeout_s: 60\n", then_step=True)
    )
    await executor.apply(aid, approved_by="admin")

    clock.advance(30 * MINUTE)
    settled = await executor.apply(aid, approved_by="admin")

    assert settled.success is False
    fm, _ = store.read(aid)
    assert fm["status"] == "failed"
    assert "after-backup" in fm["failure_reason"]
    assert pve.posts("/status/stop") == 0


async def test_revoke_refuses_while_the_last_runs_task_is_still_going(
    clock: FakeClock, tmp_artifacts_dir: Path
) -> None:
    """A rollback must not race the PVE task it would undo (integration review)."""
    pve = FakePve(clock, {VZDUMP_UPID: 30 * MINUTE})
    store, _, executor, aid = await _approved(
        tmp_artifacts_dir, pve, _vzdump_body("    timeout_s: 60\n")
    )
    await executor.apply(aid, approved_by="admin")
    assert store.read(aid)[0]["unsettled_task"]

    with pytest.raises(ConflictError, match="still running"):
        await executor.revoke(aid, "admin", "changed my mind")
    fm, _ = store.read(aid)
    assert fm["status"] == "approved", "revoke went ahead under a running task"
    assert fm["unsettled_task"]["upid"] == VZDUMP_UPID

    # Once PVE is done, revoke settles the task and then proceeds.
    clock.advance(30 * MINUTE)
    await executor.revoke(aid, "admin", "changed my mind")
    fm, _ = store.read(aid)
    assert fm["status"] == "revoked"
    assert "unsettled_task" not in fm
    assert pve.posts("/vzdump") == 1


async def test_propose_refuses_a_bad_step_timeout(tmp_artifacts_dir: Path) -> None:
    lifecycle = ArtifactLifecycle(store=ArtifactStore(tmp_artifacts_dir))
    with pytest.raises(LifecycleError, match="timeout_s"):
        await lifecycle.propose(_spec(_vzdump_body("    timeout_s: 0\n")))


# --- the task record: 'unknown', then settled when it is read -----------------


async def test_the_task_reads_unknown_then_settles_to_succeeded(
    clock: FakeClock, tmp_artifacts_dir: Path, tmp_path: Path
) -> None:
    pve = FakePve(clock, {VZDUMP_UPID: 30 * MINUTE})
    store, lifecycle, executor, aid = await _approved(
        tmp_artifacts_dir, pve, _vzdump_body("    timeout_s: 60\n")
    )
    db = Database(str(tmp_path / "tasks.db"))
    await db.connect()
    try:
        # The real schema: a CHECK that does not admit 'unknown' fails here.
        await run_migrations(db)
        repo = TaskRepository(db)
        runner = TaskRunner(
            repo=repo, lifecycle=lifecycle, executor=executor, apply_reconciler=None, store=store
        )
        task_id = await repo.create_task(aid, "apply")

        await runner._run_apply(task_id, aid, "admin")

        task = await repo.get_task(task_id)
        assert task is not None
        assert task["status"] == "unknown", task
        assert VZDUMP_UPID in (task["error"] or "")
        recorded = json.loads(task["result_json"])
        assert recorded["unsettled_task"]["upid"] == VZDUMP_UPID

        # Read while PVE still runs it: unchanged.
        task = await runner.settle(task)
        assert task["status"] == "unknown"

        clock.advance(30 * MINUTE)
        task = await runner.settle(task)

        assert task["status"] == "succeeded", task
        assert store.read(aid)[0]["status"] == "applied"
        assert pve.posts("/vzdump") == 1
    finally:
        await db.close()
