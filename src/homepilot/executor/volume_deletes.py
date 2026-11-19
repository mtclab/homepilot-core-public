"""A change that deletes a disk volume and also takes a pre-apply snapshot defeats itself.

3.14 walk: a proxmox-api-sequence removed a VM's unused disk with
``PUT .../qemu/<vmid>/config delete=unused0``. The executor first took its
automatic pre-apply snapshot (``requires_snapshot`` defaults to true for a
mutating change on a vm/lxc target) while that volume was still part of the
VM, so the snapshot referenced it - and Proxmox refused the delete with
"volume is still in use (snapshot?)", leaving the change partly applied.

The snapshot cannot be a way back for a delete either way: the volume can only
go once no snapshot holds it, and a destroyed guest takes its snapshots with
it. So the product decides BEFORE anything is changed:

* at propose (``artifacts/validator.py``) such a change is REFUSED with a
  sentence naming ``requires_snapshot: false`` - the author says, on the review
  screen, that this change runs without a rollback point. Data safety first:
  quietly skipping the snapshot (the other option) would leave an operator who
  reads "a snapshot is taken before mutating changes" with no way back from the
  one kind of change that most needs one, without being told;
* at apply (``executor/orchestrator.py``) a change proposed before this check
  is refused the same way before the snapshot is taken - nothing is changed;
* if Proxmox still answers "in use (snapshot?)" (a volume this scan cannot see,
  e.g. one named through a variable it cannot resolve), :func:`in_use_hint`
  says the cause and the remedy next to the failure.

What counts as deleting a volume, as Proxmox does it:

* ``PUT|POST .../(qemu|lxc)/<vmid>/config`` whose ``delete`` names an
  ``unusedN`` key (Proxmox frees the volume), or names a disk key
  (``scsiN``, ``virtioN``, ``sataN``, ``ideN``, ``efidisk0``, ``tpmstate0``,
  ``mpN``) with ``force`` set (without ``force`` Proxmox only detaches it to an
  ``unusedN`` entry, which deletes nothing);
* ``PUT .../qemu/<vmid>/unlink`` with ``force`` - unlink AND delete the images;
* ``DELETE .../(qemu|lxc)/<vmid>`` - destroying the guest destroys its disks and
  its snapshots;
* ``DELETE /nodes/<node>/storage/<storage>/content/<volid>`` for a volume of
  the target guest (``vm-<vmid>-...``, ``base-<vmid>-...``, ``subvol-<vmid>-...``).

A step on ANOTHER guest is not held by this guest's snapshot and is not counted.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

import yaml

from homepilot.adapters import pve_reads
from homepilot.executor.jinja_utils import InterpolationError, _interpolate, interpolation_context

_SPEC_FENCE = re.compile(r"```yaml\s+proxmox-api-spec\s*\n(.*?)```", re.DOTALL)
_CONFIG = pve_reads.GUEST_CONFIG_PATH
_UNLINK = pve_reads.GUEST_UNLINK_PATH
_GUEST_ITSELF = pve_reads.GUEST_ITSELF_PATH
_CONTENT = pve_reads.STORAGE_CONTENT_PATH
_UNUSED = re.compile(r"^unused\d+$")
_DISK = re.compile(r"^(scsi|virtio|sata|ide|mp)\d+$|^(efidisk0|tpmstate0)$")
_VOLUME_OWNER = re.compile(r"(?:^|[:/])(?:vm|base|subvol)-(\d+)-")


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _keys(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list | tuple):
        items = [str(k) for k in raw]
    else:
        items = re.split(r"[,;\s]+", str(raw))
    return [k.strip() for k in items if k.strip()]


def _steps(body: str) -> list[dict[str, Any]]:
    match = _SPEC_FENCE.search(body or "")
    if not match:
        return []
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return []
    steps = parsed.get("steps") if isinstance(parsed, dict) else None
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _resolved(path: str, context: dict[str, Any]) -> str:
    try:
        return _interpolate(path, context)
    except InterpolationError:
        return path


def _is_target(vmid_text: str, target_vmid: str) -> bool:
    """Does this path segment address the target guest? An unresolved variable
    may well be the target, so it counts (the refusal names the remedy)."""
    return vmid_text == target_vmid or "{" in vmid_text


def volume_deletes(body: str, target: dict[str, Any], fm: dict[str, Any]) -> list[str]:
    """``step <id>: <what it deletes>`` for each step that deletes a volume of the target."""
    target_vmid = str(target.get("vmid") or "")
    context = interpolation_context(target, fm)
    found: list[str] = []
    for step in _steps(body):
        step_id = str(step.get("id") or "unknown")
        method = str(step.get("method") or "GET").upper()
        path = unquote(_resolved(str(step.get("path") or ""), context).split("?", 1)[0])
        raw_args = step.get("body")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        config = _CONFIG.search(path)
        if config and method in ("PUT", "POST") and _is_target(config.group(2), target_vmid):
            force = _truthy(args.get("force"))
            freed = [
                k
                for k in _keys(args.get("delete"))
                if _UNUSED.match(k) or (force and _DISK.match(k))
            ]
            if freed:
                found.append(f"step {step_id} deletes the volume of {', '.join(freed)}")
            continue
        unlink = _UNLINK.search(path)
        if unlink and method == "PUT" and _is_target(unlink.group(1), target_vmid):
            if _truthy(args.get("force")):
                found.append(f"step {step_id} unlinks and deletes {args.get('idlist')}")
            continue
        itself = _GUEST_ITSELF.search(path)
        if itself and method == "DELETE" and _is_target(itself.group(2), target_vmid):
            found.append(f"step {step_id} destroys the guest, its disks and its snapshots")
            continue
        content = _CONTENT.search(path)
        if content and method == "DELETE":
            volid = content.group(1)
            owner = _VOLUME_OWNER.search(volid)
            if (owner and owner.group(1) == target_vmid) or "{" in volid:
                found.append(f"step {step_id} deletes the volume {volid}")
    return found


def snapshot_conflict(
    kind: str, fm: dict[str, Any], body: str, target: dict[str, Any]
) -> str | None:
    """The refusal for a change whose own pre-apply snapshot would block it, or None."""
    from homepilot.executor.orchestrator import SNAPSHOTTABLE_TARGET_KINDS

    if kind != "proxmox-api-sequence":
        return None
    if fm.get("mutating", True) is False or fm.get("requires_snapshot") is False:
        return None
    if (target or {}).get("kind") not in SNAPSHOTTABLE_TARGET_KINDS:
        return None
    deletes = volume_deletes(body, target, fm)
    if not deletes:
        return None
    return (
        "this change deletes a disk volume ("
        + "; ".join(deletes)
        + ") and would first take the automatic pre-apply snapshot of the guest. That "
        'snapshot keeps the volume in use, so Proxmox would refuse the delete ("volume '
        'is still in use (snapshot?)") and leave the change partly applied - and a '
        "snapshot is no way back from a delete in any case. Set requires_snapshot: false "
        "to say this change runs without a rollback point (take a backup first if you "
        "may want the data), or leave the delete out of this change."
    )


# Proxmox's words when a snapshot still references the volume being freed.
_IN_USE = re.compile(r"volume .*still in use|in use \(snapshot\?\)", re.IGNORECASE)


def in_use_hint(error_text: str) -> str:
    """What to add to a step failure Proxmox gave because a snapshot holds a volume."""
    if not _IN_USE.search(error_text or ""):
        return ""
    return (
        " - Proxmox keeps a volume that a snapshot still references. If this change "
        "took a pre-apply snapshot (requires_snapshot was not false), that snapshot "
        "holds it: once you no longer need it as a way back, delete that snapshot in "
        "Proxmox (the guest's Snapshots tab) and run the change again; propose such a "
        "change with requires_snapshot: false."
    )


__all__ = ["in_use_hint", "snapshot_conflict", "volume_deletes"]
