"""Guests as people (3.12): every friend with their machines, budget and invites.

The operator's Guests page used to read `GET /admin/guests` - per-CN usage
numbers and a flat invite list - and could not show a friend's actual machine,
its state, or a way to its host page. This builds the friend-shaped answer in
ONE read, for the console (`GET /admin/guests/friends`) and the assistant
(`query_guests`, key `friends`) alike, so the two can never describe a friend
differently.

A "friend" is a certificate CN that HomePilot knows from any of the three
places it can appear: a budget row, an invite bound to it, or a machine it
owns. The machines listed are exactly the ones `usage_for` bills - owned by the
CN and not absent - so "1 machine" next to "1 of 1 used" is the same count by
construction, not by coincidence.

Nothing here carries an invite token or its hash: invite rows come from
`InviteRepository.list_invites`, whose column list excludes the hash, and the
full token never exists outside the mint response.
"""

from __future__ import annotations

from typing import Any

from .quota import GuestUsage, usage_for

# The invite sizes the console offers as presets, inside the bounds below.
# "medium" is what a provision builds when it is given no size at all
# (GuestProvisionRequest: 2 cores, 2048 MB) and what the console's mint form
# was pre-filled with before 3.12, with its 20 GB disk; "small" is one step
# down on every axis that can step down. The disk is a request, not a promise:
# minting raises it to the template's own disk when that is larger (#648).
SIZE_PRESETS: tuple[dict[str, Any], ...] = (
    {"name": "small", "cores": 1, "memory_mb": 1024, "disk_gb": 20},
    {"name": "medium", "cores": 2, "memory_mb": 2048, "disk_gb": 20},
)

# What the mint route and provisioning accept for one machine. The mint route
# (InviteIn) allows up to 64 cores, but an invite's caps are re-validated as a
# ProvisionRequest (1-32 cores, 256-65536 MB, 1-2000 GB), so that is the real
# range an invite can carry.
SIZE_BOUNDS: dict[str, dict[str, int]] = {
    "cores": {"min": 1, "max": 32},
    "memory_mb": {"min": 256, "max": 65536},
    "disk_gb": {"min": 1, "max": 2000},
}
TTL_DAYS: dict[str, int] = {"min": 1, "max": 90, "default": 7}


def _usage(used: GuestUsage) -> dict[str, int]:
    return {
        "vms": used.vms,
        "cores": used.cores,
        "memory_mb": used.memory_mb,
        "disk_gb": used.disk_gb,
    }


def _limits(row: dict[str, Any] | None) -> dict[str, int | None] | None:
    if row is None:
        return None
    return {
        "vms": row.get("max_vms"),
        "cores": row.get("max_cores"),
        "memory_mb": row.get("max_memory_mb"),
        "disk_gb": row.get("max_disk_gb"),
    }


def _invite(row: dict[str, Any], state: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "prefix": row["token_prefix"],
        "state": state,
        "caps": {
            "template_vmid": row["template_vmid"],
            "node": row["node"],
            "cores": row["cores"],
            "memory_mb": row["memory_mb"],
            "disk_gb": row["disk_gb"],
        },
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "redeemed_at": row["redeemed_at"],
        "revoked_at": row["revoked_at"],
        "host_id": row["resulting_host_id"],
    }


def _machine(row: dict[str, Any]) -> dict[str, Any]:
    # The hypervisor's own word for a guest (running / stopped / paused), as
    # the inventory last read it - the same word the Hosts list shows. A row
    # the inventory has no hypervisor word for falls back to its derived status.
    return {
        "host_id": row["id"],
        "name": row["hostname"],
        "vmid": row["proxmox_id"],
        "node": row["node"],
        "state": row["pve_status"] or row["status"] or "unknown",
        "cores": row["cpu_cores"],
        "memory_mb": row["memory_mb"],
        "disk_gb": row["disk_gb"],
    }


async def friends(repo: Any, invite_repo: Any) -> list[dict[str, Any]]:
    """Every friend, sorted by CN: machines, usage, limits, invites (newest first)."""
    from ..portal.repository import invite_state

    quota_rows = {
        r["cn"]: dict(r) for r in await repo.db.fetchall("SELECT * FROM guest_quotas ORDER BY cn")
    }
    invite_rows = await invite_repo.list_invites() if invite_repo is not None else []
    # The same filter usage_for bills (owner set, not absent), in one read.
    host_rows = await repo.db.fetchall(
        """SELECT id, hostname, proxmox_id, node, pve_status, status,
                  cpu_cores, memory_mb, disk_gb, owner
           FROM hosts WHERE owner IS NOT NULL AND absent_since IS NULL
           ORDER BY hostname"""
    )
    # A CN known only from an absent machine is still a friend (their budget
    # and invites may matter); it simply has no machine listed.
    owners = await repo.db.fetchall("SELECT DISTINCT owner FROM hosts WHERE owner IS NOT NULL")

    cns: set[str] = set(quota_rows)
    cns.update(str(r["bound_cn"]) for r in invite_rows)
    cns.update(str(r["owner"]) for r in owners)

    machines: dict[str, list[dict[str, Any]]] = {}
    for h in host_rows:
        machines.setdefault(str(h["owner"]), []).append(_machine(dict(h)))
    invites: dict[str, list[dict[str, Any]]] = {}
    for row in invite_rows:  # list_invites is newest first
        r = dict(row)
        invites.setdefault(str(r["bound_cn"]), []).append(_invite(r, invite_state(r)))

    out: list[dict[str, Any]] = []
    for cn in sorted(cns):
        # Usage is what ENFORCEMENT reads (check_provision -> usage_for), not a
        # sum of the list above: the bars must show the number redemption is
        # refused on, even if the two queries were ever to drift apart.
        used = await usage_for(repo, cn)
        out.append(
            {
                "cn": cn,
                "machines": machines.get(cn, []),
                "usage": _usage(used),
                "limits": _limits(quota_rows.get(cn)),
                "invites": invites.get(cn, []),
            }
        )
    return out


__all__ = ["SIZE_BOUNDS", "SIZE_PRESETS", "TTL_DAYS", "friends"]
