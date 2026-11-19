"""Plan, apply and verify a ``guest-disk`` change (#824).

Apply = two steps, each refusing before it changes anything:

1. **attach** - through the Proxmox API (the write token), a new disk
   ``<storage>:<size>,serial=<serial>`` on the first free slot of the bus, read
   from the guest's CURRENT config (``current=1``) and its pending changes - not
   from ``/cluster/resources``, which lags (#813). Only on a running VM whose
   ``hotplug`` option includes ``disk``: without it Proxmox would only queue the
   disk for the next restart, so the change is refused before anything is sent.
2. **in the guest** - the script in ``guest_disk_script`` as root through the
   QEMU guest agent (``agent/exec``, the path the zero-touch install already
   uses). Not the hp-agent: its tier is unprivileged by design, and widening its
   grants would give every caller of the hub a standing root path. The guest
   agent runs only what an approved change sends.

Everything the apply would refuse on is checked first, the guest's side
included (a read-only ``preflight`` run of the same script), so a refused change
has changed nothing. If the attach lands and the guest part then fails, the run
is "partly applied" (#796) - naming what was done, what was not, and what the
operator can do - and the disk stays attached: HomePilot never detaches a disk.

Re-applying is safe: the disk is found by its serial (attached already = the
attach is skipped), and the guest part skips every step whose result is there.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from homepilot.adapters import pve_reads
from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError, ProxmoxTaskStillRunningError
from homepilot.artifacts.guest_disk import (
    BUS_SLOTS,
    GuestDiskSpec,
    RawDiskError,
    parse_guest_disk_spec,
)

from .guest_disk_script import GUEST_STEPS, RC_OK, RC_REFUSED, guest_script, parse_output
from .proxmox_api import describe_partial

logger = logging.getLogger(__name__)

_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide|efidisk|tpmstate)(\d+)$")
GIB = 1024**3
ATTACH_TIMEOUT_S = 600.0
PREFLIGHT_TIMEOUT_S = 60.0
APPLY_TIMEOUT_S = 900.0
_STEP_WORDS = dict(GUEST_STEPS)


def hotplug_has_disk(config: dict[str, Any]) -> bool:
    """PVE's ``hotplug``: absent = ``network,disk,usb``; ``0`` none; ``1`` the default set."""
    raw = config.get("hotplug")
    if raw is None or str(raw).strip() == "":
        return True
    value = str(raw).strip()
    if value == "0":
        return False
    if value == "1":
        return True
    return "disk" in [p.strip() for p in value.split(",")]


def agent_enabled(config: dict[str, Any]) -> bool:
    """``agent: 1`` or ``agent: enabled=1,...``."""
    raw = str(config.get("agent") or "0").strip()
    first = raw.split(",", 1)[0]
    if "=" in first:
        opts = dict(p.split("=", 1) for p in raw.split(",") if "=" in p)
        return opts.get("enabled", "0") == "1"
    return first == "1"


def serial_of(value: Any) -> str | None:
    for part in str(value or "").split(",")[1:]:
        key, _, val = part.partition("=")
        if key.strip() == "serial":
            return val.strip()
    return None


def pending_rows(rows: Any) -> dict[str, Any]:
    """Keys PVE holds as PENDING (added or changed for the next start)."""
    out: dict[str, Any] = {}
    for row in rows or []:
        if isinstance(row, dict) and "pending" in row:
            out[str(row.get("key"))] = row["pending"]
    return out


def free_slot(bus: str, config: dict[str, Any], pending: dict[str, Any]) -> str | None:
    used = {k for k in [*config, *pending] if _DISK_KEY.match(k)}
    for i in range(BUS_SLOTS[bus]):
        if f"{bus}{i}" not in used:
            return f"{bus}{i}"
    return None


def storage_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    return [r for r in data or [] if isinstance(r, dict)] if isinstance(data, list) else []


def images_storages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The storages on a node a VM disk can be allocated on (``images`` content)."""
    out = []
    for r in rows:
        content = [c.strip() for c in str(r.get("content") or "").split(",")]
        if "images" in content and r.get("enabled", 1) not in (0, "0"):
            out.append(
                {
                    "storage": str(r.get("storage")),
                    "type": r.get("type"),
                    "active": r.get("active", 1) not in (0, "0"),
                    "shared": bool(r.get("shared")),
                    "avail": r.get("avail"),
                    "total": r.get("total"),
                }
            )
    return sorted(out, key=lambda s: s["storage"])


@dataclass
class Survey:
    """What is true NOW, read without changing anything."""

    node: str
    vmid: int
    serial: str
    status: str = "unknown"
    lock: str | None = None
    hotplug_disk: bool = False
    agent_on: bool = False
    agent_ok: bool = False
    attached_slot: str | None = None
    pending_slot: str | None = None
    slot: str | None = None
    storage: dict[str, Any] | None = None
    guest_device: str | None = None
    guest_ran: bool = False
    blockers: list[str] = field(default_factory=list)

    @property
    def attached(self) -> bool:
        return self.attached_slot is not None


async def survey(
    proxmox: Any,
    target: dict[str, Any],
    spec: GuestDiskSpec,
    serial: str,
    *,
    guest_check: bool = True,
) -> Survey:
    """Read the guest, its storage and (read-only) its inside; list every blocker."""
    node = str(target.get("node") or "")
    vmid = int(target["vmid"])
    s = Survey(node=node, vmid=vmid, serial=serial)
    if proxmox is None:
        s.blockers.append("no Proxmox is configured")
        return s
    config = await proxmox.get_vm_config(node, vmid, "qemu", current=True)
    pending = pending_rows(await proxmox.get_guest_pending(node, vmid, "qemu"))
    status = await proxmox.get_guest_status(node, vmid, "qemu")
    s.status = str(status.get("status") or "unknown")
    s.lock = status.get("lock") or config.get("lock")
    s.hotplug_disk = hotplug_has_disk(config)
    s.agent_on = agent_enabled(config)
    for key, value in config.items():
        if _DISK_KEY.match(key) and serial_of(value) == serial:
            s.attached_slot = key
    for key, value in pending.items():
        if _DISK_KEY.match(key) and serial_of(value) == serial and key != s.attached_slot:
            s.pending_slot = key
    s.slot = s.attached_slot or free_slot(spec.bus, config, pending)
    storages = images_storages(storage_rows(await pve_reads.node_storage(proxmox, node)))
    s.storage = next((r for r in storages if r["storage"] == spec.storage), None)

    b = s.blockers
    if s.status != "running":
        b.append(
            f"the guest is {s.status}: a disk is hot-plugged into a running VM and the "
            "in-guest part needs it running - start it first"
        )
    if s.lock:
        b.append(f"the guest is locked ({s.lock}); wait for that operation to finish")
    if not s.hotplug_disk:
        b.append(
            f"VMID {vmid} does not have disk hotplug enabled (its hotplug option is "
            f"{config.get('hotplug')!r}): Proxmox would only add the disk at the next "
            "restart. Enable Disk under Options > Hotplug, then propose again"
        )
    if s.pending_slot:
        b.append(
            f"a disk with serial {serial} is waiting in the pending config as "
            f"{s.pending_slot} (it was not hot-plugged); restart the guest or remove that "
            "pending disk in Proxmox before retrying"
        )
    if not s.attached:
        if s.slot is None:
            b.append(f"the guest has no free {spec.bus} slot")
        if s.storage is None:
            b.append(
                f"storage {spec.storage!r} is not available on node {node} for VM disks "
                "(it needs the 'images' content type)"
            )
        elif not s.storage["active"]:
            b.append(f"storage {spec.storage!r} is not active on node {node}")
        elif s.storage.get("avail") is not None and int(s.storage["avail"]) < spec.size_gib * GIB:
            free = int(s.storage["avail"]) / GIB
            b.append(
                f"storage {spec.storage!r} has {free:.1f} GiB free, less than the "
                f"{spec.size_gib} GiB asked for"
            )
    if not s.agent_on:
        b.append(
            "the QEMU guest agent is not enabled for this VM (Options > QEMU Guest Agent); "
            "the in-guest part runs through it"
        )
    elif s.status == "running":
        s.agent_ok = await proxmox.agent_ping(node, vmid)
        if not s.agent_ok:
            b.append("the QEMU guest agent does not answer inside the guest")
    if guest_check and s.agent_ok:
        rc, out, err = await _run(proxmox, s, spec, "preflight", PREFLIGHT_TIMEOUT_S)
        steps, facts = parse_output(out)
        s.guest_ran = True
        s.guest_device = facts.get("device") if facts.get("device") not in (None, "none") else None
        if rc != RC_OK:
            failed = [w for (_, st, w) in steps if st == "failed"]
            b.append(
                "inside the guest: "
                + (failed[0] if failed else f"the check exited {rc}: {(err or out).strip()[:300]}")
            )
    return s


async def _run(
    proxmox: Any, s: Survey, spec: GuestDiskSpec, mode: str, timeout_s: float
) -> tuple[int, str, str]:
    rc, out, err = await proxmox.agent_run(
        s.node, s.vmid, guest_script(spec, s.serial, mode), timeout_s=timeout_s
    )
    return int(rc), str(out or ""), str(err or "")


def _spec(fm: dict[str, Any], body: str) -> tuple[GuestDiskSpec, str]:
    spec = parse_guest_disk_spec(body, fm.get("target") or {})
    return spec, spec.serial_or(str(fm.get("id") or ""))


def _attach_words(spec: GuestDiskSpec, slot: str | None) -> str:
    return f"attach {slot or spec.bus + '?'} ({spec.storage}, {spec.size_gib} GiB)"


def _remedy(s: Survey, spec: GuestDiskSpec, slot: str) -> str:
    return (
        f"The disk stays attached as {slot} (serial {s.serial}); HomePilot never detaches "
        "a disk by itself. To finish: fix what the guest reported, then approve this change "
        "again and apply it - the disk is found by its serial and not attached twice, and "
        f"every in-guest step already done is skipped. To take the disk off instead: in "
        f"Proxmox, the VM's Hardware > {slot} > Detach, then remove the unused disk "
        f"(on the node: qm set {s.vmid} --delete {slot}); if the fstab line was added, "
        f"restore /etc/fstab.homepilot-{s.serial}.bak in the guest first."
    )


async def execute(
    fm: dict[str, Any], body: str, target: dict[str, Any], proxmox: Any
) -> dict[str, Any]:
    log: list[str] = []
    try:
        spec, serial = _spec(fm, body)
    except (RawDiskError, ValueError) as exc:
        return {
            "success": False,
            "execution_log": f"[guest-disk] REFUSED: {exc}",
            "failure_reason": str(exc),
        }
    try:
        s = await survey(proxmox, target, spec, serial)
    except (ProxmoxError, TimeoutError, RuntimeError) as exc:
        reason = f"could not read the guest before changing anything ({exc}); nothing was changed"
        return {
            "success": False,
            "execution_log": f"[guest-disk] {reason}",
            "failure_reason": reason,
        }
    if s.blockers:
        reason = "refused, nothing was changed: " + "; ".join(s.blockers)
        return {
            "success": False,
            "execution_log": f"[guest-disk] {reason}",
            "failure_reason": reason,
        }

    guest_ids = [sid for sid, _ in GUEST_STEPS]
    slot = str(s.slot)
    attach_step = _attach_words(spec, slot)
    if s.attached:
        log.append(f"[guest-disk] {slot} already carries serial {serial}: attach skipped")
        # Still part of what this change has done to the guest: a failure below
        # leaves a disk attached, whichever run attached it.
        attach_done = [f"{attach_step} (attached by an earlier run)"]
    else:
        try:
            result = await proxmox.set_vm_config(s.node, s.vmid, {slot: spec.disk_value(serial)})
            upid = ProxmoxClient.upid_of(result)
            if upid:
                await proxmox.wait_for_task(s.node, upid, timeout_s=ATTACH_TIMEOUT_S)
        except ProxmoxTaskStillRunningError as exc:
            return {
                "success": False,
                "execution_log": "\n".join([*log, f"[guest-disk] {exc}"]),
                "failure_reason": f"outcome unknown: {exc}",
                "unsettled_task": {
                    "node": s.node,
                    "upid": exc.upid,
                    "step": attach_step,
                    "steps_not_run": [_STEP_WORDS[g] for g in guest_ids],
                },
            }
        except ProxmoxError as exc:
            after = await _attached_slot(proxmox, s, serial)
            if after is None:
                reason = f"the attach was refused by Proxmox: {exc}; nothing was attached"
                return {
                    "success": False,
                    "execution_log": "\n".join([*log, f"[guest-disk] {reason}"]),
                    "failure_reason": reason,
                }
            log.append(f"[guest-disk] the attach reported {exc}, but {after} carries the disk")
            slot = after
        now = await _attached_slot(proxmox, s, serial)
        if now is None:
            partial = {
                "steps_done": [f"{attach_step}: queued, not hot-plugged"],
                "steps_failed": ["hot-plug"],
                "steps_not_run": [_STEP_WORDS[g] for g in guest_ids],
                "error": "Proxmox took the disk into the PENDING config instead of "
                "hot-plugging it; the guest cannot see it until it restarts",
            }
            partial["remedy"] = _remedy(s, spec, slot)
            return _partial(log, partial)
        slot = now
        attach_done = [_attach_words(spec, slot)]
        log.append(f"[guest-disk] attached {slot}: {spec.disk_value(serial)}")

    try:
        rc, out, err = await _run(proxmox, s, spec, "apply", APPLY_TIMEOUT_S)
    except (ProxmoxError, TimeoutError, RuntimeError) as exc:
        partial = {
            "steps_done": attach_done,
            "steps_failed": ["in-guest part"],
            "steps_not_run": [],
            "error": f"the guest agent did not complete the in-guest part: {exc}",
            "remedy": _remedy(s, spec, slot),
        }
        return _partial(log, partial)
    steps, _ = parse_output(out)
    for sid, status, words in steps:
        log.append(f"[guest] {sid}: {status} - {words}")
    if err.strip():
        log.append(f"[guest] stderr: {err.strip()[:2000]}")
    if rc == RC_OK and steps and steps[-1][:2] == ("verify", "done"):
        log.append(f"[guest-disk] {spec.mount_point} is in use from serial {serial}")
        return {"success": True, "execution_log": "\n".join(log)}

    done = [f"{_STEP_WORDS.get(i, i)}" for i, st, _ in steps if st == "done"]
    failed = [(i, w) for i, st, w in steps if st == "failed"]
    reported = {i for i, _, _ in steps}
    partial = {
        "steps_done": attach_done + done,
        "steps_failed": [_STEP_WORDS.get(failed[0][0], failed[0][0])]
        if failed
        else ["in-guest part"],
        "steps_not_run": [_STEP_WORDS[g] for g in guest_ids if g not in reported],
        "error": (failed[0][1] if failed else f"the in-guest part exited {rc}: {err.strip()[:300]}")
        + (" (refused: that step changed nothing)" if rc == RC_REFUSED else ""),
        "remedy": _remedy(s, spec, slot),
    }
    return _partial(log, partial)


def _partial(log: list[str], partial: dict[str, Any]) -> dict[str, Any]:
    """A run that left the disk attached and the guest part unfinished (#796)."""
    sentence = f"{describe_partial(partial)}. {partial['remedy']}"
    log.append(f"[guest-disk] {sentence}")
    return {
        "success": False,
        "execution_log": "\n".join(log),
        "failure_reason": sentence,
        "partial_apply": partial,
    }


async def _attached_slot(proxmox: Any, s: Survey, serial: str) -> str | None:
    """The slot whose CURRENT config carries the serial (hot-plugged), or None."""
    config = await proxmox.get_vm_config(s.node, s.vmid, "qemu", current=True)
    for key, value in config.items():
        if _DISK_KEY.match(key) and serial_of(value) == serial:
            return str(key)
    return None


# ── plan and drift ───────────────────────────────────────────────────────────


async def plan(proxmox: Any, fm: dict[str, Any], body: str) -> dict[str, Any]:
    """What applying would do, step by step, read without changing anything."""
    spec, serial = _spec(fm, body)
    target = fm.get("target") or {}
    s = await survey(proxmox, target, spec, serial)
    mounted = False
    if s.attached and s.agent_ok:
        facts = await _facts(proxmox, s, spec)
        mounted = _in_spec(facts, spec)
    line = spec.fstab_line()
    items = [
        _item(
            "disk",
            s.slot or "-",
            f"attached, serial {serial}" if s.attached else "no disk with this serial",
            f"{spec.storage}: {spec.size_gib} GiB, serial {serial}, hot-plugged",
            not s.attached,
        ),
        _item(
            "filesystem",
            f"serial {serial}",
            "in use" if mounted else "-",
            f"{spec.filesystem}, label {spec.label}",
            not mounted,
        ),
        _item("fstab", "/etc/fstab", "has the line" if mounted else "no line", line, not mounted),
        _item(
            "mount",
            spec.mount_point,
            "mounted" if mounted else "not mounted",
            f"LABEL={spec.label} ({spec.mount_options})",
            not mounted,
        ),
    ]
    changes = [i for i in items if i["changes"]]
    disk = {
        "slot": s.slot,
        "bus": spec.bus,
        "storage": spec.storage,
        "size_gib": spec.size_gib,
        "filesystem": spec.filesystem,
        "label": spec.label,
        "mount_point": spec.mount_point,
        "mount_options": spec.mount_options,
        "serial": serial,
        "fstab_line": line,
        "hotplug_disk": s.hotplug_disk,
        "agent": s.agent_ok,
        "guest_steps": [words for _, words in GUEST_STEPS],
        "blockers": s.blockers,
    }
    if s.blockers:
        summary = "Applying now would be refused, changing nothing: " + "; ".join(s.blockers)
    elif not changes:
        summary = (
            f"{spec.mount_point} is already in use from this disk; applying would change nothing"
        )
    else:
        summary = (
            f"Attach {spec.size_gib} GiB from {spec.storage} as {s.slot} (serial {serial}), "
            f"then in the guest: {spec.filesystem} label {spec.label}, fstab line, "
            f"mount at {spec.mount_point}"
        )
    return {"items": items, "changes": changes, "summary": summary, "disk": disk}


def _item(kind: str, name: str, observed: str, desired: str, changes: bool) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": f"{kind}:{name}",
        "name": name,
        "observed": observed,
        "desired": desired,
        "changes": changes,
        "log": "",
    }


async def _facts(proxmox: Any, s: Survey, spec: GuestDiskSpec) -> dict[str, str]:
    rc, out, err = await _run(proxmox, s, spec, "verify", PREFLIGHT_TIMEOUT_S)
    if rc != RC_OK:
        raise RuntimeError(f"the in-guest check exited {rc}: {(err or out).strip()[:300]}")
    return parse_output(out)[1]


def _in_spec(facts: dict[str, str], spec: GuestDiskSpec) -> bool:
    dev = facts.get("device")
    return bool(
        dev
        and dev != "none"
        and facts.get("mounted") == dev
        and facts.get("fstype") == spec.filesystem
        and facts.get("label") == spec.label
        and facts.get("fstab") == "yes"
    )


async def drift(proxmox: Any, fm: dict[str, Any], body: str) -> tuple[str, str, dict[str, Any]]:
    """``(state, words, details)``: ``in_spec``, ``drifted`` or ``unknown``.

    In spec = the disk with this change's serial is attached (current config),
    the guest sees it, it is mounted at the mount point carrying the label and
    type, and fstab has the line. Not looked at = unknown, never green.
    """
    spec, serial = _spec(fm, body)
    target = fm.get("target") or {}
    s = await survey(proxmox, target, spec, serial, guest_check=False)
    if not s.attached:
        return "drifted", f"no disk with serial {serial} is attached to the guest", {}
    if not s.agent_ok:
        return (
            "unknown",
            f"{s.attached_slot} is attached, but the guest agent does not answer, so the "
            "mount cannot be checked",
            {"slot": s.attached_slot},
        )
    facts = await _facts(proxmox, s, spec)
    details = {"slot": s.attached_slot, **facts}
    if _in_spec(facts, spec):
        return (
            "in_spec",
            f"{spec.mount_point} is mounted from {s.attached_slot} (serial {serial})",
            details,
        )
    problems = []
    if facts.get("device") in (None, "none"):
        problems.append(f"the guest sees no disk with serial {serial}")
    else:
        if facts.get("mounted") != facts.get("device"):
            problems.append(f"{spec.mount_point} is not mounted from it")
        if facts.get("label") != spec.label or facts.get("fstype") != spec.filesystem:
            problems.append(
                f"it carries {facts.get('fstype') or 'no filesystem'} label "
                f"{facts.get('label') or '-'}, not {spec.filesystem} label {spec.label}"
            )
    if facts.get("fstab") != "yes":
        problems.append("/etc/fstab no longer has the line")
    return "drifted", "; ".join(problems), details
