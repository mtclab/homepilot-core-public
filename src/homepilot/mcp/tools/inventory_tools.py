"""Inventory and environment query tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from homepilot.adapters.proxmox import ProxmoxError
from homepilot.db.repository import Repository

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
]


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
