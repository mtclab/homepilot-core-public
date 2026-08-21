"""Inventory and environment query tools."""

from __future__ import annotations

import asyncio
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
    repo: Repository = ctx["repo"]
    target = arguments["target"]
    host = await repo.get_host_by_hostname(target)
    services = []
    if host:
        svc_list = await repo.list_services(host_id=host.get("id"))
        services = svc_list if svc_list else []

    artifacts = await asyncio.to_thread(ctx["store"].list)
    target_artifacts = [
        a
        for a in artifacts
        if a.get("target", {}).get("host") == target
        or a.get("intent", "").lower().find(target.lower()) >= 0
    ]

    doc_parts = []
    heading = target
    if host:
        heading = f"{target} (vmid {host.get('proxmox_id', '?')}, node {host.get('node', '?')})"
    doc_parts.append(f"=== {heading} ===\n")

    if host:
        doc_parts.append("Inventory facts:")
        for k, v in host.items():
            if v is not None and v != "unknown":
                doc_parts.append(f"  {k}: {v}")

    if services:
        doc_parts.append("\nServices:")
        for svc in services:
            doc_parts.append(f"  {svc.get('name', '?')} — {svc.get('status', '?')}")

    if target_artifacts:
        doc_parts.append("\nArtifact history:")
        for a in target_artifacts:
            doc_parts.append(
                f"  {a.get('created_at', '?')}  {a.get('intent', '?')[:60]}  {a.get('status', '?')}"
            )

    if not doc_parts:
        doc_parts.append(f"No inventory data found for target '{target}'")

    return [TextContent(type="text", text="\n".join(doc_parts))]
