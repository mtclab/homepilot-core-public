"""One credential, one name, on every surface of the audit trail.

WHAT THIS FORBIDS: the inventory and guest-admin routes recorded the actor as
`api:<prefix>` (#702), the artifact lifecycle and the token audit as
`label (prefix)` (#681, #686), and MCP as `mcp-api:<prefix>`. One token was
three different strings in one journal, so filtering the trail by the name it
showed for one action missed the others.

The contract: `auth.principal.audit_actor` is the only place a credential's
audit name is made, it is the Principal's name (`label (prefix)`), and the
REST routes, the MCP verifier and the token audit all record it.

Teeth: reintroduce an `f"api:{...}"` / `mcp-api:` spelling anywhere and the
source gate names the line; make the MCP verifier build its own name and the
journey sees two spellings.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI

from homepilot.auth.principal import audit_actor, credential_name
from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"


def test_no_second_spelling_of_a_credential_survives_in_the_source():
    pattern = re.compile(r"""f["'](mcp-)?api:\{|["']mcp-api:|principal_label\(|actor_of\(""")
    offenders = [
        f"{path.relative_to(SRC)}:{no}: {line.strip()}"
        for path in SRC.rglob("*.py")
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, "a credential named outside audit_actor:\n" + "\n".join(offenders)


def test_the_helper_is_the_principal_name():
    token = {"label": "ops", "prefix": "hp_0123456789ab", "scope": "admin"}
    assert (
        audit_actor(token) == credential_name("ops", "hp_0123456789ab") == "ops (hp_0123456789ab)"
    )
    assert audit_actor({"auth": "admin-secret"}) == "admin-secret"


async def test_rest_and_mcp_record_the_same_name_for_the_same_token(tmp_path, monkeypatch):
    from homepilot.auth.deps import require_token
    from homepilot.inventory.router import router as inventory_router
    from homepilot.inventory.service import InventoryService
    from homepilot.mcp import server as mcp_server

    db = Database(str(tmp_path / "n.db"))
    await db.connect()
    await run_migrations(db)
    try:
        repo = Repository(db)
        uid = await repo.create_user("operator")
        token, prefix, digest = generate_api_token()
        await repo.create_api_token(uid, "personal", prefix, digest, scope="admin", label="ops")
        await db.conn.commit()

        app = FastAPI()
        app.include_router(
            inventory_router, prefix="/inventory", dependencies=[Depends(require_token)]
        )
        app.state.repo = repo
        app.state.inventory_service = InventoryService(repo)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.post(
                "/inventory", json={"hostname": "nas"}, headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 201, r.text
        rest = await db.fetchone("SELECT user_id FROM audit_log WHERE action = 'host_added'")

        monkeypatch.setitem(mcp_server._server_context._data, "repo", repo)
        resolved = await mcp_server.resolve_api_token_tier(token)
        assert resolved is not None
        _tier, mcp_caller = resolved

        assert rest is not None
        assert rest["user_id"] == mcp_caller == f"ops ({prefix})"
    finally:
        await db.close()
