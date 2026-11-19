"""How a host is built, over MCP (#818).

``describe_host`` is a READ: it returns the stored host description (what runs,
how it is exposed, where its configuration lives, what changed since the last
one) and, with ``refresh``, asks the host's agent for a fresh one first - the
agent runs fixed read-only collectors, so nothing on the host changes. It
mirrors ``GET``/``POST /inventory/{host_id}/description`` (both API ``read``).

``export_host_asbuilt`` WRITES a knowledge-base note (``POST
/inventory/{host_id}/description/export``, API ``write``): the description as a
rebuild recipe, superseding the host's previous as-built note.
"""

from __future__ import annotations

from typing import Any

from homepilot.inventory.description import DescriptionError, HostDescriptionService

from .host_param import host_arg, host_properties, with_host_warning

_HOST_ID = {"type": "string", "description": "Inventory id of the host (instead of `host`)"}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "describe_host",
        "description": (
            "How a host is built, as its HomePilot agent sees it: listening sockets with the "
            "owning process and unit, systemd services/timers (program path, never arguments), "
            "Docker containers and compose projects, nginx server blocks and upstreams, "
            "certificate names and expiry, certbot renewals, a ufw/nftables summary, pending "
            "updates and time sync - plus what changed since the previous description. Each "
            "part says when the agent's user could not see it. Secrets never appear: the agent "
            "drops environments, command lines and credential values before sending. "
            "refresh=true asks the agent for a fresh description first (read-only on the host)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **host_properties("Hostname as in the inventory"),
                "host_id": _HOST_ID,
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Collect a fresh description from the agent first",
                },
            },
        },
    },
    {
        "name": "export_host_asbuilt",
        "description": (
            "Record a host's latest description in the knowledge base as its 'as-built' note: "
            "a rebuild recipe (what runs, how it is exposed, where its configuration lives, "
            "what it depends on, a rebuild checklist). Supersedes the host's previous "
            "as-built note, so the KB holds one current recipe per host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **host_properties("Hostname as in the inventory"),
                "host_id": _HOST_ID,
            },
        },
    },
]


def _service(ctx: dict[str, Any]) -> HostDescriptionService:
    svc = ctx.get("host_descriptions")
    if svc is None:
        repo = ctx.get("repo")
        if repo is None:
            raise RuntimeError("Repository not configured")
        svc = HostDescriptionService(repo, ctx.get("agent_adapter"))
    return svc


async def _host(svc: HostDescriptionService, arguments: dict[str, Any]) -> tuple[Any, str | None]:
    host_id = str(arguments.get("host_id") or "").strip()
    if host_id:
        return await svc.host(host_id=host_id), None
    name, warning = host_arg(arguments)
    return await svc.host(hostname=name), warning


async def handle_describe_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    svc = _service(ctx)
    try:
        host, warning = await _host(svc, arguments)
        if arguments.get("refresh"):
            result = await svc.collect(host)
        else:
            result = await svc.view(host)
    except DescriptionError as exc:
        raise ValueError(exc.detail) from None
    return with_host_warning(result, warning)


async def handle_export_host_asbuilt(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    svc = _service(ctx)
    try:
        host, warning = await _host(svc, arguments)
        result = await svc.export_asbuilt(host, ctx.get("kb_service"))
    except DescriptionError as exc:
        raise ValueError(exc.detail) from None
    return with_host_warning(result, warning)
