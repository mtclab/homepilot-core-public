"""Guest management over MCP (#442, AI-first).

The operator's assistant can do what the console's Guests card does: see every
guest's usage against their budget, adjust a budget, and revoke an invite.

What is deliberately NOT here: minting invites. A minted token is a secret
that provisions a machine, and an MCP transcript is not a safe place for one -
the console and the CLI both show it exactly once to a human. The assistant
can prepare everything about a guest and then say "mint it in Settings ->
Guests"; it cannot be the channel the secret travels through.
"""

from __future__ import annotations

from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_guests",
        "description": (
            "Every portal guest: their machines' total usage (count, cores, memory, "
            "disk) next to their budget limits, plus their invites (prefix, state, "
            "caps - never tokens)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "guests": {"type": "array", "items": {"type": "object"}},
                "invites": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["guests", "invites"],
        },
    },
    {
        "name": "set_guest_quota",
        "description": (
            "Set (replace) a guest's resource budget: totals across ALL their "
            "machines. Null on an axis means unlimited. Takes effect on their "
            "next provision; machines they already have are never touched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cn": {"type": "string", "description": "The guest's certificate CN"},
                "max_vms": {"type": ["integer", "null"]},
                "max_cores": {"type": ["integer", "null"]},
                "max_memory_mb": {"type": ["integer", "null"]},
                "max_disk_gb": {"type": ["integer", "null"]},
            },
            "required": ["cn"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "cn": {"type": "string"},
                "limits": {"type": "object"},
                "usage": {"type": "object"},
            },
            "required": ["cn", "limits", "usage"],
        },
    },
    {
        "name": "revoke_guest_invite",
        "description": "Revoke an open invite by its prefix (from query_guests).",
        "inputSchema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
            "required": ["prefix"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}, "revoked": {"type": "boolean"}},
            "required": ["prefix", "revoked"],
        },
    },
]


async def handle_query_guests(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from ...guest.quota import get_quota, usage_for
    from ...portal.repository import invite_state

    repo = ctx["repo"]
    invites_repo = ctx.get("invite_repo")

    invites: list[dict[str, Any]] = []
    cns: set[str] = set()
    if invites_repo is not None:
        for row in await invites_repo.list_invites():
            cns.add(row["bound_cn"])
            invites.append(
                {
                    "prefix": row["token_prefix"],
                    "cn": row["bound_cn"],
                    "state": invite_state(row),
                    "caps": {
                        "cores": row["cores"],
                        "memory_mb": row["memory_mb"],
                        "disk_gb": row["disk_gb"],
                    },
                    "expires_at": row["expires_at"],
                }
            )
    for r in await repo.db.fetchall("SELECT cn FROM guest_quotas"):
        cns.add(r["cn"])
    for r in await repo.db.fetchall("SELECT DISTINCT owner FROM hosts WHERE owner IS NOT NULL"):
        cns.add(r["owner"])

    guests = []
    for cn in sorted(cns):
        used = await usage_for(repo, cn)
        quota = await get_quota(repo, cn)
        guests.append(
            {
                "cn": cn,
                "usage": {
                    "vms": used.vms,
                    "cores": used.cores,
                    "memory_mb": used.memory_mb,
                    "disk_gb": used.disk_gb,
                },
                "limits": None
                if quota is None
                else {
                    "vms": quota.get("max_vms"),
                    "cores": quota.get("max_cores"),
                    "memory_mb": quota.get("max_memory_mb"),
                    "disk_gb": quota.get("max_disk_gb"),
                },
            }
        )
    return {"guests": guests, "invites": invites}


async def handle_set_guest_quota(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from ...guest.quota import get_quota, set_quota, usage_for

    repo = ctx["repo"]
    cn = str(arguments.get("cn") or "").strip()
    if not cn:
        return {"error": "cn is required"}

    def _axis(name: str) -> int | None:
        v = arguments.get(name)
        return None if v is None else max(0, int(v))

    await set_quota(
        repo,
        cn,
        max_vms=_axis("max_vms"),
        max_cores=_axis("max_cores"),
        max_memory_mb=_axis("max_memory_mb"),
        max_disk_gb=_axis("max_disk_gb"),
    )
    await repo.log_audit(
        user_id=ctx.get("caller_id", "mcp"),
        source="mcp",
        action="guest_quota_set",
        target_host=cn,
    )
    quota = await get_quota(repo, cn)
    used = await usage_for(repo, cn)
    return {
        "cn": cn,
        "limits": {
            "vms": quota.get("max_vms") if quota else None,
            "cores": quota.get("max_cores") if quota else None,
            "memory_mb": quota.get("max_memory_mb") if quota else None,
            "disk_gb": quota.get("max_disk_gb") if quota else None,
        },
        "usage": {
            "vms": used.vms,
            "cores": used.cores,
            "memory_mb": used.memory_mb,
            "disk_gb": used.disk_gb,
        },
    }


async def handle_revoke_guest_invite(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    repo = ctx["repo"]
    invites_repo = ctx.get("invite_repo")
    prefix = str(arguments.get("prefix") or "").strip()
    if invites_repo is None:
        return {"prefix": prefix, "revoked": False, "error": "invites unavailable"}
    ok = await invites_repo.revoke(prefix)
    if ok:
        await repo.log_audit(
            user_id=ctx.get("caller_id", "mcp"),
            source="mcp",
            action="guest_invite_revoked",
            target_host=prefix,
        )
    return {"prefix": prefix, "revoked": bool(ok)}
