"""THE GATE for #824: add a disk to a guest - attach, format, mount - as ONE reviewed change.

What the operator gets is asserted, not what a function returned: after the
journey the fake cluster's VM CONFIG carries the new disk with its serial, the
guest's own fstab carries the line, the guest's mount table shows the disk at
the mount point, and drift reads the result as in spec. The cluster and the
guest agent are faked at ``ProxmoxClient.call`` (``tests/disk_guest.py``); the
in-guest script is RUN by ``/bin/sh`` against tools that keep the guest's disks
in files, so its decisions - which device, blank or not, format or refuse - are
the shell's, not the test's.

Journeys:
  * propose (REST, the form) -> plan -> approve-and-apply -> verify in spec ->
    replay changes nothing -> drift notices a removed fstab line;
  * a disk that is not blank is refused and NOTHING is formatted;
  * a guest without disk hotplug is refused before ANY change is sent;
  * the attach works and the guest part fails: "partly applied", naming what was
    done and what not, the disk is NOT detached, and a retry after the fix
    finishes without a second attach or a second mkfs;
  * a physical /dev/disk/by-id disk is refused with the exact `qm set` command;
  * an assistant's add_guest_disk proposes and attaches nothing.

Teeth (each proven by reverting the named code and watching the named test
fail; evidence in the PR):
  * drop the ``blank ||`` refusal in the guest script ->
    ``test_a_disk_that_is_not_blank_is_refused_and_nothing_is_formatted``;
  * drop the hotplug blocker in ``survey`` ->
    ``test_a_guest_without_disk_hotplug_is_refused_before_any_change``;
  * report a failed guest part without ``partial_apply`` ->
    ``test_attach_ok_guest_fails_is_partly_applied_and_the_disk_stays``;
  * drop the raw-disk refusal in ``parse_guest_disk_spec`` ->
    ``test_a_physical_disk_is_refused_with_the_qm_command``;
  * compute the slot from ``/cluster/resources``-style data instead of the
    current config (``free_slot`` ignoring config keys) ->
    ``test_the_full_journey_attaches_formats_mounts_and_verifies``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.inventory.service import InventoryService
from homepilot.mcp.server import _handle_tool
from homepilot.reconciler.verify import DriftState, verify_artifact
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner
from tests.disk_guest import DiskCluster, guest_paths
from tests.test_power_controls import ADMIN, NODE

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = DiskCluster(tmp_path)
    svc = InventoryService(repo, pve)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store, repository=repo)
    lifecycle._proxmox = pve  # propose records which machine it is for (#813)
    executor = ArtifactExecutor(
        store=store, lifecycle=lifecycle, repo=repo, proxmox=pve, vault=None
    )
    runner = TaskRunner(
        repo=TaskRepository(repo.db),
        lifecycle=lifecycle,
        executor=executor,
        apply_reconciler=None,
        store=store,
    )
    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.inventory.router import router as inventory_router

    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.repo = repo
    app.state.inventory_service = svc
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
    app.state.artifact_executor = executor
    app.state.task_runner = runner
    app.state.task_repo = TaskRepository(repo.db)
    app.dependency_overrides[require_token] = lambda: dict(ADMIN)
    ctx = {
        "repo": repo,
        "lifecycle": lifecycle,
        "store": store,
        "inventory_service": svc,
        "_mcp_caller_id": "assistant (hp_asst0000001)",
        "_mcp_token_scope": "full",
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield {
                "http": http,
                "pve": pve,
                "svc": svc,
                "repo": repo,
                "store": store,
                "executor": executor,
                "ctx": ctx,
            }
        await runner.drain(timeout=5.0)
    finally:
        await db.close()


async def _vm(
    w: dict[str, Any], *, guest: dict[str, Any] | None = None, **config: Any
) -> tuple[str, Any]:
    inside = w["pve"].add_vm(101, "app-01", config=config or None, inside=guest)
    result = await w["svc"].refresh_inventory()
    assert result["complete"], result
    row = await w["repo"].get_host_by_hostname("app-01")
    return str(row["id"]), inside


def _form(inside: Any, **over: Any) -> dict[str, Any]:
    form = {
        "storage": "fast-pool",
        "size_gib": 50,
        "mount_point": guest_paths(inside),
        "label": "appdata",
        "ssd": True,
    }
    form.update(over)
    return form


async def _propose(w: dict[str, Any], hid: str, inside: Any, **over: Any) -> dict[str, Any]:
    resp = await w["http"].post(f"/inventory/{hid}/disks", json=_form(inside, **over))
    assert resp.status_code == 200, resp.text
    out: dict[str, Any] = resp.json()
    return out


async def _run(w: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    resp = await w["http"].post(f"/artifacts/{artifact_id}/approve-and-apply?sync=true")
    assert resp.status_code == 200, resp.text
    out: dict[str, Any] = resp.json()
    return out


def _fm(w: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    fm, _ = w["store"].read(artifact_id)
    return dict(fm)


class TestTheJourney:
    async def test_the_full_journey_attaches_formats_mounts_and_verifies(self, world):
        w = world
        # scsi1 is taken in the CURRENT config (a disk the guest already uses):
        # the slot must come from there, so the new disk lands on scsi2.
        hid, inside = await _vm(w, scsi1="fast-pool:vm-101-disk-9,size=8G")
        inside.add_device("QM-OLD-1", fstype="ext4", label="old")

        options = (await w["http"].get(f"/inventory/{hid}/disks")).json()
        assert [s["storage"] for s in options["storages"]] == ["fast-pool"]
        assert options["next_slot"]["scsi"] == "scsi2"
        assert options["hotplug_disk"] is True and options["agent_answers"] is True
        assert options["blockers"] == []

        proposal = await _propose(w, hid, inside)
        assert proposal["status"] == "proposed"
        assert proposal["slot"] == "scsi2"
        assert w["pve"].config_writes == [], "a proposal attaches nothing"
        assert inside.log("mkfs") == [], "a proposal formats nothing"
        serial = proposal["serial"]

        plan = (await w["http"].post(f"/artifacts/{proposal['id']}/plan")).json()
        assert plan["kind"] == "guest-disk"
        assert plan["disk"]["slot"] == "scsi2"
        assert plan["disk"]["serial"] == serial
        assert plan["disk"]["blockers"] == []
        assert plan["disk"]["fstab_line"] == (
            f"LABEL=appdata {guest_paths(inside)} ext4 defaults,nofail,discard 0 2"
        )
        assert plan["change_count"] == 4 and plan["in_spec"] is False
        assert "scsi2" in plan["summary"]

        task = await _run(w, proposal["id"])
        assert task["status"] == "succeeded", task

        # Proxmox: the disk is attached NOW (current config), with the serial.
        config = w["pve"].guests[101]["config"]
        assert f"serial={serial}" in config["scsi2"]
        assert "discard=on" in config["scsi2"] and "ssd=1" in config["scsi2"]
        assert config["scsi1"] == "fast-pool:vm-101-disk-9,size=8G", "the old disk is untouched"
        # The guest: formatted, in fstab by LABEL with nofail, mounted.
        dev = inside.device_for(serial)
        assert dev is not None
        assert inside.fs_of(dev) == ("ext4", "appdata")
        assert inside.log("mkfs") == [f"mkfs.ext4 -q -L appdata {dev}"]
        mp = guest_paths(inside)
        fstab = inside.fstab.read_text().splitlines()
        assert fstab[-1] == f"LABEL=appdata {mp} ext4 defaults,nofail,discard 0 2"
        assert f"{dev} {mp} ext4" in inside.mounts
        assert inside.log("mount") == [f"mount {mp}"], "one mount point, never mount -a"
        assert inside.log("chattr") == [f"chattr +i {mp}"]
        backup = inside.fstab.with_name(f"fstab.homepilot-{serial}.bak")
        assert backup.read_text() == "UUID=0000-root / ext4 errors=remount-ro 0 1\n"
        assert inside.fs_of(inside.device_for("QM-OLD-1") or "") == ("ext4", "old")
        assert _fm(w, proposal["id"])["status"] == "applied"

        verdict = await verify_artifact(proposal["id"], w["repo"], w["store"], w["executor"])
        assert verdict.state is DriftState.IN_SPEC, verdict.verification_log

        # Re-applying is in spec: no second attach, no second mkfs, one line.
        writes = len(w["pve"].config_writes)
        resp = await w["http"].post(f"/artifacts/{proposal['id']}/replay?sync=true")
        assert resp.json()["status"] == "succeeded", resp.json()
        assert len(w["pve"].config_writes) == writes
        assert len(inside.log("mkfs")) == 1
        assert inside.fstab.read_text().count("LABEL=appdata") == 1

        # Drift: the fstab line removed by hand is noticed.
        inside.fstab.write_text("UUID=0000-root / ext4 errors=remount-ro 0 1\n")
        verdict = await verify_artifact(proposal["id"], w["repo"], w["store"], w["executor"])
        assert verdict.state is DriftState.DRIFTED
        assert "fstab" in verdict.verification_log

    async def test_xfs_gets_its_own_mkfs_and_no_fsck_pass(self, world):
        w = world
        hid, inside = await _vm(w)
        proposal = await _propose(w, hid, inside, filesystem="xfs", label="logs", ssd=False)
        task = await _run(w, proposal["id"])
        assert task["status"] == "succeeded", task
        dev = inside.device_for(proposal["serial"])
        assert inside.log("mkfs") == [f"mkfs.xfs -q -L logs {dev}"]
        assert inside.fstab.read_text().splitlines()[-1] == (
            f"LABEL=logs {guest_paths(inside)} xfs defaults,nofail 0 0"
        )


class TestTheGuards:
    async def test_a_disk_that_is_not_blank_is_refused_and_nothing_is_formatted(self, world):
        w = world
        hid, inside = await _vm(w)
        proposal = await _propose(w, hid, inside)
        serial = proposal["serial"]
        # The volume Proxmox hands over turns out to carry an old filesystem.
        real_add = inside.add_device

        def dirty(serial_: str, **_: Any) -> str:
            return real_add(serial_, fstype="ext4", label="someone")

        inside.add_device = dirty  # type: ignore[method-assign]

        task = await _run(w, proposal["id"])

        assert task["status"] == "failed"
        dev = inside.device_for(serial)
        assert dev is not None
        assert inside.log("mkfs") == [], "a disk with a signature is never formatted"
        assert inside.fs_of(dev) == ("ext4", "someone")
        assert inside.fstab.read_text() == "UUID=0000-root / ext4 errors=remount-ro 0 1\n"
        fm = _fm(w, proposal["id"])
        assert "not blank" in fm["failure_reason"]
        assert fm["partial_apply"]["steps_failed"] == [
            "check it is blank (no partitions, no signature)"
        ]
        assert f"serial={serial}" in w["pve"].guests[101]["config"]["scsi1"], "not detached"

    async def test_a_disk_with_partitions_is_refused_too(self, world):
        w = world
        hid, inside = await _vm(w)
        proposal = await _propose(w, hid, inside)
        real_add = inside.add_device
        inside.add_device = lambda s, **_: real_add(s, parts=1)  # type: ignore[method-assign]
        task = await _run(w, proposal["id"])
        assert task["status"] == "failed"
        assert inside.log("mkfs") == []
        assert "partition" in _fm(w, proposal["id"])["failure_reason"]

    async def test_a_guest_without_disk_hotplug_is_refused_before_any_change(self, world):
        w = world
        hid, inside = await _vm(w)
        proposal = await _propose(w, hid, inside)
        # Hotplug turned off between the proposal and the apply.
        w["pve"].guests[101]["config"]["hotplug"] = "network,usb"

        task = await _run(w, proposal["id"])

        assert task["status"] == "failed"
        assert w["pve"].config_writes == [], "nothing was sent to Proxmox"
        assert w["pve"].execs and all("MODE='apply'" not in s for s in w["pve"].execs)
        assert inside.log("mkfs") == []
        fm = _fm(w, proposal["id"])
        assert "disk hotplug" in fm["failure_reason"]
        assert "nothing was changed" in fm["failure_reason"]
        assert not fm.get("partial_apply")

        # And it is refused at propose, with the same reason.
        resp = await w["http"].post(f"/inventory/{hid}/disks", json=_form(inside))
        assert resp.status_code == 409
        assert "disk hotplug" in resp.json()["detail"]["message"]

    async def test_attach_ok_guest_fails_is_partly_applied_and_the_disk_stays(self, world):
        w = world
        hid, inside = await _vm(w)
        proposal = await _propose(w, hid, inside)
        serial = proposal["serial"]
        inside.fail("mount")

        task = await _run(w, proposal["id"])

        assert task["status"] == "failed"
        fm = _fm(w, proposal["id"])
        assert fm["status"] == "failed"
        partial = fm["partial_apply"]
        assert partial["steps_done"][0] == "attach scsi1 (fast-pool, 50 GiB)"
        assert "make the filesystem with its label" in partial["steps_done"]
        assert "append the fstab line" in partial["steps_done"]
        assert partial["steps_failed"] == ["mount it"]
        assert partial["steps_not_run"] == ["verify with findmnt"]
        assert "wrong fs type" in partial["error"]
        assert fm["failure_reason"].startswith("partly applied:")
        assert "qm set 101 --delete scsi1" in fm["failure_reason"]
        assert "never detaches" in fm["failure_reason"]
        # The disk is NOT detached: still in the current config, still in the guest.
        assert f"serial={serial}" in w["pve"].guests[101]["config"]["scsi1"]
        assert not any((b or {}).get("delete") for _, b in w["pve"].config_writes), (
            "no detach was ever sent"
        )

        # Retry after the fix: approve again, apply - no second attach, no second mkfs.
        (inside.state / "fail_mount").unlink()
        writes = len(w["pve"].config_writes)
        assert (await w["http"].post(f"/artifacts/{proposal['id']}/approve")).status_code == 200
        resp = await w["http"].post(f"/artifacts/{proposal['id']}/apply?sync=true")
        assert resp.status_code == 200, resp.text
        assert resp.json()["frontmatter"]["status"] == "applied", resp.json()
        assert len(w["pve"].config_writes) == writes
        assert len(inside.log("mkfs")) == 1
        assert inside.fstab.read_text().count("LABEL=appdata") == 1
        assert _fm(w, proposal["id"])["status"] == "applied"
        assert not _fm(w, proposal["id"]).get("partial_apply")

    async def test_a_physical_disk_is_refused_with_the_qm_command(self, world):
        w = world
        hid, inside = await _vm(w)
        device = "/dev/disk/by-id/ata-EXAMPLE_SSD_0001"

        resp = await w["http"].post(f"/inventory/{hid}/disks", json=_form(inside, storage=device))

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "raw_disk"
        assert f"qm set 101 --scsi1 {device},serial=hp" in detail["message"]
        assert "only root" in detail["message"]
        assert w["store"].list() == [] and w["pve"].config_writes == []

        # A hand-written spec (the CLI) naming a device is refused at propose.
        body = (
            "```yaml guest-disk-spec\n"
            f"storage: {device}\nsize_gib: 10\nmount_point: /srv/x\nlabel: x\n```\n"
        )
        spec = {
            "id": "2026-11-18-raw-disk-test",
            "kind": "guest-disk",
            "intent": "raw",
            "target": {"kind": "vm", "host": "app-01", "vmid": 101, "node": NODE},
            "idempotence": "via-precheck",
            "requires_snapshot": False,
            "produced_by": {"session": "s", "agent": "cli", "user": "owner"},
            "body": body,
        }
        with pytest.raises(LifecycleError, match=r"qm set 101 --scsiN .*ata-EXAMPLE_SSD_0001"):
            await w["ctx"]["lifecycle"].propose(spec)

    async def test_a_mount_point_with_files_is_refused_at_propose(self, world):
        w = world
        hid, inside = await _vm(w)
        mp = Path(guest_paths(inside))
        mp.mkdir(parents=True)
        (mp / "keep.txt").write_text("data")
        resp = await w["http"].post(f"/inventory/{hid}/disks", json=_form(inside))
        assert resp.status_code == 409
        assert "not empty" in resp.json()["detail"]["message"]
        assert w["pve"].config_writes == []

    async def test_a_label_already_in_fstab_is_refused_at_propose(self, world):
        w = world
        hid, inside = await _vm(w)
        inside.fstab.write_text(
            inside.fstab.read_text() + "LABEL=appdata /mnt/old ext4 defaults 0 2\n"
        )
        resp = await w["http"].post(f"/inventory/{hid}/disks", json=_form(inside))
        assert resp.status_code == 409
        assert "LABEL=appdata" in resp.json()["detail"]["message"]

    @pytest.mark.parametrize(
        ("guest", "said", "unsaid"),
        [
            ({"missing": ("mount",)}, ["the guest has no mount"], ["SELinux"]),
            (
                {"missing": ("mount",), "selinux_confined": True},
                [
                    "cannot see mount: SELinux confines it",
                    "virt_qemu_ga_t",
                    "setsebool -P virt_qemu_ga_run_unconfined 1",
                    "HomePilot changes neither",
                ],
                ["the guest has no mount"],
            ),
            (
                {"hidden": ("mount",), "selinux_confined": True},
                [
                    "/bin/mount, but SELinux confines the QEMU guest agent",
                    "virt_qemu_ga_run_unconfined",
                ],
                ["the guest has no mount"],
            ),
            (
                {"hidden": ("mount",)},
                ["/bin/mount, but the guest agent's shell cannot run it"],
                ["the guest has no mount", "SELinux"],
            ),
            (
                {"missing": ("mkfs.ext4",)},
                ["the guest has no mkfs.ext4 - install its ext4 tools first"],
                ["SELinux"],
            ),
        ],
        ids=["really-missing", "confined-unseen", "confined-seen", "off-path", "no-mkfs"],
    )
    async def test_a_missing_tool_and_a_confined_agent_are_told_apart(
        self, world, guest, said, unsaid
    ):
        """3.14 walk: a Fedora guest with mount installed was refused "inside the
        guest: the guest has no mount" - qemu-ga runs confined by SELinux
        (virt_qemu_ga_t) and the commands it starts could not see it. The
        preflight now says which it is, and what the operator can do about
        confinement without HomePilot doing it.

        Teeth: drop the ``missing`` classification (back to "the guest has no
        $tool" for every case) and the confined and off-path cases fail."""
        w = world
        hid, inside = await _vm(w, guest=guest)
        resp = await w["http"].post(f"/inventory/{hid}/disks", json=_form(inside))
        assert resp.status_code == 409, resp.text
        message = resp.json()["detail"]["message"]
        for words in said:
            assert words in message, message
        for words in unsaid:
            assert words not in message, message
        assert w["pve"].config_writes == [], "a refused preflight changed the VM"
        assert inside.log("mkfs.ext4") == []

    async def test_a_container_cannot_get_a_disk_this_way(self, world):
        w = world
        w["pve"].add(201, "ct-01", "running", {"net0": "name=eth0,bridge=vmbr0"}, guest_type="lxc")
        await w["svc"].refresh_inventory()
        row = await w["repo"].get_host_by_hostname("ct-01")
        resp = await w["http"].get(f"/inventory/{row['id']}/disks")
        assert resp.status_code == 409
        assert "QEMU guest agent" in resp.json()["detail"]["message"]


class TestTheAssistant:
    async def test_add_guest_disk_proposes_and_attaches_nothing(self, world):
        w = world
        hid, inside = await _vm(w)
        out = await _handle_tool(
            "add_guest_disk",
            {"host_id": hid, **_form(inside), "reason": "root disk filling"},
            w["ctx"],
        )
        assert out["status"] == "proposed" and out["attached"] is False
        assert w["pve"].config_writes == []
        fm = _fm(w, out["id"])
        assert fm["kind"] == "guest-disk"
        assert fm["target"] == {"kind": "vm", "host": "app-01", "vmid": 101, "node": NODE}
        assert fm["requires_snapshot"] is False
        assert "root disk filling" in fm["intent"]

        with pytest.raises(ValueError, match="only root"):
            await _handle_tool(
                "add_guest_disk",
                {"host_id": hid, **_form(inside, storage="/dev/sdz")},
                w["ctx"],
            )

    async def test_a_read_only_token_reads_options_but_cannot_propose(self, world):
        w = world
        hid, inside = await _vm(w)
        ro = {**w["ctx"], "_mcp_token_scope": "read_only"}
        got = await _handle_tool("get_guest_disks", {"host_id": hid}, ro)
        assert got["next_slot"]["scsi"] == "scsi1"
        with pytest.raises(ValueError, match="full tier"):
            await _handle_tool("add_guest_disk", {"host_id": hid, **_form(inside)}, ro)


class TestTheCliSpec:
    async def test_the_documented_example_spec_proposes_and_applies(self, world):
        """docs/add-disk.md's example is what `hp changes propose FILE` posts."""
        import re

        import yaml

        from homepilot.artifacts.guest_disk import serial_for

        w = world
        _hid, inside = await _vm(w)
        doc = (Path(__file__).resolve().parents[1] / "docs" / "add-disk.md").read_text()
        m = re.search(r"^```yaml\n(kind: guest-disk\n.*?)^```$", doc, re.DOTALL | re.MULTILINE)
        assert m, "docs/add-disk.md lost its example spec"
        spec = yaml.safe_load(m.group(1).replace("/srv/data", guest_paths(inside)))

        resp = await w["http"].post("/artifacts", json=spec)
        assert resp.status_code == 200, resp.text
        artifact_id = resp.json()["id"]
        task = await _run(w, artifact_id)

        assert task["status"] == "succeeded", task
        dev = inside.device_for(serial_for(artifact_id))
        assert dev is not None, "no serial in the spec: the change's own derived serial is used"
        assert inside.fs_of(dev) == ("ext4", "appdata")


class TestTheSpecRefusesAtPropose:
    @pytest.mark.parametrize(
        ("field", "value", "words"),
        [
            ("mount_options", "defaults", "nofail"),
            ("mount_point", "/etc/data", "may not be in /etc"),
            ("mount_point", "/", "not /"),
            ("mount_point", "/srv/../etc", "normalised"),
            ("label", "x" * 13, "1-12"),
            ("size_gib", 0, "size_gib"),
            ("storage", "bad storage", "Proxmox storage name"),
        ],
    )
    async def test_a_bad_field_is_refused_with_its_reason(self, world, field, value, words):
        from homepilot.artifacts.guest_disk import GuestDiskSpec, spec_yaml

        good = GuestDiskSpec(
            storage="fast-pool", size_gib=5, filesystem="xfs", mount_point="/srv/d", label="d"
        )
        text = spec_yaml(good)
        data = yaml_load(text)
        data[field] = value
        body = "```yaml guest-disk-spec\n" + yaml_dump(data) + "```\n"
        spec = {
            "id": "2026-11-18-bad-disk-spec",
            "kind": "guest-disk",
            "intent": "bad",
            "target": {"kind": "vm", "host": "app-01", "vmid": 101, "node": NODE},
            "idempotence": "via-precheck",
            "requires_snapshot": False,
            "produced_by": {"session": "s", "agent": "cli", "user": "owner"},
            "body": body,
        }
        with pytest.raises(LifecycleError, match=words):
            await world["ctx"]["lifecycle"].propose(spec)

    async def test_the_kind_rules_hold(self, world):
        body = (
            "```yaml guest-disk-spec\nstorage: fast-pool\nsize_gib: 5\n"
            "mount_point: /srv/d\nlabel: d\n```\n"
        )
        base = {
            "id": "2026-11-18-kind-rules",
            "kind": "guest-disk",
            "intent": "rules",
            "target": {"kind": "vm", "host": "app-01", "vmid": 101, "node": NODE},
            "idempotence": "via-precheck",
            "requires_snapshot": False,
            "produced_by": {"session": "s", "agent": "cli", "user": "owner"},
            "body": body,
        }
        lifecycle = world["ctx"]["lifecycle"]
        for over, words in (
            ({"requires_snapshot": True}, "requires_snapshot: false"),
            ({"idempotence": "replay-only"}, "via-precheck"),
            ({"target": {"kind": "lxc", "host": "c", "vmid": 9, "node": NODE}}, "target.kind: vm"),
            ({"rollback": True}, "rollback"),
        ):
            with pytest.raises(LifecycleError, match=words):
                await lifecycle.propose({**base, **over})


def yaml_load(text: str) -> dict[str, Any]:
    import yaml

    data: dict[str, Any] = yaml.safe_load(text)
    return data


def yaml_dump(data: dict[str, Any]) -> str:
    import yaml

    return str(yaml.safe_dump(data, sort_keys=False))
