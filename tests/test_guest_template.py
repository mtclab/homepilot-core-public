"""THE GATE for building a cloud-init template over the API alone (#594).

What #594 actually was: provisioning clones ``template_vmid``, and HomePilot had
NO way to make that template - the manual route needs root on the PVE node,
which the scoped token does not have. So this suite asserts the OPERATOR'S GOAL,
never that a call returned ok: after a build, the fake PVE must be holding a
machine that IS A TEMPLATE, with the imported disk on scsi0 and a cloud-init
drive on ide2. A run that made every call in the right order and left no template
behind is a failed run here.

The harness is the one the provision journey uses (tests/test_provision_journey):
a stateful FakePVE at the httpx boundary under a REAL ProxmoxClient, so URL
building, JSON bodies, UPID quoting and error wrapping are exercised for real,
and the end state is read out of the fake cluster rather than out of mock calls.

The standing gates here, each with teeth proven below:

* **no overwrite** - a template_vmid already in use anywhere on the cluster is
  refused before a single write (TestVmidCollisionIsRefused);
* **no orphan** - any failure after the VM shell exists destroys it, and the
  recorded error names the cleanup outcome (TestFailureAfterCreateIsUnwound,
  the #595 class on this path);
* **terminal state** - every ending lands the task row in a terminal status
  (#386), never 'running'.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from httpx import Request, Response

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.models import GuestTemplateRequest, GuestTemplateRequestIn
from homepilot.provision.template import (
    GuestTemplateConflictError,
    GuestTemplateService,
)
from homepilot.tasks.repository import TaskRepository

NODE = "pve1"
VMID = 9000
IMAGE_URL = "https://cloud-images.example/ubuntu-24.04.qcow2"
STAGED_VOLID = "local:import/ubuntu-24.04.qcow2"


class FakePVE:
    """A stateful stand-in for the Proxmox API at the httpx boundary.

    It holds real STATE - which vmids exist, each one's config, whether it is a
    template, what each storage declares - because the assertions in this file
    are about the machine that ends up on the cluster, not about the calls that
    were made on the way there.
    """

    def __init__(
        self,
        *,
        storage_content: str = "iso,vztmpl,backup,images",
        existing_vmids: tuple[int, ...] = (100,),
    ) -> None:
        self.seen: list[tuple[str, str]] = []
        self.storage_content = storage_content
        self.vms: dict[int, dict[str, Any]] = {v: {"name": f"vm{v}"} for v in existing_vmids}
        self.templates: set[int] = set()
        self.destroyed: list[int] = []
        self.downloads: list[dict[str, Any]] = []
        # A step name -> the Response to answer it with instead of the real one,
        # so a test can make exactly one stage of the build fail.
        self.fail: dict[str, Response] = {}

    # ── the wire ─────────────────────────────────────────────────────────────

    def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        method = request.method
        self.seen.append((method, path))
        body: dict[str, Any] = {} if not request.content else json.loads(request.content)

        if path == "/cluster/resources":
            return Response(200, json={"data": [{"vmid": v} for v in sorted(self.vms)]})

        if path == "/storage/local" and method == "GET":
            return Response(200, json={"data": {"type": "dir", "content": self.storage_content}})
        if path == "/storage/local" and method == "PUT":
            if "storage" in self.fail:
                return self.fail["storage"]
            self.storage_content = str(body.get("content", ""))
            return Response(200, json={"data": None})

        if path == f"/nodes/{NODE}/storage/local/download-url":
            if "download" in self.fail:
                return self.fail["download"]
            self.downloads.append(body)
            return Response(200, json={"data": "UPID:pve1:0001:download:"})

        if path == f"/nodes/{NODE}/qemu" and method == "POST":
            if "create" in self.fail:
                return self.fail["create"]
            vmid = int(body["vmid"])
            self.vms[vmid] = {k: v for k, v in body.items() if k != "vmid"}
            return Response(200, json={"data": "UPID:pve1:0002:create:"})

        if path == f"/nodes/{NODE}/qemu/{VMID}/config" and method == "POST":
            if "import" in self.fail and "scsi0" in body:
                return self.fail["import"]
            if "cloudinit" in self.fail and "ide2" in body:
                return self.fail["cloudinit"]
            self.vms.setdefault(VMID, {}).update(body)
            # An import-from config write moves bytes, so PVE answers with a task.
            upid = "UPID:pve1:0003:import:" if "scsi0" in body else None
            return Response(200, json={"data": upid})

        if path == f"/nodes/{NODE}/qemu/{VMID}/template" and method == "POST":
            if "template" in self.fail:
                return self.fail["template"]
            self.templates.add(VMID)
            return Response(200, json={"data": "UPID:pve1:0004:template:"})

        if method == "DELETE" and path == f"/nodes/{NODE}/qemu/{VMID}":
            self.destroyed.append(VMID)
            self.vms.pop(VMID, None)
            self.templates.discard(VMID)
            return Response(200, json={"data": "UPID:pve1:0005:destroy:"})

        if path.startswith(f"/nodes/{NODE}/tasks/"):
            if method == "DELETE":  # a stop-task request
                return Response(200, json={"data": None})
            return Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})

        return Response(501, text=f"unhandled {method} {path}")

    # ── what an operator would look at afterwards ────────────────────────────

    def is_template(self, vmid: int = VMID) -> bool:
        return vmid in self.templates

    def config(self, vmid: int = VMID) -> dict[str, Any]:
        return self.vms.get(vmid, {})


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "template.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def pve() -> FakePVE:
    return FakePVE()


def _client(pve: FakePVE) -> ProxmoxClient:
    client = ProxmoxClient(base_url="https://pve.example:8006", token="hp@pve!t=uuid")
    transport = httpx.MockTransport(pve.handle)
    fake = httpx.AsyncClient(base_url="https://pve.example:8006/api2/json", transport=transport)
    client._client = fake
    client._write_client = fake
    return client


@pytest_asyncio.fixture
async def service(db: Database, pve: FakePVE) -> GuestTemplateService:
    return GuestTemplateService(
        proxmox=_client(pve),
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=2.0,
        download_timeout_s=2.0,
    )


def _request(**overrides: Any) -> GuestTemplateRequest:
    payload: dict[str, Any] = {
        "name": "ubuntu-2404-cloudinit",
        "node": NODE,
        "template_vmid": VMID,
        "storage": "local",
        "source_volid": STAGED_VOLID,
    }
    payload.update(overrides)
    return GuestTemplateRequest(**payload)


async def _run_to_completion(
    service: GuestTemplateService, request: GuestTemplateRequest
) -> dict[str, Any]:
    task_id = await service.start(request, actor="tester")
    for task in list(service._running_tasks):
        await task
    row = await service.task_repo.get_task(task_id)
    assert row is not None
    return dict(row)


# ── the goal ─────────────────────────────────────────────────────────────────


class TestTheOperatorEndsUpWithATemplate:
    async def test_a_staged_image_becomes_a_cloud_init_template(
        self, service: GuestTemplateService, pve: FakePVE
    ):
        task = await _run_to_completion(service, _request())

        assert task["status"] == "succeeded", task["error"]
        assert task["action"] == "create_guest_template"
        assert task["artifact_id"] is None

        # THE GOAL, read off the cluster: vmid 9000 is a TEMPLATE, its disk came
        # from the staged image, and it has the cloud-init drive a provision
        # writes ciuser/sshkeys/ipconfig0 into. Any of these missing means
        # provision_guest still cannot clone anything usable.
        assert pve.is_template(VMID), "the build finished without converting to a template"
        config = pve.config(VMID)
        assert config["scsi0"] == f"local:0,import-from={STAGED_VOLID}"
        assert config["ide2"] == "local:cloudinit"
        assert config["scsihw"] == "virtio-scsi-pci"
        assert config["boot"] == "order=scsi0"
        # The two things a provisioned guest needs to be reachable at all: a
        # serial console (what Ubuntu cloud images log to) and the guest agent
        # (how provisioning discovers the IP and joins a tailnet).
        assert config["serial0"] == "socket"
        assert config["vga"] == "serial0"
        assert config["agent"] == "enabled=1"
        assert config["name"] == "ubuntu-2404-cloudinit"

        result = json.loads(task["result_json"])
        assert result["vmid"] == VMID
        assert result["template"] is True
        assert result["source_volid"] == STAGED_VOLID
        assert result["downloaded_from"] is None

    async def test_a_download_url_is_fetched_by_the_node_and_imported(self, db: Database):
        # No import content to start with: the storage the operator points at is
        # a stock `local`, which is exactly the case #594 was blocked on.
        pve = FakePVE(storage_content="iso,vztmpl,backup")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
            download_timeout_s=2.0,
        )

        task = await _run_to_completion(
            service, _request(source_volid=None, download_url=IMAGE_URL)
        )

        assert task["status"] == "succeeded", task["error"]
        assert pve.is_template(VMID)
        # The node fetched the image itself, as IMPORT content (the only content
        # type that can then be an import-from source without node root).
        assert len(pve.downloads) == 1
        assert pve.downloads[0] == {
            "content": "import",
            "filename": "ubuntu-24.04.qcow2",
            "url": IMAGE_URL,
        }
        # ...and the disk was imported from exactly what was downloaded.
        assert pve.config(VMID)["scsi0"] == "local:0,import-from=local:import/ubuntu-24.04.qcow2"
        result = json.loads(task["result_json"])
        assert result["downloaded_from"] == IMAGE_URL
        assert result["source_volid"] == "local:import/ubuntu-24.04.qcow2"


class TestStorageGainsImportContentWhenItMust:
    async def test_a_storage_without_import_content_gets_it_added(self, db: Database):
        pve = FakePVE(storage_content="iso,vztmpl,backup,images")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "succeeded", task["error"]
        # The storage now declares import - which is what unblocks the whole
        # API-only path - and it did NOT lose the content types it already had.
        types = pve.storage_content.split(",")
        assert "import" in types
        assert {"iso", "vztmpl", "backup", "images"} <= set(types)
        # The change is on the RECORD, not just in a log line: it is a config
        # change to the operator's cluster that this run left in place.
        assert json.loads(task["result_json"])["storage_import_content_added"] is True

    async def test_a_storage_that_already_declares_import_is_left_alone(self, db: Database):
        pve = FakePVE(storage_content="import,iso,images")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "succeeded", task["error"]
        assert ("PUT", "/storage/local") not in pve.seen, (
            "a storage that already declares import content must not be rewritten"
        )
        assert json.loads(task["result_json"])["storage_import_content_added"] is False


# ── no overwrite ─────────────────────────────────────────────────────────────


class TestVmidCollisionIsRefused:
    """Converting to a template is ONE-WAY and a create at a taken vmid lands on
    somebody's machine. So a used vmid must be refused BEFORE anything is
    written - not "usually", and not after the storage has been touched."""

    async def test_a_used_vmid_is_refused_and_nothing_is_written(self, db: Database):
        pve = FakePVE(existing_vmids=(100, VMID))
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )
        before = dict(pve.config(VMID))

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert task["error"].startswith("failed at check_vmid:")
        assert "already in use" in task["error"]
        # Teeth: not one write happened. The existing machine is untouched, it
        # was not converted, and it was certainly not destroyed by the cleanup.
        assert pve.config(VMID) == before
        assert not pve.is_template(VMID)
        assert pve.destroyed == []
        writes = [(m, p) for m, p in pve.seen if m != "GET"]
        assert writes == [], f"a refused build still wrote to PVE: {writes}"

    async def test_a_second_build_for_the_same_vmid_is_refused_while_one_is_in_flight(
        self, service: GuestTemplateService
    ):
        task_id = await service.start(_request(), actor="tester")
        assert service.is_inflight(VMID)
        with pytest.raises(GuestTemplateConflictError, match="already in flight"):
            await service.start(_request(), actor="tester")
        for task in list(service._running_tasks):
            await task
        assert not service.is_inflight(VMID)
        assert (await service.task_repo.get_task(task_id))["status"] == "succeeded"


# ── no orphan ────────────────────────────────────────────────────────────────


class TestFailureAfterCreateIsUnwound:
    """#595's class, on this path: once the shell exists, nothing else will ever
    remove it. Every failure after the create must destroy it, and the recorded
    error must name the cleanup OUTCOME so an operator knows whether anything is
    still on the node."""

    @pytest.mark.parametrize("stage", ["import", "cloudinit", "template"])
    async def test_a_failure_after_the_create_destroys_the_half_made_vm(
        self, db: Database, stage: str
    ):
        pve = FakePVE()
        pve.fail[stage] = Response(500, text=f"boom at {stage}")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert f"boom at {stage}" in task["error"]
        # THE GOAL of the cleanup, read off the cluster: no half-made machine is
        # left behind, and nothing was silently converted.
        assert pve.destroyed == [VMID]
        assert VMID not in pve.vms
        assert not pve.is_template(VMID)
        # ...and the error SAYS so, rather than leaving the operator to guess.
        assert "destroyed the half-made template vmid 9000" in task["error"]

    async def test_a_failure_before_the_create_destroys_nothing(self, db: Database):
        pve = FakePVE(storage_content="iso")
        pve.fail["storage"] = Response(403, text="not permitted")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert task["error"].startswith("failed at storage_content:")
        # Teeth for the OTHER direction: a destroy here would delete a vmid this
        # run never created.
        assert pve.destroyed == []

    async def test_a_cleanup_that_fails_says_the_vm_may_remain(self, db: Database):
        pve = FakePVE()
        pve.fail["template"] = Response(500, text="convert refused")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )
        # The destroy itself fails: the operator must be TOLD, in words, that a
        # machine may still be there.
        service.proxmox.delete_vm = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("destroy refused")
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        assert "cleanup FAILED (destroy refused)" in task["error"]
        assert "vmid 9000 may remain on node pve1" in task["error"]

    async def test_a_failure_after_the_storage_change_says_the_change_was_left(self, db: Database):
        pve = FakePVE(storage_content="iso,images")
        pve.fail["create"] = Response(500, text="no room")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] == "failed"
        # The content type is NOT rolled back (another job may already rely on
        # it), so the error has to say it was added - an undocumented change to
        # an operator's storage config is the thing this forbids.
        assert "import" in pve.storage_content
        assert "'import' content was added to storage 'local'" in task["error"]


class TestTerminalState:
    """#386: every ending is terminal. A build stranded in 'running' sits in the
    operator's in-flight list forever and keeps blocking its own vmid."""

    @pytest.mark.parametrize("stage", ["create", "import", "cloudinit", "template"])
    async def test_every_failure_stage_lands_a_terminal_row(self, db: Database, stage: str):
        pve = FakePVE()
        pve.fail[stage] = Response(500, text="nope")
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )

        task = await _run_to_completion(service, _request())

        assert task["status"] in ("failed", "cancelled")
        assert task["finished_at"] is not None
        assert not service.is_inflight(VMID)

    async def test_without_proxmox_the_task_fails_rather_than_hanging(self, db: Database):
        service = GuestTemplateService(
            proxmox=None, task_repo=TaskRepository(db), repo=Repository(db)
        )
        task = await _run_to_completion(service, _request())
        assert task["status"] == "failed"
        assert "Proxmox not configured" in task["error"]


class TestCancelReachesTheBuild:
    """#452's class: a cancel that only marks the row is a cancel that cancelled
    nothing - the job keeps writing to PVE and then overwrites the row."""

    async def test_a_cancel_mid_build_destroys_the_half_made_vm(self, db: Database):
        pve = FakePVE()
        released = asyncio.Event()
        service = GuestTemplateService(
            proxmox=_client(pve),
            task_repo=TaskRepository(db),
            repo=Repository(db),
            poll_interval=0.01,
            task_timeout_s=2.0,
        )
        # Hold the build open right after the shell exists, then cancel.
        real_config = service.proxmox.set_vm_config

        async def blocking_config(*args: Any, **kwargs: Any) -> Any:
            released.set()
            await asyncio.sleep(30)
            return await real_config(*args, **kwargs)  # pragma: no cover

        service.proxmox.set_vm_config = blocking_config  # type: ignore[method-assign]

        task_id = await service.start(_request(), actor="tester")
        await asyncio.wait_for(released.wait(), timeout=5)
        row = await service.cancel(task_id)
        assert row is not None and row["status"] == "cancelled"
        await service.drain(timeout=5)

        final = await service.task_repo.get_task(task_id)
        assert final["status"] == "cancelled", "the job overwrote the cancelled row"
        # The shell the cancelled run created is taken back, not orphaned.
        assert pve.destroyed == [VMID]
        assert json.loads(final["result_json"])["cleanup"] == "deleted"


# ── the request model ────────────────────────────────────────────────────────


class TestTheRequestModel:
    async def test_exactly_one_image_source_is_required(self):
        with pytest.raises(ValueError, match="exactly one of source_volid"):
            _request(source_volid=None, download_url=None)
        with pytest.raises(ValueError, match="exactly one of source_volid"):
            _request(download_url=IMAGE_URL)

    async def test_a_volid_must_look_like_a_volid(self):
        with pytest.raises(ValueError, match="source_volid must look like"):
            _request(source_volid="/var/lib/vz/template/iso/ubuntu.qcow2")

    async def test_a_storage_id_cannot_smuggle_a_path(self):
        # The storage id is interpolated into an API PATH, so a slash or a '..'
        # would address something else entirely.
        with pytest.raises(ValueError, match="storage must be a PVE storage id"):
            _request(storage="local/../nodes")

    async def test_a_download_url_must_be_http_and_name_a_file(self):
        with pytest.raises(ValueError, match="http"):
            _request(source_volid=None, download_url="file:///etc/passwd")
        with pytest.raises(ValueError, match="plain image file name"):
            _request(source_volid=None, download_url="https://example.com/images/")

    async def test_the_defaults_fill_node_and_vmid_from_the_instance(self):
        from homepilot.provision.defaults import ProvisioningDefaults

        given = GuestTemplateRequestIn(source_volid=STAGED_VOLID)
        resolved = given.resolve(ProvisioningDefaults(node="pve9", template_vmid=9100))
        # The SAME settings provisioning resolves, so an instance builds exactly
        # the template it then clones.
        assert resolved.node == "pve9"
        assert resolved.template_vmid == 9100

    async def test_a_missing_default_names_the_setting(self):
        from homepilot.provision.defaults import (
            MissingProvisioningDefaultError,
            ProvisioningDefaults,
        )

        given = GuestTemplateRequestIn(source_volid=STAGED_VOLID)
        with pytest.raises(MissingProvisioningDefaultError, match="provision_default_node"):
            given.resolve(ProvisioningDefaults())


# ── the MCP surface ──────────────────────────────────────────────────────────


class TestTheMcpTool:
    def _ctx(self, service: Any) -> dict[str, Any]:
        return {"guest_template_service": service, "_mcp_token_scope": "admin"}

    def _service(self) -> Any:
        service = MagicMock()
        service.proxmox = MagicMock()
        service.defaults_source = None
        service.start = AsyncMock(return_value="task-tpl-1")
        return service

    async def test_it_starts_a_build_through_the_service(self):
        from homepilot.mcp.server import _handle_tool

        service = self._service()
        out = await _handle_tool(
            "create_guest_template",
            {"node": NODE, "template_vmid": VMID, "source_volid": STAGED_VOLID},
            {**self._ctx(service), "_mcp_caller_id": "tpl-tester"},
        )
        assert out == {"task_id": "task-tpl-1", "status": "pending"}
        sent = service.start.await_args.args[0]
        assert isinstance(sent, GuestTemplateRequest)
        assert sent.template_vmid == VMID and sent.source_volid == STAGED_VOLID
        assert service.start.await_args.kwargs["actor"] == "tpl-tester"

    async def test_an_invalid_request_never_reaches_the_service(self):
        from homepilot.mcp.server import _handle_tool

        service = self._service()
        with pytest.raises(ValueError, match="Invalid template request"):
            await _handle_tool(
                "create_guest_template",
                {"node": NODE, "template_vmid": VMID},  # no image source at all
                self._ctx(service),
            )
        service.start.assert_not_awaited()

    async def test_it_refuses_when_proxmox_is_not_configured(self):
        from homepilot.mcp.server import _handle_tool

        service = self._service()
        service.proxmox = None
        with pytest.raises(ValueError, match="Proxmox not configured"):
            await _handle_tool(
                "create_guest_template",
                {"node": NODE, "template_vmid": VMID, "source_volid": STAGED_VOLID},
                self._ctx(service),
            )

    async def test_it_is_admin_tier_and_a_full_token_is_denied(self):
        from homepilot.mcp.server import (
            _ADMIN_TOOLS,
            _MUTATING_TOOLS,
            _READ_ONLY_TOOLS,
            _handle_tool,
            _mcp_token_scope_var,
        )

        assert "create_guest_template" in _ADMIN_TOOLS
        assert "create_guest_template" not in _MUTATING_TOOLS
        assert "create_guest_template" not in _READ_ONLY_TOOLS

        service = self._service()
        token = _mcp_token_scope_var.set("full")
        try:
            with pytest.raises(ValueError, match="needs the admin tier"):
                await _handle_tool(
                    "create_guest_template",
                    {"node": NODE, "template_vmid": VMID, "source_volid": STAGED_VOLID},
                    {**self._ctx(service), "_mcp_token_scope": "full"},
                )
        finally:
            _mcp_token_scope_var.reset(token)
        # The scope check runs BEFORE the handler, so nothing was started.
        service.start.assert_not_awaited()

    async def test_the_tool_is_advertised_and_dispatched(self):
        from homepilot.mcp.server import _TOOL_DEFINITIONS, _TOOL_HANDLERS

        assert "create_guest_template" in _TOOL_HANDLERS
        tool = next(t for t in _TOOL_DEFINITIONS if t["name"] == "create_guest_template")
        # A definition that does not name both image sources leaves the model
        # guessing at the one required choice.
        assert "source_volid" in tool["inputSchema"]["properties"]
        assert "download_url" in tool["inputSchema"]["properties"]


# ── the cancel route knows who owns this action ──────────────────────────────


class TestTheCancelRouteReachesThisService:
    async def test_a_template_cancel_is_routed_to_the_template_service(self, db: Database):
        """Teeth for the #452 class: route this action to the TaskRunner and the
        row is marked while the build keeps writing. The shared cancel callable
        must hand it to the service that actually holds the coroutine."""
        from homepilot.tasks.router import perform_task_cancel

        task_repo = TaskRepository(db)
        task_id = await task_repo.create_task(None, "create_guest_template")
        await task_repo.update_task_status(task_id, "running")

        template_service = MagicMock()
        template_service.cancel = AsyncMock(return_value={"id": task_id, "status": "cancelled"})
        runner = MagicMock()
        runner.cancel_task = AsyncMock()

        out = await perform_task_cancel(
            task_id,
            task_repo=task_repo,
            provision_service=MagicMock(),
            task_runner=runner,
            guest_template_service=template_service,
        )

        assert out == {"id": task_id, "status": "cancelled"}
        template_service.cancel.assert_awaited_once_with(task_id)
        runner.cancel_task.assert_not_awaited()

    async def test_without_the_service_the_row_says_the_state_is_unknown(self, db: Database):
        from homepilot.tasks.router import perform_task_cancel

        task_repo = TaskRepository(db)
        task_id = await task_repo.create_task(None, "create_guest_template")
        await task_repo.update_task_status(task_id, "running")

        out = await perform_task_cancel(
            task_id,
            task_repo=task_repo,
            provision_service=None,
            task_runner=MagicMock(),
            guest_template_service=None,
        )

        assert out is not None and out["status"] == "cancelled"
        assert "process restarted" in (out["error"] or "")
