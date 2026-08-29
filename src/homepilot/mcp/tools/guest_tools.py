"""Guest management over MCP (#442, AI-first).

The operator's assistant can do what the console's Guests card does: see every
guest's usage against their budget, adjust a budget, revoke an invite, and -
since the owner's 2026-08-25 decision - provision a guest (provision_guest,
admin tier, mirroring POST /guests/provision). It can also BUILD the cloud-init
template provisioning clones (create_guest_template, admin tier, #594), which
until now needed root on the PVE node and so had no product path at all.

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
        # A separate REMOVAL tool, not "pass nulls to set_guest_quota" (#607).
        # Nulls already mean "unlimited on this axis" in set_guest_quota, and a
        # word cannot mean two things on one surface: an assistant told that all-
        # null removes the budget would also read a partly-null set as a partial
        # removal. The neighbours name removals with their own verb the same way
        # - delete_kb_doc, delete_alert_rule, delete_auth_token, delete_host.
        "name": "delete_guest_quota",
        "description": (
            "Remove a guest's resource budget entirely: from then on their "
            "provisions are gated by invites alone, exactly as for a guest who "
            "never had a budget. This is NOT the same as setting every axis to "
            "null (that keeps a budget which happens to be unlimited). Machines "
            "they already have are never touched. Reports removed=false when the "
            "guest had no budget to begin with."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cn": {"type": "string", "description": "The guest's certificate CN"},
            },
            "required": ["cn"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "cn": {"type": "string"},
                "removed": {"type": "boolean"},
                "limits": {"type": ["object", "null"]},
                "usage": {"type": "object"},
            },
            "required": ["cn", "removed", "limits", "usage"],
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
    {
        # Admin tier (owner decision 2026-08-25). POST /guests/provision is API
        # require_scope("admin"); it clones a Proxmox template into a running guest.
        "name": "provision_guest",
        "description": (
            "Provision a new guest by cloning a Proxmox template. STARTS AN ASYNC "
            "task and returns its task_id with status 'pending' - the guest is "
            "cloned, configured (cloud-init) and started in the background; poll "
            "get_task_result for the outcome. Refuses (an error) when Proxmox is not "
            "configured, when the request is invalid (name, disk, or authorized-key "
            "shapes), or when a provision for the same name is already in flight. "
            "node, template_vmid, pool, storage and ipconfig0 may be omitted when the instance "
            "has provisioning defaults configured; without both a value and a default "
            "the call is refused naming the missing setting. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Guest name: 3-63 lowercase alphanumerics/hyphens",
                },
                "node": {
                    "type": "string",
                    "description": (
                        "Proxmox node to clone on; omit to use the instance's "
                        "provision_default_node"
                    ),
                },
                "template_vmid": {
                    "type": "integer",
                    "description": (
                        "VMID of the template to clone; omit to use the instance's "
                        "provision_default_template_vmid"
                    ),
                },
                "cores": {"type": ["integer", "null"], "description": "vCPUs (1-32)"},
                "memory_mb": {"type": ["integer", "null"], "description": "RAM in MB (256-65536)"},
                "disk_gb": {
                    "type": ["integer", "null"],
                    "description": "Resize the disk to this many GB (1-2000)",
                },
                "disk": {"type": "string", "description": "PVE disk name, e.g. scsi0 (default)"},
                "ciuser": {"type": "string", "description": "cloud-init username (default friend)"},
                "ssh_authorized_key": {
                    "type": ["string", "null"],
                    "description": "One authorized_keys line for the guest",
                },
                "tailscale_auth_key": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional tskey-... auth key, used once in-memory and never "
                        "persisted or logged"
                    ),
                },
                "ipconfig0": {"type": "string", "description": "cloud-init net config (ip=dhcp)"},
                "owner": {"type": ["string", "null"], "description": "Owner CN for the guest"},
                "pool": {"type": ["string", "null"], "description": "PVE resource pool"},
                "storage": {
                    "type": ["string", "null"],
                    "description": (
                        "PVE storage the clone's disks land on; omit to use the "
                        "instance's provision_default_storage, and with neither set "
                        "the clone inherits the template's own storage"
                    ),
                },
                "full": {"type": "boolean", "description": "Full clone (default true)"},
            },
            "required": ["name"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}},
            "required": ["task_id", "status"],
        },
    },
    {
        # Admin tier, mirroring POST /guests/{vmid}/tailnet-join, which is
        # require_scope("admin") - it runs a command inside somebody's machine.
        #
        # Why the key IS allowed on this surface when a minted invite token is
        # not (see this module's docstring): provision_guest already takes a
        # tailscale_auth_key, so the assistant is already a channel for one, and
        # a retry that could not be reached from the same place as the original
        # attempt would send the operator hunting for a different surface at the
        # exact moment something has gone wrong. What is forbidden is MINTING a
        # secret into a transcript; carrying one the caller already holds, once,
        # to the machine it is for, is what this whole path does.
        "name": "rejoin_tailnet",
        "description": (
            "Retry the tailnet join on a guest that ALREADY EXISTS, with a fresh auth "
            "key - no re-provisioning, nothing on the guest is rebuilt. The usual "
            "reason a join failed is an expired or already-used key, which only a new "
            "key fixes. STARTS AN ASYNC task and returns its task_id with status "
            "'pending'; poll get_task_result: its 'result' carries 'tailnet' - 'joined', "
            "'failed' (the guest was asked and said no) or 'unknown' (nothing could be "
            "established, so a fresh key will not help) - and 'tailnet_detail', the "
            "reason in plain words. "
            "Installs tailscale in the guest first if it has none (unless "
            "provision_tailscale_install is off). node and tailnet_hostname come from "
            "the guest's inventory row when not given. The key is used once and is never "
            "stored, audited or logged. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vmid": {"type": "integer", "description": "VMID of the existing guest"},
                "auth_key": {
                    "type": "string",
                    "description": (
                        "A FRESH tskey-... auth key. Used once in-memory and never "
                        "persisted or logged"
                    ),
                },
                "node": {
                    "type": ["string", "null"],
                    "description": (
                        "Proxmox node the guest is on; omit to take it from the "
                        "guest's inventory row, then provision_default_node"
                    ),
                },
                "tailnet_hostname": {
                    "type": ["string", "null"],
                    "description": (
                        "The name the machine takes ON THE TAILNET; omit to use the "
                        "guest's inventory hostname. Not called `host`/`hostname`, "
                        "because which machine this addresses is `vmid` (#608)"
                    ),
                },
            },
            "required": ["vmid", "auth_key"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string"},
                "vmid": {"type": "integer"},
                "node": {"type": "string"},
            },
            "required": ["task_id", "status", "vmid", "node"],
        },
    },
    {
        # Admin tier, like provision_guest: this WRITES to the cluster (it can
        # add a content type to a storage and it creates a VM), and the template
        # it builds is what every later provision clones.
        "name": "create_guest_template",
        "description": (
            "Build the cloud-init TEMPLATE that provision_guest clones, using the "
            "Proxmox API only (no node root needed). STARTS AN ASYNC task and returns "
            "its task_id with status 'pending'; poll get_task_result for the outcome. "
            "It stages a cloud image (source_volid, already on the storage - or "
            "download_url, fetched by the node), creates a VM, imports the image as "
            "its disk, adds the cloud-init drive, serial console and guest agent, and "
            "converts it to a template. Adds the 'import' content type to the storage "
            "if it is missing, and says so on the result. REFUSES an already-used "
            "template_vmid rather than overwriting it; any failure after the VM is "
            "created destroys the half-made VM. Give exactly one of source_volid or "
            "download_url. node and template_vmid may be omitted when the instance "
            "has provisioning defaults. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Template name (default ubuntu-2404-cloudinit)",
                },
                "node": {
                    "type": "string",
                    "description": (
                        "Proxmox node to build on; omit to use the instance's "
                        "provision_default_node"
                    ),
                },
                "template_vmid": {
                    "type": "integer",
                    "description": (
                        "VMID for the new template; omit to use the instance's "
                        "provision_default_template_vmid. Refused if already in use."
                    ),
                },
                "storage": {
                    "type": "string",
                    "description": "PVE storage for the image, disk and cloud-init drive (local)",
                },
                "source_volid": {
                    "type": ["string", "null"],
                    "description": (
                        "A cloud image already on the storage, e.g. local:import/ubuntu-24.04.qcow2"
                    ),
                },
                "download_url": {
                    "type": ["string", "null"],
                    "description": (
                        "http(s) URL of a cloud image (.qcow2/.img) for the NODE to "
                        "fetch onto the storage"
                    ),
                },
                "memory_mb": {"type": "integer", "description": "RAM in MB (256-65536, 2048)"},
                "cores": {"type": "integer", "description": "vCPUs (1-32, 2)"},
            },
            "required": [],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}},
            "required": ["task_id", "status"],
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


async def handle_delete_guest_quota(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Mirror of DELETE /admin/guests/quota/{cn} (#607), same repository call.

    Unlike the route this reports removed=false instead of raising for a guest
    who had no budget: over MCP "there was nothing to remove" is an answer the
    assistant can act on, and the end state the caller asked for holds either way.
    """
    from ...guest.quota import delete_quota, usage_for

    repo = ctx["repo"]
    cn = str(arguments.get("cn") or "").strip()
    if not cn:
        return {"error": "cn is required"}

    removed = await delete_quota(repo, cn)
    if removed:
        await repo.log_audit(
            user_id=ctx.get("caller_id", "mcp"),
            source="mcp",
            action="guest_quota_removed",
            target_host=cn,
        )
    used = await usage_for(repo, cn)
    return {
        "cn": cn,
        "removed": removed,
        # Null, not an all-null limits object: the guest now has NO budget, and
        # this is the same shape query_guests reports for a guest without one.
        "limits": None,
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


async def handle_provision_guest(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Queue a clone-from-template provision through the SAME ProvisionService the
    POST /guests/provision route uses. Validation and refusals come from the
    ProvisionRequest model and the service, not re-implemented here."""
    from pydantic import ValidationError

    from ...provision.defaults import MissingProvisioningDefaultError, provisioning_defaults
    from ...provision.models import ProvisionRequestIn
    from ...provision.service import ProvisionConflictError

    service = ctx.get("provision_service")
    if service is None or getattr(service, "proxmox", None) is None:
        # The route returns 503 here; over MCP it is a clean error.
        raise ValueError("Proxmox not configured")
    try:
        # The same model the HTTP route takes, so the instance's provisioning
        # defaults fill the same gaps for an assistant as for the console.
        given = ProvisionRequestIn(**arguments)
        body = given.resolve(await provisioning_defaults(getattr(service, "defaults_source", None)))
    except MissingProvisioningDefaultError as exc:
        raise ValueError(str(exc)) from exc
    except ValidationError as exc:
        raise ValueError(f"Invalid provision request: {exc}") from exc
    actor = str(ctx.get("_mcp_caller_id") or "mcp")
    try:
        task_id = await service.start(body, actor=actor)
    except ProvisionConflictError as exc:
        raise ValueError(str(exc)) from exc
    return {"task_id": task_id, "status": "pending"}


async def handle_rejoin_tailnet(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Retry a tailnet join through the SAME ProvisionService the route uses (#628).

    The node/hostname fallback chain is `resolve_join_target`, which the HTTP
    route calls too: one implementation, so the two transports cannot answer
    differently about the same guest.
    """
    from pydantic import ValidationError

    from ...provision.models import TailnetJoinRequest
    from ...provision.service import (
        TailnetJoinConflictError,
        TailnetJoinTargetError,
        resolve_join_target,
    )

    service = ctx.get("provision_service")
    if service is None or getattr(service, "proxmox", None) is None:
        raise ValueError("Proxmox not configured")
    vmid = arguments.get("vmid")
    if not isinstance(vmid, int) or isinstance(vmid, bool) or vmid <= 0:
        raise ValueError("vmid must be a positive integer naming an existing guest")
    try:
        body = TailnetJoinRequest(
            auth_key=str(arguments.get("auth_key") or ""),
            node=arguments.get("node"),
            tailnet_hostname=arguments.get("tailnet_hostname"),
        )
    except ValidationError as exc:
        # The message names the FIELD, never the value: a validation error that
        # echoed the auth key back would put it in the transcript.
        raise ValueError(f"Invalid tailnet-join request: {exc.errors()[0]['msg']}") from exc

    try:
        node, hostname = await resolve_join_target(
            service, vmid, node=body.node, hostname=body.tailnet_hostname
        )
    except TailnetJoinTargetError as exc:
        raise ValueError(str(exc)) from exc

    actor = str(ctx.get("_mcp_caller_id") or "mcp")
    try:
        task_id = await service.start_tailnet_join(
            node=node, vmid=vmid, hostname=hostname, key=body.auth_key, actor=actor
        )
    except TailnetJoinConflictError as exc:
        raise ValueError(str(exc)) from exc
    return {"task_id": task_id, "status": "pending", "vmid": vmid, "node": node}


async def handle_create_guest_template(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Queue a template build through GuestTemplateService (#594).

    Validation and refusals come from the GuestTemplateRequest model and the
    service, never re-implemented here - the same discipline
    handle_provision_guest follows, and the reason the two surfaces cannot drift
    apart on what they accept."""
    from pydantic import ValidationError

    from ...provision.defaults import MissingProvisioningDefaultError, provisioning_defaults
    from ...provision.models import GuestTemplateRequestIn
    from ...provision.template import GuestTemplateConflictError

    service = ctx.get("guest_template_service")
    if service is None or getattr(service, "proxmox", None) is None:
        raise ValueError("Proxmox not configured")
    try:
        given = GuestTemplateRequestIn(**arguments)
        body = given.resolve(await provisioning_defaults(getattr(service, "defaults_source", None)))
    except MissingProvisioningDefaultError as exc:
        raise ValueError(str(exc)) from exc
    except ValidationError as exc:
        raise ValueError(f"Invalid template request: {exc}") from exc
    actor = str(ctx.get("_mcp_caller_id") or "mcp")
    try:
        task_id = await service.start(body, actor=actor)
    except GuestTemplateConflictError as exc:
        raise ValueError(str(exc)) from exc
    return {"task_id": task_id, "status": "pending"}
