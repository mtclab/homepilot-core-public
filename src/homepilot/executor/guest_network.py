"""Apply a ``guest-network`` artifact: survey, plan, execute, report (#553).

The change itself lives in ``provision/guest_network.py``; this is the thin
executor that hands the artifact's desired state to it and turns the result into
an execution log. Two properties matter here and are the reason it is thin:

* the apply runs the SAME ``plan()`` the drift check and the Settings card run,
  so "converged" means the same thing on all three surfaces;
* an artifact that changes nothing succeeds and says so, because the plan is
  empty. That is what makes a guest-network artifact re-appliable.

There is no rollback. Nothing here deletes, so the inverse of "created a zone
and a subnet" would be a delete this slice does not have - and a revoke that
quietly removed the network a guest is sitting on would be worse than one that
says it only relabelled the artifact. ``rollback.kind_can_roll_back`` returns
False for this kind, so a body claiming ``rollback: true`` is refused at propose
rather than discovered at revoke.
"""

from __future__ import annotations

import logging
from typing import Any

from homepilot.artifacts.models import parse_guest_network_spec
from homepilot.provision.guest_network import (
    DesiredGuestNetwork,
    desired_from_settings,
    enforcement_note,
    gateway_for,
    plan,
    survey,
)
from homepilot.provision.guest_network import (
    execute as run_plan,
)

logger = logging.getLogger(__name__)


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    target: dict[str, Any],
    proxmox: Any,
    settings_source: Any = None,
    gateway: Any = None,
) -> dict[str, Any]:
    # The PVE side goes through the estate's proxmox_mcp library, never through
    # endpoint paths written here. `gateway` is injectable so a test can drive
    # the whole apply against a fake cluster at that boundary.
    gateway = gateway if gateway is not None else gateway_for(proxmox)
    if gateway is None:
        return {
            "success": False,
            "execution_log": "Proxmox is not configured on this instance",
            "failure_reason": "no Proxmox client",
        }

    defaults: DesiredGuestNetwork | None = None
    try:
        defaults = await desired_from_settings(settings_source)
    except ValueError as exc:
        # The settings themselves are unusable. Reported, not ignored: the body
        # may still be complete on its own, and if it is not, the message below
        # names the real reason.
        logger.warning("Stored guest-network settings are unusable: %s", exc)

    try:
        desired = parse_guest_network_spec(body, defaults)
    except ValueError as exc:
        return {
            "success": False,
            "execution_log": f"spec error: {exc}",
            "failure_reason": str(exc),
        }

    node = str(target.get("node") or "")
    current = await survey(gateway, desired, node)
    the_plan = plan(desired, current)

    header = [
        f"desired: {desired.to_dict()}",
        enforcement_note(current),
    ]
    if current.errors:
        header.append("survey could not read: " + "; ".join(current.errors))

    if the_plan.blockers:
        detail = "; ".join(the_plan.blockers)
        return {
            "success": False,
            "execution_log": "\n".join([*header, f"BLOCKED: {detail}"]),
            "failure_reason": detail,
        }

    result = await run_plan(gateway, the_plan.steps)
    result["execution_log"] = "\n".join([*header, result.get("execution_log", "")]).strip()
    return result
