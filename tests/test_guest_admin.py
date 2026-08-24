"""Operator-side guest management (#442 G3).

What matters: minting from the console produces an invite the REAL redemption
flow accepts (journey, not call-assertion); the token appears once and is
never stored or listed; budgets set here bind immediately; and none of it is
reachable without admin scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.guest.admin_router import router as guest_admin_router
from homepilot.guest.quota import get_quota
from homepilot.portal.repository import InviteRepository

pytestmark = pytest.mark.asyncio

CN = "alice"


@pytest.fixture
async def stack(tmp_path: Path):
    db = Database(str(tmp_path / "guest-admin.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    app = FastAPI()
    app.include_router(guest_admin_router)
    app.state.repo = repo
    app.state.invite_repo = InviteRepository(db)

    from homepilot.auth.deps import require_token

    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    try:
        yield app, db, repo
    finally:
        await db.close()


class TestMintingFromTheConsole:
    async def test_a_minted_invite_is_redeemable_and_its_token_stored_nowhere(self, stack):
        app, db, _repo = stack
        with TestClient(app) as client:
            res = client.post(
                "/admin/guests/invites",
                json={
                    "cn": CN,
                    "template_vmid": 9000,
                    "node": "pve1",
                    "cores": 2,
                    "memory_mb": 2048,
                    "disk_gb": 20,
                    "ttl_days": 7,
                },
            )
        assert res.status_code == 201
        token = res.json()["token"]
        assert token

        # The REAL redemption lookup accepts it — the whole point of minting.
        row = await InviteRepository(db).get_by_token(token)
        assert row is not None and row["bound_cn"] == CN

        # And the token exists nowhere but that one response.
        for table in ("invites", "audit_log"):
            rows = await db.fetchall(f"SELECT * FROM {table}")  # nosec B608 - test, fixed names
            for r in rows:
                assert token not in str(dict(r)), f"the full token leaked into {table}"

        with TestClient(app) as client:
            listed = client.get("/admin/guests").json()
        assert token not in str(listed), "the overview leaks full tokens"

    async def test_revoking_from_the_console_kills_the_invite(self, stack):
        app, db, _repo = stack
        with TestClient(app) as client:
            minted = client.post(
                "/admin/guests/invites",
                json={
                    "cn": CN,
                    "template_vmid": 9000,
                    "node": "pve1",
                    "cores": 2,
                    "memory_mb": 2048,
                    "disk_gb": 20,
                    "ttl_days": 7,
                },
            ).json()
            prefix = minted["token"][:16]
            res = client.post(f"/admin/guests/invites/{prefix}/revoke")
        assert res.status_code == 200

        from homepilot.portal.repository import invite_state

        row = await InviteRepository(db).get_by_token(minted["token"])
        assert row is not None and invite_state(row) == "revoked"

    async def test_a_budget_set_here_lands_in_the_enforced_table(self, stack):
        app, _db, repo = stack
        with TestClient(app) as client:
            res = client.post(
                "/admin/guests/quota",
                json={
                    "cn": CN,
                    "max_vms": 2,
                    "max_cores": 8,
                    "max_memory_mb": None,
                    "max_disk_gb": 100,
                },
            )
        assert res.status_code == 200
        quota = await get_quota(repo, CN)
        assert quota is not None
        assert (quota["max_vms"], quota["max_cores"], quota["max_memory_mb"]) == (2, 8, None)

    async def test_the_overview_shows_usage_next_to_limits(self, stack):
        app, _db, repo = stack
        await repo.create_host(hostname="a0", host_type="vm", owner=CN, cpu_cores=4)
        with TestClient(app) as client:
            client.post(
                "/admin/guests/quota",
                json={
                    "cn": CN,
                    "max_vms": 3,
                    "max_cores": None,
                    "max_memory_mb": None,
                    "max_disk_gb": None,
                },
            )
            data = client.get("/admin/guests").json()
        g = next(x for x in data["guests"] if x["cn"] == CN)
        assert g["usage"]["vms"] == 1 and g["usage"]["cores"] == 4
        assert g["limits"]["vms"] == 3


class TestAdminScopeIsRequired:
    async def test_nothing_answers_without_a_token(self, tmp_path: Path):
        db = Database(str(tmp_path / "noauth.db"))
        await db.connect()
        await run_migrations(db)
        try:
            app = FastAPI()
            app.include_router(guest_admin_router)
            app.state.repo = Repository(db)
            app.state.invite_repo = InviteRepository(db)
            # NO dependency override: the real require_scope chain runs.
            with TestClient(app) as client:
                assert client.get("/admin/guests").status_code in (401, 403)
                assert client.post("/admin/guests/quota", json={"cn": "x"}).status_code in (
                    401,
                    403,
                )
        finally:
            await db.close()
