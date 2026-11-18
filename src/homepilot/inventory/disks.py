"""Add a disk to a guest: what can be asked for, and the proposal (#824).

Two things, both READS until the operator approves:

* ``describe`` - for the guest's page: the storages a VM disk can go on, the
  next free slot (from the guest's current config), whether disk hotplug and the
  guest agent are on, and what would refuse a proposal now;
* ``propose`` - turn the operator's (or an assistant's) inputs into ONE
  ``guest-disk`` change and propose it. Everything the apply would refuse on is
  checked first, the inside of the guest included, so a proposal that could
  never run is refused at once with the reason. Nothing is attached, formatted
  or mounted here: the change runs only when a person approves it.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any

from homepilot.adapters import pve_reads
from homepilot.artifacts.guest_disk import (
    DEFAULT_OPTIONS,
    FILESYSTEMS,
    LABEL_MAX,
    MAX_SIZE_GIB,
    MIN_SIZE_GIB,
    GuestDiskSpec,
    default_mount_options,
    raw_disk_sentence,
    render_guest_disk_body,
)
from homepilot.common import format_validation_error
from homepilot.executor import guest_disk


class DiskRefusedError(Exception):
    """Not proposed; ``code`` is stable, the message is the sentence an operator reads."""

    def __init__(self, code: str, message: str, *, status: int = 409, **details: Any) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        super().__init__(message)

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30].strip("-") or "guest"


class DiskService:
    def __init__(self, repo: Any, proxmox: Any) -> None:
        self.repo = repo
        self.proxmox = proxmox

    async def _guest(self, host_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = await self.repo.get_host(host_id)
        if row is None:
            raise DiskRefusedError("not_found", f"no host {host_id!r} in inventory", status=404)
        host = dict(row)
        name = host.get("hostname") or "this host"
        if (
            host.get("host_type") != "qemu"
            or host.get("proxmox_id") is None
            or not host.get("node")
        ):
            raise DiskRefusedError(
                "not_a_vm",
                f"{name} is not a Proxmox VM with a known node and VMID. Adding a disk is for "
                "VMs: the filesystem is made inside the guest through the QEMU guest agent, "
                "which a container does not have",
            )
        if self.proxmox is None:
            raise DiskRefusedError(
                "no_proxmox", "no Proxmox is configured, so no disk can be added", status=503
            )
        target = {
            "kind": "vm",
            "host": str(host.get("hostname") or ""),
            "vmid": int(host["proxmox_id"]),
            "node": str(host["node"]),
        }
        return host, target

    async def describe(self, host_id: str) -> dict[str, Any]:
        """What the "Add a disk" form needs. A read: nothing is sent to the guest."""
        host, target = await self._guest(host_id)
        node, vmid = target["node"], target["vmid"]
        config = await self.proxmox.get_vm_config(node, vmid, "qemu", current=True)
        pending = guest_disk.pending_rows(await self.proxmox.get_guest_pending(node, vmid, "qemu"))
        status = await self.proxmox.get_guest_status(node, vmid, "qemu")
        storages = guest_disk.images_storages(
            guest_disk.storage_rows(await pve_reads.node_storage(self.proxmox, node))
        )
        running = status.get("status") == "running"
        hotplug = guest_disk.hotplug_has_disk(config)
        agent_on = guest_disk.agent_enabled(config)
        agent_ok = bool(running and agent_on and await self.proxmox.agent_ping(node, vmid))
        blockers: list[str] = []
        if not running:
            blockers.append(f"the guest is {status.get('status') or 'not running'}; start it first")
        if not hotplug:
            blockers.append(
                "disk hotplug is off for this VM (Options > Hotplug > Disk): a disk added now "
                "would only appear after a restart"
            )
        if not agent_on:
            blockers.append("the QEMU guest agent is not enabled for this VM")
        elif running and not agent_ok:
            blockers.append("the QEMU guest agent does not answer inside the guest")
        if not storages:
            blockers.append(f"node {node} has no storage that holds VM disks")
        return {
            "host_id": host["id"],
            "hostname": target["host"],
            "vmid": vmid,
            "node": node,
            "status": status.get("status") or "unknown",
            "hotplug_disk": hotplug,
            "agent_enabled": agent_on,
            "agent_answers": agent_ok,
            "next_slot": {
                bus: guest_disk.free_slot(bus, config, pending) for bus in ("scsi", "virtio")
            },
            "storages": storages,
            "filesystems": list(FILESYSTEMS),
            "label_max": LABEL_MAX,
            "size_gib": {"min": MIN_SIZE_GIB, "max": MAX_SIZE_GIB},
            "default_mount_options": DEFAULT_OPTIONS,
            "ssd_mount_options": default_mount_options(True),
            "blockers": blockers,
        }

    async def propose(
        self,
        host_id: str,
        inputs: dict[str, Any],
        *,
        lifecycle: Any,
        produced_by: dict[str, Any],
        reason: str = "",
    ) -> dict[str, Any]:
        """Propose ONE guest-disk change, or refuse with the reason; never applies it."""
        _host, target = await self._guest(host_id)
        serial = "hp" + secrets.token_hex(6)
        device = str(inputs.get("device") or inputs.get("storage") or "").strip()
        if device.startswith("/"):
            config = await self.proxmox.get_vm_config(
                target["node"], target["vmid"], "qemu", current=True
            )
            pending = guest_disk.pending_rows(
                await self.proxmox.get_guest_pending(target["node"], target["vmid"], "qemu")
            )
            slot = guest_disk.free_slot("scsi", config, pending)
            raise DiskRefusedError(
                "raw_disk",
                raw_disk_sentence(device, target["vmid"], target["node"], slot, serial),
                status=422,
            )
        ssd = bool(inputs.get("ssd"))
        fields = {
            "storage": device,
            "size_gib": inputs.get("size_gib"),
            "filesystem": inputs.get("filesystem") or "ext4",
            "mount_point": inputs.get("mount_point"),
            "label": inputs.get("label"),
            "mount_options": inputs.get("mount_options") or default_mount_options(ssd),
            "bus": inputs.get("bus") or "scsi",
            "ssd": ssd,
            "serial": serial,
        }
        from pydantic import ValidationError

        try:
            spec = GuestDiskSpec.model_validate(fields)
        except ValidationError as exc:
            raise DiskRefusedError("invalid", format_validation_error(exc), status=422) from None
        s = await guest_disk.survey(self.proxmox, target, spec, serial)
        if s.blockers:
            raise DiskRefusedError("refused", "; ".join(s.blockers), blockers=s.blockers)
        hostname = target["host"]
        intent = f"Add a {spec.size_gib} GiB disk to {hostname} at {spec.mount_point}"
        if reason:
            intent += f": {reason}"
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        artifact = {
            "id": f"{stamp}-add-disk-{_slug(hostname)}-{secrets.token_hex(3)}",
            "kind": "guest-disk",
            "intent": intent[:200],
            "target": target,
            "idempotence": "via-precheck",
            "requires_snapshot": False,
            "produced_by": produced_by,
            "body": render_guest_disk_body(spec, hostname, serial),
        }
        from homepilot.artifacts.models import LifecycleError

        try:
            artifact_id = await lifecycle.propose(artifact)
        except LifecycleError as exc:
            raise DiskRefusedError("invalid", str(exc), status=400) from None
        return {
            "id": artifact_id,
            "status": "proposed",
            "kind": "guest-disk",
            "intent": artifact["intent"],
            "slot": s.slot,
            "serial": serial,
            "attached": False,
            "message": (
                f"Proposed adding {spec.size_gib} GiB from {spec.storage} to {hostname} as "
                f"{s.slot}; nothing was attached or formatted. It runs when the owner approves "
                "it, and the apply checks everything again first."
            ),
        }
