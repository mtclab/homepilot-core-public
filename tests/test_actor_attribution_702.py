"""The audit trail names who acted, from the authenticated principal (#702).

WHAT THIS FORBIDS:
* REST inventory and guest-admin routes hard-coded ``user_id="ui"``, whoever's
  token it was - a CLI script, an automation token and the operator in the
  browser all read "ui" in the journal;
* the MCP guest quota tools read ``ctx.get("caller_id", "mcp")`` while the key
  the dispatcher sets is ``_mcp_caller_id`` - so every guest quota action over
  MCP was logged as "mcp", and the shared inventory helpers MCP calls logged
  its add/delete as "ui".

The journeys drive the real routes with a real token and read the audit rows;
the source gate forbids the literal-actor pattern coming back anywhere.

Teeth: put ``user_id="ui"`` back in a route or ``ctx.get("caller_id"`` in a
tool and the matching test fails.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"


@pytest.fixture
async def estate(tmp_path: Path):
    db = Database(str(tmp_path / "a.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    uid = await repo.create_user("operator")
    token, prefix, digest = generate_api_token()
    await repo.create_api_token(uid, "personal", prefix, digest, scope="*", label="ops-script")
    await db.conn.commit()
    yield repo, token, prefix
    await db.close()


async def _audit_users(repo: Repository, action: str) -> list[str]:
    rows = await repo.db.fetchall("SELECT user_id FROM audit_log WHERE action = ?", (action,))
    return [dict(r)["user_id"] for r in rows]


def _app(repo: Repository) -> FastAPI:
    from homepilot.guest.admin_router import router as guest_admin_router
    from homepilot.inventory.router import router as inventory_router
    from homepilot.inventory.service import InventoryService
    from homepilot.portal.repository import InviteRepository

    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.include_router(guest_admin_router)
    app.state.repo = repo
    app.state.inventory_service = InventoryService(repo)
    app.state.invite_repo = InviteRepository(repo.db)
    return app


async def test_rest_routes_log_the_token_that_acted(estate):
    repo, token, prefix = estate
    from homepilot.portal.models import InviteCaps
    from homepilot.portal.repository import InviteRepository

    app = _app(repo)
    auth = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/inventory", json={"hostname": "nas"}, headers=auth)
        assert r.status_code == 201, r.text
        r = await c.delete(f"/inventory/{r.json()['id']}", headers=auth)
        assert r.status_code == 200, r.text
        r = await c.post("/admin/guests/quota", json={"cn": "friend", "max_vms": 2}, headers=auth)
        assert r.status_code == 200, r.text
        r = await c.delete("/admin/guests/quota/friend", headers=auth)
        assert r.status_code == 200, r.text
        _iid, invite = await InviteRepository(repo.db).create_invite(
            bound_cn="friend",
            caps=InviteCaps(template_vmid=9000, node="pve1", cores=1, memory_mb=512, disk_gb=8),
            created_by="setup",
            ttl=__import__("datetime").timedelta(days=1),
        )
        r = await c.post(f"/admin/guests/invites/{invite[:16]}/revoke", headers=auth)
        assert r.status_code in (200, 404), r.text

    for action in ("host_added", "host_forgotten", "guest_quota_set", "guest_quota_removed"):
        users = await _audit_users(repo, action)
        assert users, f"no audit row for {action}"
        # `label (prefix)`: the ONE spelling every surface uses (auth.principal.
        # audit_actor) - it was `api:<prefix>` here and `label (prefix)` in the
        # lifecycle and token audit, so a filter on one missed the other.
        assert users == [f"ops-script ({prefix})"], f"{action} logged {users}"


async def test_mcp_tools_log_the_mcp_caller(estate):
    repo, _token, _prefix = estate
    from homepilot.inventory.service import InventoryService
    from homepilot.mcp.server import _handle_tool

    ctx: dict[str, Any] = {
        "repo": repo,
        "inventory_service": InventoryService(repo),
        "_mcp_token_scope": "admin",
        "_mcp_caller_id": "ops-script (hp_caller000001)",
    }
    await _handle_tool("set_guest_quota", {"cn": "friend", "max_vms": 1}, ctx)
    await _handle_tool("delete_guest_quota", {"cn": "friend"}, ctx)
    added = await _handle_tool("add_host", {"host": "pi"}, ctx)
    await _handle_tool("delete_host", {"host_id": str(added["id"])}, ctx)

    for action in ("guest_quota_set", "guest_quota_removed", "host_added", "host_forgotten"):
        assert await _audit_users(repo, action) == ["ops-script (hp_caller000001)"], action


def test_no_route_or_tool_names_a_literal_actor():
    offenders: list[str] = []
    pattern = re.compile(
        r'user_id\s*=\s*"(ui|web|mcp)"|created_by\s*=\s*"(ui|web)"|ctx\.get\("caller_id"'
    )
    for path in SRC.rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{no}: {line.strip()}")
    assert not offenders, "actor hard-coded instead of taken from the principal:\n" + "\n".join(
        offenders
    )
