"""The structured PVE reads HomePilot makes over ``ProxmoxClient.read`` (#717).

The rule (owner decision 2026-11-18, amending the 2026-08-26 mandate): a
Proxmox endpoint path lives ONLY in the adapter modules -
``adapters/proxmox.py``, ``adapters/pve_reads.py``, ``adapters/pve_sdn.py``,
``adapters/pve_access.py`` and ``adapters/pve_firewall_guard.py``. Everything
else asks an adapter by NAME ("the cluster's guests", "this node's network"),
and ``tests/test_pve_paths_only_in_adapters.py`` fails the build, with
file:line, on any PVE path literal anywhere else.

These are module functions taking the client, not methods, on purpose: each
one makes exactly the ``read`` call its callers used to make by hand, so every
caller - and every test double that fakes PVE at ``read(path)``, which is the
wire - behaves exactly as before. Each returns PVE's payload untouched (the
``{"data": ...}`` wrapper included); unwrapping stays with the caller that
knows what shape it expects.
"""

from __future__ import annotations

from typing import Any

# The collections the read-only passthrough tool (MCP ``proxmox_api_read``) may
# forward a caller's path into: its scope, kept here with every other PVE path.
READ_PASSTHROUGH_PREFIXES: tuple[str, ...] = (
    "/nodes/",
    "/storage/",
    "/access/",
    "/cluster/",
    "/pools/",
    "/version",
    "/api2/json/version",
)


async def version(proxmox: Any) -> Any:
    """``GET /version``: the cheapest authenticated read - a connection test."""
    return await proxmox.read("/version")


async def nodes(proxmox: Any) -> Any:
    """``GET /nodes``: every node of the cluster with its online status."""
    return await proxmox.read("/nodes")


async def cluster_guests(proxmox: Any) -> Any:
    """``GET /cluster/resources?type=vm``: every VM, container and template, cluster-wide."""
    return await proxmox.read("/cluster/resources", {"type": "vm"})


async def vmid_free(proxmox: Any, vmid: int) -> Any:
    """``GET /cluster/nextid?vmid=N``: answers N when N is free, raises when taken."""
    return await proxmox.read("/cluster/nextid", query={"vmid": vmid})


async def pools(proxmox: Any) -> Any:
    """``GET /pools``: the pools this token can see."""
    return await proxmox.read("/pools")


async def node_storage(proxmox: Any, node: str) -> Any:
    """``GET /nodes/{node}/storage``: the storages usable on one node, with content types."""
    return await proxmox.read(f"/nodes/{node}/storage")


async def node_network(proxmox: Any, node: str, query: dict[str, Any] | None = None) -> Any:
    """``GET /nodes/{node}/network``: one node's interfaces (``type=any_bridge`` for bridges)."""
    path = f"/nodes/{node}/network"
    return await proxmox.read(path, query) if query else await proxmox.read(path)


async def node_guests(proxmox: Any, node: str, guest_type: str) -> Any:
    """``GET /nodes/{node}/{qemu|lxc}``: one node's VMs or containers."""
    return await proxmox.read(f"/nodes/{node}/{guest_type}")


async def guest_config(
    proxmox: Any, node: str, guest_type: str, vmid: int | str, query: dict[str, Any] | None = None
) -> Any:
    """``GET /nodes/{node}/{type}/{vmid}/config`` (``current=1``: what is attached now)."""
    path = f"/nodes/{node}/{guest_type}/{vmid}/config"
    return await proxmox.read(path, query) if query else await proxmox.read(path)


async def guest_pending(proxmox: Any, node: str, guest_type: str, vmid: int | str) -> Any:
    """``GET /nodes/{node}/{type}/{vmid}/pending``: config keys with their pending values."""
    return await proxmox.read(f"/nodes/{node}/{guest_type}/{vmid}/pending")


async def guest_agent_interfaces(proxmox: Any, node: str, vmid: int | str) -> Any:
    """``GET /nodes/{node}/qemu/{vmid}/agent/network-get-interfaces`` (raises if no agent)."""
    return await proxmox.read(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")


__all__ = [
    "READ_PASSTHROUGH_PREFIXES",
    "cluster_guests",
    "guest_agent_interfaces",
    "guest_config",
    "guest_pending",
    "node_guests",
    "node_network",
    "node_storage",
    "nodes",
    "pools",
    "version",
    "vmid_free",
]
