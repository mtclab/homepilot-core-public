"""Tests for _handle_tool structured returns and error propagation."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture()
def ctx():
    repo = AsyncMock()
    repo.list_hosts = AsyncMock(return_value=[{"id": 1, "hostname": "pve1"}])
    repo.list_services = AsyncMock(return_value=[{"id": 1, "name": "nginx"}])

    store = MagicMock()
    store.list = MagicMock(return_value=[{"id": "a1", "status": "proposed", "kind": "kb-note"}])
    store.read = MagicMock(
        return_value=({"status": "proposed", "kind": "kb-note", "intent": "test"}, "body")
    )

    lifecycle = MagicMock()
    lifecycle.propose = AsyncMock(return_value="2026-05-09-kb-host-abc123")

    kb_service = AsyncMock()
    kb_service.search = AsyncMock(return_value=[{"id": "k1", "content": "note"}])

    ssh = AsyncMock()
    ssh.exec_readonly = AsyncMock(return_value=(0, "stdout text", ""))
    ssh.read_file = AsyncMock(return_value="file content")

    proxmox = AsyncMock()
    proxmox.read = AsyncMock(return_value={"data": []})

    vault = AsyncMock()
    vault.get_secret = AsyncMock(return_value={"url": "https://svc.local", "token": "tok"})

    return {
        "repo": repo,
        "lifecycle": lifecycle,
        "store": store,
        "kb_service": kb_service,
        "agent_adapter": ssh,
        "proxmox": proxmox,
        "vault": vault,
    }


async def call(name, arguments, ctx):
    from homepilot.mcp.server import _handle_tool

    return await _handle_tool(name, arguments, ctx)


class TestQueryInventoryStructured:
    @pytest.mark.asyncio
    async def test_returns_dict(self, ctx):
        result = await call("query_inventory", {}, ctx)
        assert isinstance(result, dict)
        assert "hosts" in result
        assert "services" in result

    @pytest.mark.asyncio
    async def test_hosts_and_services_populated(self, ctx):
        result = await call("query_inventory", {}, ctx)
        assert result["hosts"] == [{"id": 1, "hostname": "pve1"}]
        assert result["services"] == [{"id": 1, "name": "nginx"}]

    @pytest.mark.asyncio
    async def test_filter_json_passed_through(self, ctx):
        await call("query_inventory", {"filter": '{"role": "guest"}'}, ctx)
        ctx["repo"].list_hosts.assert_called_once_with(managed=None, role="guest")


class TestRefreshInventoryStructured:
    @pytest.mark.asyncio
    async def test_returns_dict_with_status(self, ctx):
        result = await call("refresh_inventory", {}, ctx)
        assert result["status"] == "refreshed"
        assert "nodes" in result
        assert "version" in result

    @pytest.mark.asyncio
    async def test_raises_when_no_proxmox(self, ctx):
        ctx["proxmox"] = None
        with pytest.raises(RuntimeError, match="Proxmox not configured"):
            await call("refresh_inventory", {}, ctx)

    @pytest.mark.asyncio
    async def test_raises_on_proxmox_error(self, ctx):
        from homepilot.adapters.proxmox import ProxmoxError

        ctx["proxmox"].read.side_effect = ProxmoxError("GET", "/version", 500, "timeout")
        with pytest.raises(RuntimeError, match="Proxmox refresh failed"):
            await call("refresh_inventory", {}, ctx)


class TestQueryArtifactsStructured:
    @pytest.mark.asyncio
    async def test_returns_dict(self, ctx):
        result = await call("query_artifacts", {}, ctx)
        assert isinstance(result, dict)
        assert "items" in result
        assert "total" in result

    @pytest.mark.asyncio
    async def test_total_matches_items_length(self, ctx):
        result = await call("query_artifacts", {}, ctx)
        assert result["total"] == len(result["items"])


class TestSearchKbStructured:
    @pytest.mark.asyncio
    async def test_returns_dict(self, ctx):
        result = await call("search_kb", {"query": "nginx"}, ctx)
        assert isinstance(result, dict)
        assert "results" in result
        assert "total" in result

    @pytest.mark.asyncio
    async def test_total_matches_results_length(self, ctx):
        result = await call("search_kb", {"query": "nginx"}, ctx)
        assert result["total"] == len(result["results"])


class TestExecOnGuestReadonlyStructured:
    @pytest.mark.asyncio
    async def test_returns_dict(self, ctx):
        result = await call("exec_on_guest_readonly", {"host": "pve1", "command": "uptime"}, ctx)
        assert isinstance(result, dict)
        assert result["exit_code"] == 0
        assert result["stdout"] == "stdout text"
        assert result["stderr"] == ""

    @pytest.mark.asyncio
    async def test_raises_when_no_ssh(self, ctx):
        ctx["agent_adapter"] = None
        with pytest.raises(RuntimeError, match="no agent hub"):
            await call("exec_on_guest_readonly", {"host": "pve1", "command": "uptime"}, ctx)

    @pytest.mark.asyncio
    async def test_readonly_error_propagates(self, ctx):
        from homepilot.adapters.ssh import ReadOnlyCommandError

        ctx["agent_adapter"].exec_readonly.side_effect = ReadOnlyCommandError("rm not allowed")
        with pytest.raises(ReadOnlyCommandError):
            await call("exec_on_guest_readonly", {"host": "pve1", "command": "rm -rf /"}, ctx)


class TestRecordFactStructured:
    @pytest.mark.asyncio
    async def test_returns_dict(self, ctx):
        result = await call(
            "record_fact",
            {"target": "pve1", "kind": "note", "content": "test note"},
            ctx,
        )
        assert isinstance(result, dict)
        assert "id" in result
        assert result["status"] == "applied"
        assert result["kind"] == "note"

    @pytest.mark.asyncio
    async def test_lifecycle_propose_called(self, ctx):
        await call(
            "record_fact",
            {"target": "pve1", "kind": "note", "content": "hello"},
            ctx,
        )
        ctx["lifecycle"].propose.assert_called_once()

    @pytest.mark.asyncio
    async def test_propose_error_propagates(self, ctx):
        ctx["lifecycle"].propose.side_effect = RuntimeError("disk full")
        with pytest.raises(RuntimeError, match="disk full"):
            await call(
                "record_fact",
                {"target": "pve1", "kind": "note", "content": "hello"},
                ctx,
            )


class TestProposeArtifactStructured:
    @pytest.mark.asyncio
    async def test_returns_dict(self, ctx):
        import json

        spec = json.dumps(
            {
                "id": "2026-05-09-test",
                "kind": "kb-note",
                "intent": "test",
                "body": "body",
                "produced_by": {"session": "mcp", "agent": "test", "user": "test"},
            }
        )
        result = await call("propose_artifact", {"spec": spec}, ctx)
        assert isinstance(result, dict)
        assert "id" in result
        assert "status" in result
        assert "message" in result

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self, ctx):
        with pytest.raises(ValueError, match="valid JSON"):
            await call("propose_artifact", {"spec": "not json {"}, ctx)


class TestUnknownToolRaises:
    @pytest.mark.asyncio
    async def test_unknown_tool_raises_value_error(self, ctx):
        with pytest.raises(ValueError, match="Unknown tool"):
            await call("nonexistent_tool", {}, ctx)


class TestGetEnvironmentDocTextOnly:
    """The doc now comes from `InventoryService.get_environment_doc` (#427).

    These tests used to stub `repo.get_host_by_hostname`, which is what the MCP
    handler's OWN lesser re-implementation called - the one that rendered no KB
    at all while the tool description advertised "inventory facts, KB intent, and
    artifact history". They drive the service now, because that is what the
    handler asks.
    """

    @staticmethod
    def _service(doc: dict) -> AsyncMock:
        service = AsyncMock()
        service.get_environment_doc = AsyncMock(return_value=doc)
        return service

    @pytest.mark.asyncio
    async def test_returns_textcontent_with_host(self, ctx):
        from mcp.types import TextContent

        ctx["inventory_service"] = self._service(
            {
                "target": "pve1",
                "hosts": [{"hostname": "pve1", "node": "node1", "proxmox_id": 100}],
                "services": [],
                "kb_entries": [],
                "artifact_history": [],
            }
        )
        result = await call("get_environment_doc", {"target": "pve1"}, ctx)
        assert isinstance(result, list)
        assert all(isinstance(c, TextContent) for c in result)
        assert "pve1" in result[0].text

    @pytest.mark.asyncio
    async def test_the_kb_is_included(self, ctx):
        """The whole point of the fix: the AI is the caller this tool exists for,
        and it was the one getting the answer without the knowledge base."""
        ctx["inventory_service"] = self._service(
            {
                "target": "pve1",
                "hosts": [],
                "services": [],
                "kb_entries": [
                    {"title": "nginx policy", "content": "always reload, never restart"}
                ],
                "artifact_history": [],
            }
        )
        result = await call("get_environment_doc", {"target": "pve1"}, ctx)
        assert "nginx policy" in result[0].text
        assert "always reload" in result[0].text

    @pytest.mark.asyncio
    async def test_no_host_found_still_returns_text(self, ctx):
        from mcp.types import TextContent

        ctx["inventory_service"] = self._service(
            {
                "target": "unknown-host",
                "hosts": [],
                "services": [],
                "kb_entries": [],
                "artifact_history": [],
            }
        )
        result = await call("get_environment_doc", {"target": "unknown-host"}, ctx)
        assert isinstance(result, list)
        assert isinstance(result[0], TextContent)
        assert "unknown-host" in result[0].text

    @pytest.mark.asyncio
    async def test_repo_error_propagates(self, ctx):
        service = AsyncMock()
        service.get_environment_doc = AsyncMock(side_effect=sqlite3.OperationalError("db error"))
        ctx["inventory_service"] = service
        with pytest.raises(sqlite3.OperationalError, match="db error"):
            await call("get_environment_doc", {"target": "pve1"}, ctx)


class TestProxmoxApiReadTextOnly:
    @pytest.mark.asyncio
    async def test_returns_textcontent_on_success(self, ctx):
        from mcp.types import TextContent

        result = await call("proxmox_api_read", {"path": "/nodes/pve1/status"}, ctx)
        assert isinstance(result, list)
        assert isinstance(result[0], TextContent)

    @pytest.mark.asyncio
    async def test_no_proxmox_raises(self, ctx):
        ctx["proxmox"] = None
        with pytest.raises(RuntimeError, match="Proxmox not configured"):
            await call("proxmox_api_read", {"path": "/nodes/pve1"}, ctx)

    @pytest.mark.asyncio
    async def test_allowlist_reject_raises(self, ctx):
        with pytest.raises(ValueError, match="not in read allowlist"):
            await call("proxmox_api_read", {"path": "/qemu/100"}, ctx)

    @pytest.mark.asyncio
    async def test_forbidden_token_raises(self, ctx):
        with pytest.raises(ValueError, match="forbidden token"):
            await call("proxmox_api_read", {"path": "/nodes/pve1/execute"}, ctx)

    @pytest.mark.asyncio
    async def test_proxmox_error_propagates(self, ctx):
        from homepilot.adapters.proxmox import ProxmoxError

        ctx["proxmox"].read.side_effect = ProxmoxError("GET", "/nodes/pve1", 500, "err")
        with pytest.raises(ProxmoxError):
            await call("proxmox_api_read", {"path": "/nodes/pve1/status"}, ctx)


class TestHttpCallReadTextOnly:
    @pytest.mark.asyncio
    async def test_no_vault_raises(self, ctx):
        ctx["vault"] = None
        with pytest.raises(RuntimeError, match="Vault not unlocked"):
            await call("http_call_read", {"name": "svc", "path": "/api"}, ctx)

    @pytest.mark.asyncio
    async def test_no_url_in_vault_raises(self, ctx):
        ctx["vault"].get_secret = AsyncMock(return_value={"token": "tok"})
        with pytest.raises(RuntimeError, match="has no URL"):
            await call("http_call_read", {"name": "svc", "path": "/api"}, ctx)

    @pytest.mark.asyncio
    async def test_invalid_url_scheme_raises(self, ctx):
        ctx["vault"].get_secret = AsyncMock(return_value={"url": "ftp://svc.local"})
        with pytest.raises(ValueError, match="invalid base_url"):
            await call("http_call_read", {"name": "svc", "path": "/api"}, ctx)

    @pytest.mark.asyncio
    async def test_vault_secret_not_found_propagates(self, ctx):
        from homepilot.vault.manager import VaultError

        ctx["vault"].get_secret = AsyncMock(side_effect=VaultError("secret not found"))
        with pytest.raises(VaultError, match="secret not found"):
            await call("http_call_read", {"name": "missing", "path": "/api"}, ctx)

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_blocked(self, ctx, monkeypatch):
        from homepilot.mcp.tools.ssrf_guard import SSRFError

        async def _fail(url, **kw):
            raise SSRFError("resolved IP 10.0.0.1 is private/forbidden")

        monkeypatch.setattr("homepilot.mcp.tools.system_tools.validate_url", _fail)
        with pytest.raises(ValueError, match="SSRF protection"):
            await call("http_call_read", {"name": "svc", "path": "/api"}, ctx)


class TestReadFileOnGuestTextOnly:
    @pytest.mark.asyncio
    async def test_returns_textcontent_on_success(self, ctx):
        from mcp.types import TextContent

        result = await call("read_file_on_guest", {"host": "pve1", "path": "/etc/hosts"}, ctx)
        assert isinstance(result, list)
        assert isinstance(result[0], TextContent)
        assert result[0].text == "file content"

    @pytest.mark.asyncio
    async def test_no_ssh_raises(self, ctx):
        ctx["agent_adapter"] = None
        with pytest.raises(RuntimeError, match="no agent hub"):
            await call("read_file_on_guest", {"host": "pve1", "path": "/etc/hosts"}, ctx)

    @pytest.mark.asyncio
    async def test_read_error_propagates(self, ctx):
        # Use an allowed path so the adapter is reached: this asserts that an error
        # raised by read_file propagates (the read-prefix/secret guard is covered
        # separately in test_mcp_read_guard.py).
        ctx["agent_adapter"].read_file = AsyncMock(side_effect=OSError("permission denied"))
        with pytest.raises(OSError, match="permission denied"):
            await call("read_file_on_guest", {"host": "pve1", "path": "/etc/hosts"}, ctx)
