"""The apply journey, with nothing mocked between the parts.

Every stage of apply is well tested in isolation, and each of those tests puts a
mock at the boundary to the next stage: TaskRunner tests pass a MagicMock
lifecycle, ApplyReconciler tests pass an AsyncMock executor. So a real
TaskRunner, a real ApplyReconciler, a real ArtifactExecutor and a real
ArtifactLifecycle were never in the same process, and the seam between them was
the one place nothing looked.

Only the leaf - the per-kind handler that would touch a host - is stubbed. Every
component that owns a piece of the apply CONTRACT is real, including the store,
the database and the transition table.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import ArtifactStatus
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor, ExecutionResult
from homepilot.reconciler.apply import ApplyReconciler
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner


def _spec(artifact_id: str = "2026-08-20-journey-apply-a1b2c3") -> dict:
    return {
        "id": artifact_id,
        "kind": "http-sequence",
        "intent": "Journey: apply an artifact end to end",
        "body": "```yaml http-sequence\nsteps:\n  - method: GET\n    path: /health\n```\n",
        "target": {"kind": "service", "service": "demo", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "operator"},
    }


@pytest.fixture
async def journey(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")

    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    executor = ArtifactExecutor(
        store=store,
        lifecycle=lifecycle,
        repo=repo,
        proxmox=AsyncMock(),
        vault=AsyncMock(),
    )
    # The ONLY stub: the per-kind handler that would reach a host. Everything
    # that owns part of the apply contract stays real.
    executor._dispatch = AsyncMock(
        return_value=ExecutionResult(success=True, execution_log="stub: applied")
    )
    task_repo = TaskRepository(db)
    runner = TaskRunner(
        repo=task_repo,
        lifecycle=lifecycle,
        executor=executor,
        apply_reconciler=ApplyReconciler(store=store, executor=executor, repo=repo),
        store=store,
    )
    yield runner, task_repo, store, lifecycle
    await db.close()


class TestApplyJourney:
    async def test_a_successful_apply_reports_success(self, journey):
        """The goal: an operator who applies a working artifact is told it worked.

        Teeth: this is red whenever more than one component performs the
        APPROVED -> APPLIED transition. The second one is an illegal
        applied -> applied, the runner catches it, and the task lands 'failed'
        while the host has in fact been changed and the artifact is 'applied'.
        """
        runner, _task_repo, store, lifecycle = journey
        artifact_id = await lifecycle.propose(_spec())
        await lifecycle.approve(artifact_id, "operator")

        started = await runner.start_apply(artifact_id, approved_by="operator")
        task = await runner.await_task(started["task_id"], timeout=10.0)

        fm, _ = store.read(artifact_id)
        assert fm["status"] == ArtifactStatus.APPLIED.value, "the artifact should be applied"
        assert task["status"] == "succeeded", (
            f"apply succeeded and the artifact is {fm['status']}, "
            f"but the task reports {task['status']}: {task['error']}"
        )
        assert not task["error"]

    async def test_a_failed_apply_reports_failure(self, journey):
        runner, _task_repo, store, lifecycle = journey
        artifact_id = await lifecycle.propose(_spec("2026-08-20-journey-fails-d4e5f6"))
        await lifecycle.approve(artifact_id, "operator")
        runner.executor._dispatch = AsyncMock(
            return_value=ExecutionResult(
                success=False, execution_log="", failure_reason="host refused"
            )
        )

        started = await runner.start_apply(artifact_id, approved_by="operator")
        task = await runner.await_task(started["task_id"], timeout=10.0)

        fm, _ = store.read(artifact_id)
        assert fm["status"] == ArtifactStatus.FAILED.value
        assert task["status"] == "failed"
        assert "host refused" in (task["error"] or "")

    async def test_exactly_one_component_records_the_transition(self, journey):
        """The audit log tells us whether the transition happened twice.

        A duplicated apply is not only a wrong task status: it doubles the audit
        row that says who applied what, which is the record an operator trusts.
        """
        runner, _task_repo, _store, lifecycle = journey
        artifact_id = await lifecycle.propose(_spec("2026-08-20-journey-audit-778899"))
        await lifecycle.approve(artifact_id, "operator")

        started = await runner.start_apply(artifact_id, approved_by="operator")
        await runner.await_task(started["task_id"], timeout=10.0)

        rows = await lifecycle.repo.db.fetchall(
            "SELECT user_id FROM audit_log WHERE artifact_id = ? AND action = 'apply'",
            (artifact_id,),
        )
        # Exactly one row names WHO applied it. The transition layer also writes
        # an actorless row, which is a separate duplication tracked for the
        # architecture epic; what must never happen is two rows both claiming an
        # actor, because then "who applied this" has two answers.
        with_actor = [r["user_id"] for r in rows if r["user_id"] not in ("", "system", None)]
        assert with_actor == ["operator"], f"expected one actor-bearing apply row, got {with_actor}"
