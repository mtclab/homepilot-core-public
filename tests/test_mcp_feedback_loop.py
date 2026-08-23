"""The AI half of "AI-first" can close its loop (#427).

Four holes, all of them one-way: the agent could act and never learn.

* **It could never read an artifact body.** `get_artifact_status` returns six
  metadata fields; `query_artifacts` is frontmatter-only. The execution log is
  appended to the BODY on apply, so an agent could not see what happened when its
  own artifact ran, and could not use a prior artifact as a pattern.
* **No task tools.** Results lived under `/tasks` with no MCP equivalent.
* **`host-provision` was invisible.** The `propose_artifact` schema enumerated the
  six legacy kinds, so an agent reading it could not know the one working
  provisioning kind existed.
* **`get_environment_doc` under-delivered against its own description** - it
  advertised KB intent and rendered none, while the correct implementation sat
  unused on the service.
* **No reachability check.** An artifact targeting a host with no connected agent
  fails at apply, and the AI could not find that out before proposing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import _TOOL_DEFINITIONS, _TOOL_HANDLERS
from homepilot.mcp.tools.artifact_tools import handle_get_artifact, handle_get_task_result
from homepilot.mcp.tools.system_tools import handle_check_host_reachable
from homepilot.tasks.repository import TaskRepository

pytestmark = pytest.mark.asyncio

BODY = """\
## Plan
Restart nginx
## Idempotence preamble
Idempotent: guarded.
## Spec
```bash shell-spec
#!/bin/bash
systemctl restart nginx
```
"""


def _tool(name: str) -> dict:
    return next(t for t in _TOOL_DEFINITIONS if t["name"] == name)


@pytest.fixture
async def ctx(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    yield {
        "store": store,
        "lifecycle": lifecycle,
        "repo": repo,
        "task_repo": TaskRepository(db),
    }
    await db.close()


class TestTheAgentCanReadBackWhatItProposed:
    async def test_get_artifact_returns_the_body(self, ctx):
        """The execution log is appended to the BODY on apply, so without a body
        an agent cannot see what happened when its own artifact ran."""
        artifact_id = "2026-08-21-mcp-readback"
        await ctx["lifecycle"].propose(
            {
                "id": artifact_id,
                "kind": "shell-script",
                "intent": "Restart nginx",
                "body": BODY,
                "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
                "idempotence": "via-precheck",
                "produced_by": {"session": "s", "agent": "a", "user": "u"},
            }
        )

        result = await handle_get_artifact({"artifact_id": artifact_id}, ctx)

        assert "systemctl restart nginx" in result["body"]
        assert result["frontmatter"]["intent"] == "Restart nginx"

    async def test_an_unknown_artifact_is_an_error_not_an_empty_body(self, ctx):
        with pytest.raises(ValueError, match="not found"):
            await handle_get_artifact({"artifact_id": "2026-01-01-nope"}, ctx)

    async def test_a_huge_body_keeps_its_tail(self, ctx):
        """The end of an execution log is where the failure is."""
        artifact_id = "2026-08-21-mcp-huge"
        await ctx["lifecycle"].propose(
            {
                "id": artifact_id,
                "kind": "kb-note",
                "intent": "Big note",
                "body": ("x" * 60000) + "THE-END",
                "produced_by": {"session": "s", "agent": "a", "user": "u"},
            }
        )

        result = await handle_get_artifact({"artifact_id": artifact_id}, ctx)

        assert result["truncated"] is True
        assert result["body"].endswith("THE-END")


class TestTheAgentCanLearnTheOutcome:
    async def test_get_task_result_returns_the_execution_log(self, ctx):
        task_repo = ctx["task_repo"]
        task_id = await task_repo.create_task("2026-08-21-task-art", "apply")
        await task_repo.update_task_status(
            task_id,
            "failed",
            error="host refused",
            result_json=json.dumps({"execution_log": "line 1\nline 2: refused"}),
        )

        result = await handle_get_task_result({"task_id": task_id}, ctx)

        assert result["status"] == "failed"
        assert result["error"] == "host refused"
        assert "refused" in result["execution_log"]

    async def test_it_can_be_asked_by_artifact(self, ctx):
        """An agent knows the artifact it proposed; it does not know a task id."""
        task_repo = ctx["task_repo"]
        task_id = await task_repo.create_task("2026-08-21-by-artifact", "apply")
        await task_repo.update_task_status(task_id, "succeeded")

        result = await handle_get_task_result({"artifact_id": "2026-08-21-by-artifact"}, ctx)

        assert result["id"] == task_id
        assert result["status"] == "succeeded"

    async def test_asking_for_nothing_is_an_error(self, ctx):
        with pytest.raises(ValueError, match="task_id or artifact_id"):
            await handle_get_task_result({}, ctx)


class TestTheAgentCanSeeTheWorkingProvisioningKind:
    async def test_host_provision_is_in_the_propose_schema(self):
        """An agent reading the schema could not know the one working
        provisioning kind existed."""
        description = _tool("propose_artifact")["inputSchema"]["properties"]["spec"]["description"]

        assert "host-provision" in description

    async def test_the_new_tools_are_registered(self):
        """A definition nothing dispatches is decoration."""
        for name in ("get_artifact", "get_task_result", "check_host_reachable"):
            assert name in _TOOL_HANDLERS, f"{name} is defined but not dispatched"
            assert _tool(name), f"{name} is dispatched but not advertised"


class TestTheAgentCanCheckReachabilityBeforeProposing:
    async def test_a_connected_host_is_reachable(self, ctx):
        registry = MagicMock()
        registry.get_by_hostname.return_value = MagicMock(system_info={"agent_version": "v2.8.0"})
        ctx["agent_registry"] = registry

        result = await handle_check_host_reachable({"host": "web1"}, ctx)

        assert result["reachable"] is True
        assert result["agent_version"] == "v2.8.0"

    async def test_a_host_with_no_agent_is_not(self, ctx):
        """An artifact targeting it would fail at apply - which is what the AI
        could not find out before proposing."""
        registry = MagicMock()
        registry.get_by_hostname.return_value = None
        ctx["agent_registry"] = registry

        result = await handle_check_host_reachable({"host": "web1"}, ctx)

        assert result["reachable"] is False
        assert "no agent" in result["reason"]

    async def test_no_hub_says_unknown_rather_than_reachable(self, ctx):
        ctx["agent_registry"] = None

        result = await handle_check_host_reachable({"host": "web1"}, ctx)

        assert result["reachable"] is False


class TestTheEnvironmentDocDeliversWhatItAdvertises:
    async def test_the_description_and_the_render_agree_about_the_kb(self):
        from homepilot.mcp.tools.inventory_tools import handle_get_environment_doc

        description = _tool("get_environment_doc")["description"].lower()
        assert "kb" in description or "knowledge" in description

        service = AsyncMock()
        service.get_environment_doc = AsyncMock(
            return_value={
                "target": "web1",
                "hosts": [],
                "services": [],
                "kb_entries": [{"title": "the policy", "content": "reload, never restart"}],
                "artifact_history": [],
            }
        )

        result = await handle_get_environment_doc(
            {"target": "web1"}, {"inventory_service": service}
        )

        assert "the policy" in result[0].text, (
            "the tool advertises KB intent and renders none - the AI is the caller "
            "this tool exists for"
        )
