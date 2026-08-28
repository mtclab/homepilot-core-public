"""Provisioning defaults, and the cluster's veto over them (#553 C3).

THE DEFECTS THIS FORBIDS:

* a provisioning default the cluster refutes being STORED anyway - a bridge no
  node has, a template that is a running VM, a pool the token cannot see. The
  operator would find out weeks later, when a friend's redemption dies half way
  through a clone;
* a value saved while the cluster could not be asked at all, and reported as if
  it had been checked ("unreachable" is not "fine");
* a refusal that paraphrases the cluster instead of repeating it - "invalid
  bridge" cannot be acted on, "node has: vmbr0, vmbr1" can;
* an invite that still has to carry raw infra details, or one that silently
  gets no node at all;
* net0 being rewritten on a clone by an instance that was never told which
  bridge to use (the pre-C3 behaviour must survive untouched), or NOT being
  written when it was.

The journey gate drives the REAL surfaces end to end: the settings API, the
mint route, the portal redemption, and the provision service talking to a fake
PVE at the httpx boundary - because "the PUT returned 200" says nothing about
what a guest's NIC ends up on.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Request, Response

from homepilot.admin.router import _require_admin_dep
from homepilot.admin.router import router as admin_router
from homepilot.app_settings import DB_KEY_PREFIX, REGISTRY, SettingsResolver
from homepilot.auth.deps import require_token
from homepilot.config import Settings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.defaults import (
    ProvisioningDefaults,
    provisioning_defaults,
)
from homepilot.provision.probes import (
    ProbeContext,
    probe_bridge,
    probe_ipconfig,
    probe_node,
    probe_pool,
    probe_storage,
    probe_template_vmid,
    probe_vlan_tag,
)

from .portal_support import (
    CN_A,
    PUBKEY,
    FakePVE,
    cert_headers,
    client_for,
    poll_status,
    portal_settings,
)

pytestmark = pytest.mark.asyncio


def _admin_token() -> dict[str, Any]:
    return {"user_id": 1, "scope": "*", "role": "admin"}


# ── A cluster that answers ───────────────────────────────────────────────────
# One shape, used by the probe unit tests and by the journey, so the two gates
# cannot drift into disagreeing about what a PVE reply looks like.

NODES = [{"node": "pve1"}, {"node": "pve2"}]
RESOURCES = [
    {"type": "qemu", "vmid": 9000, "name": "debian-12-tpl", "node": "pve1", "template": 1},
    {"type": "qemu", "vmid": 9100, "name": "alpine-tpl", "node": "pve2", "template": 1},
    {"type": "qemu", "vmid": 105, "name": "running-box", "node": "pve1", "template": 0},
    {"type": "lxc", "vmid": 200, "name": "a-container", "node": "pve1"},
]
POOLS = [{"poolid": "guests"}, {"poolid": "infra"}]
# What `GET /nodes/pve1/storage` answers. `backups` is the trap #618's probe
# exists to catch: PVE reports it happily, and a clone aimed at it dies inside
# the clone task because it holds no `images` content.
STORAGES = [
    {"storage": "local", "content": "vztmpl,iso,images"},
    {"storage": "local-zfs", "content": "images,rootdir"},
    {"storage": "backups", "content": "backup,iso"},
]
NETWORK = [
    {"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1},
    {"iface": "vmbr1", "type": "bridge", "bridge_vlan_aware": 0},
    {"iface": "eno1", "type": "eth"},
]


class FakeCluster:
    """The three reads the probes make, and a switch to take them away."""

    def __init__(self, network: list[dict[str, Any]] | None = None) -> None:
        self.network = NETWORK if network is None else network
        self.down = False
        self.reads: list[str] = []

    async def read(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        self.reads.append(path)
        if self.down:
            raise RuntimeError("connection refused")
        if path == "/nodes":
            return {"data": NODES}
        if path == "/cluster/resources":
            return {"data": RESOURCES}
        if path == "/pools":
            return {"data": POOLS}
        if path.endswith("/storage"):
            return {"data": STORAGES}
        if path.endswith("/network"):
            return {"data": self.network}
        if path == "/cluster/sdn/vnets":
            # The bridge probe asks the SDN too (an SDN vnet is a valid guest
            # bridge); this cluster has none unless a test seeds them.
            return {"data": getattr(self, "sdn_vnets", [])}
        raise AssertionError(f"unexpected probe read: {path}")


def ctx(cluster: FakeCluster | None, **kwargs: Any) -> ProbeContext:
    return ProbeContext(proxmox=cluster, **kwargs)


class TestTheProbesRepeatWhatTheClusterSaid:
    async def test_a_node_the_cluster_does_not_have_is_refused_with_the_list(self):
        result = await probe_node("pve9", ctx(FakeCluster()))
        assert result.ok is False
        assert result.detail == "no node pve9 in the cluster; cluster has: pve1, pve2"

    async def test_a_node_the_cluster_has_is_confirmed(self):
        assert (await probe_node("pve1", ctx(FakeCluster()))).ok is True

    async def test_an_empty_value_is_always_allowed_and_asks_nothing(self):
        cluster = FakeCluster()
        result = await probe_node("", ctx(cluster))
        assert result.ok is True
        # Clearing a default must not need a reachable cluster.
        assert cluster.reads == []

    async def test_a_vmid_that_is_not_a_template_is_refused_saying_so(self):
        result = await probe_template_vmid(105, ctx(FakeCluster(), node="pve1"))
        assert result.ok is False
        assert "is not a template" in result.detail
        assert "105" in result.detail and "running-box" in result.detail

    async def test_a_vmid_the_cluster_does_not_have_lists_the_templates_it_does(self):
        result = await probe_template_vmid(9999, ctx(FakeCluster()))
        assert result.ok is False
        assert "no VM 9999 in the cluster" in result.detail
        assert "9000 (debian-12-tpl on pve1)" in result.detail
        assert "9100 (alpine-tpl on pve2)" in result.detail

    async def test_a_template_says_where_it_was_found(self):
        result = await probe_template_vmid(9100, ctx(FakeCluster(), node="pve1"))
        assert result.ok is True
        # Not a refusal - a template on another node is legal - but the operator
        # is told, because it is not where their default node is.
        assert "on node pve2, NOT on the default node pve1" in result.detail

    async def test_a_pool_the_token_cannot_see_is_refused_with_the_ones_it_can(self):
        result = await probe_pool("secret-pool", ctx(FakeCluster()))
        assert result.ok is False
        assert result.detail == (
            "this token cannot see a pool secret-pool; pools it can see: guests, infra"
        )

    async def test_a_visible_pool_is_confirmed(self):
        assert (await probe_pool("guests", ctx(FakeCluster()))).ok is True

    async def test_a_bridge_is_refused_with_the_bridges_the_node_has(self):
        result = await probe_bridge("vmbr7", ctx(FakeCluster(), node="pve1"))
        assert result.ok is False
        assert result.detail == "no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1"

    async def test_a_bridge_without_a_node_refuses_rather_than_guessing(self):
        cluster = FakeCluster()
        result = await probe_bridge("vmbr0", ctx(cluster, node=""))
        assert result.ok is False
        assert "set the node first" in result.detail
        assert cluster.reads == []

    async def test_a_vlan_on_a_bridge_that_is_not_vlan_aware_is_refused(self):
        result = await probe_vlan_tag(42, ctx(FakeCluster(), node="pve1", bridge="vmbr1"))
        assert result.ok is False
        assert "is not VLAN-aware" in result.detail
        assert "42" in result.detail

    async def test_a_vlan_on_a_vlan_aware_bridge_is_confirmed(self):
        result = await probe_vlan_tag(42, ctx(FakeCluster(), node="pve1", bridge="vmbr0"))
        assert result.ok is True

    async def test_a_vlan_the_node_cannot_answer_for_is_saved_with_the_doubt_stated(self):
        silent = FakeCluster(network=[{"iface": "vmbr0", "type": "bridge"}])
        result = await probe_vlan_tag(42, ctx(silent, node="pve1", bridge="vmbr0"))
        # Saved: refusing a correct tag is as wrong as promising a broken one.
        assert result.ok is True
        assert "unverified" in result.detail
        assert "does not report whether bridge vmbr0 is VLAN-aware" in result.detail

    async def test_a_vlan_without_a_bridge_refuses_rather_than_pretending(self):
        result = await probe_vlan_tag(42, ctx(FakeCluster(), node="pve1", bridge=""))
        assert result.ok is False
        assert "set the bridge first" in result.detail

    async def test_ipconfig_is_checked_locally_with_no_cluster_call(self):
        cluster = FakeCluster()
        result = await probe_ipconfig("ip=10.0.0.5/24,gw=10.0.0.1", ctx(cluster))
        assert result.ok is True
        assert cluster.reads == []

    @pytest.mark.parametrize(
        "probe,value,kwargs",
        [
            (probe_node, "pve1", {}),
            (probe_template_vmid, 9000, {}),
            (probe_pool, "guests", {}),
            (probe_bridge, "vmbr0", {"node": "pve1"}),
            (probe_vlan_tag, 42, {"node": "pve1", "bridge": "vmbr0"}),
        ],
    )
    async def test_an_unreachable_cluster_says_it_could_not_ask(self, probe, value, kwargs):
        cluster = FakeCluster()
        cluster.down = True
        result = await probe(value, ctx(cluster, **kwargs))
        assert (result.ok, result.reachable) == (False, False)
        assert "could not be asked" in result.detail

    @pytest.mark.parametrize(
        "probe,value,kwargs",
        [
            (probe_node, "pve1", {}),
            (probe_template_vmid, 9000, {}),
            (probe_pool, "guests", {}),
            (probe_bridge, "vmbr0", {"node": "pve1"}),
            (probe_vlan_tag, 42, {"node": "pve1", "bridge": "vmbr0"}),
        ],
    )
    async def test_no_proxmox_at_all_is_a_could_not_ask_too(self, probe, value, kwargs):
        result = await probe(value, ctx(None, **kwargs))
        assert (result.ok, result.reachable) == (False, False)
        assert "not configured" in result.detail


# ── The settings API refuses what the cluster refutes ────────────────────────


def _settings(**overrides: Any) -> Settings:
    return Settings(
        data_dir="/tmp/hp-c3-test", artifacts_dir="/tmp/hp-c3-test/artifacts", **overrides
    )


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "c3.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield Repository(db)
    finally:
        await db.close()


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()


@pytest.fixture
async def api(repo, cluster):
    """The real admin routes over a real repository and a fake cluster."""
    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    settings = _settings()
    app.state.repo = repo
    app.state.settings = settings
    app.state.settings_resolver = SettingsResolver(repo, settings)
    app.state.proxmox = cluster
    app.dependency_overrides[_require_admin_dep.dependency] = lambda: {
        "user_id": 1,
        "scope": "*",
        "role": "admin",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _stored(repo: Repository, key: str) -> str | None:
    row = await repo.get_setting(DB_KEY_PREFIX + key)
    return None if row is None else str(row["value"])


class TestASaveTheClusterRefutesIsRefused:
    async def test_a_bad_bridge_is_refused_with_the_clusters_answer(self, api, repo):
        await api.put("/admin/settings/overrides/provision_default_node", json={"value": "pve1"})

        response = await api.put(
            "/admin/settings/overrides/provision_default_bridge", json={"value": "vmbr7"}
        )

        assert response.status_code == 422
        assert response.json()["detail"] == ("no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1")
        # And nothing was written: a refused value must not be waiting in the DB
        # for the next provision to pick up.
        assert await _stored(repo, "provision_default_bridge") is None

    async def test_a_good_bridge_is_saved_with_what_the_cluster_said(self, api, repo):
        await api.put("/admin/settings/overrides/provision_default_node", json={"value": "pve1"})

        response = await api.put(
            "/admin/settings/overrides/provision_default_bridge", json={"value": "vmbr0"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["probe"] == {
            "ok": True,
            "detail": "Bridge vmbr0 is on node pve1.",
        }
        assert await _stored(repo, "provision_default_bridge") == "vmbr0"

    async def test_a_template_that_is_a_running_vm_is_refused(self, api, repo):
        response = await api.put(
            "/admin/settings/overrides/provision_default_template_vmid", json={"value": 105}
        )
        assert response.status_code == 422
        assert "is not a template" in response.json()["detail"]
        assert await _stored(repo, "provision_default_template_vmid") is None

    async def test_a_pool_the_token_cannot_see_is_refused(self, api, repo):
        response = await api.put(
            "/admin/settings/overrides/provision_default_pool", json={"value": "secret-pool"}
        )
        assert response.status_code == 422
        assert "pools it can see: guests, infra" in response.json()["detail"]
        assert await _stored(repo, "provision_default_pool") is None

    async def test_a_vlan_on_a_bridge_that_cannot_carry_it_is_refused(self, api, repo):
        await api.put("/admin/settings/overrides/provision_default_node", json={"value": "pve1"})
        await api.put("/admin/settings/overrides/provision_default_bridge", json={"value": "vmbr1"})

        response = await api.put(
            "/admin/settings/overrides/provision_default_vlan_tag", json={"value": 42}
        )

        assert response.status_code == 422
        assert "not VLAN-aware" in response.json()["detail"]
        assert await _stored(repo, "provision_default_vlan_tag") is None

    async def test_an_unreachable_cluster_saves_nothing_and_says_the_probe_failed(
        self, api, repo, cluster
    ):
        cluster.down = True

        response = await api.put(
            "/admin/settings/overrides/provision_default_node", json={"value": "pve1"}
        )

        # 502-shaped, not 422: the cluster did not refute the value, it said
        # nothing at all - and an unchecked provisioning default is not saved.
        assert response.status_code == 502
        assert "could not be asked" in response.json()["detail"]
        assert await _stored(repo, "provision_default_node") is None

    async def test_a_malformed_ipconfig_is_refused_by_the_type_before_any_probe(
        self, api, repo, cluster
    ):
        response = await api.put(
            "/admin/settings/overrides/provision_default_ipconfig",
            json={"value": "dhcp please"},
        )
        assert response.status_code == 400
        assert "not a PVE ipconfig0" in response.json()["detail"]
        assert await _stored(repo, "provision_default_ipconfig") is None
        assert cluster.reads == []

    async def test_an_env_locked_provisioning_default_is_refused_409(self, repo, monkeypatch):
        # The C2 rule holds for the new keys; the registry-generic tests in
        # test_app_settings.py cover every key, this is the C3 spot check.
        monkeypatch.setenv("HP_PROVISION_DEFAULT_NODE", "pve2")
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        settings = _settings()
        app.state.repo = repo
        app.state.settings = settings
        app.state.settings_resolver = SettingsResolver(repo, settings)
        app.state.proxmox = FakeCluster()
        app.dependency_overrides[_require_admin_dep.dependency] = lambda: {
            "user_id": 1,
            "scope": "*",
            "role": "admin",
        }
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                "/admin/settings/overrides/provision_default_node", json={"value": "pve1"}
            )

        assert response.status_code == 409
        assert "HP_PROVISION_DEFAULT_NODE" in response.json()["detail"]
        assert await _stored(repo, "provision_default_node") is None

    async def test_the_report_marks_which_settings_can_be_tested(self, api):
        report = (await api.get("/admin/settings/overrides")).json()["settings"]
        by_key = {entry["key"]: entry for entry in report}
        assert by_key["provision_default_bridge"]["probeable"] is True
        assert by_key["retention_days"]["probeable"] is False
        assert set(REGISTRY) == set(by_key)


class TestTheProbeEndpointAsksWithoutSaving:
    async def test_it_returns_the_refusal_without_storing_anything(self, api, repo):
        await api.put("/admin/settings/overrides/provision_default_node", json={"value": "pve1"})

        response = await api.post(
            "/admin/settings/overrides/provision_default_bridge/probe", json={"value": "vmbr7"}
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "key": "provision_default_bridge",
            "ok": False,
            "reachable": True,
            "detail": "no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1",
        }
        assert await _stored(repo, "provision_default_bridge") is None

    async def test_it_confirms_a_good_value_without_storing_it_either(self, api, repo):
        await api.put("/admin/settings/overrides/provision_default_node", json={"value": "pve1"})

        response = await api.post(
            "/admin/settings/overrides/provision_default_bridge/probe", json={"value": "vmbr0"}
        )

        assert response.json()["ok"] is True
        assert await _stored(repo, "provision_default_bridge") is None

    async def test_a_setting_with_no_probe_says_so_rather_than_claiming_a_check(self, api):
        response = await api.post(
            "/admin/settings/overrides/retention_days/probe", json={"value": 30}
        )
        body = response.json()
        assert body["ok"] is True
        assert "no cluster probe" in body["detail"]

    async def test_an_unreachable_cluster_is_reported_as_unreachable(self, api, cluster):
        cluster.down = True
        response = await api.post(
            "/admin/settings/overrides/provision_default_node/probe", json={"value": "pve1"}
        )
        assert response.json()["reachable"] is False


# ── The journey: set the defaults, mint blind, get a VLAN'd guest ────────────


class JourneyPVE(FakePVE):
    """The portal journey's fake PVE, plus the reads the probes make."""

    def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        if path == "/nodes":
            return Response(200, json={"data": NODES})
        if path == "/cluster/resources":
            return Response(200, json={"data": RESOURCES})
        if path == "/pools":
            return Response(200, json={"data": POOLS})
        if path == "/nodes/pve1/network":
            return Response(200, json={"data": NETWORK})
        if path == "/nodes/pve1/storage":
            return Response(200, json={"data": STORAGES})
        return super().handle(request)


def _journey_app(db: Database, pve: JourneyPVE) -> FastAPI:
    """Settings + mint + redemption + provisioning, one instance, one database."""
    from homepilot.adapters.proxmox import ProxmoxClient
    from homepilot.guest.admin_router import router as guest_admin_router
    from homepilot.portal import router as portal_router_module
    from homepilot.portal.repository import InviteRepository
    from homepilot.portal.router import router as portal_router
    from homepilot.provision.service import ProvisionService
    from homepilot.tasks.repository import TaskRepository

    portal_router_module._redeem_attempts.clear()

    proxmox = ProxmoxClient(base_url="https://pve.example:8006", token="root@pam!t=uuid")
    fake = httpx.AsyncClient(
        base_url="https://pve.example:8006/api2/json", transport=httpx.MockTransport(pve.handle)
    )
    proxmox._client = fake
    proxmox._write_client = fake

    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    app.include_router(guest_admin_router)
    app.include_router(portal_router, prefix="/invite")
    # Every admin route in this app, whichever router declared it: the guest
    # mint routes build their scope check from require_token, not from the
    # admin router's dependency.
    app.dependency_overrides[_require_admin_dep.dependency] = _admin_token
    app.dependency_overrides[require_token] = _admin_token

    # A real Settings, carrying the portal's wiring - and NOTHING about
    # provisioning, so every provisioning default in this journey comes from the
    # database the API writes to.
    settings = _settings(**vars(portal_settings()))
    repo = Repository(db)
    task_repo = TaskRepository(db)
    app.state.repo = repo
    app.state.settings = settings
    app.state.settings_resolver = SettingsResolver(repo, settings)
    app.state.proxmox = proxmox
    app.state.task_repo = task_repo
    app.state.invite_repo = InviteRepository(db)
    app.state.provision_service = ProvisionService(
        proxmox=proxmox,
        task_repo=task_repo,
        repo=repo,
        poll_interval=0.01,
        task_timeout_s=5.0,
        ip_wait_s=0.5,
        ip_interval=0.05,
        defaults_source=app.state,
    )
    return app


@pytest.fixture
async def journey_db(tmp_path: Path):
    db = Database(str(tmp_path / "journey.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield db
    finally:
        await db.close()


class TestTheWholeJourney:
    async def _set_defaults(self, admin: AsyncClient, **values: Any) -> None:
        for key, value in values.items():
            response = await admin.put(f"/admin/settings/overrides/{key}", json={"value": value})
            assert response.status_code == 200, (key, response.text)

    async def test_defaults_reach_the_guests_nic_through_an_invite_that_names_nothing(
        self, journey_db
    ):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await self._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
                provision_default_pool="guests",
                provision_default_bridge="vmbr0",
                provision_default_vlan_tag=42,
                provision_default_ipconfig="ip=dhcp",
            )

            # The operator picks a person and a size. No node. No template.
            minted = await admin.post(
                "/admin/guests/invites",
                json={"cn": CN_A, "cores": 2, "memory_mb": 2048, "disk_gb": 20},
            )
            assert minted.status_code == 201, minted.text
            caps = minted.json()["caps"]
            assert caps["node"] == "pve1"
            assert caps["template_vmid"] == 9000
            assert caps["pool"] == "guests"
            token = minted.json()["token"]

        async with client_for(app) as guest:
            posted = await guest.post(
                f"/invite/{token}",
                data={"ciuser": "olli", "ssh_authorized_key": PUBKEY, "hostname": "lab-box"},
                headers=cert_headers(),
            )
            assert posted.status_code in (302, 303), posted.text
            status = await poll_status(guest, token)
            assert "Ready" in status.text, status.text

        # What the cluster was actually ASKED to build - the only evidence that
        # matters. The clone lands on the default node, from the default
        # template, into the default pool...
        assert ("POST", "/nodes/pve1/qemu/9000/clone") in pve.seen
        assert pve.bodies["/nodes/pve1/qemu/9000/clone"]["pool"] == "guests"
        # ...and net0 carries the bridge AND the VLAN, which before C3 could not
        # be enforced at all because the template's NIC was cloned untouched.
        config = pve.bodies["/nodes/pve1/qemu/105/config"]
        assert config["net0"] == "virtio,bridge=vmbr0,tag=42"
        assert config["ipconfig0"] == "ip=dhcp"

    async def test_without_a_bridge_default_net0_is_never_touched(self, journey_db):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await self._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
            )
            minted = await admin.post(
                "/admin/guests/invites",
                json={"cn": CN_A, "cores": 2, "memory_mb": 2048, "disk_gb": 20},
            )
            token = minted.json()["token"]

        async with client_for(app) as guest:
            await guest.post(
                f"/invite/{token}",
                data={"ciuser": "olli", "ssh_authorized_key": PUBKEY, "hostname": "lab-box"},
                headers=cert_headers(),
            )
            await poll_status(guest, token)

        config = pve.bodies["/nodes/pve1/qemu/105/config"]
        assert "net0" not in config

    async def test_a_mint_with_neither_a_value_nor_a_default_names_the_setting(self, journey_db):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            response = await admin.post(
                "/admin/guests/invites",
                json={"cn": CN_A, "cores": 2, "memory_mb": 2048, "disk_gb": 20},
            )

        assert response.status_code == 422
        assert "provision_default_node" in response.json()["detail"]

    async def test_an_explicit_node_and_template_still_win(self, journey_db):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await self._set_defaults(admin, provision_default_node="pve1")
            minted = await admin.post(
                "/admin/guests/invites",
                json={
                    "cn": CN_A,
                    "node": "pve2",
                    "template_vmid": 9100,
                    "cores": 2,
                    "memory_mb": 2048,
                    "disk_gb": 20,
                },
            )

        assert minted.status_code == 201, minted.text
        assert minted.json()["caps"] == {
            "node": "pve2",
            "template_vmid": 9100,
            "pool": None,
            "storage": None,
            "ipconfig0": "ip=dhcp",
        }


# ── The provision API and the service, without an invite ─────────────────────


class TestTheDirectProvisionPathUsesTheSameDefaults:
    async def test_a_request_that_omits_the_infra_gets_it_from_the_instance(self, journey_db):
        from homepilot.provision.models import ProvisionRequestIn

        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
                provision_default_pool="guests",
                provision_default_ipconfig="ip=10.0.0.5/24,gw=10.0.0.1",
            )

        defaults = await provisioning_defaults(app.state)
        resolved = ProvisionRequestIn(name="web-01").resolve(defaults)

        assert (resolved.node, resolved.template_vmid) == ("pve1", 9000)
        assert resolved.pool == "guests"
        assert resolved.ipconfig0 == "ip=10.0.0.5/24,gw=10.0.0.1"

    async def test_the_defaults_are_re_read_per_provision_not_cached(self, journey_db):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        first = await provisioning_defaults(app.state)
        assert first.bridge == ""

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_bridge="vmbr0",
            )

        second = await provisioning_defaults(app.state)
        assert second.bridge == "vmbr0"
        assert second.net0 == "virtio,bridge=vmbr0"


class TestNet0IsOnlyEverWrittenOnPurpose:
    async def test_no_bridge_means_no_net0_line_at_all(self):
        assert ProvisioningDefaults().net0 is None
        assert ProvisioningDefaults(vlan_tag=42).net0 is None

    async def test_a_bridge_alone_is_untagged(self):
        assert ProvisioningDefaults(bridge="vmbr0").net0 == "virtio,bridge=vmbr0"

    async def test_a_bridge_with_a_tag_carries_it(self):
        assert (
            ProvisioningDefaults(bridge="vmbr0", vlan_tag=42).net0 == "virtio,bridge=vmbr0,tag=42"
        )


class TestTheMcpToolFillsTheSameGaps:
    async def test_provision_guest_over_mcp_takes_the_instance_defaults(self, journey_db):
        from homepilot.mcp.tools.guest_tools import handle_provision_guest

        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
            )

        service = app.state.provision_service
        result = await handle_provision_guest(
            {"name": "web-01", "ssh_authorized_key": PUBKEY},
            {"provision_service": service, "repo": app.state.repo},
        )

        assert result["status"] == "pending"
        for task in list(service._running_tasks):
            await task
        row = await service.task_repo.get_task(result["task_id"])
        assert row is not None and row["status"] == "succeeded", row["error"]
        assert json.loads(row["result_json"])["node"] == "pve1"

    async def test_it_refuses_naming_the_setting_when_nothing_supplies_the_node(self, journey_db):
        from homepilot.mcp.tools.guest_tools import handle_provision_guest

        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        service = app.state.provision_service

        with pytest.raises(ValueError, match="provision_default_node"):
            await handle_provision_guest(
                {"name": "web-01", "ssh_authorized_key": PUBKEY},
                {"provision_service": service, "repo": app.state.repo},
            )


class TestTheInviteFreezesWhatItWasMintedWith:
    async def test_changing_a_default_does_not_re_point_an_open_invite(self, journey_db):
        from homepilot.portal.repository import InviteRepository

        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
            )
            minted = await admin.post(
                "/admin/guests/invites",
                json={"cn": CN_A, "cores": 2, "memory_mb": 2048, "disk_gb": 20},
            )
            assert minted.status_code == 201
            # The operator changes their mind AFTER the invite is in someone's
            # inbox. The promise already made must not move under them.
            await admin.put(
                "/admin/settings/overrides/provision_default_node", json={"value": "pve2"}
            )

        rows = await InviteRepository(journey_db).list_invites()
        assert [row["node"] for row in rows] == ["pve1"]


class TestNoResolverIsHonestRatherThanWrong:
    async def test_a_process_with_no_database_has_no_defaults(self):
        from homepilot import app_settings

        previous = app_settings.bound_resolver()
        app_settings.bind_resolver(None)
        try:
            assert await provisioning_defaults(None) == ProvisioningDefaults()
        finally:
            app_settings.bind_resolver(previous)


class TestTheTypesRefuseNonsenseBeforeTheClusterIsAsked:
    @pytest.mark.parametrize(
        "key,value",
        [
            ("provision_default_template_vmid", 42),
            ("provision_default_template_vmid", "not-a-number"),
            ("provision_default_vlan_tag", 5000),
            ("provision_default_vlan_tag", -1),
            ("provision_default_ipconfig", "ip=999"),
            ("provision_default_ipconfig", "rm -rf /"),
        ],
    )
    async def test_the_registry_rejects_it_400(self, api, cluster, key, value):
        response = await api.put(f"/admin/settings/overrides/{key}", json={"value": value})
        assert response.status_code == 400, response.text
        assert cluster.reads == []

    @pytest.mark.parametrize(
        "value", ["ip=dhcp", "ip=10.0.0.5/24", "ip=10.0.0.5/24,gw=10.0.0.1", ""]
    )
    async def test_a_valid_ipconfig_is_accepted(self, api, value):
        response = await api.put(
            "/admin/settings/overrides/provision_default_ipconfig", json={"value": value}
        )
        assert response.status_code == 200, response.text


class TestInvitesStillWorkTheOldWay:
    async def test_an_invite_minted_with_explicit_caps_is_unchanged(self, journey_db):
        from homepilot.portal.repository import InviteRepository

        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        invites = InviteRepository(journey_db)
        from homepilot.portal.models import InviteCaps

        # The pre-C3 shape, written straight through the repository as the CLI
        # does: no defaults anywhere near it.
        await invites.create_invite(
            bound_cn=CN_A,
            caps=InviteCaps(template_vmid=9000, node="pve1", cores=2, memory_mb=2048, disk_gb=20),
            created_by="cli",
            ttl=timedelta(days=7),
        )
        rows = await invites.list_invites()
        assert rows[0]["node"] == "pve1"
        assert app.state.provision_service is not None


class TestTheBridgeProbeSeesSdnVnets:
    async def test_the_listing_asks_for_any_bridge(self) -> None:
        """A plain /network listing omits SDN vnet bridges - the guest vnet IS
        a valid guest NIC bridge, and the probe refused it the moment the
        guest network went live (live catch #4). type=any_bridge includes them."""
        from homepilot.provision.probes import ProbeContext, _network_entries

        seen: list[tuple[str, dict | None]] = []

        class Px:
            async def read(self, path, query=None):
                seen.append((path, query))
                return {"data": [{"iface": "innkeep", "type": "any_bridge"}]}

        ctx = ProbeContext(proxmox=Px(), node="elizabeth")
        entries, failure = await _network_entries(ctx)
        assert failure is None
        assert seen and seen[0][1] == {"type": "any_bridge"}
        assert entries and entries[0]["iface"] == "innkeep"


class TestTheBridgeProbeAcceptsSdnVnets:
    async def test_a_vnet_is_a_valid_guest_bridge(self) -> None:
        """The node network listing does not include SDN vnets (even with
        type=any_bridge on this PVE) - and a vnet IS what the guest bridge
        default points at. The probe asks the SDN too (live catch #5)."""
        from homepilot.provision.probes import ProbeContext, probe_bridge

        class Px:
            async def read(self, path, query=None):
                if path.startswith("/nodes/"):
                    return {"data": [{"iface": "vmbr0", "type": "bridge"}]}
                assert path == "/cluster/sdn/vnets"
                return {"data": [{"vnet": "innkeep", "zone": "guest"}]}

        result = await probe_bridge("innkeep", ProbeContext(proxmox=Px(), node="elizabeth"))
        assert result.ok, result.detail
        assert "SDN vnet" in result.detail

    async def test_a_missing_bridge_names_the_vnets_too(self) -> None:
        from homepilot.provision.probes import ProbeContext, probe_bridge

        class Px:
            async def read(self, path, query=None):
                if path.startswith("/nodes/"):
                    return {"data": [{"iface": "vmbr0", "type": "bridge"}]}
                return {"data": [{"vnet": "innkeep", "zone": "guest"}]}

        result = await probe_bridge("vmbr9", ProbeContext(proxmox=Px(), node="elizabeth"))
        assert result.ok is False
        assert "SDN vnets: innkeep" in result.detail


# ── Target storage for the clone (#618) ──────────────────────────────────────
#
# THE DEFECT THIS FORBIDS: provisioning had no way to say WHERE a guest's disks
# land, so every clone inherited the template's storage - the whole cluster's
# guests piling onto whatever storage the template happened to sit on, with no
# setting, no request field and no invite cap able to move them.
#
# Every assertion below is on the body PVE actually received, because "the
# setting saved" says nothing about where a disk ended up. Two properties ride
# along with the new option and are asserted everywhere it appears:
#
#   * `full=1` is in EVERY clone body, storage or no storage. A linked clone
#     binds the guest to its template forever and cannot leave the template's
#     storage at all, so a target storage would be silently meaningless - and
#     the owner's standing rule is that this product never mints one.
#   * NO `storage` key at all when nothing names one. Sending an empty string,
#     or guessing at 'local', would move every existing install's disks.


async def _mint_and_redeem(app: FastAPI, admin_defaults: dict[str, Any]) -> None:
    """Drive the real operator+friend journey: set defaults, mint, redeem, wait.

    Returned nothing on purpose - what happened is read off the fake PVE, which
    is the only witness that cannot agree with a bug in the code under test.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as admin:
        await TestTheWholeJourney()._set_defaults(admin, **admin_defaults)
        minted = await admin.post(
            "/admin/guests/invites",
            json={"cn": CN_A, "cores": 2, "memory_mb": 2048, "disk_gb": 20},
        )
        assert minted.status_code == 201, minted.text
        token = minted.json()["token"]

    async with client_for(app) as guest:
        posted = await guest.post(
            f"/invite/{token}",
            data={"ciuser": "olli", "ssh_authorized_key": PUBKEY, "hostname": "lab-box"},
            headers=cert_headers(),
        )
        assert posted.status_code in (302, 303), posted.text
        status = await poll_status(guest, token)
        assert "Ready" in status.text, status.text


class TestTheTargetStorageReachesTheCloneCall:
    async def test_the_default_storage_is_what_pve_is_asked_to_clone_onto(self, journey_db):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)

        await _mint_and_redeem(
            app,
            {
                "provision_default_node": "pve1",
                "provision_default_template_vmid": 9000,
                "provision_default_storage": "local-zfs",
            },
        )

        clone = pve.bodies["/nodes/pve1/qemu/9000/clone"]
        assert clone["storage"] == "local-zfs"
        # The regression gate: a target storage is only honoured on a FULL
        # clone, and a linked clone is never what this product hands a friend.
        assert clone["full"] == 1

    async def test_with_no_storage_anywhere_the_clone_body_carries_no_storage_key(self, journey_db):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)

        await _mint_and_redeem(
            app,
            {
                "provision_default_node": "pve1",
                "provision_default_template_vmid": 9000,
            },
        )

        clone = pve.bodies["/nodes/pve1/qemu/9000/clone"]
        # INHERIT, and inherit by saying nothing: an empty string or a guessed
        # 'local' would move the disks of every install that upgrades into this.
        assert "storage" not in clone
        assert clone["full"] == 1

    async def test_a_storage_named_in_the_request_beats_the_instance_default(self, journey_db):
        from homepilot.mcp.tools.guest_tools import handle_provision_guest

        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
                provision_default_storage="local-zfs",
            )

        service = app.state.provision_service
        result = await handle_provision_guest(
            {"name": "web-01", "ssh_authorized_key": PUBKEY, "storage": "local"},
            {"provision_service": service, "repo": app.state.repo},
        )
        for task in list(service._running_tasks):
            await task
        row = await service.task_repo.get_task(result["task_id"])
        assert row is not None and row["status"] == "succeeded", row["error"]

        clone = pve.bodies["/nodes/pve1/qemu/9000/clone"]
        assert clone["storage"] == "local"
        assert clone["full"] == 1

    async def test_an_invite_keeps_the_storage_it_was_minted_with(self, journey_db):
        """The frozen-caps property, proven where it can actually hurt.

        Not "the row still says local-zfs" but "the guest built after the
        operator changed their mind still LANDS on local-zfs": the invite is a
        promise about a machine, and the disks are part of that machine.
        """
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
                provision_default_storage="local-zfs",
            )
            minted = await admin.post(
                "/admin/guests/invites",
                json={"cn": CN_A, "cores": 2, "memory_mb": 2048, "disk_gb": 20},
            )
            assert minted.status_code == 201, minted.text
            assert minted.json()["caps"]["storage"] == "local-zfs"
            token = minted.json()["token"]

            # The operator repoints the instance AFTER the invite is in
            # someone's inbox.
            await admin.put(
                "/admin/settings/overrides/provision_default_storage", json={"value": "local"}
            )

        async with client_for(app) as guest:
            posted = await guest.post(
                f"/invite/{token}",
                data={"ciuser": "olli", "ssh_authorized_key": PUBKEY, "hostname": "lab-box"},
                headers=cert_headers(),
            )
            assert posted.status_code in (302, 303), posted.text
            assert "Ready" in (await poll_status(guest, token)).text

        clone = pve.bodies["/nodes/pve1/qemu/9000/clone"]
        assert clone["storage"] == "local-zfs"
        assert clone["full"] == 1

    async def test_a_storage_named_in_the_mint_beats_the_default_and_is_frozen(self, journey_db):
        from homepilot.portal.repository import InviteRepository

        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(
                admin,
                provision_default_node="pve1",
                provision_default_template_vmid=9000,
                provision_default_storage="local-zfs",
            )
            minted = await admin.post(
                "/admin/guests/invites",
                json={
                    "cn": CN_A,
                    "storage": "local",
                    "cores": 2,
                    "memory_mb": 2048,
                    "disk_gb": 20,
                },
            )
            assert minted.status_code == 201, minted.text
            assert minted.json()["caps"]["storage"] == "local"

        rows = await InviteRepository(journey_db).list_invites()
        assert [row["storage"] for row in rows] == ["local"]


class TestTheStorageProbeRefusesWhatCannotHoldADisk:
    async def test_a_storage_without_images_content_is_refused_saying_what_it_holds(self):
        result = await probe_storage("backups", ctx(FakeCluster(), node="pve1"))
        assert result.ok is False
        assert "does not hold 'images' content" in result.detail
        # The cluster's own answer, repeated - an operator can act on this.
        assert "it holds: backup, iso" in result.detail

    async def test_the_settings_api_refuses_it_too_and_saves_nothing(self, journey_db):
        pve = JourneyPVE()
        app = _journey_app(journey_db, pve)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as admin:
            await TestTheWholeJourney()._set_defaults(admin, provision_default_node="pve1")
            refused = await admin.put(
                "/admin/settings/overrides/provision_default_storage",
                json={"value": "backups"},
            )
            assert refused.status_code == 422, refused.text
            assert "does not hold 'images' content" in refused.text

        # Nothing was stored: a refused probe must never leave the value behind.
        assert (await provisioning_defaults(app.state)).storage == ""

    async def test_a_storage_the_node_does_not_have_lists_the_ones_it_does(self):
        result = await probe_storage("nvme9", ctx(FakeCluster(), node="pve1"))
        assert result.ok is False
        assert result.detail == (
            "no storage nvme9 on node pve1; node has: backups, local, local-zfs"
        )

    async def test_a_storage_that_holds_images_is_confirmed(self):
        result = await probe_storage("local-zfs", ctx(FakeCluster(), node="pve1"))
        assert result.ok is True

    async def test_an_empty_storage_is_allowed_and_asks_the_cluster_nothing(self):
        cluster = FakeCluster()
        result = await probe_storage("", ctx(cluster, node="pve1"))
        assert result.ok is True
        assert cluster.reads == []

    async def test_without_a_node_it_refuses_rather_than_guessing_where_to_look(self):
        cluster = FakeCluster()
        result = await probe_storage("local-zfs", ctx(cluster, node=""))
        assert result.ok is False
        assert "set the node first" in result.detail
        assert cluster.reads == []
