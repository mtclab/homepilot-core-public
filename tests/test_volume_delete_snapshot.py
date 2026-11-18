"""A change that deletes a disk volume is never defeated by its own snapshot (3.14 walk).

On the 3.14.0 candidate a proxmox-api-sequence removed a VM's unused disk with
``PUT .../config delete=unused0``. The executor's automatic pre-apply snapshot
(taken while the volume was still the VM's) held the volume, Proxmox refused
the delete "volume is still in use (snapshot?)" and the change was left partly
applied.

The fake cluster here models what Proxmox does: a snapshot references every
volume in the config at the time, and freeing a volume a snapshot references is
refused with exactly those words. Journeys, through the real lifecycle and
executor:

* propose refuses the change while a snapshot would be taken, naming
  ``requires_snapshot: false`` - nothing is proposed;
* with ``requires_snapshot: false`` it proposes, applies and the volume is gone,
  with no snapshot taken;
* a change proposed BEFORE propose checked this is refused at apply, before the
  snapshot - no snapshot, no write, the volume still there;
* a delete Proxmox refuses because some OTHER snapshot holds the volume fails
  with the cause and the remedy next to Proxmox's words.

Teeth: drop the ``snapshot_conflict`` call in the validator -> the propose test
fails; drop it in ``_maybe_snapshot`` -> the apply test sees a snapshot and a
partly applied change; drop ``in_use_hint`` -> the last test's remedy is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homepilot.adapters.proxmox import ProxmoxError
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import ArtifactStatus, LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.principal import Principal
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor import volume_deletes
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.inventory.service import InventoryService
from tests.test_power_controls import NODE, FakeCluster

VMID = 101
UNUSED = f"fast-pool:vm-{VMID}-disk-1"


class SnapshotCluster(FakeCluster):
    """Snapshots that hold volumes, and config deletes that free them."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshots: dict[str, set[str]] = {}
        self.freed: list[str] = []

    @staticmethod
    def _volumes(config: dict[str, Any]) -> set[str]:
        return {
            str(v).split(",", 1)[0]
            for k, v in config.items()
            if k.startswith(("scsi", "virtio", "sata", "ide", "unused", "efidisk"))
        }

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = "/" + path.lstrip("/")
        parts = path.strip("/").split("/")
        if method == "POST" and len(parts) == 5 and parts[4] == "snapshot":
            self.posts.append((path, body))
            g = self.guests[int(parts[3])]
            self.snapshots[str((body or {})["snapname"])] = self._volumes(g["config"])
            return {"data": self._task(NODE, "OK")}
        if method == "PUT" and len(parts) == 5 and parts[4] == "config":
            self.posts.append((path, body))
            g = self.guests[int(parts[3])]
            for key in str((body or {}).get("delete") or "").split(","):
                key = key.strip()
                if not key.startswith("unused") or key not in g["config"]:
                    continue
                volume = str(g["config"][key]).split(",", 1)[0]
                if any(volume in held for held in self.snapshots.values()):
                    # Proxmox's own words (PVE::QemuServer vmconfig_delete_or_detach_drive).
                    raise ProxmoxError(
                        "PUT", path, 500, f"volume '{volume}' is still in use (snapshot?)"
                    )
                del g["config"][key]
                self.freed.append(volume)
            return {"data": None}
        return await super().call(method, path, body, query)


def _spec(artifact_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "kind": "proxmox-api-sequence",
        "intent": "remove the unused data disk",
        "body": (
            "# Remove the unused disk\n\n```yaml proxmox-api-spec\nsteps:\n"
            "  - id: drop-unused\n    method: PUT\n"
            "    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/config\n"
            "    body:\n      delete: unused0\n```\n"
        ),
        "target": {"kind": "vm", "vmid": VMID, "node": NODE, "hostname": "app-01"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s", "agent": "a", "user": "u"},
        **extra,
    }


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = SnapshotCluster()
    pve.add(
        VMID,
        "app-01",
        "running",
        {
            "scsi0": f"fast-pool:vm-{VMID}-disk-0,size=32G",
            "unused0": UNUSED,
            "agent": "1",
        },
    )
    assert (await InventoryService(repo, pve).refresh_inventory())["complete"]
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store, repository=repo)
    lifecycle._proxmox = pve
    executor = ArtifactExecutor(
        store=store, lifecycle=lifecycle, repo=repo, proxmox=pve, vault=None
    )
    try:
        yield {"pve": pve, "lifecycle": lifecycle, "executor": executor, "store": store}
    finally:
        await db.close()


def _snapshot_posts(pve: SnapshotCluster) -> list[str]:
    return [p for p, _ in pve.posts if p.endswith("/snapshot")]


async def _apply(w: dict[str, Any], artifact_id: str) -> Any:
    await w["lifecycle"].approve(artifact_id, Principal.operator("operator"))
    return await w["executor"].apply(artifact_id, approved_by="operator")


async def test_propose_refuses_a_delete_its_own_snapshot_would_block(world) -> None:
    w = world
    with pytest.raises(LifecycleError) as refused:
        await w["lifecycle"].propose(_spec("2026-11-18-drop-unused-a1"))
    words = str(refused.value)
    assert "requires_snapshot: false" in words
    assert "step drop-unused deletes the volume of unused0" in words
    assert "still in use (snapshot?)" in words
    assert not w["store"].exists("2026-11-18-drop-unused-a1")
    assert w["pve"].posts == []


async def test_without_a_snapshot_the_delete_is_applied(world) -> None:
    w = world
    aid = await w["lifecycle"].propose(_spec("2026-11-18-drop-unused-b2", requires_snapshot=False))
    result = await _apply(w, aid)
    assert result.success, result.failure_reason
    assert w["pve"].freed == [UNUSED]
    assert "unused0" not in w["pve"].guests[VMID]["config"]
    assert _snapshot_posts(w["pve"]) == []
    fm, _ = w["store"].read(aid)
    assert fm["status"] == ArtifactStatus.APPLIED.value


async def test_a_change_proposed_before_the_check_is_refused_before_its_snapshot(
    world, monkeypatch
) -> None:
    w = world
    # As 3.14.0 proposed it: no check at propose.
    with monkeypatch.context() as m:
        m.setattr(volume_deletes, "snapshot_conflict", lambda *_a, **_k: None)
        aid = await w["lifecycle"].propose(_spec("2026-11-18-drop-unused-c3"))
    result = await _apply(w, aid)
    assert not result.success
    reason = result.failure_reason or result.execution_log
    assert "nothing was changed" in reason and "requires_snapshot: false" in reason, reason
    assert _snapshot_posts(w["pve"]) == [], "the self-defeating snapshot was taken"
    assert w["pve"].guests[VMID]["config"]["unused0"] == UNUSED
    assert [p for p, _ in w["pve"].posts if p.endswith("/config")] == []
    fm, _ = w["store"].read(aid)
    assert not fm.get("partial_apply"), "nothing ran, so nothing is partly applied"


async def test_a_volume_another_snapshot_holds_fails_with_the_cause_and_remedy(world) -> None:
    w = world
    # An operator's own older snapshot holds the volume.
    w["pve"].snapshots["before-upgrade"] = {UNUSED}
    aid = await w["lifecycle"].propose(_spec("2026-11-18-drop-unused-d4", requires_snapshot=False))
    result = await _apply(w, aid)
    assert not result.success
    reason = result.failure_reason or ""
    assert "is still in use (snapshot?)" in reason, reason
    assert "a snapshot still references" in reason
    assert "delete that snapshot in Proxmox" in reason
    assert w["pve"].guests[VMID]["config"]["unused0"] == UNUSED


def test_what_counts_as_deleting_a_volume() -> None:
    target = {"kind": "vm", "vmid": VMID, "node": NODE}

    def found(method: str, path: str, body: dict[str, Any] | None = None) -> list[str]:
        lines = [f"  - id: s\n    method: {method}\n    path: {path}\n"]
        if body:
            lines.append("    body:\n" + "".join(f"      {k}: {v}\n" for k, v in body.items()))
        spec = "```yaml proxmox-api-spec\nsteps:\n" + "".join(lines) + "```\n"
        return volume_deletes.volume_deletes(spec, target, {})

    cfg = f"/nodes/{NODE}/qemu/{VMID}/config"
    assert found("PUT", cfg, {"delete": "unused0"})
    assert found("POST", cfg, {"delete": "scsi1", "force": 1})
    assert not found("PUT", cfg, {"delete": "scsi1"}), "a plain detach frees nothing"
    assert not found("PUT", cfg, {"delete": "hostpci0"})
    assert not found("PUT", f"/nodes/{NODE}/qemu/202/config", {"delete": "unused0"})
    assert found("DELETE", f"/nodes/{NODE}/qemu/{VMID}?purge=1")
    assert found("DELETE", f"/nodes/{NODE}/storage/fast-pool/content/fast-pool:vm-{VMID}-disk-3")
    assert not found("DELETE", f"/nodes/{NODE}/storage/local/content/local:iso/x.iso")
    assert found("PUT", f"/nodes/{NODE}/qemu/{VMID}/unlink", {"idlist": "unused0", "force": 1})
    assert not found("DELETE", f"/nodes/{NODE}/qemu/{VMID}/snapshot/old")
    assert not found("GET", cfg)
