"""THE GATE for lane B1 (review 02 steps 1-3): the fleet knows what Proxmox knows.

Three operator questions had no answer:

1. "Why are two offline guests waiting for adoption?" - they were Proxmox
   TEMPLATES. A template can never run, so it sat in the adoption queue as an
   offline guest and inflated "pending adoption" and "offline" on the Overview.
2. "Which VMs are dev?" - nothing recorded an environment; the VMID the PVE UI
   leads with was not shown; there was no way to jump to the guest in Proxmox.
3. "Which of these is the machine HomePilot runs on?" - nothing knew, so a
   future stop button could switch the control plane off from inside itself.

Asserted on what the operator sees: the /inventory payload, the Overview
summary numbers, and what the sync leaves behind after a real-shaped
/cluster/resources answer (the shapes were read from a live dev Proxmox).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.dashboard.service import build_summary
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.service import InventoryService

pytestmark = pytest.mark.asyncio

GIB = 1024**3


def _guest(vmid: int, name: str, kind: str = "qemu", **extra: Any) -> dict[str, Any]:
    return {"vmid": vmid, "name": name, "type": kind, "status": "stopped", **extra}


class FakePVE:
    """A one-node cluster answering the reads a sync and the self-guest probe make.

    `listing` rows mirror /nodes/<node>/<type> (no pool; `template` only on
    templates; `cpus`), `resources` rows mirror /cluster/resources?type=vm
    (`pool`, `tags`, `template: 0|1`, `maxcpu`) - the two shapes differ in real
    Proxmox and the sync must read the right one for each fact.
    """

    api_host = "pve.example.com"
    api_port = 8006

    def __init__(self) -> None:
        self.node = "pve"
        self.guests: list[dict[str, Any]] = []
        self.configs: dict[int, dict[str, Any]] = {}
        self.resources_fail = False

    def add(
        self,
        vmid: int,
        name: str,
        kind: str = "qemu",
        *,
        template: bool = False,
        pool: str | None = None,
        tags: str | None = None,
        status: str = "stopped",
        config: dict[str, Any] | None = None,
    ) -> None:
        row = _guest(vmid, name, kind, status=status, cpus=2, maxmem=2 * GIB, maxdisk=5 * GIB)
        if template:
            row["template"] = 1
        if tags:
            row["tags"] = tags
        row["_pool"] = pool
        self.guests.append(row)
        self.configs[vmid] = config or {}

    def tag(self, vmid: int, tags: str | None) -> None:
        for g in self.guests:
            if g["vmid"] == vmid:
                if tags:
                    g["tags"] = tags
                else:
                    g.pop("tags", None)

    async def read(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/nodes":
            return {"data": [{"node": self.node, "status": "online", "ip": "10.0.0.2"}]}
        if path == "/cluster/resources":
            if self.resources_fail:
                raise ConnectionError("cluster/resources is down")
            out = []
            for g in self.guests:
                row = {
                    "id": f"{g['type']}/{g['vmid']}",
                    "vmid": g["vmid"],
                    "name": g["name"],
                    "type": g["type"],
                    "node": self.node,
                    "status": g["status"],
                    "template": g.get("template", 0),
                    "maxcpu": g["cpus"],
                    "maxmem": g["maxmem"],
                    "maxdisk": g["maxdisk"],
                }
                if g.get("_pool"):
                    row["pool"] = g["_pool"]
                if g.get("tags"):
                    row["tags"] = g["tags"]
                out.append(row)
            return {"data": out}
        for kind in ("qemu", "lxc"):
            if path == f"/nodes/{self.node}/{kind}":
                return {
                    "data": [
                        {k: v for k, v in g.items() if k not in ("_pool", "type")}
                        for g in self.guests
                        if g["type"] == kind
                    ]
                }
        if path.endswith("/config"):
            vmid = int(path.split("/")[-2])
            return {"data": dict(self.configs.get(vmid, {}))}
        return {"data": []}

    async def get_vm_config(self, node: str, vmid: int, guest_type: str = "qemu") -> dict[str, Any]:
        return dict(self.configs.get(vmid, {}))


def dev_shaped() -> FakePVE:
    """The dev estate the review walked: five VMs, two CTs, two templates."""
    pve = FakePVE()
    pve.add(
        100,
        "hp-test-server",
        pool="HomePilot",
        status="running",
        config={"ipconfig0": "ip=10.0.0.100/24,gw=10.0.0.1", "net0": "virtio=AA:BB,bridge=vmbr0"},
    )
    for vmid, name in ((101, "probe-a"), (102, "live-b"), (103, "live-c"), (104, "fence-a")):
        pve.add(vmid, name, pool="HomePilot", config={"ipconfig0": f"ip=10.0.0.{vmid + 50}/24"})
    pve.add(201, "ct-web", "lxc", config={"net0": "name=eth0,bridge=vmbr0,ip=dhcp"})
    pve.add(202, "ct-db", "lxc", config={"net0": "name=eth0,bridge=vmbr0,ip=10.0.0.202/24"})
    pve.add(9000, "ubuntu-cloudinit", template=True, pool="HomePilot")
    pve.add(9001, "fedora-tmpl", template=True)
    return pve


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "fleet.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield Repository(db)
    finally:
        await db.close()


@pytest.fixture
def settings(monkeypatch):
    """The control plane's own addresses, as configured - no real name lookups."""
    import homepilot.config as config_mod
    import homepilot.inventory.self_guest as self_guest_mod

    values = SimpleNamespace(agent_hub_advertise_host="10.0.0.100:8443", agent_hub_host="0.0.0.0")
    monkeypatch.setattr(config_mod, "get_settings", lambda: values)
    monkeypatch.setattr(self_guest_mod.socket, "gethostname", lambda: "localhost")
    return values


def _client(
    repo: Repository, svc: InventoryService, scope: str = "*", role: str | None = "admin"
) -> AsyncClient:
    from homepilot.inventory.router import router as inventory_router

    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = repo
    app.state.inventory_service = svc
    app.dependency_overrides[require_token] = lambda: {"scope": scope, "role": role, "prefix": "t"}
    # An address literal, so the "page was opened at" candidate needs no DNS.
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://10.0.0.250")


async def _row(repo: Repository, vmid: int) -> dict[str, Any]:
    rows = await repo.list_hosts_by_proxmox_id(vmid)
    assert rows, f"no row for vmid {vmid}"
    return rows[0]


# ── 1. Templates are provisioning inputs, not fleet members ──────────────────


class TestTemplatesLeaveTheFleetNumbers:
    async def test_the_overview_no_longer_counts_templates_as_offline_or_pending(self, repo):
        pve = dev_shaped()
        await InventoryService(repo, pve).refresh_inventory()

        inv = (await build_summary(repo))["inventory"]

        # 1 node + 7 guests; the 2 templates are reported apart, not hidden away.
        assert inv["total"] == 8
        assert inv["templates"] == 2
        # Before: 9 discovered+pending (7 guests + 2 templates) and 8 offline.
        assert inv["uncovered"] == 7, "a template is still waiting for adoption"
        assert inv["by_status"].get("offline") == 6, "a template still counts as offline"
        assert inv["by_type"] == {"node": 1, "qemu": 5, "lxc": 2}

    async def test_the_fleet_list_hides_them_and_says_how_many(self, repo):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()

        async with _client(repo, svc) as client:
            hidden = (await client.get("/inventory", params={"templates": "exclude"})).json()
            only = (await client.get("/inventory", params={"templates": "only"})).json()
            everything = (await client.get("/inventory")).json()

        names = {h["hostname"] for h in hidden["items"]}
        assert "ubuntu-cloudinit" not in names and "fedora-tmpl" not in names
        assert hidden["total"] == 8
        assert hidden["templates_hidden"] == 2
        assert {h["hostname"] for h in only["items"]} == {"ubuntu-cloudinit", "fedora-tmpl"}
        assert all(h["is_template"] == 1 for h in only["items"])
        # The API's default is unchanged for existing clients: everything.
        assert everything["total"] == 10

    async def test_a_template_cannot_be_adopted(self, repo):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        tmpl = await _row(repo, 9000)
        guest = await _row(repo, 101)

        async with _client(repo, svc) as client:
            one = await client.post(f"/inventory/{tmpl['id']}/adopt")
            bulk = await client.post(
                "/inventory/bulk", json={"action": "adopt", "host_ids": [tmpl["id"], guest["id"]]}
            )

        assert one.status_code == 409 and "template" in one.json()["detail"]
        assert bulk.json() == {"succeeded": 1, "failed": 1}
        assert (await repo.get_host(tmpl["id"]))["import_state"] == "pending"
        assert (await repo.get_host(guest["id"]))["import_state"] == "adopted"


# ── What /cluster/resources says, kept ───────────────────────────────────────


class TestProxmoxFactsAreRead:
    async def test_pool_tags_and_sizes_land_on_the_row(self, repo):
        pve = dev_shaped()
        pve.tag(103, "web;hp-env-dev")
        await InventoryService(repo, pve).refresh_inventory()

        row = await _row(repo, 103)
        assert row["pool"] == "HomePilot"
        assert row["pve_tags"] == "web;hp-env-dev"
        assert (row["maxcpu"], row["maxmem_mb"], row["maxdisk_gb"]) == (2, 2048, 5.0)
        assert (await _row(repo, 202))["pool"] is None

    async def test_a_failed_resources_read_costs_nothing_but_those_facts(self, repo):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()

        pve.resources_fail = True
        result = await svc.refresh_inventory()

        # The sweep's honesty is untouched: every listing answered.
        assert result["complete"] is True
        assert result.get("absent", 0) == 0
        # The pool is NOT wiped by a read that failed - and the template flag
        # still comes from the node listing, so the template stays out.
        assert (await _row(repo, 101))["pool"] == "HomePilot"
        assert (await _row(repo, 9000))["is_template"] == 1

    async def test_leaving_a_pool_in_proxmox_clears_it(self, repo):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        next(g for g in pve.guests if g["vmid"] == 101)["_pool"] = None

        await svc.refresh_inventory()

        assert (await _row(repo, 101))["pool"] is None


# ── 2. VMID, the Proxmox link, and env ───────────────────────────────────────


class TestWhichVmsAreDev:
    async def test_a_pve_tag_answers_it_in_one_filter(self, repo):
        pve = dev_shaped()
        pve.tag(103, "hp-env-dev")
        pve.tag(104, "HP-ENV-PROD")
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()

        async with _client(repo, svc) as client:
            dev = (await client.get("/inventory", params={"env": "dev"})).json()

        assert [h["hostname"] for h in dev["items"]] == ["live-c"]
        assert dev["items"][0]["env_source"] == "pve_tag"
        assert (await _row(repo, 104))["env"] == "prod"
        assert (await _row(repo, 9000))["env"] == "template"
        assert (await build_summary(repo))["inventory"]["by_env"]["dev"] == 1

    async def test_the_operators_env_survives_the_next_sync_and_can_be_withdrawn(self, repo):
        pve = dev_shaped()
        pve.tag(103, "hp-env-dev")
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        row = await _row(repo, 103)

        async with _client(repo, svc) as client:
            set_resp = await client.patch(f"/inventory/{row['id']}", json={"env": "prod"})
            assert set_resp.status_code == 200, set_resp.text
            await svc.refresh_inventory()
            after_sync = await repo.get_host(row["id"])

            # The operator withdraws their value: Proxmox's tag applies again
            # at once, and the next sync follows the tag again.
            cleared = (await client.patch(f"/inventory/{row['id']}", json={"env": ""})).json()
            pve.tag(103, None)
            await svc.refresh_inventory()
            untagged = await repo.get_host(row["id"])

        assert (after_sync["env"], after_sync["env_source"]) == ("prod", "user")
        assert (cleared["env"], cleared["env_source"]) == ("dev", "pve_tag")
        assert untagged["env"] is None and untagged["env_source"] is None

    async def test_an_env_outside_the_vocabulary_is_refused(self, repo):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        row = await _row(repo, 101)

        async with _client(repo, svc) as client:
            resp = await client.patch(f"/inventory/{row['id']}", json={"env": "staging"})

        assert resp.status_code == 422
        assert (await repo.get_host(row["id"]))["env"] is None

    async def test_two_different_env_tags_name_no_env(self, repo):
        pve = dev_shaped()
        pve.tag(101, "hp-env-dev;hp-env-prod")
        await InventoryService(repo, pve).refresh_inventory()

        assert (await _row(repo, 101))["env"] is None

    async def test_every_row_carries_its_vmid_and_a_link_into_proxmox(self, repo):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        ct = await _row(repo, 202)

        async with _client(repo, svc) as client:
            items = (await client.get("/inventory")).json()["items"]
            detail = (await client.get(f"/inventory/{ct['id']}")).json()

        by_name = {h["hostname"]: h for h in items}
        assert by_name["live-c"]["proxmox_id"] == 103
        assert by_name["live-c"]["proxmox_url"] == "https://pve.example.com:8006/#v1:0:=qemu%2F103"
        assert by_name["pve"]["proxmox_url"] == "https://pve.example.com:8006/#v1:0:=node%2Fpve"
        assert detail["proxmox_url"] == "https://pve.example.com:8006/#v1:0:=lxc%2F202"

    async def test_a_gone_guest_has_no_proxmox_page(self, repo):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        pve.guests = [g for g in pve.guests if g["vmid"] != 104]
        await svc.refresh_inventory()

        async with _client(repo, svc) as client:
            items = (await client.get("/inventory")).json()["items"]

        gone = next(h for h in items if h["hostname"] == "fence-a")
        assert gone["absent_since"] and gone["proxmox_url"] is None


# ── 3. The self-guest ────────────────────────────────────────────────────────


class TestHomePilotKnowsItsOwnHost:
    async def test_it_proposes_the_guest_holding_its_address_and_marks_it_once_confirmed(
        self, repo, settings
    ):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        me = await _row(repo, 100)

        async with _client(repo, svc) as client:
            before = (await client.get("/inventory/self-guest")).json()
            assert before["confirmed"] is None
            assert before["proposal"]["host_id"] == me["id"]
            assert "net0 (qemu-ipconfig) = 10.0.0.100" in before["proposal"]["evidence"]
            assert "advertise" in before["proposal"]["evidence"]
            # Proposed is not marked: nothing in the fleet says "self" yet.
            listed = (await client.get("/inventory")).json()["items"]
            assert not any(h["is_self"] for h in listed)

            confirmed = await client.put("/inventory/self-guest", json={"host_id": me["id"]})
            assert confirmed.status_code == 200, confirmed.text
            listed = (await client.get("/inventory")).json()["items"]
            detail = (await client.get(f"/inventory/{me['id']}")).json()

        assert [h["hostname"] for h in listed if h["is_self"]] == ["hp-test-server"]
        assert detail["is_self"] == 1
        assert confirmed.json()["confirmed"]["id"] == me["id"]
        assert confirmed.json()["proposal"] is None
        summary = (await build_summary(repo))["inventory"]["self_guest"]
        assert summary == {
            "host_id": me["id"],
            "hostname": "hp-test-server",
            "proxmox_id": 100,
            "has_agent": False,
            "absent": False,
        }
        audit = await repo.db.fetchall(
            "SELECT action, target_host FROM audit_log WHERE action LIKE 'self_guest%'"
        )
        assert [dict(a) for a in audit] == [
            {"action": "self_guest_confirmed", "target_host": "hp-test-server"}
        ]

    async def test_there_is_only_ever_one_self(self, repo, settings):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        a, b = await _row(repo, 100), await _row(repo, 101)

        async with _client(repo, svc) as client:
            await client.put("/inventory/self-guest", json={"host_id": a["id"]})
            await client.put("/inventory/self-guest", json={"host_id": b["id"]})

        selfs = await repo.db.fetchall("SELECT id FROM hosts WHERE is_self = 1")
        assert [r["id"] for r in selfs] == [b["id"]]
        # And the database refuses a second one whoever writes it.
        with pytest.raises(sqlite3.IntegrityError):
            await repo.db.execute("UPDATE hosts SET is_self = 1 WHERE id = ?", (a["id"],))

    async def test_a_template_or_a_gone_guest_is_never_proposed(self, repo, settings):
        pve = dev_shaped()
        # The address the control plane advertises is on a template and on a
        # guest that no longer exists; neither can be running HomePilot.
        pve.configs[100] = {}
        pve.configs[9000] = {"ipconfig0": "ip=10.0.0.100/24"}
        pve.configs[104] = {"ipconfig0": "ip=10.0.0.100/24"}
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        pve.guests = [g for g in pve.guests if g["vmid"] != 104]
        await svc.refresh_inventory()

        async with _client(repo, svc) as client:
            answer = (await client.get("/inventory/self-guest")).json()
            tmpl = await _row(repo, 9000)
            refused = await client.put("/inventory/self-guest", json={"host_id": tmpl["id"]})

        assert answer["proposal"] is None and answer["candidates"] == []
        assert refused.status_code == 409

    async def test_two_guests_on_the_advertised_address_is_a_question_not_an_answer(
        self, repo, settings
    ):
        pve = dev_shaped()
        pve.configs[101] = {"ipconfig0": "ip=10.0.0.100/24"}
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()

        async with _client(repo, svc) as client:
            answer = (await client.get("/inventory/self-guest")).json()

        assert answer["proposal"] is None
        assert {c["hostname"] for c in answer["candidates"]} == {"hp-test-server", "probe-a"}

    async def test_a_proxy_matching_only_the_browser_address_does_not_outvote_the_hub(
        self, repo, settings
    ):
        pve = dev_shaped()
        # The page is opened through a proxy guest at .250; the hub advertise
        # address (.100) is the deliberate one.
        pve.configs[202] = {"net0": "name=eth0,bridge=vmbr0,ip=10.0.0.250/24"}
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()

        async with _client(repo, svc) as client:
            answer = (await client.get("/inventory/self-guest")).json()

        assert answer["proposal"]["hostname"] == "hp-test-server"
        assert [c["hostname"] for c in answer["candidates"]] == ["hp-test-server", "ct-db"]

    async def test_not_this_one_stops_the_proposal_and_clearing_unmarks(self, repo, settings):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        me = await _row(repo, 100)

        async with _client(repo, svc) as client:
            dismissed = (
                await client.post("/inventory/self-guest/dismiss", json={"host_id": me["id"]})
            ).json()
            await client.put("/inventory/self-guest", json={"host_id": me["id"]})
            cleared = (await client.delete("/inventory/self-guest")).json()

        assert dismissed["proposal"] is None and dismissed["dismissed_host_id"] == me["id"]
        assert cleared["confirmed"] is None
        assert (await repo.get_host(me["id"]))["is_self"] == 0

    async def test_the_proposal_reads_the_one_address_table(self, repo, settings):
        """Release 3.8.0 wiring: the self-guest match reads lane B2's ``host_addresses``.

        Before integration B1 assembled addresses live (guest config plus
        ``hosts.ip_address``) next to B2's table: two address sources that could
        disagree. Now the table is the only one. A DHCP guest whose address only
        its guest agent reported is found through the table; a row whose bare
        ``ip_address`` (a guess with no provenance) names the address is not
        evidence; and a config change the table has not seen yet is not either.
        """
        from homepilot.inventory.addresses import SOURCE_GUEST_AGENT, NicAddress

        pve = dev_shaped()
        pve.configs[100] = {"net0": "virtio=AA:BB:CC:00:00:64,bridge=vmbr0", "ipconfig0": "ip=dhcp"}
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        me, guesser, late = await _row(repo, 100), await _row(repo, 101), await _row(repo, 102)
        # Only the guest agent of VM 100 says it holds the advertised address.
        await repo.replace_host_addresses(
            me["id"],
            (SOURCE_GUEST_AGENT,),
            [
                NicAddress(
                    address="10.0.0.100",
                    source=SOURCE_GUEST_AGENT,
                    iface="eth0",
                    mac="AA:BB:CC:00:00:64",
                    bridge="vmbr0",
                )
            ],
        )
        # A stale primary-address guess on another row names it too.
        await repo.update_host(guesser["id"], ip_address="10.0.0.100")
        # And a third guest's live config now claims it, after the last sync.
        pve.configs[102] = {"ipconfig0": "ip=10.0.0.100/24"}

        async with _client(repo, svc) as client:
            answer = (await client.get("/inventory/self-guest")).json()

        assert [c["hostname"] for c in answer["candidates"]] == ["hp-test-server"]
        assert answer["proposal"]["host_id"] == me["id"]
        assert "eth0 (guest-agent) = 10.0.0.100" in answer["proposal"]["evidence"]
        assert late["id"] not in {c["host_id"] for c in answer["candidates"]}

        # The next sync files the new config claim in the table, and only then
        # does it count: two guests on the advertised address is a question.
        await svc.refresh_inventory()
        async with _client(repo, svc) as client:
            again = (await client.get("/inventory/self-guest")).json()
        assert again["proposal"] is None
        assert {c["hostname"] for c in again["candidates"]} == {"hp-test-server", "live-b"}

    async def test_a_default_containerised_install_proposes_the_vm_its_agent_runs_on(
        self, repo, settings, tmp_path, monkeypatch
    ):
        """The 3.8.0-rc1 dev walk: no proposal on a default install.

        HomePilot runs as a container on a bridge network on its own VM. With no
        advertise address set (a zero-touch install sets none) the only address
        it knows for itself is the bridge's, which no guest holds, so nothing
        matched. What does name the machine: the VM's own agent dials the hub
        from the container's default gateway - the docker host's end of the
        bridge - so that agent's host is the one HomePilot runs on.
        """
        import homepilot.inventory.router as router_mod
        import homepilot.inventory.self_guest as self_guest_mod
        from homepilot.agent_hub.registry import AgentRegistry

        # The container's view: its own address is the bridge's, no advertise.
        settings.agent_hub_advertise_host = ""
        monkeypatch.setattr(self_guest_mod.socket, "gethostname", lambda: "172.18.0.2")
        route = tmp_path / "route"
        route.write_text(
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n"
            "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"
        )
        marker = tmp_path / ".dockerenv"
        marker.touch()
        monkeypatch.setattr(
            router_mod, "default_gateway", lambda: self_guest_mod.default_gateway(route)
        )
        monkeypatch.setattr(
            router_mod,
            "running_in_container",
            lambda: self_guest_mod.running_in_container((marker,)),
        )

        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        me, other = await _row(repo, 100), await _row(repo, 101)
        # Each VM's agent enrolled and was linked to its row by hostname.
        assert await repo.link_agent_host("ag-self", "hp-test-server") == me["id"]
        assert await repo.link_agent_host("ag-other", "probe-a") == other["id"]

        class _Writer:
            def __init__(self, peer: str) -> None:
                self.peer = peer

            def get_extra_info(self, key: str) -> Any:
                return (self.peer, 40000) if key == "peername" else None

        registry = AgentRegistry()
        # The VM's own agent, through the docker host's end of the bridge; a
        # second agent from elsewhere on the LAN.
        registry.register("ag-self", "hp-test-server", writer=_Writer("172.18.0.1"))  # type: ignore[arg-type]
        registry.register("ag-other", "probe-a", writer=_Writer("10.0.0.151"))  # type: ignore[arg-type]

        async with _client(repo, svc) as client:
            client._transport.app.state.agent_registry = registry  # type: ignore[attr-defined]
            answer = (await client.get("/inventory/self-guest")).json()

            # Not in a container (a VM or bare-metal install): its gateway is the
            # LAN router, and an agent arriving from it proves nothing.
            marker.unlink()
            elsewhere = (await client.get("/inventory/self-guest")).json()

        # Every address it knows for itself is one no guest holds.
        assert "172.18.0.2" in [a["address"] for a in answer["control_plane_addresses"]]
        assert answer["candidates"] and all(c["address"] is None for c in answer["candidates"])
        assert answer["proposal"] is not None, answer
        assert answer["proposal"]["host_id"] == me["id"]
        assert "172.18.0.1" in answer["proposal"]["evidence"]
        assert "default gateway" in answer["proposal"]["evidence"]
        assert [c["hostname"] for c in answer["candidates"]] == ["hp-test-server"]
        assert elsewhere["proposal"] is None and elsewhere["candidates"] == []

    async def test_the_advertise_address_outranks_the_agent_on_the_docker_host(
        self, repo, settings, tmp_path, monkeypatch
    ):
        """An operator who set the advertise address said where HomePilot is."""
        import homepilot.inventory.router as router_mod
        from homepilot.inventory.self_guest import ColocatedAgent

        monkeypatch.setattr(
            router_mod,
            "colocated_agents",
            lambda *_: [ColocatedAgent("ag-other", "probe-a", "172.18.0.1", "from the gateway")],
        )
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        me, other = await _row(repo, 100), await _row(repo, 101)
        assert await repo.link_agent_host("ag-other", "probe-a") == other["id"]

        async with _client(repo, svc) as client:
            answer = (await client.get("/inventory/self-guest")).json()

        assert answer["proposal"]["host_id"] == me["id"]
        assert [(c["hostname"], c["rank"]) for c in answer["candidates"]] == [
            ("hp-test-server", 0),
            ("probe-a", 1),
        ]

    async def test_moving_the_mark_needs_admin(self, repo, settings):
        pve = dev_shaped()
        svc = InventoryService(repo, pve)
        await svc.refresh_inventory()
        me = await _row(repo, 100)

        async with _client(repo, svc, scope="read,write", role=None) as client:
            readable = await client.get("/inventory/self-guest")
            put = await client.put("/inventory/self-guest", json={"host_id": me["id"]})
            delete = await client.delete("/inventory/self-guest")

        assert readable.status_code == 200
        assert (put.status_code, delete.status_code) == (403, 403)
        assert (await repo.get_host(me["id"]))["is_self"] == 0
