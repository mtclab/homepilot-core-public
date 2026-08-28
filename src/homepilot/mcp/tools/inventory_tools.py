"""Inventory and environment query tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from homepilot.adapters.proxmox import ProxmoxError
from homepilot.db.repository import Repository
from homepilot.mcp.tools.host_param import host_arg, host_properties, with_host_warning

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_inventory",
        "description": (
            "List hosts, services, and networks from the HomePilot inventory. "
            'Pass a JSON filter object (e.g. {"role": "guest"}) or None for all.'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": ["string", "null"],
                    "description": "JSON filter object or null for all items",
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "hosts": {"type": "array", "items": {"type": "object"}},
                "services": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["hosts", "services"],
        },
    },
    {
        "name": "refresh_inventory",
        "description": (
            "Re-pull inventory data from the Proxmox REST API and connected hp-agents. "
            'Pass "full" to refresh everything, or a specific scope like '
            '"hosts", "services", or "networks".'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": ["string", "null"],
                    "description": (
                        '"full" for complete refresh, or a specific scope, or null for full'
                    ),
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "nodes": {"type": "object"},
                "version": {"type": "object"},
            },
            "required": ["status"],
        },
    },
    {
        "name": "get_environment_doc",
        "description": (
            "Render a fused environment document for a target (host, service, or "
            "network name). Combines inventory facts, KB intent, and artifact history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Host, service, or network name to document",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "get_host",
        "description": (
            "One host in full: its inventory record, every service recorded on it, and "
            "- when an agent is linked - that agent's block (connected, version, arch, "
            "runtime, first seen, last heartbeat, and last_error saying why it is not "
            "connected). Takes the host's inventory id. Reads the database; it does not "
            "touch the host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the host"},
            },
            "required": ["host_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "hostname": {"type": "string"},
                "services": {"type": "array", "items": {"type": "object"}},
                "agent": {"type": ["object", "null"]},
            },
            "required": ["hostname"],
        },
    },
    # ── Mutators (MCP<->API parity, wave 2). Each calls the SAME shared callable
    # its management route calls, so the inventory an operator edits and the one
    # the assistant edits cannot diverge. ───────────────────────────────────────
    {
        "name": "add_host",
        "description": (
            "Add a host HomePilot cannot learn from Proxmox - the NAS, the router, the "
            "Pi, an old tower - to the inventory. Recorded as source=manual and adopted "
            "on the spot (a machine typed in by hand is not a discovery awaiting "
            "triage), and never declared absent by a Proxmox sync. host must be a "
            "DNS hostname or an IPv4 address; a duplicate name is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **host_properties("DNS hostname or IPv4 address"),
                "ip_address": {"type": ["string", "null"]},
                "role": {"type": "string", "description": "Default 'guest'"},
                "host_type": {"type": "string", "description": "Default 'baremetal'"},
                "description": {"type": ["string", "null"]},
                "tags": {"type": ["string", "null"]},
                "fqdn": {"type": ["string", "null"]},
            },
            "required": ["host"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "hostname": {"type": "string"}},
        },
    },
    {
        "name": "adopt_host",
        "description": (
            "Adopt a discovered host by its inventory id: mark it managed and imported, "
            "then best-effort introspect it (an offline or agent-less host still "
            "adopts, just without the introspection block). Returns the updated host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the host"},
            },
            "required": ["host_id"],
        },
        "outputSchema": {"type": "object", "properties": {"hostname": {"type": "string"}}},
    },
    {
        "name": "ignore_host",
        "description": (
            "Set a host's import state to 'ignored' by its inventory id, keeping it out "
            "of the way without deleting it. Returns the updated host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the host"},
            },
            "required": ["host_id"],
        },
        "outputSchema": {"type": "object", "properties": {"hostname": {"type": "string"}}},
    },
    {
        "name": "update_host",
        "description": (
            "Edit one host's fields by its inventory id (managed, tags, role, "
            "ip_address, description, import_state, status). Only the fields you pass "
            "are changed, and every field you set is PINNED so the next Proxmox sync or "
            "enrich pass leaves it alone. Passing no recognised field is an error. "
            "Returns the updated host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the host"},
                "managed": {"type": ["boolean", "null"]},
                "tags": {"type": ["string", "null"]},
                "role": {"type": ["string", "null"]},
                "ip_address": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
                "import_state": {
                    "type": ["string", "null"],
                    "description": "One of: pending, adopted, ignored",
                },
                "status": {"type": ["string", "null"]},
            },
            "required": ["host_id"],
        },
        "outputSchema": {"type": "object", "properties": {"hostname": {"type": "string"}}},
    },
    {
        "name": "delete_host",
        "description": (
            "Remove a host from inventory by its id, with its services and observation "
            "note. Refused (409) for a host the hypervisor still reports, because the "
            "next sync would bring it straight back - destroy the guest in Proxmox or "
            "ignore_host it instead. Manually added and already-absent hosts remove "
            "cleanly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {"type": "string", "description": "Inventory id of the host"},
            },
            "required": ["host_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "forgotten": {"type": "boolean"}},
        },
    },
    {
        "name": "enrich_inventory",
        "description": (
            "Fill in inferred inventory fields (role, IP and the like) for hosts that "
            "lack them, without overwriting anything an operator pinned. Pass host_ids "
            "to enrich only those, or a scope; omit both to enrich what needs it. "
            "Returns a summary of what was touched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Only these hosts, or null for all that need it",
                },
                "scope": {"type": ["string", "null"]},
            },
        },
        "outputSchema": {"type": "object"},
    },
    {
        "name": "bulk_host_action",
        "description": (
            "Apply ONE action to many hosts by id: 'adopt' (mark managed+imported and "
            "best-effort introspect), 'ignore' (set import state ignored), or 'enrich' "
            "(fill inferred fields). This does not ADD hosts - use add_host for that. "
            "Best-effort per host: an unknown id or a per-host failure counts as failed "
            "and the rest still run. Returns {succeeded, failed}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "adopt, ignore, or enrich",
                },
                "host_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action", "host_ids"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "succeeded": {"type": "integer"},
                "failed": {"type": "integer"},
            },
            "required": ["succeeded", "failed"],
        },
    },
]


async def handle_get_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """One host, exactly as GET /inventory/{host_id} returns it.

    Same InventoryService method, so the host page an operator reads and the
    host record the assistant reads cannot describe different machines.
    """
    service = ctx.get("inventory_service")
    if service is None:
        raise RuntimeError("Inventory service not configured")
    host_id = str(arguments["host_id"])
    host: dict[str, Any] | None = await service.get_host_detail(host_id)
    if host is None:
        raise ValueError(f"Host not found: {host_id}")
    return host


async def handle_query_inventory(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo: Repository = ctx["repo"]
    filter_str = arguments.get("filter")
    filter_obj = None
    if filter_str:
        try:
            filter_obj = json.loads(filter_str)
        except json.JSONDecodeError:
            filter_obj = {"hostname": filter_str}

    hosts = await repo.list_hosts(
        managed=filter_obj.get("managed") if filter_obj else None,
        role=filter_obj.get("role") if filter_obj else None,
    )
    services = await repo.list_services(
        host_id=filter_obj.get("host_id") if filter_obj else None,
    )
    return {"hosts": hosts, "services": services}


async def handle_refresh_inventory(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    proxmox = ctx["proxmox"]
    if proxmox is None:
        raise RuntimeError("Proxmox not configured")
    try:
        version = await proxmox.read("/version")
        nodes_data = await proxmox.read("/nodes")
        return {"status": "refreshed", "nodes": nodes_data, "version": version}
    except ProxmoxError as e:
        raise RuntimeError(f"Proxmox refresh failed — {e}") from e


async def handle_get_environment_doc(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> list[TextContent]:
    """Everything HomePilot knows about a target, INCLUDING the KB (#427).

    This handler rendered inventory + services + artifacts and no KB at all,
    while its own tool description advertised "inventory facts, KB intent, and
    artifact history". The correct implementation already existed on
    `InventoryService.get_environment_doc` and nothing called it - so the AI, for
    whom this tool exists, was the one caller getting the lesser answer.
    """
    service = ctx.get("inventory_service")
    target = arguments["target"]
    if service is None:
        raise RuntimeError("Inventory service not configured")

    doc = await service.get_environment_doc(target)

    lines: list[str] = [f"=== {target} ==="]

    hosts = doc.get("hosts") or []
    for host in hosts:
        lines.append(
            f"\nHost {host.get('hostname', '?')} "
            f"(vmid {host.get('proxmox_id', '?')}, node {host.get('node', '?')})"
        )
        for key, value in host.items():
            if key == "services" or value is None or value == "unknown":
                continue
            lines.append(f"  {key}: {value}")

    services = doc.get("services") or []
    if services:
        lines.append("\nServices:")
        for svc in services:
            lines.append(f"  {svc.get('name', '?')} - {svc.get('status', '?')}")

    kb_entries = doc.get("kb_entries") or []
    if kb_entries:
        lines.append("\nKnowledge base:")
        for entry in kb_entries:
            title = entry.get("title") or entry.get("target") or "?"
            content = (entry.get("content") or "").strip().replace("\n", " ")
            lines.append(f"  {title}: {content[:200]}")

    history = doc.get("artifact_history") or []
    if history:
        lines.append("\nArtifact history:")
        for a in history:
            lines.append(
                f"  {a.get('created_at', '?')}  {str(a.get('intent', '?'))[:60]}  "
                f"{a.get('status', '?')}"
            )

    if len(lines) == 1:
        lines.append(f"No data found for target '{target}'")

    return [TextContent(type="text", text="\n".join(lines))]


# ── Mutators (wave 2). Each handler builds the SAME request model and calls the
# SAME shared callable the management route uses; InventoryError.status is mapped
# to a ValueError so the MCP client sees a clean message. ──────────────────────


def _require_repo(ctx: dict[str, Any]) -> Repository:
    repo: Repository | None = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    return repo


def _require_service(ctx: dict[str, Any]) -> Any:
    service = ctx.get("inventory_service")
    if service is None:
        raise RuntimeError("Inventory service not configured")
    return service


async def handle_add_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from pydantic import ValidationError

    from homepilot.inventory.router import (
        HostCreateRequest,
        InventoryError,
        create_manual_host_record,
    )

    # #608: the tool takes `host`; the REQUEST MODEL (and the row, and the API
    # body) call the field `hostname`, so the standard name is translated here
    # rather than renaming a database column to tidy a tool signature.
    host, warning = host_arg(arguments)
    fields = {k: v for k, v in arguments.items() if k not in ("host", "hostname")}
    try:
        body = HostCreateRequest(hostname=host, **fields)
    except ValidationError as exc:
        raise ValueError(f"Invalid host: {exc}") from exc
    try:
        return with_host_warning(await create_manual_host_record(_require_repo(ctx), body), warning)
    except InventoryError as exc:
        raise ValueError(exc.detail) from exc


async def handle_adopt_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.inventory.router import InventoryError, adopt_host_record

    try:
        return await adopt_host_record(
            str(arguments["host_id"]),
            repo=_require_repo(ctx),
            svc=_require_service(ctx),
            adapter=ctx.get("agent_adapter"),
        )
    except InventoryError as exc:
        raise ValueError(exc.detail) from exc


async def handle_ignore_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.inventory.router import InventoryError, ignore_host_record

    try:
        return await ignore_host_record(_require_repo(ctx), str(arguments["host_id"]))
    except InventoryError as exc:
        raise ValueError(exc.detail) from exc


async def handle_update_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from pydantic import ValidationError

    from homepilot.inventory.router import (
        HostPatchRequest,
        InventoryError,
        update_host_record,
    )

    host_id = str(arguments["host_id"])
    fields = {k: v for k, v in arguments.items() if k != "host_id"}
    try:
        body = HostPatchRequest(**fields)
    except ValidationError as exc:
        raise ValueError(f"Invalid update: {exc}") from exc
    try:
        return await update_host_record(_require_repo(ctx), host_id, body)
    except InventoryError as exc:
        raise ValueError(exc.detail) from exc


async def handle_delete_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.inventory.router import InventoryError, forget_host_record

    try:
        return await forget_host_record(_require_repo(ctx), str(arguments["host_id"]))
    except InventoryError as exc:
        raise ValueError(exc.detail) from exc


async def handle_enrich_inventory(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    service = _require_service(ctx)
    result: dict[str, Any] = await service.enrich_inventory(
        host_ids=arguments.get("host_ids"),
        scope=arguments.get("scope"),
    )
    return result


async def handle_bulk_host_action(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from pydantic import ValidationError

    from homepilot.inventory.router import BulkRequest, bulk_host_action_record

    try:
        body = BulkRequest(action=str(arguments["action"]), host_ids=list(arguments["host_ids"]))
    except (ValidationError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid bulk request: {exc}") from exc
    return await bulk_host_action_record(
        body,
        repo=_require_repo(ctx),
        svc=_require_service(ctx),
        adapter=ctx.get("agent_adapter"),
    )
