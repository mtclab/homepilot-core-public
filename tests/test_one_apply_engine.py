"""One engine applies, replays and revokes an artifact (#423).

There were two. The API and UI went through `TaskRunner` -> `ArtifactExecutor`.
The CLI went through `mcp/executor_tools.apply_artifact` / `revoke_artifact`,
which had **no** host-provision dispatch, **no** pre-apply snapshot, **no**
approved-body hash-tamper check, **no** task row and **no** rollback: on revoke
it scanned for a `## Rollback` heading and printed "Rollback spec exists in
artifact body" while executing nothing - telling the operator a rollback had
happened with the host untouched.

Worse, `hp artifacts replay` routed through it and so bypassed the
`replay_safe: false` and replay-only guards `ArtifactExecutor.replay` enforces
and ARTIFACT_SPEC promises.

These gates assert the property that fixes all of it at once: there is exactly
ONE way in, and the guards live on it.
"""

from __future__ import annotations

import re
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

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"
CLI_SOURCE = SRC / "cli" / "main.py"


def _spec(artifact_id: str, **overrides: object) -> dict:
    spec: dict = {
        "id": artifact_id,
        "kind": "http-sequence",
        "intent": "One engine: replay guards must hold on the only path in",
        "body": "```yaml http-sequence\nsteps:\n  - method: GET\n    path: /health\n```\n",
        "target": {"kind": "service", "service": "demo", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "operator"},
    }
    spec.update(overrides)
    return spec


@pytest.fixture
async def engine(tmp_path: Path):
    """The real runner over the real executor. Only the per-kind handler that
    would touch a host is stubbed - every component that owns part of the
    contract stays real, including the guards under test."""
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
    executor._dispatch = AsyncMock(
        return_value=ExecutionResult(success=True, execution_log="stub: executed")
    )
    runner = TaskRunner(
        repo=TaskRepository(db),
        lifecycle=lifecycle,
        executor=executor,
        apply_reconciler=ApplyReconciler(store=store, executor=executor, repo=repo),
        store=store,
    )
    yield runner, store, lifecycle, executor
    await db.close()


@pytest.mark.asyncio
class TestReplayGuardsHoldOnTheOnlyPathIn:
    async def _applied(self, runner, lifecycle, artifact_id: str, **overrides) -> None:
        await lifecycle.propose(_spec(artifact_id, **overrides))
        await lifecycle.approve(artifact_id, "operator")
        started = await runner.start_apply(artifact_id, approved_by="operator")
        await runner.await_task(started["task_id"], timeout=10.0)

    async def test_a_not_replay_safe_artifact_is_refused(self, engine):
        """THE #423 bug: `hp artifacts replay` went through an engine that had
        never heard of `replay_safe`, so the guard the spec promises simply did
        not apply to the CLI."""
        runner, _store, lifecycle, executor = engine
        await self._applied(runner, lifecycle, "2026-08-21-not-replay-safe", replay_safe=False)
        calls_before = executor._dispatch.await_count

        started = await runner.start_replay("2026-08-21-not-replay-safe")
        task = await runner.await_task(started["task_id"], timeout=10.0)

        assert task["status"] == "failed", "a not-replay-safe artifact was replayed"
        assert "replay-safe" in (task["error"] or "")
        assert executor._dispatch.await_count == calls_before, (
            "the guard reported a refusal AFTER touching the host"
        )

    async def test_a_replay_only_artifact_is_refused_once_applied(self, engine):
        runner, _store, lifecycle, executor = engine
        await self._applied(runner, lifecycle, "2026-08-21-replay-only", idempotence="replay-only")
        calls_before = executor._dispatch.await_count

        started = await runner.start_replay("2026-08-21-replay-only")
        task = await runner.await_task(started["task_id"], timeout=10.0)

        assert task["status"] == "failed"
        assert "replay-only" in (task["error"] or "")
        assert executor._dispatch.await_count == calls_before

    async def test_a_tampered_body_is_refused(self, engine):
        """The shadow engine had no hash check at all, so an artifact edited
        after approval replayed whatever the file said now."""
        runner, store, lifecycle, executor = engine
        await self._applied(runner, lifecycle, "2026-08-21-tampered")
        path = store.resolve_path("2026-08-21-tampered")
        path.write_text(
            path.read_text(encoding="utf-8").replace("/health", "/pwned"), encoding="utf-8"
        )
        calls_before = executor._dispatch.await_count

        started = await runner.start_replay("2026-08-21-tampered")
        task = await runner.await_task(started["task_id"], timeout=10.0)

        assert task["status"] == "failed", "a body edited after approval was replayed"
        assert "tamper" in (task["error"] or "").lower()
        assert executor._dispatch.await_count == calls_before

    async def test_a_legitimate_replay_still_runs(self, engine):
        """The guards must refuse the dangerous cases WITHOUT breaking replay -
        a gate that blocks everything is not a gate."""
        runner, store, lifecycle, executor = engine
        await self._applied(runner, lifecycle, "2026-08-21-replayable")
        calls_before = executor._dispatch.await_count

        started = await runner.start_replay("2026-08-21-replayable")
        task = await runner.await_task(started["task_id"], timeout=10.0)

        assert task["status"] == "succeeded", task["error"]
        assert executor._dispatch.await_count == calls_before + 1
        fm, _ = store.read("2026-08-21-replayable")
        assert fm["status"] == ArtifactStatus.APPLIED.value

    async def test_the_replay_is_recorded_as_a_task(self, engine):
        """The shadow engine created no task row, so a CLI apply/replay left no
        trace an operator could find afterwards."""
        runner, _store, lifecycle, _executor = engine
        await self._applied(runner, lifecycle, "2026-08-21-recorded")

        started = await runner.start_replay("2026-08-21-recorded")
        task = await runner.await_task(started["task_id"], timeout=10.0)

        assert task["action"] == "replay"
        assert task["artifact_id"] == "2026-08-21-recorded"


class TestTheShadowEngineIsGone:
    def test_the_second_apply_engine_no_longer_exists(self) -> None:
        """`mcp/executor_tools.py` WAS the shadow engine. Deleting it is the fix;
        a behavioural test cannot catch someone re-adding a parallel path, so the
        absence itself is the assertion."""
        assert not (SRC / "mcp" / "executor_tools.py").exists(), (
            "the shadow apply/revoke engine is back - one engine, one contract"
        )

    def test_nothing_imports_it(self) -> None:
        offenders = [
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if "executor_tools" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"still referencing the deleted engine: {offenders}"

    def test_the_cli_does_not_execute_artifacts_in_process(self) -> None:
        """The CLI is a separate process: the executor's agent transport, its
        snapshots and its task rows all live in the backend. Anything that
        applies from here is by construction a second engine."""
        block = CLI_SOURCE.read_text(encoding="utf-8")
        start = block.index('@artifacts_app.command("apply")')
        end = block.index('@artifacts_app.command("push")')
        section = block[start:end]

        for forbidden in ("ArtifactExecutor", "executor_tools", "_dispatch", "mark_applied"):
            assert forbidden not in section, (
                f"`hp artifacts apply/replay/revoke` reaches for {forbidden} again - "
                "it must go through the backend API, which owns the one engine"
            )


class TestTheCliUsesTheOneEngine:
    def _section(self, command: str) -> str:
        text = CLI_SOURCE.read_text(encoding="utf-8")
        start = text.index(f'@artifacts_app.command("{command}")')
        rest = text[start:]
        nxt = rest.index("@artifacts_app.command", 1)
        return rest[:nxt]

    def test_apply_goes_to_the_apply_endpoint(self) -> None:
        assert "/apply" in self._section("apply")

    def test_replay_goes_to_the_replay_endpoint(self) -> None:
        """Replay is the command that made the shadow engine dangerous: it
        bypassed guards the engine it called had never heard of."""
        section = self._section("replay")
        assert "/replay" in section

    def test_revoke_goes_to_the_revoke_endpoint(self) -> None:
        section = self._section("revoke")
        assert "DELETE" in section and "/artifacts/" in section

    def test_every_one_of_them_waits_for_the_outcome(self) -> None:
        """A CLI that returns before the host has been touched is the same lie as
        a rollback that prints and does nothing."""
        text = CLI_SOURCE.read_text(encoding="utf-8")
        helper = text[
            text.index("def _run_artifact_task(") : text.index('@artifacts_app.command("apply")')
        ]
        assert "sync=true" in helper
        assert re.search(r'status in \(\s*"failed"', helper), (
            "the CLI does not fail when the task failed - it would report success "
            "for a change that did not happen"
        )
