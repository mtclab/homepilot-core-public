from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import httpx

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.adapters.ssh import SSHAdapter
from homepilot.db.repository import Repository
from homepilot.db.utils import escape_like

logger = logging.getLogger(__name__)


async def _guess_ip(hostname: str) -> str | None:
    """Resolve hostname to IP via DNS. Returns None on failure."""
    try:
        loop = asyncio.get_event_loop()
        ip = await loop.run_in_executor(None, socket.gethostbyname, hostname)
        return ip
    except socket.gaierror as exc:
        logger.debug("DNS resolution failed for %s: %s", hostname, exc)
        return None


async def verify_connectivity(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    """Check if TCP port is reachable. Returns False on any failure."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (ConnectionError, OSError, TimeoutError) as exc:
        logger.debug("TCP connectivity check failed for %s:%d: %s", host, port, exc)
        return False


class InventoryService:
    def __init__(
        self,
        repo: Repository,
        proxmox: ProxmoxClient | None = None,
        ssh: SSHAdapter | None = None,
        kb_service: Any = None,
        proxmox_host: str = "",
    ):
        self.repo = repo
        self.proxmox = proxmox
        self.ssh = ssh
        self.kb_service = kb_service
        self.proxmox_host = proxmox_host

    async def query_inventory(self, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if filter:
            if "managed" in filter:
                clauses.append("managed = ?")
                params.append(int(filter["managed"]))
            if "role" in filter:
                clauses.append("role = ?")
                params.append(filter["role"])
            if "host_type" in filter:
                clauses.append("host_type = ?")
                params.append(filter["host_type"])
            if "status" in filter:
                clauses.append("status = ?")
                params.append(filter["status"])
            if "node" in filter:
                clauses.append("node = ?")
                params.append(filter["node"])
            if "hostname" in filter:
                clauses.append("hostname LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(filter['hostname'])}%")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = (filter or {}).get("limit", 100)
        offset = (filter or {}).get("offset", 0)
        params.extend([limit, offset])

        hosts = await self.repo.db.fetchall(
            f"SELECT * FROM hosts{where} ORDER BY hostname LIMIT ? OFFSET ?",
            params,
        )

        result = []
        for host in hosts:
            host_id = host.get("id")
            services = await self.repo.list_services(host_id=host_id)
            host_dict = dict(host)
            host_dict["services"] = [dict(s) for s in services]
            result.append(host_dict)

        return result

    async def _fetch_node_ip(self, node_name: str) -> str:
        """Try GET /nodes/{node}/network to find the primary non-loopback IP."""
        if self.proxmox is None:
            return ""
        try:
            result = await self.proxmox.read(f"/nodes/{node_name}/network")
            ifaces = result.get("data", result)
            if not isinstance(ifaces, list):
                return ""
            for iface in ifaces:
                addr = str(iface.get("address", ""))
                iface_type = str(iface.get("type", ""))
                if addr and iface_type not in ("loopback",) and not addr.startswith("127."):
                    return addr
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
            logger.debug("Could not fetch network info for node %s: %s", node_name, exc)
        return ""

    async def refresh_inventory(self, scope: str | None = None) -> dict[str, Any]:
        refreshed: dict[str, Any] = {"hosts": 0, "services": 0, "proxmox_host_ids": []}

        if self.proxmox is None:
            return refreshed

        try:
            nodes = await self.proxmox.read("/nodes")
            node_list = nodes.get("data", nodes)
            if not isinstance(node_list, list):
                node_list = []

            for node_info in node_list:
                node_name = node_info.get("node") or node_info.get("name", "")
                if not node_name:
                    continue
                if scope and scope != node_name:
                    continue

                node_ip = (
                    node_info.get("ip", "")
                    or await self._fetch_node_ip(node_name)
                    or await _guess_ip(node_name)
                    or self.proxmox_host
                    or ""
                )
                existing = await self.repo.get_host_by_hostname(node_name)
                data = {
                    "host_type": "node",
                    "role": "node",
                    "ip_address": node_ip,
                }
                if existing:
                    await self.repo.update_host(existing["id"], **data)
                    refreshed["proxmox_host_ids"].append(existing["id"])
                else:
                    new_id = await self.repo.create_host(
                        hostname=node_name,
                        host_type="node",
                        role="node",
                        ip_address=node_ip,
                    )
                    refreshed["proxmox_host_ids"].append(new_id)

                for guest_type in ("qemu", "lxc"):
                    try:
                        guests = await self.proxmox.read(f"/nodes/{node_name}/{guest_type}")
                        guest_list = guests.get("data", guests)
                        if not isinstance(guest_list, list):
                            guest_list = []
                        for guest in guest_list:
                            vmid = guest.get("vmid")
                            g_hostname = guest.get("name", f"{guest_type}-{vmid}")
                            g_status = guest.get("status", "unknown")
                            g_ip = guest.get("ip", "") or await _guess_ip(g_hostname) or ""
                            g_data = {
                                "host_type": guest_type,
                                "role": "guest",
                                "status": g_status,
                                "ip_address": g_ip,
                            }
                            g_existing = await self.repo.get_host_by_proxmox_id(vmid)
                            if g_existing:
                                await self.repo.update_host(g_existing["id"], **g_data)
                                refreshed["proxmox_host_ids"].append(g_existing["id"])
                            else:
                                g_new_id = await self.repo.create_host(
                                    hostname=g_hostname,
                                    host_type=guest_type,
                                    role="guest",
                                    proxmox_id=vmid,
                                    node=node_name,
                                    ip_address=g_ip,
                                )
                                refreshed["proxmox_host_ids"].append(g_new_id)
                            refreshed["hosts"] += 1
                    except Exception as e:
                        logger.warning("Failed to refresh %s on %s: %s", guest_type, node_name, e)

                refreshed["hosts"] += 1

        except Exception as e:
            logger.warning("Failed to refresh proxmox inventory: %s", e)

        return refreshed

    async def get_environment_doc(self, target: str) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "target": target,
            "hosts": [],
            "services": [],
            "kb_entries": [],
            "artifact_history": [],
        }

        hosts = await self.repo.db.fetchall(
            "SELECT * FROM hosts WHERE hostname LIKE ? ESCAPE '\\' OR node LIKE ? ESCAPE '\\'",
            [f"%{escape_like(target)}%", f"%{escape_like(target)}%"],
        )
        for host in hosts:
            host_dict = dict(host)
            host_services = await self.repo.list_services(host_id=host_dict.get("id"))
            host_dict["services"] = [dict(s) for s in host_services]
            doc["hosts"].append(host_dict)
            doc["services"].extend(host_dict["services"])

        if self.kb_service:
            try:
                kb_results = await self.kb_service.search(target, limit=20)
                doc["kb_entries"] = kb_results
            except Exception as e:
                logger.warning("KB search failed for %s: %s", target, e)

        artifacts = await self.repo.db.fetchall(
            "SELECT id, kind, intent, status, created_at FROM artifacts "
            "WHERE target_json LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 20",
            [f"%{escape_like(target)}%"],
        )
        doc["artifact_history"] = [dict(a) for a in artifacts]

        return doc
