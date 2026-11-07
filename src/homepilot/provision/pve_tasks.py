"""Stopping a PVE task of ours before taking back what it was building (#711).

Shared by provisioning and template builds, which unwind the same way.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def stop_inflight(
    proxmox: Any,
    node: str,
    upid: str,
    timeout_s: float,
    poll_interval: float,
    check_first: bool = False,
) -> str:
    """Stop a PVE task and WAIT until it no longer holds the guest.

    Returns "stopped", "failed" (it could not be stopped, or was still running
    when the wait ran out) or - with `check_first` - "not_needed" when PVE
    already reports it finished: a task that FAILED is over and there is nothing
    to stop. `stop_task` alone is not enough: it is itself an acceptance, and a
    destroy behind it is refused ("VM is locked (clone)") for as long as the
    task holds the guest's lock - after which the task finishes into an orphan
    nobody removes.
    """
    if check_first:
        try:
            await proxmox.wait_until_stopped(node, upid, timeout_s=0, poll_interval=0)
            return "not_needed"
        except Exception as exc:
            logger.info("PVE task %s is still running (or unreadable): %s", upid, exc)
    outcome = "stopped"
    try:
        await proxmox.stop_task(node, upid)
    except Exception as exc:
        # It may simply have finished already; whether it still holds the guest
        # is what the wait below settles.
        logger.warning("Could not stop PVE task %s: %s", upid, exc)
        outcome = "failed"
    try:
        await proxmox.wait_until_stopped(
            node, upid, timeout_s=timeout_s, poll_interval=poll_interval
        )
    except Exception as exc:
        logger.warning("PVE task %s had not stopped after being asked to: %s", upid, exc)
        return "failed"
    return outcome


__all__ = ["stop_inflight"]
