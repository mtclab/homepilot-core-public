"""The ``guest-disk`` change: add a disk to a QEMU guest and take it into use (#824).

ONE reviewed change (owner decision 2026-11-18): attach a new storage-backed
disk to a running VM through the Proxmox API (hotplug), then - inside the
guest, as root, through the QEMU guest agent - make a filesystem on it, add one
fstab line by LABEL with ``nofail`` and mount it. The root part runs only
through the approved change, only on the blank disk that change just attached.

This module is the CONTENT of such a change: the spec, the checks a spec must
pass before it can even be proposed, and the body a reviewer reads. It talks to
nothing; the executor (``executor/guest_disk.py``) does the work.

How the new disk is known inside the guest: the attach sets ``serial=<serial>``
on it, and the guest sees that serial (``lsblk -o SERIAL``,
``/dev/disk/by-id/...<serial>``). A ``/dev/sdX`` letter is never used - the
order in which a kernel names disks is not stable, and the owner's own runbook
for this case located the disk by id for exactly that reason. The serial is
unique to the change, so a disk carrying it was attached by this change.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from ..common import format_validation_error

FENCE = "guest-disk-spec"

FILESYSTEMS = ("ext4", "xfs")
# mkfs refuses a longer label: ext4 stores 16 bytes, xfs 12.
LABEL_MAX = {"ext4": 16, "xfs": 12}
BUSES = ("scsi", "virtio")
# PVE's own slot ranges for the buses that can be hot-plugged.
BUS_SLOTS = {"scsi": 31, "virtio": 16}
MIN_SIZE_GIB = 1
MAX_SIZE_GIB = 65536
DEFAULT_OPTIONS = "defaults,nofail"

_SERIAL = re.compile(r"^[A-Za-z0-9_-]{4,20}$")
_LABEL = re.compile(r"^[A-Za-z0-9_-]+$")
_STORAGE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_OPTION = re.compile(r"^[a-z0-9_.=-]+$")
_MOUNT_POINT = re.compile(r"^/[A-Za-z0-9._/-]+$")
# A data disk is never mounted over the system: these and everything below them.
_SYSTEM_TREES = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/proc",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
)


class RawDiskError(ValueError):
    """The request names a physical disk; only root on the Proxmox host can attach one."""

    def __init__(self, device: str, message: str) -> None:
        self.device = device
        super().__init__(message)


def raw_disk_command(device: str, vmid: Any, slot: str | None, serial: str | None) -> str:
    """The exact command root runs on the node to attach a physical disk."""
    where = slot or "scsiN"
    opts = f",serial={serial}" if serial else ""
    return f"qm set {vmid} --{where} {device}{opts}"


def raw_disk_sentence(
    device: str, vmid: Any, node: Any, slot: str | None, serial: str | None
) -> str:
    command = raw_disk_command(device, vmid, slot, serial)
    where = "" if slot else " (scsiN: the first scsi slot `qm config` does not list)"
    return (
        f"{device} is a physical disk. Proxmox lets only root on the host attach a disk "
        "by its device path - an API token is refused ('only root can pass arbitrary "
        "filesystem paths') - so HomePilot cannot do this step. As root on node "
        f"{node}, run: {command}{where}. HomePilot never runs it for you."
    )


def serial_for(artifact_id: str) -> str:
    """The serial a change sets when its spec names none: stable per change."""
    return "hp" + hashlib.sha256(artifact_id.encode()).hexdigest()[:12]


class GuestDiskSpec(BaseModel):
    """What the operator asked for. Every field is checked before it is proposed."""

    model_config = {"extra": "forbid"}

    storage: str
    size_gib: int
    filesystem: Literal["ext4", "xfs"] = "ext4"
    mount_point: str
    label: str
    mount_options: str = DEFAULT_OPTIONS
    bus: Literal["scsi", "virtio"] = "scsi"
    # The storage sits on SSDs: the disk is attached with discard (and ssd=1 on
    # scsi) and mounted with `discard`, so freed blocks reach the device.
    ssd: bool = False
    # Set by the attach and what the guest step finds the disk by. Derived from
    # the change id when omitted (``serial_for``).
    serial: str | None = None

    @field_validator("storage")
    @classmethod
    def _storage(cls, v: str) -> str:
        v = str(v).strip()
        if v.startswith("/"):
            # ``parse_guest_disk_spec`` refuses this first, with the root command.
            raise ValueError(
                f"{v} is a physical disk: only root on the Proxmox host can attach one"
            )
        if not _STORAGE.match(v):
            raise ValueError("storage must be a Proxmox storage name (e.g. local-lvm)")
        return v

    @field_validator("size_gib")
    @classmethod
    def _size(cls, v: int) -> int:
        if not MIN_SIZE_GIB <= v <= MAX_SIZE_GIB:
            raise ValueError(f"size_gib must be from {MIN_SIZE_GIB} to {MAX_SIZE_GIB}")
        return v

    @field_validator("mount_point")
    @classmethod
    def _mount_point(cls, v: str) -> str:
        if not _MOUNT_POINT.match(v) or posixpath.normpath(v) != v or v == "/":
            raise ValueError(
                "mount_point must be an absolute, normalised path of letters, digits, "
                "'.', '_', '-' and '/' (e.g. /srv/data), and not /"
            )
        for tree in _SYSTEM_TREES:
            if v == tree or v.startswith(tree + "/"):
                raise ValueError(f"mount_point may not be in {tree}: a data disk never covers it")
        return v

    @field_validator("serial")
    @classmethod
    def _serial(cls, v: str | None) -> str | None:
        if v is not None and not _SERIAL.match(v):
            raise ValueError("serial must be 4-20 letters, digits, '_' or '-'")
        return v

    @field_validator("mount_options")
    @classmethod
    def _options(cls, v: str) -> str:
        parts = [p.strip() for p in str(v).split(",")]
        if not parts or any(not _OPTION.match(p) for p in parts):
            raise ValueError("mount_options must be a comma list such as defaults,nofail")
        if "nofail" not in parts:
            raise ValueError(
                "mount_options must include nofail: without it a missing disk stops the "
                "guest from booting"
            )
        return ",".join(parts)

    @model_validator(mode="after")
    def _label_fits(self) -> GuestDiskSpec:
        limit = LABEL_MAX[self.filesystem]
        if not _LABEL.match(self.label) or len(self.label) > limit:
            raise ValueError(
                f"label must be 1-{limit} letters, digits, '_' or '-' for {self.filesystem}"
            )
        return self

    def serial_or(self, artifact_id: str) -> str:
        return self.serial or serial_for(artifact_id)

    def fstab_line(self) -> str:
        # xfs is never checked by fsck at boot (fsck.xfs does nothing), ext4 is.
        passno = 0 if self.filesystem == "xfs" else 2
        fields = " ".join((self.mount_point, self.filesystem, self.mount_options))
        return f"LABEL={self.label} {fields} 0 {passno}"

    def disk_value(self, serial: str) -> str:
        """The Proxmox config value for the new disk: allocate on the storage."""
        value = f"{self.storage}:{self.size_gib},serial={serial}"
        if self.ssd:
            value += ",discard=on" + (",ssd=1" if self.bus == "scsi" else "")
        return value


def default_mount_options(ssd: bool) -> str:
    return DEFAULT_OPTIONS + (",discard" if ssd else "")


def _block(body: str) -> str:
    m = re.search(rf"```yaml\s+{FENCE}\s*\n(.*?)```", body, re.DOTALL)
    if not m:
        raise ValueError(f"missing ```yaml {FENCE}``` block")
    return m.group(1)


def parse_guest_disk_spec(body: str, target: dict[str, Any] | None = None) -> GuestDiskSpec:
    """Parse the ```yaml guest-disk-spec``` block; ValueError names what is wrong.

    A physical disk (a ``/dev/...`` path where the storage goes) raises
    ``RawDiskError`` carrying the root command that does it on the node.
    """
    import yaml

    try:
        parsed = yaml.safe_load(_block(body).strip())
    except yaml.YAMLError as exc:
        raise ValueError(f"guest-disk spec is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("guest-disk spec must be a YAML mapping")
    device = str(parsed.get("storage") or parsed.get("device") or "").strip()
    if device.startswith("/"):
        t = target or {}
        raise RawDiskError(
            device,
            raw_disk_sentence(
                device, t.get("vmid", "<vmid>"), t.get("node", "<node>"), None, parsed.get("serial")
            ),
        )
    try:
        return GuestDiskSpec.model_validate(parsed)
    except ValidationError as exc:
        raise ValueError(f"invalid guest-disk spec: {format_validation_error(exc)}") from None


def spec_yaml(spec: GuestDiskSpec) -> str:
    import yaml

    data = spec.model_dump(exclude_none=True)
    return str(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))


def render_guest_disk_body(spec: GuestDiskSpec, hostname: str, artifact_serial: str) -> str:
    """The body of a guest-disk change: what a reviewer reads, and the spec."""
    fs_cmd = f"mkfs.{spec.filesystem} -L {spec.label}"
    lines = [
        f"## Add a {spec.size_gib} GiB disk to {hostname} at {spec.mount_point}",
        "",
        f"1. Attach a new {spec.size_gib} GiB disk from storage `{spec.storage}` on the "
        f"first free {spec.bus} slot (read from the guest's current config when it runs), "
        f"hot-plugged, with serial `{artifact_serial}`.",
        f"2. In the guest, as root through the QEMU guest agent: find the disk by serial "
        f"`{artifact_serial}` - never by a /dev/sdX letter - and refuse unless it has no "
        "partitions and no filesystem signature (`wipefs -n`, `blkid -p`).",
        f"3. `{fs_cmd}` on it; create {spec.mount_point} (empty, made immutable so a "
        "missing disk cannot silently fill the root disk).",
        f"4. Back up /etc/fstab, append `{spec.fstab_line()}`, mount {spec.mount_point} "
        "alone (no `mount -a`) and check it with `findmnt`.",
        "",
        "Nothing is detached or formatted on a disk that was there before. If the attach "
        "works and the guest part fails, the change says so (partly applied) and leaves "
        "the disk attached for a retry.",
        "",
        "## Spec",
        "",
        f"```yaml {FENCE}",
        spec_yaml(spec) + "```",
        "",
    ]
    return "\n".join(lines)
