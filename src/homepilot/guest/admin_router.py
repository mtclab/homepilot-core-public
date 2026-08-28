"""Operator-side guest management (#442 G3): invites and budgets from the console.

The portal's original design kept invite minting CLI-only ("small surface").
G3 revises that deliberately - the operator console is where the operator
works, and "mint an invite for a friend" is operator work. The GUEST-facing
surface is unchanged and stays as small as ever; these routes are admin-scoped
API like any other console feature.

The full invite token appears exactly once: in the response of the mint call.
The database keeps prefix + hash, the list never shows tokens - identical to
API tokens and to the CLI behaviour.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.deps import require_scope
from ..portal.models import InviteCaps
from ..portal.repository import InviteRepository, invite_state
from ..provision.defaults import (
    MissingProvisioningDefaultError,
    provisioning_defaults,
    resolve_ipconfig,
    resolve_node,
    resolve_pool,
    resolve_template_vmid,
)
from .quota import delete_quota, get_quota, set_quota, usage_for

router = APIRouter(prefix="/admin/guests", tags=["guests"])

_admin = Depends(require_scope("admin"))


class QuotaIn(BaseModel):
    cn: str = Field(min_length=1, max_length=128)
    max_vms: int | None = Field(default=None, ge=0)
    max_cores: int | None = Field(default=None, ge=0)
    max_memory_mb: int | None = Field(default=None, ge=0)
    max_disk_gb: int | None = Field(default=None, ge=0)


class InviteIn(BaseModel):
    """What the operator says when minting. The infra half is OPTIONAL (#553 C3).

    "An invite stops carrying raw infra details" (facelift-v2 C3): the node and
    the template come from this instance's provisioning defaults unless this
    request names them, so the operator picks a person and a size, not a
    cluster topology. An explicit value still wins, unchanged.
    """

    cn: str = Field(min_length=1, max_length=128)
    template_vmid: int | None = Field(default=None, ge=100)
    node: str | None = Field(default=None, min_length=1)
    cores: int = Field(ge=1, le=64)
    memory_mb: int = Field(ge=256)
    disk_gb: int = Field(ge=1)
    ttl_days: int = Field(default=7, ge=1, le=90)


def _repo(request: Request) -> Any:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="The backend has not finished starting")
    return repo


def _invites(request: Request) -> InviteRepository:
    inv = getattr(request.app.state, "invite_repo", None)
    if not isinstance(inv, InviteRepository):
        raise HTTPException(status_code=503, detail="The backend has not finished starting")
    return inv


@router.get("", dependencies=[_admin])
async def overview(request: Request) -> dict[str, Any]:
    """Every guest the system knows: budget, real usage, open invites."""
    repo = _repo(request)
    invites = await _invites(request).list_invites()

    cns: set[str] = set()
    quota_rows = await repo.db.fetchall("SELECT * FROM guest_quotas ORDER BY cn")
    for r in quota_rows:
        cns.add(r["cn"])
    for row in invites:
        cns.add(row["bound_cn"])
    owner_rows = await repo.db.fetchall("SELECT DISTINCT owner FROM hosts WHERE owner IS NOT NULL")
    for r in owner_rows:
        cns.add(r["owner"])

    quotas = {r["cn"]: dict(r) for r in quota_rows}
    guests = []
    for cn in sorted(cns):
        used = await usage_for(repo, cn)
        q = quotas.get(cn)
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
                if q is None
                else {
                    "vms": q.get("max_vms"),
                    "cores": q.get("max_cores"),
                    "memory_mb": q.get("max_memory_mb"),
                    "disk_gb": q.get("max_disk_gb"),
                },
            }
        )

    return {
        "guests": guests,
        "invites": [
            {
                "id": row["id"],
                "prefix": row["token_prefix"],
                "cn": row["bound_cn"],
                "state": invite_state(row),
                "caps": {
                    "template_vmid": row["template_vmid"],
                    "node": row["node"],
                    "cores": row["cores"],
                    "memory_mb": row["memory_mb"],
                    "disk_gb": row["disk_gb"],
                },
                "expires_at": row["expires_at"],
                "created_at": row["created_at"],
            }
            for row in invites
        ],
    }


@router.post("/quota", dependencies=[_admin])
async def put_quota(request: Request, body: QuotaIn) -> dict[str, Any]:
    repo = _repo(request)
    await set_quota(
        repo,
        body.cn,
        max_vms=body.max_vms,
        max_cores=body.max_cores,
        max_memory_mb=body.max_memory_mb,
        max_disk_gb=body.max_disk_gb,
    )
    quota = await get_quota(repo, body.cn)
    used = await usage_for(repo, body.cn)
    await repo.log_audit(user_id="ui", source="ui", action="guest_quota_set", target_host=body.cn)
    return {
        "cn": body.cn,
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


@router.delete("/quota/{cn}", dependencies=[_admin])
async def drop_quota(request: Request, cn: str) -> dict[str, Any]:
    """Remove a guest's budget (#607): the set route had no undo.

    DELETE with the CN in the path, matching every other removal in this API
    (DELETE /auth/tokens/{prefix}, /monitoring/rules/{rule_id},
    /admin/settings/overrides/{key}). "Set every axis to null" is NOT the same
    thing - it leaves a quota row that the console keeps showing as a budget,
    unlimited on every axis - so removal has its own route.

    404 when the guest has no budget, like revoking an invite that is not open:
    the operator asked for a change that did not happen, and a cheerful 200
    would tell them they removed something they did not.
    """
    repo = _repo(request)
    removed = await delete_quota(repo, cn)
    if not removed:
        raise HTTPException(status_code=404, detail="That guest has no budget set")
    await repo.log_audit(user_id="ui", source="ui", action="guest_quota_removed", target_host=cn)
    used = await usage_for(repo, cn)
    return {
        "cn": cn,
        "limits": None,
        "usage": {
            "vms": used.vms,
            "cores": used.cores,
            "memory_mb": used.memory_mb,
            "disk_gb": used.disk_gb,
        },
    }


@router.post("/invites", dependencies=[_admin], status_code=201)
async def mint_invite(request: Request, body: InviteIn) -> dict[str, Any]:
    """Mint a one-time, CN-bound invite. The token in this response is shown
    exactly once and stored nowhere."""
    # Resolved HERE, at mint time, and frozen into the invite row: the caps are
    # the contract the redeemer is promised, and a default changed next week
    # must not silently re-point an invite that is already in someone's inbox.
    defaults = await provisioning_defaults(request.app.state)
    try:
        node = resolve_node(body.node, defaults)
        template_vmid = resolve_template_vmid(body.template_vmid, defaults)
    except MissingProvisioningDefaultError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    caps = InviteCaps(
        template_vmid=template_vmid,
        node=node,
        pool=resolve_pool(None, defaults),
        ipconfig0=resolve_ipconfig(None, defaults),
        cores=body.cores,
        memory_mb=body.memory_mb,
        disk_gb=body.disk_gb,
    )
    invite_id, full_token = await _invites(request).create_invite(
        bound_cn=body.cn,
        caps=caps,
        created_by="ui",
        ttl=timedelta(days=body.ttl_days),
    )
    repo = _repo(request)
    await repo.log_audit(
        user_id="ui", source="ui", action="guest_invite_minted", target_host=body.cn
    )
    # The caps are echoed back: when the operator named neither node nor
    # template, this response is where they see which ones the invite got.
    return {
        "id": invite_id,
        "token": full_token,
        "cn": body.cn,
        "caps": {
            "node": caps.node,
            "template_vmid": caps.template_vmid,
            "pool": caps.pool,
            "ipconfig0": caps.ipconfig0,
        },
    }


@router.post("/invites/{prefix}/revoke", dependencies=[_admin])
async def revoke_invite(request: Request, prefix: str) -> dict[str, Any]:
    ok = await _invites(request).revoke(prefix)
    if not ok:
        raise HTTPException(status_code=404, detail="No open invite with that prefix")
    repo = _repo(request)
    await repo.log_audit(
        user_id="ui", source="ui", action="guest_invite_revoked", target_host=prefix
    )
    return {"prefix": prefix, "revoked": True}
