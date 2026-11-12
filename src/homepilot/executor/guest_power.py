"""Apply a ``guest-power`` artifact: one power action an assistant proposed (#781).

An assistant never powers a guest. Its ``power_guest`` tool proposes this
artifact; the owner approves it; the apply lands here and runs the SAME
``PowerService`` the operator's direct route runs - every guard re-checked now,
because the guest, its addresses and its neighbours may have changed since the
proposal. Two differences from the direct path, both deliberate:

* the self guest and protected guests are refused outright for shutdown /
  reboot / stop: their guard is a name a PERSON types back on the Hosts page,
  and an approval of a proposal is not that;
* an outcome that is not known (the PVE task still running when the run
  stopped following it) is reported as ``unsettled_task``, so the artifact is
  neither applied nor failed until PVE says how the task ended (#774).
"""

from __future__ import annotations

from typing import Any

from homepilot.artifacts.models import parse_guest_power_spec
from homepilot.inventory.power import (
    OUTCOME_OK,
    OUTCOME_UNKNOWN,
    PowerRefusedError,
    PowerService,
)


async def _target_host(repo: Any, target: dict[str, Any]) -> dict[str, Any] | None:
    """The inventory row the target names: its VMID, on its node, under its name."""
    from homepilot.inventory.service import same_guest

    vmid = target.get("vmid")
    if vmid is None:
        return None
    rows = await repo.list_hosts_by_proxmox_id(int(vmid))
    for row in rows:
        row = dict(row)
        if str(row.get("node") or "") == str(target.get("node") or "") and same_guest(
            row, target.get("host")
        ):
            return row
    return None


async def execute(
    fm: dict[str, Any],
    body: str,
    target: dict[str, Any],
    repo: Any,
    proxmox: Any,
    *,
    power_service: PowerService | None = None,
) -> dict[str, Any]:
    artifact_id = str(fm.get("id") or "")
    try:
        spec = parse_guest_power_spec(body)
    except ValueError as exc:
        return {
            "success": False,
            "execution_log": f"[guest-power] REFUSED: {exc}",
            "failure_reason": str(exc),
        }
    host = await _target_host(repo, target)
    if host is None:
        reason = (
            f"no inventory row is guest {target.get('host')!r} at VMID {target.get('vmid')} "
            f"on {target.get('node')}; nothing was sent"
        )
        return {
            "success": False,
            "execution_log": f"[guest-power] REFUSED: {reason}",
            "failure_reason": reason,
        }
    approver = (fm.get("approved_by") or {}).get("user") or "approved artifact"
    svc = power_service or PowerService(repo, proxmox)
    try:
        result = await svc.execute(
            str(host["id"]),
            spec.action,
            actor=str(approver),
            source="artifact",
            timeout_s=spec.timeout_s,
            via_artifact=True,
            artifact_id=artifact_id,
        )
    except PowerRefusedError as refused:
        return {
            "success": False,
            "execution_log": f"[guest-power] REFUSED ({refused.code}): {refused.message}",
            "failure_reason": refused.message,
        }
    log = (
        f"[guest-power] {spec.action} {result.hostname} (VMID {result.vmid} on {result.node}): "
        f"{result.outcome} - {result.message}" + (f" [task {result.upid}]" if result.upid else "")
    )
    if result.outcome == OUTCOME_OK:
        return {"success": True, "execution_log": log}
    if result.outcome == OUTCOME_UNKNOWN and result.upid:
        return {
            "success": False,
            "execution_log": log,
            "failure_reason": f"outcome unknown: {result.message}",
            "unsettled_task": {
                "node": result.node,
                "upid": result.upid,
                "step": f"guest-power {spec.action}",
            },
        }
    return {"success": False, "execution_log": log, "failure_reason": result.message}
