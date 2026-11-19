"""Adding a disk over MCP (#824): read the options, PROPOSE the change.

An assistant never attaches, formats or mounts anything. ``get_guest_disks``
reads what the guest's page reads (``GET /inventory/{host_id}/disks``);
``add_guest_disk`` proposes ONE ``guest-disk`` change (``POST
/inventory/{host_id}/disks``) that the owner approves - refused at once, with
the reason, when the apply would refuse it.
"""

from __future__ import annotations

from typing import Any

from homepilot.artifacts.guest_disk import MAX_SIZE_GIB, MIN_SIZE_GIB
from homepilot.inventory.disks import DiskRefusedError, DiskService

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_guest_disks",
        "description": (
            "What adding a disk to a Proxmox VM can use: the storages on its node that hold "
            "VM disks (with free space), the next free scsi/virtio slot from its current "
            "config, whether disk hotplug and the QEMU guest agent are on, and what would "
            "refuse a proposal now. Reads Proxmox; sends nothing to the guest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the VM"},
            },
            "required": ["host_id"],
        },
    },
    {
        "name": "add_guest_disk",
        "description": (
            "PROPOSE adding a disk to a running Proxmox VM and taking it into use, as ONE "
            "change the owner approves: attach a new disk from a storage (hot-plugged, with "
            "a serial), then inside the guest as root through the QEMU guest agent - only "
            "if that disk is blank - mkfs with a label, back up /etc/fstab, add one line "
            "by LABEL with nofail, mount it and verify. Nothing is attached or formatted "
            "by this call. Refused at once with the reason when the apply would refuse: "
            "no disk hotplug, guest agent not answering, storage missing or too small, "
            "mount point not empty, label or mount point already in fstab. A physical "
            "/dev/disk/by-id disk is refused with the root command to run on the node."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the VM"},
                "storage": {
                    "type": "string",
                    "description": "A Proxmox storage with 'images' content (get_guest_disks)",
                },
                "size_gib": {"type": "integer", "minimum": MIN_SIZE_GIB, "maximum": MAX_SIZE_GIB},
                "filesystem": {"type": "string", "enum": ["ext4", "xfs"], "default": "ext4"},
                "mount_point": {"type": "string", "description": "e.g. /srv/data"},
                "label": {
                    "type": "string",
                    "description": "Filesystem label (ext4 up to 16, xfs up to 12 chars)",
                },
                "mount_options": {
                    "type": "string",
                    "description": (
                        "Default defaults,nofail (plus discard when ssd); must keep nofail"
                    ),
                },
                "bus": {"type": "string", "enum": ["scsi", "virtio"], "default": "scsi"},
                "ssd": {
                    "type": "boolean",
                    "description": (
                        "The storage is on SSDs: attach with discard, mount with discard"
                    ),
                },
                "reason": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Why - shown to the owner in the proposal's intent",
                },
            },
            "required": ["host_id", "storage", "size_gib", "mount_point", "label"],
        },
    },
]


def _service(ctx: dict[str, Any]) -> DiskService:
    inventory = ctx.get("inventory_service")
    repo = ctx.get("repo") or getattr(inventory, "repo", None)
    if repo is None:
        raise RuntimeError("Repository not configured")
    return DiskService(repo, getattr(inventory, "proxmox", None) or ctx.get("proxmox"))


async def handle_get_guest_disks(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _service(ctx).describe(str(arguments["host_id"]))
    except DiskRefusedError as exc:
        raise ValueError(exc.message) from None


async def handle_add_guest_disk(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    inputs = {
        k: arguments.get(k)
        for k in (
            "storage",
            "size_gib",
            "filesystem",
            "mount_point",
            "label",
            "mount_options",
            "bus",
            "ssd",
        )
    }
    produced_by = {
        "session": "mcp",
        "agent": "add_guest_disk",
        "user": str(ctx.get("_mcp_caller_id") or "mcp"),
    }
    try:
        return await _service(ctx).propose(
            str(arguments["host_id"]),
            inputs,
            lifecycle=ctx["lifecycle"],
            produced_by=produced_by,
            reason=str(arguments.get("reason") or "").strip()[:120],
        )
    except DiskRefusedError as exc:
        raise ValueError(f"not proposed: {exc.message}") from None
