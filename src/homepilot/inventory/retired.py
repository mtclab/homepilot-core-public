"""Forget the machines HomePilot destroyed itself (3.12, quiet Overview).

On prod 3.11.0 four of the seven "gone" lines on the Overview were machines the
owner had retired through approved changes: HomePilot destroyed them, Proxmox
stopped reporting them, and then HomePilot asked the owner what to do about the
machines it had just deleted. A machine HomePilot destroyed through an applied
change is forgotten on its own, in the inventory sweep that finds it gone, with
a journal row naming the change.

Everything else that is gone stays: a machine that vanished for any other reason
(deleted in the Proxmox UI, a node that went away, a sweep that misread) is news
the operator decides on. So the match is strict, and every part of it is a fact
HomePilot recorded, never a guess:

* a run of the change SUCCEEDED (an apply or replay task), and the change's
  spec destroys a guest at this VMID (``summary.destroyed_vmids``);
* the guest destroyed is THIS machine: the name recorded for that VMID when the
  change was written or approved (``target_identity``, #813) - or the change's
  own target name when it names that VMID - is this row's hostname
  (``same_guest``: PVE reuses VMIDs);
* the machine went missing AFTER that run started (``absent_since``), so the
  run is what explains the absence.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from ..runtime.blocking import run_blocking

logger = logging.getLogger(__name__)

# How far before a machine went missing the destroy may have run. The sweep
# notices an absence within one inventory interval; a week covers an install
# whose sweeps were failing for days without reading the whole history.
LOOKBACK = timedelta(days=7)


def _read_destroys(store: Any, artifact_id: str) -> list[tuple[int, str]]:
    """(VMID, name) of every guest this change destroys. Reads and parses the
    file - run on the io pool, never on the event loop."""
    from ..artifacts.summary import destroyed_vmids

    fm, body = store.read(artifact_id)
    out: list[tuple[int, str]] = []
    for vmid in destroyed_vmids(fm, body):
        name = _destroyed_name(fm, vmid)
        if name:
            out.append((vmid, name))
    return out


def _destroyed_name(fm: dict[str, Any], vmid: int) -> str | None:
    """The name of the guest at `vmid` this change was written for, or None."""
    from ..artifacts.summary import confirm_name

    recorded = ((fm.get("target_identity") or {}).get("guests") or {}).get(str(vmid))
    if isinstance(recorded, dict) and recorded.get("name"):
        return str(recorded["name"])
    target = fm.get("target") or {}
    if str(target.get("vmid") or "") == str(vmid):
        return confirm_name(target)
    return None


async def forget_destroyed_by_homepilot(repo: Any, store: Any) -> list[dict[str, Any]]:
    """Forget every gone machine an applied change of HomePilot's destroyed.

    Returns what it forgot (`host_id`, `hostname`, `artifact_id`). Best effort
    per machine: one that cannot be forgotten is logged and left for the
    operator, never raised into the sweep.
    """
    from .router import InventoryError, forget_host_record
    from .service import same_guest

    gone = await repo.db.fetchall(
        "SELECT id, hostname, proxmox_id, absent_since FROM hosts "
        "WHERE absent_since IS NOT NULL AND proxmox_id IS NOT NULL AND source != 'manual'"
    )
    if not gone:
        return []
    earliest = min(str(g["absent_since"]) for g in gone)
    since = (
        (datetime.fromisoformat(earliest.replace("Z", "+00:00")) - LOOKBACK)
        .astimezone(UTC)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    runs = await repo.db.fetchall(
        "SELECT t.artifact_id, MIN(t.created_at) AS ran_at FROM tasks t "
        "JOIN artifacts a ON a.id = t.artifact_id "
        "WHERE t.status = 'succeeded' AND t.action IN ('apply', 'replay') "
        "AND a.kind = 'proxmox-api-sequence' AND t.created_at >= ? "
        "GROUP BY t.artifact_id",
        (since,),
    )
    # VMID -> [(name the change destroyed there, when it ran, which change)]
    destroyed: dict[int, list[tuple[str, str, str]]] = {}
    for run in runs:
        artifact_id = str(run["artifact_id"])
        try:
            pairs = await run_blocking("io", _read_destroys, store, artifact_id)
        except (FileNotFoundError, ValueError, OSError, yaml.YAMLError):
            continue
        for vmid, name in pairs:
            destroyed.setdefault(vmid, []).append((name, str(run["ran_at"]), artifact_id))

    forgotten: list[dict[str, Any]] = []
    for host in gone:
        try:
            vmid = int(host["proxmox_id"])
        except (TypeError, ValueError):
            continue
        for name, ran_at, artifact_id in destroyed.get(vmid, []):
            if not same_guest(dict(host), name) or str(host["absent_since"]) < ran_at:
                continue
            try:
                await forget_host_record(
                    repo,
                    str(host["id"]),
                    actor="system",
                    source="reconciler:inventory",
                    artifact_id=artifact_id,
                    details={
                        "reason": "destroyed by an applied change",
                        "artifact_id": artifact_id,
                        "vmid": vmid,
                    },
                )
            except InventoryError:
                logger.warning("Could not forget %s after its destroy", host["hostname"])
                break
            forgotten.append(
                {
                    "host_id": str(host["id"]),
                    "hostname": host["hostname"],
                    "artifact_id": artifact_id,
                }
            )
            break
    return forgotten
