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
from homepilot.guest.quota import check_provision, get_quota
from homepilot.guest.router import router as guest_router
from homepilot.portal.repository import InviteRepository

from .portal_support import cert_headers, client_for, portal_settings

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
                # #607: the removal is the same authority as the setting.
                assert client.delete("/admin/guests/quota/x").status_code in (401, 403)
        finally:
            await db.close()


class TestRemovingABudget:
    """#607: the console could set a budget and never take one back.

    The gate is the GUEST's view and the ENFORCEMENT path, not the delete call's
    status code: a removal that returns 200 while the friend's portal still
    shows a budget - or while provisioning still stops at the old line - has
    removed nothing that matters.
    """

    @pytest.fixture
    async def both_ends(self, tmp_path: Path):
        """One database behind BOTH surfaces: the operator's admin API and the
        friend's own portal API, exactly as they sit in the running app."""
        db = Database(str(tmp_path / "guest-both-ends.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        app = FastAPI()
        app.include_router(guest_admin_router)
        app.include_router(guest_router, prefix="/guest")
        app.state.repo = repo
        app.state.invite_repo = InviteRepository(db)
        app.state.settings = portal_settings()

        from homepilot.auth.deps import require_token

        app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
        try:
            yield app, repo
        finally:
            await db.close()

    async def test_a_removed_budget_is_gone_from_the_guests_own_view(self, both_ends):
        app, repo = both_ends
        async with client_for(app) as client:
            set_res = await client.post(
                "/admin/guests/quota",
                json={
                    "cn": CN,
                    "max_vms": 1,
                    "max_cores": 4,
                    "max_memory_mb": 4096,
                    "max_disk_gb": 50,
                },
            )
            assert set_res.status_code == 200

            # The friend sees the budget - otherwise the removal below proves nothing.
            before = await client.get("/guest/quota", headers=cert_headers(cn=CN))
            assert before.status_code == 200
            assert before.json()["limits"] == {
                "vms": 1,
                "cores": 4,
                "memory_mb": 4096,
                "disk_gb": 50,
            }

            res = await client.delete(f"/admin/guests/quota/{CN}")
            assert res.status_code == 200

            after = await client.get("/guest/quota", headers=cert_headers(cn=CN))

        assert after.status_code == 200
        # THE GOAL: the friend's portal shows no budget at all, not a budget of
        # nulls. `limits: null` is what a guest who never had one is told.
        assert after.json()["limits"] is None, "the guest is still shown a budget"
        assert await get_quota(repo, CN) is None, "a quota row survived the removal"

    async def test_removal_actually_unblocks_provisioning(self, both_ends):
        """The other half of "gone": the enforcement path stops refusing."""
        app, repo = both_ends
        await repo.create_host(hostname="a0", host_type="vm", owner=CN, cpu_cores=2)
        async with client_for(app) as client:
            await client.post(
                "/admin/guests/quota",
                json={
                    "cn": CN,
                    "max_vms": 1,
                    "max_cores": None,
                    "max_memory_mb": None,
                    "max_disk_gb": None,
                },
            )
            blocked = await check_provision(repo, CN, cores=1, memory_mb=512, disk_gb=1)
            assert not blocked.allowed, (
                "the budget was never binding, so removing it proves nothing"
            )

            res = await client.delete(f"/admin/guests/quota/{CN}")
            assert res.status_code == 200

        allowed = await check_provision(repo, CN, cores=1, memory_mb=512, disk_gb=1)
        assert allowed.allowed, "provisioning is still gated by a budget that was removed"

    async def test_the_overview_stops_listing_limits_for_that_guest(self, both_ends):
        app, _repo = both_ends
        async with client_for(app) as client:
            await client.post(
                "/admin/guests/quota",
                json={
                    "cn": CN,
                    "max_vms": 3,
                    "max_cores": None,
                    "max_memory_mb": None,
                    "max_disk_gb": None,
                },
            )
            await client.delete(f"/admin/guests/quota/{CN}")
            data = (await client.get("/admin/guests")).json()

        row = next((g for g in data["guests"] if g["cn"] == CN), None)
        # Either the guest is gone from the overview entirely (nothing else knows
        # them) or they are listed with no limits - never with the old numbers.
        assert row is None or row["limits"] is None

    async def test_removing_a_budget_nobody_has_is_a_404(self, both_ends):
        app, _repo = both_ends
        async with client_for(app) as client:
            res = await client.delete("/admin/guests/quota/nobody")
        assert res.status_code == 404

    async def test_removal_is_audited(self, both_ends):
        app, repo = both_ends
        async with client_for(app) as client:
            await client.post(
                "/admin/guests/quota",
                json={
                    "cn": CN,
                    "max_vms": 1,
                    "max_cores": None,
                    "max_memory_mb": None,
                    "max_disk_gb": None,
                },
            )
            await client.delete(f"/admin/guests/quota/{CN}")

        rows = await repo.db.fetchall("SELECT action, target_host FROM audit_log")
        assert ("guest_quota_removed", CN) in [(r["action"], r["target_host"]) for r in rows]
