"""THE GATE for #719: a guest is destroyed only when THIS run is shown to have made it.

The incident class: PVE refused a provision's clone with `500 unable to create
VM 105: config file already exists` - somebody else (a concurrent provision, a
template build, the operator's own GUI) had taken 105 between HomePilot's read
of the next free id and its clone. The run still counted the clone as issued,
so its failure path hard-stopped and purged vmid 105, disks and all: a machine
this run never created.

What is forbidden here, as a class:

* **destroying on a refusal** - a clone (or template create) PVE refused made
  nothing, so nothing may be stopped or destroyed, whatever holds that id;
* **destroying on a guess** - when the request's fate is unknown (a transport
  failure, a cancel mid-request) the guest is destroyed only if its config
  carries this run's own marker; anything else is left alone and reported;
* **handing one id or one address to two concurrent provisions** - PVE's
  `/cluster/nextid` and the address scan reserve nothing, so the in-process
  reservation is what stands between two runs and one vmid.

The fake is PVE's HTTP API at the httpx boundary, STATEFUL, and it answers the
way PVE does: a clone onto a taken id is a 500 naming "config file already
exists", `/cluster/nextid` is lowest-free and reserves nothing, a config read of
a missing guest is a 500 "does not exist". The assertions read the fake's end
state - which machines are still on it - not which calls were made.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import Request, Response

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.models import GuestTemplateRequest, ProvisionRequest
from homepilot.provision.reservations import GuestReservations
from homepilot.provision.service import ProvisionService, clone_marker
from homepilot.provision.template import GuestTemplateService
from homepilot.tasks.repository import TaskRepository
from tests.test_provision_static_ip import world  # noqa: F401 - the static-IP fixture

pytestmark = pytest.mark.asyncio

NODE = "pve1"
TEMPLATE = 9000
OPERATOR_VM = {"name": "operator-db", "description": "the operator's database"}
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"


class Rendezvous:
    """Hold the first caller until a second one arrives, or a short while passes.

    Forces the interleaving the incident needs - two runs that have BOTH read
    "free" before either has written - instead of hoping the scheduler produces
    it. Under the fix the second run cannot get here while the first holds the
    reservation lock, so the first simply waits out the timeout and goes on.
    """

    def __init__(self, parties: int = 2, timeout: float = 0.3) -> None:
        self.parties = parties
        self.timeout = timeout
        self.arrived = 0
        self.event = asyncio.Event()

    async def meet(self) -> None:
        self.arrived += 1
        if self.arrived >= self.parties:
            self.event.set()
        if self.arrived <= self.parties:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.event.wait(), self.timeout)


class ClusterPVE:
    """A PVE that remembers which machines are on it and answers like PVE."""

    def __init__(self) -> None:
        self.vms: dict[int, dict[str, Any]] = {TEMPLATE: {"name": "tmpl", "template": 1}}
        self.stopped: list[int] = []
        self.destroyed: list[int] = []
        self.clone_attempts: list[int] = []
        # Somebody else takes these ids the instant OUR clone for them arrives -
        # the window between HomePilot's nextid read and its clone.
        self.stolen_on_clone: set[int] = set()
        # Every clone is refused as "already exists" (a cluster racing us on
        # every id we try).
        self.refuse_every_clone = False
        # The clone request's connection drops: "landed" - PVE had already
        # created our guest; "lost" - nothing was created; "occupied" - ours
        # never arrived but somebody else's machine now holds the id.
        self.drop_clone: str | None = None
        self.nextid_rendezvous: Rendezvous | None = None
        self.created_templates: list[int] = []
        self.steal_on_create = False

    async def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        method = request.method
        body = json.loads(request.content) if request.content else {}

        if path == "/cluster/nextid":
            # Lowest free id from 100, and NOTHING reserved - PVE's behaviour.
            # Read FIRST, then (maybe) wait: the answer is what the cluster
            # held when the request arrived.
            candidate = 100
            while candidate in self.vms:
                candidate += 1
            if self.nextid_rendezvous is not None:
                await self.nextid_rendezvous.meet()
            return Response(200, json={"data": candidate})
        if path == "/cluster/resources":
            return Response(
                200,
                json={
                    "data": [{"vmid": v, "node": NODE, "type": "qemu"} for v in sorted(self.vms)]
                },
            )
        if path.startswith(f"/nodes/{NODE}/tasks/"):
            return Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
        if path == f"/nodes/{NODE}/qemu" and method == "POST":
            vmid = int(body["vmid"])
            if self.steal_on_create:
                self.vms[vmid] = dict(OPERATOR_VM)
            if vmid in self.vms:
                return Response(500, text=f"unable to create VM {vmid}: config file already exists")
            self.vms[vmid] = {k: v for k, v in body.items() if k != "vmid"}
            self.created_templates.append(vmid)
            return Response(200, json={"data": f"UPID:{NODE}:create:{vmid}"})

        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "nodes" and parts[2] == "qemu":
            vmid = int(parts[3])
            tail = "/".join(parts[4:])
            if tail == "clone":
                newid = int(body["newid"])
                self.clone_attempts.append(newid)
                if newid in self.stolen_on_clone:
                    self.vms[newid] = dict(OPERATOR_VM)
                if self.refuse_every_clone or newid in self.vms:
                    return Response(
                        500, text=f"unable to create VM {newid}: config file already exists"
                    )
                if self.drop_clone == "landed":
                    self.vms[newid] = {"name": body["name"], "description": body.get("description")}
                    raise httpx.ReadError("connection reset by peer")
                if self.drop_clone == "lost":
                    raise httpx.ConnectError("connection refused")
                if self.drop_clone == "occupied":
                    # Our request never reached PVE, and meanwhile somebody
                    # else's machine appeared at that id.
                    self.vms[newid] = dict(OPERATOR_VM)
                    raise httpx.ConnectError("connection refused")
                self.vms[newid] = {"name": body["name"], "description": body.get("description")}
                return Response(200, json={"data": f"UPID:{NODE}:clone:{newid}"})
            if vmid not in self.vms:
                return Response(
                    500,
                    text=f"Configuration file 'nodes/{NODE}/qemu-server/{vmid}.conf' does not exist",
                )
            if tail == "" and method == "DELETE":
                self.destroyed.append(vmid)
                del self.vms[vmid]
                return Response(200, json={"data": f"UPID:{NODE}:destroy:{vmid}"})
            if tail == "config" and method == "GET":
                return Response(200, json={"data": dict(self.vms[vmid])})
            if tail == "config":
                self.vms[vmid].update(body)
                return Response(200, json={"data": None})
            if tail == "status/current":
                return Response(200, json={"data": {"status": "running"}})
            if tail == "status/stop":
                self.stopped.append(vmid)
                return Response(200, json={"data": f"UPID:{NODE}:stop:{vmid}"})
            if tail == "status/start":
                return Response(200, json={"data": f"UPID:{NODE}:start:{vmid}"})
            if tail == "resize":
                return Response(200, json={"data": None})
            if tail == "agent/network-get-interfaces":
                return Response(500, text="QEMU guest agent is not running")
            if tail == "template":
                return Response(200, json={"data": None})
        if path.startswith("/storage/"):
            return Response(200, json={"data": {"content": "images,import"}})
        return Response(501, text=f"unhandled {method} {path}")


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "hp.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def pve() -> ClusterPVE:
    return ClusterPVE()


def _client(pve: ClusterPVE) -> ProxmoxClient:
    client = ProxmoxClient(base_url="https://pve.example:8006", token="root@pam!t=uuid")
    fake = httpx.AsyncClient(
        base_url="https://pve.example:8006/api2/json", transport=httpx.MockTransport(pve.handle)
    )
    client._client = fake
    client._write_client = fake
    return client


def _service(db: Database, pve: ClusterPVE, reservations: GuestReservations | None = None):
    return ProvisionService(
        proxmox=_client(pve),
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=2.0,
        ip_wait_s=0.0,
        ip_interval=0.01,
        reservations=reservations,
    )


def _request(name: str = "web-01") -> ProvisionRequest:
    return ProvisionRequest(
        name=name,
        node=NODE,
        template_vmid=TEMPLATE,
        ssh_authorized_key=PUBKEY,
        owner="a-friend",
    )


async def _run(service: ProvisionService, request: ProvisionRequest) -> dict[str, Any]:
    task_id = await service.start(request, actor="tester")
    while service._running_tasks:
        await asyncio.gather(*list(service._running_tasks), return_exceptions=True)
    row = await service.task_repo.get_task(task_id)
    assert row is not None
    return dict(row)


def _untouched(pve: ClusterPVE, vmid: int) -> None:
    assert pve.vms.get(vmid) == OPERATOR_VM, (
        f"the machine at vmid {vmid} was not this run's, and it is gone or changed: "
        f"destroyed={pve.destroyed} stopped={pve.stopped}"
    )
    assert vmid not in pve.stopped, f"vmid {vmid} was hard-stopped by a run that never made it"
    assert vmid not in pve.destroyed, f"vmid {vmid} was destroyed by a run that never made it"


class TestARefusedCloneDestroysNothing:
    async def test_an_id_taken_between_read_and_clone_is_retried_and_the_occupant_kept(
        self, db, pve
    ):
        """THE #719 incident: PVE says 105 is taken. The occupant is left alone
        and the provision goes on at a fresh id instead of failing."""
        pve.stolen_on_clone = {100}
        row = await _run(_service(db, pve), _request())

        _untouched(pve, 100)
        assert row["status"] == "succeeded", row["error"]
        result = json.loads(row["result_json"])
        assert result["vmid"] == 101
        assert pve.vms[101]["name"] == "web-01"
        assert pve.clone_attempts == [100, 101]

    async def test_a_cluster_that_refuses_every_id_fails_the_run_and_touches_nothing(self, db, pve):
        pve.vms[100] = dict(OPERATOR_VM)
        pve.refuse_every_clone = True
        row = await _run(_service(db, pve), _request())

        assert row["status"] == "failed"
        assert "already exists" in row["error"]
        _untouched(pve, 100)
        assert pve.destroyed == [] and pve.stopped == []
        assert json.loads(row["result_json"])["cleanup"] == "nothing_created"
        # Bounded: a cluster that refuses every id does not spin forever.
        assert 1 < len(pve.clone_attempts) <= 5


class TestAnUnknownOutcomeIsReadBackFromTheCluster:
    async def test_a_dropped_clone_that_landed_is_ours_and_is_taken_back(self, db, pve):
        """The request's fate is unknown, but the guest carries this run's marker:
        it IS ours, and leaving it would orphan it (#595)."""
        pve.drop_clone = "landed"
        row = await _run(_service(db, pve), _request())

        assert row["status"] == "failed"
        assert pve.destroyed == [100]
        assert 100 not in pve.vms
        assert json.loads(row["result_json"])["cleanup"] == "deleted"

    async def test_a_dropped_clone_with_somebody_elses_machine_there_is_left_alone(self, db, pve):
        # The operator's VM appears at 100 after our read: nothing of ours is
        # on it, so nothing may happen to it.
        pve.drop_clone = "occupied"
        row = await _run(_service(db, pve), _request())

        assert row["status"] == "failed"
        _untouched(pve, 100)
        outcome = json.loads(row["result_json"])
        assert outcome["cleanup"] == "unverified"
        assert outcome["vmid"] == 100
        assert "NOT touched" in row["error"]

    async def test_a_dropped_clone_that_left_nothing_created_nothing(self, db, pve):
        pve.drop_clone = "lost"
        row = await _run(_service(db, pve), _request())

        assert row["status"] == "failed"
        assert pve.destroyed == [] and pve.stopped == []
        assert json.loads(row["result_json"])["cleanup"] == "nothing_created"

    async def test_a_cancel_mid_request_leaves_a_machine_that_is_not_shown_to_be_ours(
        self, db, pve
    ):
        """A cancel lands while the clone request is in flight. Whatever is at
        the id afterwards is destroyed only if it carries this run's marker."""
        arrived = asyncio.Event()
        original = pve.handle

        async def held(request: Request) -> Response:
            if request.url.path.endswith("/clone"):
                pve.vms[100] = dict(OPERATOR_VM)
                arrived.set()
                await asyncio.Event().wait()  # never answers; the cancel lands here
            return await original(request)

        pve.handle = held  # type: ignore[method-assign]
        service = _service(db, pve)
        task_id = await service.start(_request(), actor="tester")
        await asyncio.wait_for(arrived.wait(), 5)
        await service.cancel(task_id)
        while service._running_tasks:
            await asyncio.gather(*list(service._running_tasks), return_exceptions=True)

        row = await service.task_repo.get_task(task_id)
        assert row["status"] == "cancelled"
        _untouched(pve, 100)
        outcome = json.loads(row["result_json"])
        assert outcome["cleanup"] == "unverified"
        assert "NOT touched" in row["error"]

    async def test_the_marker_names_this_run(self, db, pve):
        row = await _run(_service(db, pve), _request())
        assert row["status"] == "succeeded", row["error"]
        assert pve.vms[100]["description"] == clone_marker(row["id"])


class TestConcurrentProvisionsNeverShareAnId:
    async def test_two_provisions_at_once_get_two_ids_and_both_guests_survive(self, db, pve):
        """nextid reserves nothing, so both runs are told 100. Without an
        in-process reservation the second clone is refused and its unwind
        destroyed the FIRST run's guest."""
        pve.nextid_rendezvous = Rendezvous()
        service = _service(db, pve)
        first = await service.start(_request("first"), actor="tester")
        second = await service.start(_request("second"), actor="tester")
        while service._running_tasks:
            await asyncio.gather(*list(service._running_tasks), return_exceptions=True)
        rows = [await service.task_repo.get_task(t) for t in (first, second)]

        assert [r["status"] for r in rows] == ["succeeded", "succeeded"], [r["error"] for r in rows]
        vmids = {json.loads(r["result_json"])["vmid"] for r in rows}
        assert vmids == {100, 101}
        assert pve.destroyed == [] and pve.stopped == []
        assert {pve.vms[100]["name"], pve.vms[101]["name"]} == {"first", "second"}
        assert service.reservations.vmids == frozenset(), "a reservation outlived its run"

    async def test_two_provisions_at_once_get_two_addresses(self, world):  # noqa: F811
        """The address scan reserves nothing either: a guest whose config is not
        written yet claims nothing in it, so both runs were handed .10."""
        w = world()
        scan = Rendezvous()

        async def meet_at_the_scan(request: Request) -> Response:
            # The answer is computed when the request arrives; only its
            # delivery waits.
            response = w.pve.handle(request)
            if request.url.path.endswith("/cluster/resources"):
                await scan.meet()
            return response

        fake = httpx.AsyncClient(
            base_url="https://pve.example:8006/api2/json",
            transport=httpx.MockTransport(meet_at_the_scan),
        )
        w.proxmox._client = fake
        w.proxmox._write_client = fake
        rows = await asyncio.gather(w.provision("first"), w.provision("second"))

        assert [r["status"] for r in rows] == ["succeeded", "succeeded"], [r["error"] for r in rows]
        results = [json.loads(r["result_json"]) for r in rows]
        assert len({r["vmid"] for r in results}) == 2
        assert sorted(r["ip"] for r in results) == ["198.51.100.10", "198.51.100.11"]
        configs = [w.pve.config_of(r["vmid"])["ipconfig0"] for r in results]
        assert configs[0] != configs[1], "two guests were written the same address"
        assert w.service.reservations.addresses == frozenset()

    async def test_a_template_builds_claimed_id_is_never_handed_to_a_provision(self, db, pve):
        """The template build checks its id, then downloads for minutes. The
        shared reservation keeps a provision off that id meanwhile."""
        book = GuestReservations()
        assert book.reserve_vmid(100)  # held by an in-flight template build
        row = await _run(_service(db, pve, reservations=book), _request())

        assert row["status"] == "succeeded", row["error"]
        assert json.loads(row["result_json"])["vmid"] == 101


class TestTemplateBuildDestroysOnlyWhatItMade:
    def _templates(self, db, pve, book: GuestReservations | None = None) -> GuestTemplateService:
        return GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
            download_timeout_s=2.0,
            reservations=book,
        )

    async def _build(self, service: GuestTemplateService, vmid: int = 150) -> dict[str, Any]:
        request = GuestTemplateRequest(
            node=NODE,
            template_vmid=vmid,
            name="ubuntu-tmpl",
            storage="local",
            source_volid="local:import/ubuntu.qcow2",
        )
        task_id = await service.start(request, actor="tester")
        while service._running_tasks:
            await asyncio.gather(*list(service._running_tasks), return_exceptions=True)
        row = await service.task_repo.get_task(task_id)
        assert row is not None
        return dict(row)

    async def test_a_create_refused_because_the_id_was_taken_leaves_the_occupant(self, db, pve):
        """The operator's GUI took the id during the download: PVE refuses the
        create, and the failure path must not purge the operator's VM."""
        pve.steal_on_create = True
        row = await self._build(self._templates(db, pve))

        assert row["status"] == "failed"
        _untouched(pve, 150)
        assert "nothing was created" in row["error"]

    async def test_a_build_refuses_an_id_a_provision_has_reserved(self, db, pve):
        book = GuestReservations()
        assert book.reserve_vmid(150)  # an in-flight provision's id
        row = await self._build(self._templates(db, pve, book))

        assert row["status"] == "failed"
        assert "already in use" in row["error"]
        assert pve.created_templates == []
        assert book.vmids == frozenset({150}), "the build released a claim it never held"
