"""Per-guest resource quotas (#442 G1.5).

THE OWNER'S RULE: a friend gets a budget across ALL their machines - cores,
memory, disk, count - and provisioning stops at the line. The invite already
caps one machine; the quota caps the guest.

The enforcement gate drives the REAL redemption route (real migrated DB, real
portal trust, FakePVE) and asserts the OUTCOME: over budget means no clone
call reaches the hypervisor, the invite stays open for a later try, and the
page tells the guest which axis overflowed. No quota row means no quota.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.guest.quota import check_provision, set_quota, usage_for

from .portal_support import PUBKEY, FakePVE, cert_headers, client_for, portal_settings

pytestmark = pytest.mark.asyncio

CN = "alice"


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "quota.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
async def repo(db: Database):
    return Repository(db)


class TestTheBudgetMath:
    async def test_usage_sums_every_machine_the_guest_owns(self, repo):
        for i, (cores, mem, disk) in enumerate([(2, 2048, 20), (4, 4096, 40)]):
            await repo.create_host(
                hostname=f"a{i}",
                host_type="vm",
                owner=CN,
                cpu_cores=cores,
                memory_mb=mem,
                disk_gb=disk,
            )
        await repo.create_host(hostname="not-hers", host_type="vm", owner="bob", cpu_cores=64)

        used = await usage_for(repo, CN)
        assert (used.vms, used.cores, used.memory_mb, used.disk_gb) == (2, 6, 6144, 60)

    async def test_no_quota_row_means_no_quota(self, repo):
        decision = await check_provision(repo, CN, cores=128, memory_mb=1, disk_gb=1)
        assert decision.allowed

    async def test_the_decision_names_every_overflowing_axis(self, repo):
        await set_quota(repo, CN, max_vms=1, max_cores=4, max_memory_mb=None, max_disk_gb=100)
        await repo.create_host(
            hostname="a0", host_type="vm", owner=CN, cpu_cores=4, memory_mb=1024, disk_gb=10
        )

        decision = await check_provision(repo, CN, cores=1, memory_mb=1024, disk_gb=200)

        assert not decision.allowed
        assert set(decision.exceeded) == {"machines", "CPU cores", "disk"}
        # memory is unlimited (NULL) and must not appear.
        assert "memory" not in decision.exceeded


class TestRedemptionStopsAtTheLine:
    """Through the real portal route: the journey gate."""

    @pytest.fixture
    async def portal_app(self, db, repo):
        from homepilot.portal.repository import InviteRepository
        from homepilot.portal.router import _redeem_attempts
        from homepilot.portal.router import router as portal_router
        from homepilot.provision.service import ProvisionService
        from homepilot.tasks.repository import TaskRepository

        _redeem_attempts.clear()
        import httpx

        from homepilot.adapters.proxmox import ProxmoxClient

        pve = FakePVE()
        client = ProxmoxClient(base_url="https://pve.example:8006", token="root@pam!t=uuid")
        transport = httpx.MockTransport(pve.handle)
        fake = httpx.AsyncClient(base_url="https://pve.example:8006/api2/json", transport=transport)
        client._client = fake
        client._write_client = fake

        app = FastAPI()
        app.include_router(portal_router, prefix="/invite")
        task_repo = TaskRepository(db)
        app.state.repo = repo
        app.state.task_repo = task_repo
        app.state.invite_repo = InviteRepository(db)
        app.state.settings = portal_settings()
        app.state.provision_service = ProvisionService(
            proxmox=client,
            task_repo=task_repo,
            repo=repo,
            poll_interval=0.01,
            task_timeout_s=5.0,
            ip_wait_s=0.5,
            ip_interval=0.05,
        )
        return app, pve

    async def _mint(self, db, cn: str) -> str:
        from .portal_support import mint

        _invite_id, full_token = await mint(db, cn=cn)
        return full_token

    async def test_over_budget_blocks_before_the_hypervisor_and_keeps_the_invite(
        self, db, repo, portal_app
    ):
        app, pve = portal_app
        await set_quota(repo, CN, max_vms=1, max_cores=None, max_memory_mb=None, max_disk_gb=None)
        await repo.create_host(hostname="existing", host_type="vm", owner=CN)
        token = await self._mint(db, CN)

        async with client_for(app) as client:
            res = await client.post(
                f"/invite/{token}",
                data={"ciuser": "alice", "ssh_authorized_key": PUBKEY},
                headers=cert_headers(cn=CN),
            )

        assert res.status_code == 409
        assert "machines" in res.text, "the page does not name the overflowing axis"
        assert pve.seen == [], "an over-budget redemption reached the hypervisor"
        # The invite survives: freeing resources and retrying must work.
        from homepilot.portal.repository import InviteRepository
        from homepilot.portal.router import invite_state

        row = await InviteRepository(db).get_by_token(token)
        assert row is not None and invite_state(row) == "open", (
            "the blocked redemption consumed the invite"
        )

    async def test_within_budget_redeems_normally(self, db, repo, portal_app):
        app, pve = portal_app
        await set_quota(repo, CN, max_vms=2, max_cores=8, max_memory_mb=8192, max_disk_gb=100)
        token = await self._mint(db, CN)

        async with client_for(app) as client:
            res = await client.post(
                f"/invite/{token}",
                data={"ciuser": "alice", "ssh_authorized_key": PUBKEY},
                headers=cert_headers(cn=CN),
            )

        # 303 = redeemed, redirected to the status page.
        assert res.status_code == 303
        # The provision runs as a background task; give it a beat to reach PVE.
        import asyncio

        for _ in range(40):
            if any("clone" in path for _m, path in pve.seen):
                break
            await asyncio.sleep(0.05)
        assert any("clone" in path for _m, path in pve.seen), (
            "a within-budget redemption never reached the hypervisor"
        )
