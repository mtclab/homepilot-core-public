"""The Guests page's one read (3.12): GET /admin/guests/friends.

The owner on prod 3.11.0: "Guests is a bit too simple at the moment, and
clunky" - the page showed per-CN numbers and a flat invite list, and nothing
showed a friend's actual machine, its state, or a way to its host page. The
gates here are the ANSWER the page is built from, read over the real router
and a real migrated database:

* a friend with a machine gets that machine - host id (the host page's
  address), live state word, size - and the machine count is the count the
  budget is enforced on (an absent machine is in neither);
* every place a CN can come from makes it a friend (budget, invite, machine);
* the operator's journeys land in this read: a minted invite appears under the
  friend, a revoke turns it revoked with its time, a budget set here shows up
  as that friend's limits;
* the invite token is in the mint response and in NO later read - this one,
  the old overview, or query_guests;
* the assistant reads the same friends (query_guests `friends`) and only an
  admin may read either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.guest.admin_router import router as guest_admin_router
from homepilot.guest.quota import usage_for
from homepilot.mcp.tools.guest_tools import handle_query_guests
from homepilot.portal.repository import InviteRepository

pytestmark = pytest.mark.asyncio

MINT = {
    "cn": "mika-260115",
    "template_vmid": 9000,
    "node": "pve1",
    "cores": 1,
    "memory_mb": 1024,
    "disk_gb": 20,
    "ttl_days": 7,
}


@pytest.fixture
async def stack(tmp_path: Path):
    db = Database(str(tmp_path / "guest-friends.db"))
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


def _friend(body: dict[str, Any], cn: str) -> dict[str, Any]:
    (one,) = [f for f in body["friends"] if f["cn"] == cn]
    return one


async def _machine(repo: Repository, name: str, owner: str, **kw: Any) -> str:
    return await repo.create_host(
        hostname=name,
        host_type="qemu",
        owner=owner,
        proxmox_id=kw.pop("vmid", 130),
        node="pve1",
        cpu_cores=kw.pop("cores", 1),
        memory_mb=kw.pop("memory_mb", 1024),
        disk_gb=kw.pop("disk_gb", 30),
        **kw,
    )


class TestAFriendAndTheirMachine:
    async def test_the_machine_is_listed_with_its_host_id_state_and_size(self, stack):
        app, _db, repo = stack
        host_id = await _machine(repo, "mika-web", "mika-260115", pve_status="running")
        with TestClient(app) as client:
            res = client.get("/admin/guests/friends")
        assert res.status_code == 200, res.text
        mika = _friend(res.json(), "mika-260115")
        assert mika["machines"] == [
            {
                "host_id": host_id,
                "name": "mika-web",
                "vmid": 130,
                "node": "pve1",
                "state": "running",
                "cores": 1,
                "memory_mb": 1024,
                "disk_gb": 30,
            }
        ]
        # The host page's address is a real host: /inventory/{id} reads it.
        assert (await repo.get_host(host_id))["hostname"] == "mika-web"

    async def test_the_listed_machines_are_the_ones_the_budget_is_enforced_on(self, stack):
        """An absent machine (destroyed out of band) is billed nowhere (#613) and
        must not be listed either - "1 machine" beside "0 of 1 used" would be a
        page contradicting itself."""
        app, db, repo = stack
        await _machine(repo, "mika-web", "mika-260115", pve_status="running")
        gone = await _machine(repo, "mika-old", "mika-260115", vmid=131, pve_status="stopped")
        await db.execute(
            "UPDATE hosts SET absent_since = '2026-11-01T00:00:00Z' WHERE id = ?", (gone,)
        )
        await db.conn.commit()
        with TestClient(app) as client:
            mika = _friend(client.get("/admin/guests/friends").json(), "mika-260115")
        assert [m["name"] for m in mika["machines"]] == ["mika-web"]
        used = await usage_for(repo, "mika-260115")
        assert mika["usage"] == {
            "vms": used.vms,
            "cores": used.cores,
            "memory_mb": used.memory_mb,
            "disk_gb": used.disk_gb,
        }
        assert mika["usage"]["vms"] == len(mika["machines"]) == 1

    async def test_a_machine_with_no_hypervisor_word_falls_back_to_its_status(self, stack):
        app, _db, repo = stack
        await _machine(repo, "aino-box", "aino", status="offline")
        with TestClient(app) as client:
            aino = _friend(client.get("/admin/guests/friends").json(), "aino")
        assert aino["machines"][0]["state"] == "offline"

    async def test_every_source_of_a_cn_makes_a_friend(self, stack):
        app, _db, repo = stack
        await _machine(repo, "mika-web", "mika-260115")
        with TestClient(app) as client:
            client.post("/admin/guests/invites", json={**MINT, "cn": "aino"})
            client.post("/admin/guests/quota", json={"cn": "eeva", "max_vms": 1})
            body = client.get("/admin/guests/friends").json()
        assert [f["cn"] for f in body["friends"]] == ["aino", "eeva", "mika-260115"]
        assert _friend(body, "aino")["machines"] == []
        assert _friend(body, "eeva")["limits"] == {
            "vms": 1,
            "cores": None,
            "memory_mb": None,
            "disk_gb": None,
        }
        assert _friend(body, "mika-260115")["limits"] is None


class TestTheOperatorsJourneysLandInTheRead:
    async def test_a_minted_invite_appears_under_the_friend_open(self, stack):
        app, _db, _repo = stack
        with TestClient(app) as client:
            minted = client.post("/admin/guests/invites", json=MINT)
            assert minted.status_code == 201, minted.text
            mika = _friend(client.get("/admin/guests/friends").json(), "mika-260115")
        (inv,) = mika["invites"]
        assert inv["id"] == minted.json()["id"]
        assert inv["prefix"] == minted.json()["token"][:16]
        assert inv["state"] == "open"
        assert inv["caps"]["cores"] == 1 and inv["caps"]["memory_mb"] == 1024
        assert inv["redeemed_at"] is None and inv["revoked_at"] is None
        assert inv["expires_at"] > inv["created_at"]

    async def test_a_revoke_turns_it_revoked_with_its_time(self, stack):
        app, _db, _repo = stack
        with TestClient(app) as client:
            prefix = client.post("/admin/guests/invites", json=MINT).json()["token"][:16]
            assert client.post(f"/admin/guests/invites/{prefix}/revoke").status_code == 200
            (inv,) = _friend(client.get("/admin/guests/friends").json(), "mika-260115")["invites"]
        assert inv["state"] == "revoked"
        assert inv["revoked_at"]

    async def test_invites_are_newest_first(self, stack):
        app, db, _repo = stack
        with TestClient(app) as client:
            first = client.post("/admin/guests/invites", json=MINT).json()["id"]
            second = client.post("/admin/guests/invites", json=MINT).json()["id"]
            # Same-second mints: make the order a fact, not a tie.
            await db.execute(
                "UPDATE invites SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?", (first,)
            )
            await db.conn.commit()
            invites = _friend(client.get("/admin/guests/friends").json(), "mika-260115")["invites"]
        assert [i["id"] for i in invites] == [second, first]

    async def test_a_budget_set_here_is_that_friends_limits(self, stack):
        app, _db, repo = stack
        await _machine(repo, "mika-web", "mika-260115")
        with TestClient(app) as client:
            client.post(
                "/admin/guests/quota",
                json={"cn": "mika-260115", "max_vms": 1, "max_memory_mb": 1024, "max_disk_gb": 30},
            )
            mika = _friend(client.get("/admin/guests/friends").json(), "mika-260115")
        assert mika["limits"] == {"vms": 1, "cores": None, "memory_mb": 1024, "disk_gb": 30}
        assert mika["usage"]["vms"] == 1 and mika["usage"]["disk_gb"] == 30


class TestTheTokenIsShownOnce:
    async def test_no_later_read_carries_the_token(self, stack):
        app, db, _repo = stack
        ctx = {"repo": Repository(db), "invite_repo": InviteRepository(db)}
        with TestClient(app) as client:
            token = client.post("/admin/guests/invites", json=MINT).json()["token"]
            assert token.startswith("hpi_") and len(token) > 16
            friends = client.get("/admin/guests/friends").text
            overview = client.get("/admin/guests").text
        assert token not in friends
        assert token not in overview
        assert token not in str(await handle_query_guests({}, ctx))
        # Nor the part after the prefix, in any shape (the hash is not listed).
        assert token[16:] not in friends
        assert "token_hash" not in friends


class TestTheConsoleFurniture:
    async def test_sizes_bounds_and_unset_defaults(self, stack):
        app, _db, _repo = stack
        with TestClient(app) as client:
            body = client.get("/admin/guests/friends").json()
        names = [p["name"] for p in body["sizing"]["presets"]]
        assert names == ["small", "medium"]
        bounds = body["sizing"]["bounds"]
        for preset in body["sizing"]["presets"]:
            for axis in ("cores", "memory_mb", "disk_gb"):
                assert bounds[axis]["min"] <= preset[axis] <= bounds[axis]["max"]
        assert body["sizing"]["ttl_days"] == {"min": 1, "max": 90, "default": 7}
        # No provisioning defaults and no portal origin on a bare instance: said
        # as null, so the dialog asks for node/template instead of guessing.
        assert body["defaults"] == {"node": None, "template_vmid": None}
        assert body["portal_origin"] is None

    async def test_every_preset_mints(self, stack):
        """A preset the mint route refuses would be a button that cannot work."""
        app, _db, _repo = stack
        with TestClient(app) as client:
            body = client.get("/admin/guests/friends").json()
            for preset in body["sizing"]["presets"]:
                size = {k: preset[k] for k in ("cores", "memory_mb", "disk_gb")}
                res = client.post("/admin/guests/invites", json={**MINT, **size})
                assert res.status_code == 201, (preset, res.text)

    async def test_the_portal_origin_is_the_configured_one(self, stack):
        app, _db, _repo = stack
        from types import SimpleNamespace

        app.state.settings = SimpleNamespace(portal_base_url="https://guests.example.com/")
        with TestClient(app) as client:
            body = client.get("/admin/guests/friends").json()
        assert body["portal_origin"] == "https://guests.example.com"


class TestSameAnswerAndScope:
    async def test_query_guests_reads_the_same_friends(self, stack):
        app, db, repo = stack
        await _machine(repo, "mika-web", "mika-260115", pve_status="running")
        ctx = {"repo": repo, "invite_repo": InviteRepository(db)}
        with TestClient(app) as client:
            client.post("/admin/guests/invites", json=MINT)
            client.post("/admin/guests/quota", json={"cn": "mika-260115", "max_vms": 1})
            route = client.get("/admin/guests/friends").json()["friends"]
        tool = (await handle_query_guests({}, ctx))["friends"]
        assert route and tool == route

    async def test_a_read_only_token_is_refused(self, stack):
        app, _db, _repo = stack
        from homepilot.auth.deps import require_token

        app.dependency_overrides[require_token] = lambda: {"scope": "read_only", "role": None}
        with TestClient(app) as client:
            assert client.get("/admin/guests/friends").status_code == 403

    async def test_nothing_answers_without_a_token(self, tmp_path: Path):
        db = Database(str(tmp_path / "noauth.db"))
        await db.connect()
        await run_migrations(db)
        try:
            app = FastAPI()
            app.include_router(guest_admin_router)
            app.state.repo = Repository(db)
            app.state.invite_repo = InviteRepository(db)
            with TestClient(app) as client:
                assert client.get("/admin/guests/friends").status_code in (401, 403)
        finally:
            await db.close()
