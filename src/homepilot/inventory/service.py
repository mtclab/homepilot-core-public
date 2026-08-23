from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from typing import Any

import httpx

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.repository import Repository
from homepilot.db.utils import escape_like

from .introspect import introspect_host

logger = logging.getLogger(__name__)

# ``services.managed_by`` value marking a row as OBSERVED (found by adoption-time
# introspection), never authored intent. Observed rows are namespaced by name
# (``observed:...``) and cleared-then-rewritten on every re-adopt so the record
# is idempotent and can never collide with an operator-authored service.
OBSERVED_MARKER = "observed"

ROLE_PATTERNS = [
    (
        "database",
        re.IGNORECASE,
        [
            "db",
            "database",
            "postgres",
            "mysql",
            "mariadb",
            "redis",
            "mongo",
        ],
    ),
    (
        "web-server",
        re.IGNORECASE,
        [
            "web",
            "nginx",
            "apache",
            "frontend",
            "app",
        ],
    ),
    (
        "api-server",
        re.IGNORECASE,
        [
            "api",
            "backend",
            "service",
        ],
    ),
    (
        "monitoring",
        re.IGNORECASE,
        [
            "monitor",
            "zabbix",
            "prometheus",
            "grafana",
        ],
    ),
    (
        "control-plane",
        re.IGNORECASE,
        [
            "k8s",
            "kube",
            "control-plane",
            "master",
        ],
    ),
    (
        "worker",
        re.IGNORECASE,
        [
            "worker",
            "node",
            "agent",
        ],
    ),
]


def _match_role(hostname: str, description: str | None = None) -> str | None:
    """Match a role from description then hostname, or None if nothing matches.

    Distinct from :func:`_infer_role`, which substitutes ``"guest"`` when there
    is no match. Callers that would OVERWRITE an existing role must use this and
    skip the write on ``None`` - otherwise an unmatched hostname silently
    demotes a real role to ``guest`` (#416).
    """
    text = f"{description or ''} {hostname}"
    for role, flag, keywords in ROLE_PATTERNS:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", text, flag):
                return role
    return None


def _infer_role(hostname: str, description: str | None = None) -> str:
    """Infer role from description then hostname, defaulting to ``"guest"``.

    Correct when CREATING a host (a newly discovered guest with no signal is a
    guest). Wrong when re-enriching an existing host - see :func:`_match_role`.
    """
    return _match_role(hostname, description) or "guest"


def derive_status(pve_status: str, ip: str | None) -> str:
    if pve_status == "stopped":
        return "offline"
    if pve_status == "running" and ip:
        return "online"
    return "unknown"


def node_pve_status(raw_status: str | None) -> str:
    """Map a Proxmox ``/nodes`` node status onto the pve_status vocabulary the
    rest of the inventory uses (``running``/``stopped``/``unknown``).

    Proxmox reports node liveness as ``online``/``offline``. Only a confirmed
    ``online`` node is recorded as ``running`` — an ``offline`` node maps to
    ``stopped`` (so ``derive_status`` yields ``offline``) and any absent or
    unrecognized value maps to ``unknown``. A node whose liveness is not
    confirmed online must never be stored as running.
    """
    normalized = (raw_status or "").strip().lower()
    if normalized == "online":
        return "running"
    if normalized == "offline":
        return "stopped"
    return "unknown"


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


# Sources that Proxmox is the authority for. A host an operator added by hand is
# not one of them: a sync that never looked for it must never declare it gone.
_HYPERVISOR_SOURCES: tuple[str, ...] = ("discovered", "hp_created", "imported")


class InventoryService:
    def __init__(
        self,
        repo: Repository,
        proxmox: ProxmoxClient | None = None,
        kb_service: Any = None,
        proxmox_host: str = "",
    ):
        self.repo = repo
        self.proxmox = proxmox
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

    async def _get_guest_description(
        self, node_name: str, guest_type: str, vmid: int
    ) -> str | None:
        if self.proxmox is None:
            return None
        try:
            result = await self.proxmox.read(f"/nodes/{node_name}/{guest_type}/{vmid}/config")
            data = result.get("data", result)
            if isinstance(data, dict):
                return data.get("description") or None
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
            logger.debug(
                "Could not fetch config for %s/%s/%s: %s",
                node_name,
                guest_type,
                vmid,
                exc,
            )
        return None

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

    async def _try_qemu_agent_ip(
        self, node_name: str, vmid: int, timeout: float = 3.0
    ) -> str | None:
        if self.proxmox is None:
            return None
        try:
            result = await asyncio.wait_for(
                self.proxmox.read(f"/nodes/{node_name}/qemu/{vmid}/agent/network-get-interfaces"),
                timeout=timeout,
            )
            data = result.get("data", result)
            if isinstance(data, dict):
                ifaces = data.get("result", data)
                if isinstance(ifaces, list):
                    for iface in ifaces:
                        if iface.get("name") == "lo":
                            continue
                        addrs = iface.get("ip-addresses", [])
                        for addr in addrs:
                            ip = str(addr.get("ip-address", ""))
                            if ip and not ip.startswith("127.") and ":" not in ip:
                                return ip
        except (
            TimeoutError,
            httpx.HTTPStatusError,
            httpx.ConnectError,
        ) as exc:
            logger.debug("QEMU agent IP failed for %s/%s: %s", node_name, vmid, exc)
        return None

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
                # Observe the node's real liveness from /nodes rather than
                # assuming every node is up: an offline/unknown node must not be
                # recorded as running.
                node_status = node_pve_status(node_info.get("status"))
                data = {
                    "host_type": "node",
                    "role": "node",
                    "ip_address": node_ip,
                    "pve_status": node_status,
                    "status": derive_status(node_status, node_ip),
                    "source": "hp_created",
                    # NOT "user": this is a sync deciding, and stamping an
                    # operator's provenance over its own guess is the forgery
                    # #424 names - it defeated #416 and hid the "inferred" badge.
                    # (`role_source` is constrained to inferred/user/artifact.)
                    "role_source": "inferred",
                    "ip_source": "pve",
                }
                if existing:
                    await self.repo.update_host_from_automation(existing["id"], **data)
                    refreshed["proxmox_host_ids"].append(existing["id"])
                else:
                    new_id = await self.repo.create_host(
                        hostname=node_name,
                        host_type="node",
                        role="node",
                        ip_address=node_ip,
                        pve_status=node_status,
                        status=derive_status(node_status, node_ip),
                        source="hp_created",
                        role_source="inferred",
                        ip_source="pve",
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
                            g_description = await self._get_guest_description(
                                node_name, guest_type, vmid
                            )
                            g_existing = await self.repo.get_host_by_proxmox_id(vmid)
                            if g_existing:
                                data = {
                                    "host_type": guest_type,
                                    "pve_status": g_status,
                                    "status": derive_status(g_status, g_existing.get("ip_address")),
                                    "description": (
                                        g_description or g_existing.get("description") or None
                                    ),
                                }
                                src = g_existing.get("source")
                                imp = g_existing.get("import_state")
                                if src == "discovered" and not imp:
                                    data["import_state"] = "pending"
                                await self.repo.update_host_from_automation(
                                    g_existing["id"], **data
                                )
                                refreshed["proxmox_host_ids"].append(g_existing["id"])
                            else:
                                g_new_id = await self.repo.create_host(
                                    hostname=g_hostname,
                                    host_type=guest_type,
                                    role="guest",
                                    proxmox_id=vmid,
                                    node=node_name,
                                    pve_status=g_status,
                                    status=derive_status(g_status, None),
                                    source="discovered",
                                    description=g_description,
                                    import_state="pending",
                                    role_source="inferred",
                                    ip_source=None,
                                )
                                refreshed["proxmox_host_ids"].append(g_new_id)
                            refreshed["hosts"] += 1
                    except Exception as e:
                        logger.warning("Failed to refresh %s on %s: %s", guest_type, node_name, e)

                refreshed["hosts"] += 1

            # Everything Proxmox still reports has now been seen; anything
            # hypervisor-derived that was NOT is gone from the hypervisor (#445
            # A5). Only done for a FULL refresh: a scoped sync looks at one node,
            # so every guest on every other node would look absent.
            if scope is None:
                refreshed["absent"] = await self.repo.mark_hosts_absent(
                    set(refreshed["proxmox_host_ids"]), _HYPERVISOR_SOURCES
                )
        except Exception as e:
            logger.warning("Failed to refresh proxmox inventory: %s", e)

        return refreshed

    async def enrich_inventory(
        self, host_ids: list[str] | None = None, scope: str | None = None
    ) -> dict[str, int]:
        result = {"enriched": 0, "failed": 0, "skipped": 0}
        where_clauses: list[str] = []
        params: list[Any] = []
        if host_ids:
            placeholders = ", ".join("?" for _ in host_ids)
            where_clauses.append(f"id IN ({placeholders})")
            params.extend(host_ids)
        if scope:
            where_clauses.append("node = ?")
            params.append(scope)

        if not self.proxmox:
            return result

        try:
            rows = await self.repo.db.fetchall(
                (
                    f"SELECT * FROM hosts"
                    f"{' WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''}"
                ),
                params,
            )
        except Exception as exc:
            logger.warning("Failed to fetch hosts for enrichment: %s", exc)
            return result

        for row in rows:
            host = dict(row)
            try:
                await self._enrich_single_host(host)
                result["enriched"] += 1
            except Exception as exc:
                logger.debug("Enrichment failed for %s: %s", host.get("id"), exc)
                result["failed"] += 1

        return result

    async def _enrich_single_host(self, host: dict[str, Any]) -> None:
        host_id = host["id"]
        pve_status = host.get("pve_status") or "unknown"
        role_source = host.get("role_source") or "inferred"
        ip_source = host.get("ip_source") or None

        updates: dict[str, Any] = {}

        # Role inference. Only overwrite when a pattern actually MATCHES - an
        # unmatched hostname must leave the stored role alone. Previously this
        # wrote `_infer_role(...)`, whose no-match fallback is "guest", so every
        # enrich cycle demoted any non-matching host to guest and destroyed roles
        # set by paths that did not stamp role_source="user" (#416).
        if role_source == "inferred":
            matched = _match_role(host.get("hostname", ""), host.get("description") or None)
            if matched is not None:
                updates["role"] = matched
                updates["role_source"] = "inferred"

        # IP enrichment
        current_ip = host.get("ip_address") or None
        new_ip: str | None = None
        ip_source_val: str | None = None

        if ip_source != "user":
            node = host.get("node")
            vmid = host.get("proxmox_id")
            host_type = host.get("host_type")
            if node and vmid and host_type == "qemu":
                new_ip = await self._try_qemu_agent_ip(node, int(vmid))
                if new_ip:
                    ip_source_val = "pve"
            if not new_ip:
                new_ip = await _guess_ip(host.get("hostname", ""))
                if new_ip:
                    ip_source_val = "dns"
            if new_ip:
                updates["ip_address"] = new_ip
                updates["ip_source"] = ip_source_val

        # Status derivation
        derived = derive_status(pve_status, updates.get("ip_address") or current_ip)
        updates["status"] = derived

        if updates:
            # Through the one door: enrich used to re-derive `status` over
            # anything an operator had set with PATCH, which made that PATCH
            # field a lie (#424).
            skipped = await self.repo.update_host_from_automation(host_id, **updates)
            if skipped:
                logger.debug(
                    "enrich left operator-set fields alone on %s: %s", host_id, ", ".join(skipped)
                )

    async def introspect_and_record(self, host: dict[str, Any], adapter: Any) -> dict[str, Any]:
        """Run best-effort read-only introspection of an adopted host and record
        the OBSERVED state (services rows + an as-found KB note).

        Descriptive only: nothing recorded here is authored intent and nothing is
        ever re-applied. Idempotent — re-adopting clears and rewrites the observed
        rows/note rather than duplicating them. Never creates an artifact.
        """
        hostname = host.get("hostname") or host.get("id", "")
        result = await introspect_host(hostname, adapter)
        if not result.skipped:
            await self._persist_observation(host, result)
        return result.summary()

    async def _persist_observation(self, host: dict[str, Any], result: Any) -> None:
        host_id = host["id"]
        hostname = host.get("hostname") or host_id

        # Idempotence: drop any prior observed rows for this host before writing
        # the fresh snapshot. Only OBSERVED rows are touched — operator-authored
        # services are left untouched.
        existing = await self.repo.list_services(host_id=host_id)
        for svc in existing:
            if svc.get("managed_by") == OBSERVED_MARKER:
                await self.repo.delete_service(svc["id"])

        # Build the observed service rows, namespaced + de-duplicated by name so
        # the UNIQUE(host_id, name) constraint can never be violated.
        seen: set[str] = set()
        for port in result.listening_ports:
            name = f"observed:listen:{port.get('local', '')}"
            if name in seen:
                continue
            seen.add(name)
            await self.repo.create_service(
                host_id=host_id,
                name=name,
                runtime="socket",
                status="unknown",
                managed_by=OBSERVED_MARKER,
                config=json.dumps({"observed": True, "kind": "listening_port", **port}),
            )
        for container in result.docker_containers:
            name = f"observed:container:{container.get('name', '')}"
            if name in seen:
                continue
            seen.add(name)
            await self.repo.create_service(
                host_id=host_id,
                name=name,
                runtime="docker",
                version=container.get("image") or None,
                status="unknown",
                managed_by=OBSERVED_MARKER,
                config=json.dumps({"observed": True, "kind": "container", **container}),
            )

        # As-found KB note. Written directly to doc_metadata (NOT via the KB
        # artifact lifecycle) so introspection never produces an artifact. Keyed
        # by host so re-adoption replaces the prior note instead of duplicating.
        source = f"introspect:{host_id}"
        prior = await self.repo.search_docs_by_source(source)
        for doc in prior:
            await self.repo.delete_doc_metadata(doc["id"])
        await self.repo.create_doc_metadata(
            source=source,
            title=f"As-found observation: {hostname}",
            content=result.as_found_note(),
            kind="observed-state",
            target=hostname,
        )

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
