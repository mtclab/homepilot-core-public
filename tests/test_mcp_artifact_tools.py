"""Tests for new MCP artifact tools: approve_artifact, get_artifact_status, query_artifacts target filter, rate limiting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture()
def ctx():
    repo = AsyncMock()
    repo.list_hosts = AsyncMock(return_value=[{"id": 1, "hostname": "pve1"}])
    repo.list_services = AsyncMock(return_value=[{"id": 1, "name": "nginx"}])

    store = MagicMock()
    store.list = MagicMock(
        return_value=[
            {
                "id": "a1",
                "status": "proposed",
                "kind": "kb-note",
                "intent": "test note",
                "target": {"host": "pve1"},
                "created_at": "2026-05-14",
            },
            {
                "id": "a2",
                "status": "applied",
                "kind": "shell-script",
                "intent": "deploy thing",
                "target": {"node": "pve1"},
                "created_at": "2026-05-13",
            },
        ]
    )
    store.read = MagicMock(
        return_value=(
            {
                "id": "2026-05-14-approve-test",
                "status": "approved",
                "kind": "shell-script",
                "intent": "test approve",
                "approved_by": {
                    "user": "test-caller",
                    "at": "2026-05-14T12:00:00Z",
                    "reason": "Approved via MCP",
                },
                "target": {"host": "pve1", "kind": "vm", "vmid": 100, "node": "pve1"},
                "applied_at": None,
            },
            "body text",
        ),
    )

    lifecycle = MagicMock()
    lifecycle.propose = AsyncMock(return_value="2026-05-09-kb-host-abc123")
    lifecycle.approve = AsyncMock()

    kb_service = AsyncMock()
    kb_service.search = AsyncMock(return_value=[{"id": "k1", "content": "note"}])

    ssh = AsyncMock()
    ssh.exec_readonly = AsyncMock(return_value=(0, "stdout text", ""))
    ssh.read_file = AsyncMock(return_value="file content")

    proxmox = AsyncMock()
    proxmox.read = AsyncMock(return_value={"data": []})

    vault = AsyncMock()
    vault.get_secret = AsyncMock(return_value={"url": "https://svc.local", "token": "tok"})

    drift_reconciler = AsyncMock()

    return {
        "repo": repo,
        "lifecycle": lifecycle,
        "store": store,
        "kb_service": kb_service,
        "ssh_adapter": ssh,
        "proxmox": proxmox,
        "vault": vault,
        "drift_reconciler": drift_reconciler,
        "_mcp_caller_id": "test-caller",
    }


async def call(name, arguments, ctx):
    from homepilot.mcp.server import _handle_tool

    return await _handle_tool(name, arguments, ctx)


class TestApproveArtifact:
    @pytest.mark.asyncio
    async def test_approve_calls_lifecycle_approve(self, ctx):
        await call("approve_artifact", {"artifact_id": "2026-05-14-approve-test"}, ctx)
        ctx["lifecycle"].approve.assert_called_once_with(
            "2026-05-14-approve-test", user="test-caller", reason="Approved via MCP"
        )

    @pytest.mark.asyncio
    async def test_approve_returns_metadata(self, ctx):
        result = await call("approve_artifact", {"artifact_id": "2026-05-14-approve-test"}, ctx)
        assert isinstance(result, dict)
        assert result["id"] == "2026-05-14-approve-test"
        assert result["status"] == "approved"
        assert result["kind"] == "shell-script"
        assert result["intent"] == "test approve"
        assert "approved_by" in result

    @pytest.mark.asyncio
    async def test_approve_read_only_denied(self, ctx):
        ctx["_mcp_token_scope"] = "read_only"
        with pytest.raises(ValueError, match="write scope"):
            await call("approve_artifact", {"artifact_id": "2026-05-14-approve-test"}, ctx)

    @pytest.mark.asyncio
    async def test_approve_rate_limit(self, ctx):
        from homepilot.mcp.tools.artifact_tools import _APPROVE_RATELIMIT_MAX, _approve_ratelimit

        _approve_ratelimit.clear()
        for i in range(_APPROVE_RATELIMIT_MAX):
            await call("approve_artifact", {"artifact_id": f"art-{i}"}, ctx)
        with pytest.raises(ValueError, match="Rate limit exceeded"):
            await call("approve_artifact", {"artifact_id": "art-extra"}, ctx)

    @pytest.mark.asyncio
    async def test_approve_rate_limit_per_caller(self, ctx):
        from homepilot.mcp.tools.artifact_tools import _APPROVE_RATELIMIT_MAX, _approve_ratelimit

        _approve_ratelimit.clear()
        ctx_a = {**ctx, "_mcp_caller_id": "caller-a"}
        ctx_b = {**ctx, "_mcp_caller_id": "caller-b"}
        for i in range(_APPROVE_RATELIMIT_MAX):
            await call("approve_artifact", {"artifact_id": f"art-a-{i}"}, ctx_a)
        await call("approve_artifact", {"artifact_id": "art-b-0"}, ctx_b)


class TestGetArtifactStatus:
    @pytest.mark.asyncio
    async def test_returns_artifact_metadata(self, ctx):
        result = await call("get_artifact_status", {"artifact_id": "2026-05-14-approve-test"}, ctx)
        assert isinstance(result, dict)
        assert result["id"] == "2026-05-14-approve-test"
        assert result["kind"] == "shell-script"
        assert result["status"] == "approved"
        assert result["intent"] == "test approve"
        assert "last_updated" in result
        assert "target" in result

    @pytest.mark.asyncio
    async def test_not_found_raises(self, ctx):
        ctx["store"].read = MagicMock(side_effect=FileNotFoundError("not found"))
        with pytest.raises(ValueError, match="Artifact not found"):
            await call("get_artifact_status", {"artifact_id": "nonexistent"}, ctx)

    @pytest.mark.asyncio
    async def test_read_only_allowed(self, ctx):
        ctx["_mcp_token_scope"] = "read_only"
        result = await call("get_artifact_status", {"artifact_id": "2026-05-14-approve-test"}, ctx)
        assert result["id"] == "2026-05-14-approve-test"


class TestQueryArtifactsTargetFilter:
    @pytest.mark.asyncio
    async def test_filter_by_target_host(self, ctx):
        result = await call("query_artifacts", {"filter": '{"target": "pve1"}'}, ctx)
        assert result["total"] > 0
        for item in result["items"]:
            assert "id" in item
            assert "kind" in item

    @pytest.mark.asyncio
    async def test_returns_full_items_and_summary(self, ctx):
        result = await call("query_artifacts", {}, ctx)
        assert "items" in result
        assert "summary" in result
        assert result["total"] == len(result["items"])
        assert len(result["summary"]) == len(result["items"])
        full_item = result["items"][0]
        summary_item = result["summary"][0]
        assert "target" in full_item
        assert "created_at" in full_item
        assert "id" in summary_item
        assert "kind" in summary_item
        assert "status" in summary_item
        assert "intent" in summary_item
