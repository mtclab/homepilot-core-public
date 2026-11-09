"""Guest power over MCP (lane B3, #781): read the state, PROPOSE an action.

An assistant never powers a guest. ``get_guest_power`` reads what the operator's
Hosts page reads (``GET /inventory/{host_id}/power``); ``power_guest`` turns a
request into a one-step ``guest-power`` artifact the owner approves - it sends
nothing to Proxmox. The operator's direct route (``POST
/inventory/{host_id}/power``) has no MCP twin on purpose.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any

from homepilot.inventory.power import (
    MAX_SHUTDOWN_TIMEOUT_S,
    MIN_SHUTDOWN_TIMEOUT_S,
    POWER_ACTIONS,
    PowerRefusedError,
    PowerService,
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_guest_power",
        "description": (
            "A Proxmox guest's live power state (running / stopped), the config changes "
            "Proxmox has pending for its next boot (what a reboot would apply, e.g. a "
            "cloud-init ipconfig), whether it is the machine HomePilot runs on or "
            "protected, and for each power action (start / shutdown / reboot / stop) "
            "whether it is available now and whether it needs the guest's name typed "
            "back. Reads Proxmox; sends nothing to the guest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the guest"},
            },
            "required": ["host_id"],
        },
    },
    {
        "name": "power_guest",
        "description": (
            "PROPOSE a power action on a Proxmox VM or container: start, shutdown "
            "(graceful, never forced), reboot (a Proxmox reboot: pending config such as a "
            "cloud-init ipconfig applies) or stop (forced). Nothing is powered: this "
            "creates a one-step guest-power artifact that the owner approves; the apply "
            "then re-checks every guard. Refused at once, with the reason, when a guard "
            "would refuse it now: the guest is gone or renamed at its VMID, already in "
            "that state, a running guest holds one of its addresses or one of its "
            "passthrough disks, or it is the machine HomePilot runs on / a protected "
            "guest (those are only ever powered off by a person on the Hosts page)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the guest"},
                "action": {"type": "string", "enum": list(POWER_ACTIONS)},
                "timeout_s": {
                    "type": "integer",
                    "minimum": MIN_SHUTDOWN_TIMEOUT_S,
                    "maximum": MAX_SHUTDOWN_TIMEOUT_S,
                    "description": (
                        "shutdown/reboot only: seconds the guest gets to shut down before "
                        "the apply reports a timeout (nothing is forced). Default 120."
                    ),
                },
                "reason": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Why - shown to the owner in the proposal's intent",
                },
            },
            "required": ["host_id", "action"],
        },
    },
]


def _service(ctx: dict[str, Any]) -> PowerService:
    inventory = ctx.get("inventory_service")
    repo = ctx.get("repo") or getattr(inventory, "repo", None)
    if repo is None:
        raise RuntimeError("Repository not configured")
    return PowerService(repo, getattr(inventory, "proxmox", None) or ctx.get("proxmox"))


async def handle_get_guest_power(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _service(ctx).describe(str(arguments["host_id"]))
    except PowerRefusedError as exc:
        raise ValueError(exc.message) from None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30].strip("-") or "guest"


async def handle_power_guest(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.artifacts.lifecycle import ConflictError, LifecycleError
    from homepilot.artifacts.models import render_guest_power_body

    host_id = str(arguments["host_id"])
    action = str(arguments["action"])
    timeout_s = arguments.get("timeout_s")
    if action not in ("shutdown", "reboot"):
        timeout_s = None
    svc = _service(ctx)
    # Every guard the apply will run, run now: a proposal the apply would refuse
    # is refused here with the same reason, and nothing is proposed.
    try:
        await svc.check(host_id, action, timeout_s=timeout_s, via_artifact=True)
    except PowerRefusedError as exc:
        raise ValueError(f"not proposed: {exc.message}") from None
    host = await svc.repo.get_host(host_id)
    if host is None:  # gone between the check and now
        raise ValueError(f"not proposed: no host {host_id!r} in inventory")
    hostname = str(host.get("hostname") or "")
    vmid = int(host["proxmox_id"])
    node = str(host["node"])
    reason = str(arguments.get("reason") or "").strip()
    intent = f"{action} {hostname} (VMID {vmid} on {node})" + (f": {reason}" if reason else "")
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    spec: dict[str, Any] = {
        "id": f"{stamp}-power-{action}-{_slug(hostname)}-{secrets.token_hex(3)}",
        "kind": "guest-power",
        "intent": intent[:200],
        "target": {
            "kind": "lxc" if host.get("host_type") == "lxc" else "vm",
            "host": hostname,
            "vmid": vmid,
            "node": node,
        },
        "idempotence": "replay-only",
        "requires_snapshot": False,
        "produced_by": {
            "session": "mcp",
            "agent": "power_guest",
            "user": str(ctx.get("_mcp_caller_id") or "mcp"),
        },
        "body": render_guest_power_body(action, hostname, timeout_s),
    }
    try:
        artifact_id = await ctx["lifecycle"].propose(spec)
    except (LifecycleError, ConflictError) as exc:
        raise ValueError(str(exc)) from None
    return {
        "id": artifact_id,
        "status": "proposed",
        "kind": "guest-power",
        "intent": spec["intent"],
        "powered": False,
        "message": (
            f"Proposed {action} of {hostname}; nothing was powered. The owner approves it "
            "(Changes, or with the approval code they relay); the apply re-checks every "
            "guard before it sends anything."
        ),
    }
