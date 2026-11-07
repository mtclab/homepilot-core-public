from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..artifacts.lifecycle import ArtifactLifecycle, ConflictError
from ..artifacts.models import ArtifactBusyError, ArtifactStatus
from ..artifacts.store import ArtifactStore
from ..background import DEFAULT_DRAIN_TIMEOUT, drain_tasks
from ..executor.orchestrator import ArtifactExecutor
from .repository import TERMINAL_TASK_STATUSES, TaskRepository

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(
        self,
        repo: TaskRepository,
        lifecycle: ArtifactLifecycle,
        executor: ArtifactExecutor | None,
        apply_reconciler: Any | None,
        store: ArtifactStore,
    ):
        self.repo = repo
        self.lifecycle = lifecycle
        self.executor = executor
        self.apply_reconciler = apply_reconciler
        self.store = store
        self._running_tasks: set[asyncio.Task[Any]] = set()
        # task_id → in-flight asyncio.Task, so cancel() can reach the coroutine
        # that is actually executing an apply/revoke. Cleared on completion.
        self._task_by_id: dict[str, asyncio.Task[Any]] = {}

    def _track_task(self, task: asyncio.Task[Any], task_id: str) -> None:
        self._running_tasks.add(task)
        self._task_by_id[task_id] = task

        def _done(t: asyncio.Task[Any]) -> None:
            self._running_tasks.discard(t)
            # Only drop the mapping if it still points at THIS task — a fresh
            # task for a reused id (shouldn't happen: ids are uuid4) must win.
            if self._task_by_id.get(task_id) is t:
                del self._task_by_id[task_id]

        task.add_done_callback(_done)

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Let in-flight apply/revoke/replay runs finish before the database
        goes away (#496). Nothing else awaits them - they run behind an already
        -accepted request - so a shutdown is where they get lost."""
        await drain_tasks(self._running_tasks, "artifact run(s)", timeout)

    async def _claim(self, artifact_id: str, action: str) -> str:
        """Take the artifact's ONE run slot for `action`, or say who holds it (#710).

        Every apply, replay and revoke - from the API, MCP, the CLI through the
        API, and auto-apply - starts here, and the claim is a single INSERT the
        database refuses while another pending/running row exists for the
        artifact (migration 34's partial unique index). So the guarantee holds
        across connections and processes, not just inside this event loop: two
        concurrent replays used to both pass a check-then-insert with an await
        between them.

        A refused claim raises `ArtifactBusyError` naming the run that holds the
        slot - every transport turns that into a 409 (or a tool error) that says
        which task to wait on or cancel.
        """
        # Two attempts: the holder can finish between our refused insert and the
        # read that names it, and then the slot is simply free.
        for _ in range(2):
            task_id = await self.repo.create_task_if_no_active(artifact_id, action)
            if task_id is not None:
                return task_id
            active = await self.repo.get_active_task(artifact_id)
            if active is not None:
                raise ArtifactBusyError(
                    artifact_id, active["id"], active["action"], active["status"]
                )
        raise ConflictError(
            f"{artifact_id} is busy with another run that finished while this one "
            "was being started; ask again."
        )

    async def start_apply(self, artifact_id: str, approved_by: str = "system") -> dict[str, Any]:
        # Validate the artifact BEFORE creating a task (#386). An already-APPLIED
        # (or otherwise non-approved) artifact must be rejected cleanly here: if
        # we created the task first and then rejected, that task would be stranded
        # (executor.apply raises, the APPLIED→FAILED transition also raises, and
        # the task never leaves 'running'), blocking all future apply/revoke.
        try:
            fm, _body = self.store.read(artifact_id)
        except FileNotFoundError:
            raise ValueError(f"Artifact not found: {artifact_id}") from None

        status = ArtifactStatus(fm["status"])
        if status != ArtifactStatus.APPROVED:
            raise ConflictError(f"Invalid transition: {status.value} → apply")

        task_id = await self._claim(artifact_id, "apply")
        task = asyncio.create_task(self._run_apply(task_id, artifact_id, approved_by))
        self._track_task(task, task_id)

        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "action": "apply",
            "status": "pending",
        }

    async def start_revoke(
        self, artifact_id: str, user: str = "system", reason: str | None = None
    ) -> dict[str, Any]:
        try:
            fm, _body = self.store.read(artifact_id)
        except FileNotFoundError:
            raise ValueError(f"Artifact not found: {artifact_id}") from None

        status = ArtifactStatus(fm["status"])
        if status not in (ArtifactStatus.APPROVED, ArtifactStatus.APPLIED, ArtifactStatus.FAILED):
            raise ConflictError(f"Invalid transition: {status.value} → revoke")

        # A revoke beside ANY other run is refused - a second revoke included:
        # two rollbacks of one change ran in parallel before #710.
        task_id = await self._claim(artifact_id, "revoke")
        task = asyncio.create_task(self._run_revoke(task_id, artifact_id, user, reason))
        self._track_task(task, task_id)

        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "action": "revoke",
            "status": "pending",
        }

    async def start_replay(self, artifact_id: str) -> dict[str, Any]:
        """Re-apply an already-applied artifact, through the ONE engine (#423).

        Replay used to exist only on the CLI, where it called a second, weaker
        apply path - so `hp artifacts replay` bypassed the `replay_safe: false`
        and replay-only guards that `ArtifactExecutor.replay` enforces and
        ARTIFACT_SPEC promises. Those guards live in the executor, so the fix is
        to have exactly one way in.
        """
        try:
            fm, _body = self.store.read(artifact_id)
        except FileNotFoundError:
            raise ValueError(f"Artifact not found: {artifact_id}") from None

        status = ArtifactStatus(fm["status"])
        if status not in (ArtifactStatus.APPROVED, ArtifactStatus.APPLIED):
            raise ConflictError(f"Invalid transition: {status.value} -> replay")

        task_id = await self._claim(artifact_id, "replay")
        task = asyncio.create_task(self._run_replay(task_id, artifact_id))
        self._track_task(task, task_id)

        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "action": "replay",
            "status": "pending",
        }

    async def run_apply_now(self, artifact_id: str, approved_by: str) -> dict[str, Any]:
        """Apply through the task gate and WAIT for the outcome (auto-apply, #710).

        The auto-apply reconciler used to call the executor directly, with no
        task row, so a tick during a running apply dispatched the same artifact
        again; the second run then died on `applied -> applied` and the
        uncaught error skipped every later approved artifact in that tick. It
        now claims the same slot every other run does - a busy artifact raises
        `ArtifactBusyError` and is left to the run that holds it - and the run
        is a tracked task, so cancel and shutdown drain reach it.

        Returns the finished task row.
        """
        task_id = await self._claim(artifact_id, "apply")
        task = asyncio.create_task(self._run_apply(task_id, artifact_id, approved_by))
        self._track_task(task, task_id)
        # `wait`, not `await task`: a cancel of THIS run (hp/UI cancel) must
        # end this run only, not raise into the reconciler's loop over the rest.
        await asyncio.wait({task})
        row = await self.repo.get_task(task_id)
        return row or {"id": task_id, "artifact_id": artifact_id, "status": "failed"}

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        # Best-effort stop-then-mark. If we still hold the in-flight asyncio.Task
        # (same process, still running) cancel it — the CancelledError raised into
        # _run_apply/_run_revoke is a BaseException, so their `except Exception`
        # never fires and never overwrites the record we mark below. If the task
        # is gone (a different process, or after a restart) we STILL mark the
        # record cancelled so it stops blocking future applies for the artifact.
        # Cancelling an already-finished task is a no-op returning its status.
        at = self._task_by_id.get(task_id)
        if at is not None and not at.done():
            at.cancel()
        result = await self.repo.cancel_task(task_id)
        if result is None:
            raise ValueError(f"Task not found: {task_id}")
        return result

    async def await_task(self, task_id: str, timeout: float = 300.0) -> dict[str, Any]:
        interval = 1.0
        max_interval = 10.0
        elapsed = 0.0
        while elapsed < timeout:
            task = await self.repo.get_task(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task["status"] in TERMINAL_TASK_STATUSES:
                return task
            await asyncio.sleep(interval)
            elapsed += interval
            interval = min(interval * 2, max_interval)
        task = await self.repo.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        return task

    # How much execution output a task row keeps. Enough to diagnose an apply
    # without letting one chatty run bloat the table the Tasks page reads - the
    # full text is still in the artifact's applied record.
    _MAX_STORED_LOG = 20000

    async def _record_outcome(self, task_id: str, result: Any) -> None:
        """Finish a task, KEEPING the execution output (#445 A3).

        The executor produces an execution_log for every apply and the runner
        used to drop it on the floor, so the one artefact that says what actually
        happened on the host existed for the length of one function call and was
        then unrecoverable. A failed apply left an operator with a one-line
        `error` and nothing to diagnose from.
        """
        log = str(getattr(result, "execution_log", "") or "")
        if len(log) > self._MAX_STORED_LOG:
            # Truncate from the FRONT: the end of a log is where the failure is.
            kept = log[-self._MAX_STORED_LOG :]
            log = (
                f"[earlier output truncated, showing the last {self._MAX_STORED_LOG} chars]\n{kept}"
            )
        payload = json.dumps({"execution_log": log}) if log else None

        unsettled = getattr(result, "unsettled_task", None)
        if isinstance(unsettled, dict) and unsettled:
            # Neither verdict is true yet (#774): the PVE task is still running.
            # The UPID rides on the record so an operator can look at it in PVE.
            payload = json.dumps(
                {"execution_log": log, "outcome": "unknown", "unsettled_task": unsettled}
            )
            await self.repo.update_task_status(
                task_id,
                "unknown",
                error=getattr(result, "failure_reason", None)
                or f"outcome unknown: PVE task {unsettled.get('upid')} is still running",
                result_json=payload,
            )
        elif getattr(result, "success", False):
            await self.repo.update_task_status(task_id, "succeeded", result_json=payload)
        else:
            await self.repo.update_task_status(
                task_id,
                "failed",
                error=getattr(result, "failure_reason", None) or "apply failed",
                result_json=payload,
            )

    async def settle(self, task: dict[str, Any]) -> dict[str, Any]:
        """Bring an 'unknown' apply/replay task to its verdict, if PVE has one.

        A task reads 'unknown' when its run stopped following a PVE task that
        was still running (#774). Reading it is the natural moment to ask PVE
        how that task ended. Returns the task as it now stands; anything that is
        not an unknown apply/replay is returned untouched.
        """
        if (
            task.get("status") != "unknown"
            or task.get("action") not in ("apply", "replay")
            or not task.get("artifact_id")
            or self.executor is None
        ):
            return task
        try:
            result = await self.executor.settle(str(task["artifact_id"]))
        except Exception:
            # Settling is opportunistic: the task stays 'unknown', which is
            # still the truth, and the next read asks again.
            logger.warning("Could not settle task %s", task.get("id"), exc_info=True)
            return task
        if result is None or result.unsettled_task:
            return task
        await self._record_outcome(str(task["id"]), result)
        settled = await self.repo.get_task(str(task["id"]))
        return settled if settled is not None else task

    async def _run_apply(self, task_id: str, artifact_id: str, approved_by: str) -> None:
        await self.repo.update_task_status(task_id, "running")
        try:
            if self.apply_reconciler is not None:
                # ArtifactExecutor.apply already performs the artifact transition
                # and writes the audit row (orchestrator.py). Repeating it here
                # meant a SUCCESSFUL apply attempted applied -> applied, which the
                # transition table forbids, so every apply through the API and the
                # UI - the product's primary action - reported the task as failed
                # while the host had in fact been changed. This branch owns the
                # TASK record only; the artifact's state has exactly one owner.
                result = await self.apply_reconciler.apply_single(artifact_id, approved_by)
                await self._record_outcome(task_id, result)
            elif self.executor is not None:
                result = await self.executor.apply(artifact_id, approved_by)
                await self._record_outcome(task_id, result)
            else:
                # No executor means NOTHING can run - and an apply that runs
                # nothing must not say "applied". This branch used to
                # mark_applied("Applied without executor") and succeed, which is
                # the product recording a change it never made (caught live on a
                # proxmox-less instance: a guest-network apply "succeeded" with
                # an empty log and no cluster). Handled here rather than raised:
                # the artifact must STAY approved - it is the instance that is
                # missing a limb, not the change that is wrong - and the except
                # below would mark it failed.
                await self.repo.update_task_status(
                    task_id,
                    "failed",
                    error=(
                        "no executor configured - this instance cannot apply "
                        "artifacts (Proxmox and vault are not wired up); the "
                        "artifact stays approved"
                    ),
                )
        except Exception as exc:
            logger.exception("Apply task %s failed for artifact %s", task_id, artifact_id)
            error_msg = str(exc)
            # Marking the artifact failed is best-effort: it may already be in a
            # terminal state that forbids the →failed transition (e.g. an
            # apply-on-APPLIED raises ConflictError). That must NEVER prevent the
            # task itself from being marked failed — otherwise the task is stranded
            # in 'running' forever and blocks all future apply/revoke (#386).
            try:
                await self.lifecycle.mark_failed(artifact_id, error_msg)
            except Exception:
                logger.warning("Could not mark artifact %s as failed", artifact_id, exc_info=True)
            await self.repo.update_task_status(task_id, "failed", error=error_msg)

    async def _run_replay(self, task_id: str, artifact_id: str) -> None:
        await self.repo.update_task_status(task_id, "running")
        try:
            if self.executor is None:
                raise RuntimeError("no executor configured - cannot replay")
            # `executor.replay` owns the guards (replay_safe, replay-only,
            # tamper) AND the artifact's state; this branch owns the task record.
            result = await self.executor.replay(artifact_id)
            await self._record_outcome(task_id, result)
        except Exception as exc:
            logger.exception("Replay task %s failed for artifact %s", task_id, artifact_id)
            await self.repo.update_task_status(task_id, "failed", error=str(exc))

    async def _run_revoke(
        self, task_id: str, artifact_id: str, user: str, reason: str | None
    ) -> None:
        await self.repo.update_task_status(task_id, "running")
        try:
            outcome = None
            if self.executor is not None:
                outcome = await self.executor.revoke(artifact_id, user, reason)
            else:
                await self.lifecycle.revoke(artifact_id, user, reason)
            # Carry "reversed vs relabelled" onto the task record (#426): the
            # revoke SUCCEEDED either way - the artifact is revoked - but whether
            # the host was actually put back is a different fact, and it is the
            # one an operator undoing a change came here for.
            payload = None
            if outcome is not None:
                payload = json.dumps(
                    {
                        "rolled_back": outcome.rolled_back,
                        "rollback_detail": outcome.reason,
                        "execution_log": outcome.execution_log,
                    }
                )
            await self.repo.update_task_status(task_id, "succeeded", result_json=payload)
        except Exception as exc:
            logger.exception("Revoke task %s failed for artifact %s", task_id, artifact_id)
            await self.repo.update_task_status(task_id, "failed", error=str(exc))
