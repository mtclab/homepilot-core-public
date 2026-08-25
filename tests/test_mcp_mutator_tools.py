"""MCP mutator tools <-> management API parity (wave 2).

Wave 1 gave the assistant the READ surface; this wave gives it the STANDARD
MUTATORS at the `full` scope. These tests assert the property that matters: not
"the handler returned ok" but that the write ACTUALLY HAPPENED - the repo/service
saw it, the task row flipped, the artifact transitioned - and that a `read_only`
token is refused every one of them.

The route<->tool coverage map and its anti-regression teeth live in
``tests/test_mcp_read_parity.py`` (``TestMutationParityGate``). This file is the
behavioural half.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.service import InventoryService
from homepilot.mcp.server import _handle_tool, _mcp_token_scope_var
from homepilot.metrics.repository import MetricsRepository
from homepilot.tasks.repository import TaskRepository

pytestmark = pytest.mark.asyncio


HOSTNAME = "mut-host"


@pytest.fixture
async def estate(tmp_path: Path):
    db = Database(str(tmp_path / "mut.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    task_repo = TaskRepository(db)
    metrics_repo = MetricsRepository(db)
    host_id = await repo.create_host(
        hostname=HOSTNAME, host_type="vm", role="app", cpu_cores=2, managed=True
    )
    yield SimpleNamespace(
        db=db,
        repo=repo,
        task_repo=task_repo,
        metrics_repo=metrics_repo,
        host_id=str(host_id),
    )
    await db.close()


@pytest.fixture
def ctx(estate):
    return {
        "repo": estate.repo,
        "database": estate.db,
        "task_repo": estate.task_repo,
        "metrics_repo": estate.metrics_repo,
        "inventory_service": InventoryService(estate.repo),
        "agent_adapter": None,
        "_mcp_token_scope": "full",
        "_mcp_caller_id": "mut-tester",
    }


# ── Inventory ────────────────────────────────────────────────────────────────


class TestInventoryMutators:
    async def test_add_host_writes_a_manual_adopted_host(self, ctx, estate) -> None:
        out = await _handle_tool("add_host", {"hostname": "nas01", "role": "storage"}, ctx)
        # The write really landed, and the return is the stored row.
        stored = await estate.repo.get_host_by_hostname("nas01")
        assert stored is not None
        assert out["id"] == stored["id"]
        assert out["source"] == "manual"
        assert out["import_state"] == "adopted"
        assert out["role"] == "storage"

    async def test_add_host_refuses_a_duplicate(self, ctx) -> None:
        await _handle_tool("add_host", {"hostname": "nas01"}, ctx)
        with pytest.raises(ValueError, match="already in inventory"):
            await _handle_tool("add_host", {"hostname": "nas01"}, ctx)

    async def test_add_host_refuses_a_non_hostname(self, ctx) -> None:
        with pytest.raises(ValueError, match="Invalid host"):
            await _handle_tool("add_host", {"hostname": "http://x/;rm -rf /"}, ctx)

    async def test_update_host_pins_the_edited_fields(self, ctx, estate) -> None:
        out = await _handle_tool(
            "update_host", {"host_id": estate.host_id, "description": "the app box"}, ctx
        )
        assert out["description"] == "the app box"
        reread = await estate.repo.get_host(estate.host_id)
        assert reread["description"] == "the app box"

    async def test_update_host_with_no_fields_is_refused(self, ctx, estate) -> None:
        with pytest.raises(ValueError, match="No valid fields"):
            await _handle_tool("update_host", {"host_id": estate.host_id}, ctx)

    async def test_update_host_unknown_id(self, ctx) -> None:
        with pytest.raises(ValueError, match="Host not found"):
            await _handle_tool("update_host", {"host_id": "nope", "status": "running"}, ctx)

    async def test_ignore_host_sets_import_state(self, ctx, estate) -> None:
        out = await _handle_tool("ignore_host", {"host_id": estate.host_id}, ctx)
        assert out["import_state"] == "ignored"
        assert (await estate.repo.get_host(estate.host_id))["import_state"] == "ignored"

    async def test_adopt_host_marks_managed_imported(self, ctx, estate) -> None:
        out = await _handle_tool("adopt_host", {"host_id": estate.host_id}, ctx)
        assert out["import_state"] == "adopted"
        assert out["source"] == "imported"
        reread = await estate.repo.get_host(estate.host_id)
        assert reread["import_state"] == "adopted"

    async def test_delete_host_forgets_a_manual_host(self, ctx, estate) -> None:
        created = await _handle_tool("add_host", {"hostname": "nas01"}, ctx)
        out = await _handle_tool("delete_host", {"host_id": created["id"]}, ctx)
        assert out["forgotten"] is True
        assert await estate.repo.get_host(created["id"]) is None

    async def test_delete_host_refuses_a_hypervisor_reported_host(self, ctx, estate) -> None:
        # The seeded host has no `absent_since` and source != manual, so the next
        # sync would bring it straight back: deleting it is refused.
        with pytest.raises(ValueError, match="still reported by the hypervisor"):
            await _handle_tool("delete_host", {"host_id": estate.host_id}, ctx)

    async def test_bulk_host_action_ignores_many(self, ctx, estate) -> None:
        b = await estate.repo.create_host(hostname="b", host_type="vm", role="app")
        out = await _handle_tool(
            "bulk_host_action", {"action": "ignore", "host_ids": [estate.host_id, str(b)]}, ctx
        )
        assert out == {"succeeded": 2, "failed": 0}
        assert (await estate.repo.get_host(str(b)))["import_state"] == "ignored"

    async def test_enrich_inventory_runs_and_returns_a_summary(self, ctx) -> None:
        out = await _handle_tool("enrich_inventory", {"host_ids": []}, ctx)
        assert isinstance(out, dict)


# ── KB ───────────────────────────────────────────────────────────────────────


class TestKbMutators:
    async def _make_doc(self, estate) -> int:
        return await estate.repo.create_doc_metadata(
            source="kb:mut", title="orig", content="orig body", kind="note", target=HOSTNAME
        )

    async def test_update_kb_doc_edits_and_returns_the_row(self, ctx, estate) -> None:
        doc_id = await self._make_doc(estate)
        out = await _handle_tool("update_kb_doc", {"doc_id": doc_id, "title": "new title"}, ctx)
        assert out["title"] == "new title"
        assert (await estate.repo.get_doc_metadata(doc_id))["title"] == "new title"

    async def test_update_kb_doc_unknown_id(self, ctx) -> None:
        with pytest.raises(ValueError, match="KB entry not found"):
            await _handle_tool("update_kb_doc", {"doc_id": 424242, "title": "x"}, ctx)

    # delete_kb_doc / ingest_kb / reindex_kb are NOT built this wave: their routes
    # are API require_scope('admin') and there is no admin MCP tier yet (see the
    # admin-wave exclusions in test_mcp_read_parity.py). Only update_kb_doc
    # (PUT = write) ships. Likewise the alert-rule create/update/delete tools:
    # POST/PATCH/DELETE /monitoring/rules are all API admin, so none is built.


# ── Tasks: cancel routes provision vs runner ─────────────────────────────────


class TestCancelTaskRouting:
    async def test_a_provision_cancel_reaches_the_provision_service(self, ctx, estate) -> None:
        task_id = await estate.task_repo.create_task(None, "provision")
        service = MagicMock()
        service.cancel = AsyncMock(return_value={"id": task_id, "status": "cancelled"})
        runner = MagicMock()
        runner.cancel_task = AsyncMock()
        out = await _handle_tool(
            "cancel_task",
            {"task_id": task_id},
            {**ctx, "provision_service": service, "task_runner": runner},
        )
        assert out["status"] == "cancelled"
        service.cancel.assert_awaited_once_with(task_id)
        runner.cancel_task.assert_not_awaited()

    async def test_a_non_provision_cancel_reaches_the_runner(self, ctx, estate) -> None:
        task_id = await estate.task_repo.create_task("2026-01-01-x-aaa", "apply")
        service = MagicMock()
        service.cancel = AsyncMock()
        runner = MagicMock()
        runner.cancel_task = AsyncMock(return_value={"id": task_id, "status": "cancelled"})
        out = await _handle_tool(
            "cancel_task",
            {"task_id": task_id},
            {**ctx, "provision_service": service, "task_runner": runner},
        )
        assert out["status"] == "cancelled"
        runner.cancel_task.assert_awaited_once_with(task_id)
        service.cancel.assert_not_awaited()

    async def test_cancel_unknown_task_is_refused(self, ctx) -> None:
        with pytest.raises(ValueError, match="Task not found"):
            await _handle_tool(
                "cancel_task",
                {"task_id": "no-such"},
                {**ctx, "provision_service": None, "task_runner": MagicMock()},
            )


# ── Artifacts: plan / preview / reject / apply / replay ──────────────────────

PLAN_SPEC_BODY = """
Install nginx.

```yaml host-provision-spec
packages:
  - nginx
```
"""


def _plan_agent(*, installed: bool) -> Any:
    agent = MagicMock()

    async def exec_readonly(host: str, command: str):
        if command == "dpkg -s nginx":
            return (0, "install ok installed", "") if installed else (1, "", "not found")
        return (1, "", "unexpected")

    agent.exec_readonly = AsyncMock(side_effect=exec_readonly)
    agent.read_file = AsyncMock(return_value="")
    return agent


class TestArtifactPlanAndPreview:
    def _store(self, fm: dict[str, Any], body: str) -> Any:
        store = MagicMock()
        store.read = MagicMock(return_value=(fm, body))
        return store

    async def test_plan_returns_the_host_plan(self, ctx) -> None:
        fm = {"id": "a1", "kind": "host-provision", "status": "approved", "target": {"host": "h"}}
        lifecycle = MagicMock()
        lifecycle._executor_ref = SimpleNamespace(agent=_plan_agent(installed=False))
        out = await _handle_tool(
            "plan_artifact",
            {"artifact_id": "a1"},
            {
                **ctx,
                "store": self._store(fm, PLAN_SPEC_BODY),
                "lifecycle": lifecycle,
                "kb_service": None,
            },
        )
        assert out["host"] == "h"
        assert out["change_count"] == 1
        assert out["in_spec"] is False

    async def test_plan_refuses_an_unsupported_kind(self, ctx) -> None:
        fm = {"id": "a1", "kind": "kb-note", "status": "approved", "target": {"host": "h"}}
        with pytest.raises(ValueError, match="No plan engine"):
            await _handle_tool(
                "plan_artifact",
                {"artifact_id": "a1"},
                {
                    **ctx,
                    "store": self._store(fm, "body"),
                    "lifecycle": MagicMock(_executor_ref=None),
                    "kb_service": None,
                },
            )

    async def test_preview_returns_the_frontmatter_and_a_diff_field(self, ctx) -> None:
        fm = {"id": "a1", "kind": "kb-note", "status": "proposed", "intent": "note"}
        store = MagicMock()
        store.read = MagicMock(return_value=(fm, "body"))
        store.resolve_path = MagicMock(side_effect=RuntimeError("no git here"))
        out = await _handle_tool("preview_artifact", {"artifact_id": "a1"}, {**ctx, "store": store})
        assert out["id"] == "a1"
        assert out["status"] == "proposed"
        assert "diff" in out

    async def test_plan_and_preview_are_callable_by_a_read_only_token(self, ctx) -> None:
        """plan/preview mirror API `read` routes and change no host, so a read_only
        MCP token must reach them (they are NOT mutators)."""
        fm = {"id": "a1", "kind": "host-provision", "status": "approved", "target": {"host": "h"}}
        lifecycle = MagicMock()
        lifecycle._executor_ref = SimpleNamespace(agent=_plan_agent(installed=True))
        ro = {**ctx, "_mcp_token_scope": "read_only"}
        token = _mcp_token_scope_var.set("read_only")
        try:
            plan = await _handle_tool(
                "plan_artifact",
                {"artifact_id": "a1"},
                {
                    **ro,
                    "store": self._store(fm, PLAN_SPEC_BODY),
                    "lifecycle": lifecycle,
                    "kb_service": None,
                },
            )
            assert plan["host"] == "h"
            store = MagicMock()
            store.read = MagicMock(return_value=(fm, "body"))
            store.resolve_path = MagicMock(side_effect=RuntimeError("no git"))
            prev = await _handle_tool(
                "preview_artifact", {"artifact_id": "a1"}, {**ro, "store": store}
            )
            assert prev["id"] == "a1"
        finally:
            _mcp_token_scope_var.reset(token)


class TestArtifactRejectApplyReplay:
    def _runner(self, task_repo, *, status: str, store):
        from homepilot.tasks.runner import TaskRunner

        lifecycle = MagicMock()
        lifecycle.mark_applied = AsyncMock()
        return TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=store,
        )

    def _store(self, status: str) -> Any:
        store = MagicMock()
        store.read = MagicMock(
            return_value=({"id": "a1", "kind": "shell-script", "status": status}, "body")
        )
        return store

    async def test_reject_marks_the_artifact_rejected(self, ctx) -> None:
        lifecycle = MagicMock()
        lifecycle.reject = AsyncMock()
        out = await _handle_tool(
            "reject_artifact",
            {"artifact_id": "a1", "reason": "nope"},
            {**ctx, "lifecycle": lifecycle},
        )
        assert out == {"id": "a1", "status": "rejected"}
        lifecycle.reject.assert_awaited_once()
        assert lifecycle.reject.await_args.args[0] == "a1"

    async def test_reject_surfaces_a_lifecycle_refusal(self, ctx) -> None:
        from homepilot.artifacts.lifecycle import ConflictError

        lifecycle = MagicMock()
        lifecycle.reject = AsyncMock(
            side_effect=ConflictError("Invalid transition: applied -> reject")
        )
        with pytest.raises(ValueError, match="Invalid transition"):
            await _handle_tool(
                "reject_artifact", {"artifact_id": "a1"}, {**ctx, "lifecycle": lifecycle}
            )

    async def test_apply_of_a_non_approved_artifact_is_refused(self, ctx, estate) -> None:
        """The human-approval gate: apply must refuse anything not APPROVED, and
        it must do so BEFORE creating any task."""
        store = self._store("proposed")
        runner = self._runner(estate.task_repo, status="proposed", store=store)
        with pytest.raises(ValueError, match="Invalid transition"):
            await _handle_tool(
                "apply_artifact", {"artifact_id": "a1"}, {**ctx, "task_runner": runner}
            )
        # No task was created by the refused apply.
        assert await estate.task_repo.list_tasks("a1", limit=10) == []

    async def test_apply_of_an_approved_artifact_starts_a_task(self, ctx, estate) -> None:
        store = self._store("approved")
        runner = self._runner(estate.task_repo, status="approved", store=store)
        out = await _handle_tool(
            "apply_artifact", {"artifact_id": "a1"}, {**ctx, "task_runner": runner}
        )
        assert out["action"] == "apply"
        assert out["artifact_id"] == "a1"
        tasks = await estate.task_repo.list_tasks("a1", limit=10)
        assert len(tasks) == 1 and tasks[0]["action"] == "apply"

    async def test_replay_of_a_proposed_artifact_is_refused(self, ctx, estate) -> None:
        store = self._store("proposed")
        runner = self._runner(estate.task_repo, status="proposed", store=store)
        with pytest.raises(ValueError, match="Invalid transition"):
            await _handle_tool(
                "replay_artifact", {"artifact_id": "a1"}, {**ctx, "task_runner": runner}
            )

    async def test_replay_of_an_applied_artifact_starts_a_task(self, ctx, estate) -> None:
        store = self._store("applied")
        runner = self._runner(estate.task_repo, status="applied", store=store)
        out = await _handle_tool(
            "replay_artifact", {"artifact_id": "a1"}, {**ctx, "task_runner": runner}
        )
        assert out["action"] == "replay"
        tasks = await estate.task_repo.list_tasks("a1", limit=10)
        assert len(tasks) == 1 and tasks[0]["action"] == "replay"


# ── Scope: every new mutator is denied a read_only token ─────────────────────

# Every FULL-scope mutator this wave ships (plan/preview are read_only, so they
# are NOT here - they must be REACHABLE by a read_only token, asserted above).
_NEW_MUTATORS: list[tuple[str, dict[str, Any]]] = [
    ("cancel_task", {"task_id": "x"}),
    ("add_host", {"hostname": "z"}),
    ("adopt_host", {"host_id": "1"}),
    ("ignore_host", {"host_id": "1"}),
    ("update_host", {"host_id": "1", "status": "running"}),
    ("delete_host", {"host_id": "1"}),
    ("enrich_inventory", {}),
    ("bulk_host_action", {"action": "ignore", "host_ids": []}),
    ("update_kb_doc", {"doc_id": 1, "title": "x"}),
    ("reject_artifact", {"artifact_id": "a1"}),
    ("apply_artifact", {"artifact_id": "a1"}),
    ("replay_artifact", {"artifact_id": "a1"}),
]


class TestReadOnlyScopeIsRefusedEveryMutator:
    @pytest.mark.parametrize("name,arguments", _NEW_MUTATORS)
    async def test_read_only_token_is_denied(
        self, ctx, name: str, arguments: dict[str, Any]
    ) -> None:
        token = _mcp_token_scope_var.set("read_only")
        try:
            with pytest.raises(ValueError, match="requires write scope"):
                await _handle_tool(name, arguments, {**ctx, "_mcp_token_scope": "read_only"})
        finally:
            _mcp_token_scope_var.reset(token)

    async def test_the_guard_lets_a_full_token_through(self, ctx, estate) -> None:
        """Guard the guard: the SAME harness must NOT refuse a full-scope call, so
        the refusals above are the scope check firing, not a blanket failure."""
        out = await _handle_tool(
            "add_host", {"hostname": "through01"}, {**ctx, "_mcp_token_scope": "full"}
        )
        assert out["hostname"] == "through01"

    async def test_every_parametrized_mutator_is_a_real_registered_tool(self) -> None:
        """Guard the guard: a typo in the list above would make a case vacuous."""
        from homepilot.mcp.server import _MUTATING_TOOLS, _TOOL_HANDLERS

        for name, _ in _NEW_MUTATORS:
            assert name in _TOOL_HANDLERS, f"{name} is not a registered tool"
            assert name in _MUTATING_TOOLS, f"{name} is not marked mutating"
