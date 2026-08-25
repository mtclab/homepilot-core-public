"""Guest management over MCP (#442, AI-first).

Two properties matter beyond "the tools work":

* NO INVITE TOKEN EVER CROSSES MCP. There is deliberately no mint tool, and
  query_guests must never carry a full token - an MCP transcript is not a
  safe place for a secret that provisions machines.
* The mutating tools are write-scoped: a read-only MCP token can look, not
  touch (same rule as propose_artifact).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.tools.guest_tools import (
    handle_query_guests,
    handle_revoke_guest_invite,
    handle_set_guest_quota,
)
from homepilot.portal.models import InviteCaps
from homepilot.portal.repository import InviteRepository

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def ctx(tmp_path: Path):
    db = Database(str(tmp_path / "guest-mcp.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    invites = InviteRepository(db)
    try:
        yield {"repo": repo, "invite_repo": invites, "caller_id": "test-mcp"}, db
    finally:
        await db.close()


async def _mint(invites: InviteRepository) -> tuple[str, str]:
    return await invites.create_invite(
        bound_cn="alice",
        caps=InviteCaps(template_vmid=9000, node="pve1", cores=2, memory_mb=2048, disk_gb=20),
        created_by="test",
        ttl=timedelta(days=7),
    )


class TestNoSecretCrossesMcp:
    async def test_there_is_no_mint_tool(self):
        from homepilot.mcp.server import _TOOL_HANDLERS

        assert "mint_guest_invite" not in _TOOL_HANDLERS, (
            "an invite token is a machine-provisioning secret; MCP must not be "
            "the channel it travels through"
        )

    async def test_query_guests_never_carries_a_full_token(self, ctx):
        context, _db = ctx
        _invite_id, full_token = await _mint(context["invite_repo"])

        out = await handle_query_guests({}, context)

        assert full_token not in str(out), "a full invite token leaked over MCP"
        assert out["invites"] and out["invites"][0]["prefix"] == full_token[:16]


class TestTheToolsWork:
    async def test_set_quota_then_query_pairs_usage_with_limits(self, ctx):
        context, _db = ctx
        repo = context["repo"]
        await repo.create_host(hostname="a0", host_type="vm", owner="alice", cpu_cores=4)

        res = await handle_set_guest_quota({"cn": "alice", "max_vms": 2, "max_cores": 8}, context)
        assert res["limits"]["vms"] == 2 and res["usage"]["cores"] == 4

        overview = await handle_query_guests({}, context)
        alice = next(g for g in overview["guests"] if g["cn"] == "alice")
        assert alice["limits"]["cores"] == 8 and alice["usage"]["vms"] == 1

    async def test_revoke_by_prefix_lands(self, ctx):
        context, _db = ctx
        _invite_id, full_token = await _mint(context["invite_repo"])

        res = await handle_revoke_guest_invite({"prefix": full_token[:16]}, context)
        assert res["revoked"] is True

        from homepilot.portal.repository import invite_state

        row = await context["invite_repo"].get_by_token(full_token)
        assert row is not None and invite_state(row) == "revoked"


class TestScope:
    async def test_guest_tools_are_admin_scoped(self):
        """Wave 3: the guest routes are all API `admin` (GET /admin/guests and the
        quota/invite writes), so the whole set moved to the admin MCP tier. A
        `full` MCP token can no longer see or change guests - only an `admin` one."""
        from homepilot.mcp.server import _ADMIN_TOOLS, _MUTATING_TOOLS, _READ_ONLY_TOOLS

        guest_tools = {"query_guests", "set_guest_quota", "revoke_guest_invite"}
        assert guest_tools <= _ADMIN_TOOLS, (
            "the guest tools mirror API-admin routes and must sit at the admin tier"
        )
        assert not (guest_tools & _MUTATING_TOOLS)
        assert not (guest_tools & _READ_ONLY_TOOLS)

    async def test_a_full_token_is_denied_and_an_admin_token_passes(self, ctx):
        """The real scope check in _handle_tool: a `full` MCP token is refused a
        guest tool (admin required), an `admin` token is not."""
        from homepilot.mcp.server import _handle_tool, _mcp_token_scope_var

        context, _db = ctx
        token = _mcp_token_scope_var.set("full")
        try:
            with pytest.raises(ValueError, match="requires admin scope"):
                await _handle_tool("query_guests", {}, {**context, "_mcp_token_scope": "full"})
            # Same harness, admin token: reaches the handler and returns a dict.
            out = await _handle_tool("query_guests", {}, {**context, "_mcp_token_scope": "admin"})
            assert isinstance(out, dict) and "guests" in out
        finally:
            _mcp_token_scope_var.reset(token)
