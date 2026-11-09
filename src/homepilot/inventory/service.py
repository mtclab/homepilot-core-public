from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import sqlite3
from typing import Any

import httpx

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.repository import Repository
from homepilot.db.utils import escape_like
from homepilot.runtime.blocking import run_blocking

from .addresses import (
    GUEST_CONFIG_SOURCES,
    SOURCE_GUEST_AGENT,
    SOURCE_PVE_NODE,
    AddressGuard,
    Holder,
    NicAddress,
    find_conflicts,
    normalise_address,
    parse_agent_interfaces,
    parse_guest_config,
)
from .fleet_model import env_update, guest_facts, proxmox_console_url, proxmox_ui_url
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
        return await run_blocking("net", socket.gethostbyname, hostname)
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


# The target fields that NAME a machine, service or network. `kind` and
# numeric ids are not names and never match a target string.
_TARGET_NAME_FIELDS = ("host", "hostname", "node", "service", "network", "name")


def _target_names(target_json: str | None, target: str) -> bool:
    """Whether an artifact's target names ``target`` as a whole (case-insensitive)."""
    if not target_json:
        return False
    try:
        parsed = json.loads(target_json)
    except ValueError:
        return False
    wanted = target.strip().lower()
    targets = parsed if isinstance(parsed, list) else [parsed]
    for t in targets:
        if not isinstance(t, dict):
            continue
        for field in _TARGET_NAME_FIELDS:
            value = t.get(field)
            if isinstance(value, str) and value.strip().lower() == wanted:
                return True
    return False


def _vmid_key(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def same_guest(row: dict[str, Any], pve_name: Any) -> bool:
    """Is the row for this VMID the machine PVE now reports under it? (#706)

    Identity is the VMID AND the guest's name. The name is what HomePilot gave a
    guest it provisioned and what it recorded for one it discovered, so a
    different name at the same number is a different machine - most often a
    VMID PVE reused. (A guest renamed in PVE reads as a new machine too; that
    errs towards nobody being handed a machine that is not theirs.)
    """
    recorded = str(row.get("hostname") or "").strip().lower()
    reported = str(pve_name or "").strip().lower()
    return bool(recorded) and recorded == reported


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

    async def get_host_detail(self, host_id: str) -> dict[str, Any] | None:
        """One host with its services and its agent, or None if there is no such host.

        The single implementation behind GET /inventory/{host_id} and the
        `get_host` MCP tool. The agent block is part of the host (#514 S2): the
        answer to "is the channel live, which version, and why was it last
        refused" must not require a second lookup - or a second, divergent copy
        of this join.
        """
        import contextlib as _contextlib

        host = await self.repo.get_host(host_id)
        if host is None:
            return None
        host_dict = dict(host)
        services = await self.repo.list_services(host_id=host_id)
        host_dict["services"] = [dict(s) for s in services]
        # Here, not in the route, so the MCP `get_host` tool carries it too.
        host_dict["proxmox_url"] = proxmox_ui_url(self.proxmox, host_dict)
        host_dict["console_url"] = proxmox_console_url(self.proxmox, host_dict)
        # Every address it holds, with provenance and age, and every clash it
        # is in (lane B2) - the same answer as its Network tab.
        network = await self.host_network(host_id)
        host_dict["addresses"] = network["addresses"]
        host_dict["address_conflicts"] = network["conflicts"]
        if host_dict.get("agent_id"):
            agent = await self.repo.db.fetchone(
                "SELECT agent_id, hostname, connected, first_seen, connected_at, "
                "       last_heartbeat, disconnected_at, last_error, last_error_at, "
                "       system_info, credential_set_at, revoked_at "
                "FROM agents WHERE agent_id = ?",
                (host_dict["agent_id"],),
            )
            if agent is not None:
                info: dict[str, Any] = {}
                with _contextlib.suppress(ValueError, TypeError):
                    info = json.loads(agent["system_info"] or "{}")
                # Decided HERE, not in the page: the same comparison feeds the
                # self-check, and three hand-rolled copies of one rule is how
                # #631 happened. `None` means the version could not be read,
                # which is not the same as up to date (#648 tranche-1 follow-up).
                from homepilot.agent_hub.version_skew import control_plane_version, is_behind

                host_dict["agent"] = {
                    "agent_id": agent["agent_id"],
                    "connected": bool(agent["connected"]),
                    "version": info.get("agent_version"),
                    "control_version": control_plane_version(),
                    "behind": is_behind(info.get("agent_version"), control_plane_version()),
                    "arch": info.get("arch"),
                    "runtime": info.get("runtime"),
                    "first_seen": agent["first_seen"],
                    "connected_at": agent["connected_at"],
                    "last_heartbeat": agent["last_heartbeat"],
                    "disconnected_at": agent["disconnected_at"],
                    "last_error": agent["last_error"],
                    "last_error_at": agent["last_error_at"],
                    "credential_set_at": agent["credential_set_at"],
                    "revoked_at": agent["revoked_at"],
                }
        return host_dict

    async def host_network(self, host_id: str) -> dict[str, Any]:
        """One host's addresses and the clashes it is in (lane B2).

        The single implementation behind GET /inventory/{id}/network (the
        Network tab) and the ``addresses`` / ``address_conflicts`` that
        ``get_host`` carries, so the console and the assistant cannot disagree.
        """
        conflicts = find_conflicts(await self.repo.list_address_rows())
        return {
            "host_id": host_id,
            "addresses": await self.repo.list_host_addresses(host_id),
            "conflicts": [c.as_dict() for c in conflicts if host_id in c.host_ids],
        }

    async def _get_guest_config(
        self, node_name: str, guest_type: str, vmid: int
    ) -> dict[str, Any] | None:
        """A guest's PVE config, or None when it could not be read.

        Read once per guest per sweep: the description and the configured
        addresses (lane B2) both come out of it.
        """
        if self.proxmox is None:
            return None
        try:
            result = await self.proxmox.read(f"/nodes/{node_name}/{guest_type}/{vmid}/config")
            data = result.get("data", result)
            if isinstance(data, dict):
                return data
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
            logger.debug(
                "Could not fetch config for %s/%s/%s: %s",
                node_name,
                guest_type,
                vmid,
                exc,
            )
        return None

    async def _read_node_network(self, node_name: str) -> list[dict[str, Any]] | None:
        """GET /nodes/{node}/network, or None when it could not be read."""
        if self.proxmox is None:
            return None
        try:
            result = await self.proxmox.read(f"/nodes/{node_name}/network")
            ifaces = result.get("data", result)
            return ifaces if isinstance(ifaces, list) else None
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
            logger.debug("Could not fetch network info for node %s: %s", node_name, exc)
        return None

    @staticmethod
    def _first_node_address(ifaces: list[dict[str, Any]] | None) -> str:
        for iface in ifaces or []:
            addr = str(iface.get("address", ""))
            iface_type = str(iface.get("type", ""))
            if addr and iface_type not in ("loopback",) and not addr.startswith("127."):
                return addr
        return ""

    async def _fetch_node_ip(self, node_name: str) -> str:
        """Try GET /nodes/{node}/network to find the primary non-loopback IP."""
        return self._first_node_address(await self._read_node_network(node_name))

    @staticmethod
    def _node_addresses(ifaces: list[dict[str, Any]]) -> list[NicAddress]:
        """Every address the node's own interfaces carry, with the bridge it is on.

        A guest configured with a node's address takes the hypervisor off the
        LAN when it starts, so node addresses are claims like any other.
        """
        out: list[NicAddress] = []
        for iface in ifaces:
            iface_type = str(iface.get("type", ""))
            if iface_type in ("loopback",):
                continue
            name = str(iface.get("iface") or "")
            for key, cidr_key in (("address", "cidr"), ("address6", "cidr6")):
                address = normalise_address(iface.get(key))
                if address is None:
                    continue
                cidr = str(iface.get(cidr_key) or "")
                prefix = cidr.split("/", 1)[1] if "/" in cidr else ""
                out.append(
                    NicAddress(
                        address=address,
                        prefixlen=int(prefix) if prefix.isdigit() else None,
                        iface=name,
                        # The node sits on the wire its bridge is; a plain
                        # NIC's address is on no bridge a guest can join.
                        bridge=name if iface_type == "bridge" else None,
                        source=SOURCE_PVE_NODE,
                    )
                )
        return out

    async def _qemu_agent_interfaces(self, node_name: str, vmid: int, timeout: float = 3.0) -> Any:
        """The guest agent's raw interface list, or None when it did not answer."""
        if self.proxmox is None:
            return None
        try:
            return await asyncio.wait_for(
                self.proxmox.read(f"/nodes/{node_name}/qemu/{vmid}/agent/network-get-interfaces"),
                timeout=timeout,
            )
        except (
            TimeoutError,
            httpx.HTTPStatusError,
            httpx.ConnectError,
        ) as exc:
            logger.debug("QEMU agent IP failed for %s/%s: %s", node_name, vmid, exc)
        return None

    async def _cluster_guest_resources(
        self,
    ) -> tuple[dict[tuple[str, int], dict[str, Any]], bool]:
        """Every guest's /cluster/resources row by (type, vmid), and whether the read worked."""
        if self.proxmox is None:
            return {}, False
        try:
            result = await self.proxmox.read("/cluster/resources", {"type": "vm"})
        except Exception as exc:
            # Logged and carried on: the facts it holds are additive.
            logger.warning("Could not read /cluster/resources for guest facts: %s", exc)
            return {}, False
        rows = result.get("data", result) if isinstance(result, dict) else result
        if not isinstance(rows, list):
            logger.warning("Could not read /cluster/resources for guest facts: unexpected shape")
            return {}, False
        out: dict[tuple[str, int], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _vmid_key(row.get("vmid"))
            if key is None:
                continue
            out[(str(row.get("type") or ""), key)] = row
        if not out:
            # An empty answer from a cluster whose node listings carry guests
            # is not "no guest is in a pool"; it is an answer that says nothing.
            return {}, False
        return out, True

    @staticmethod
    def _first_agent_ipv4(result: Any) -> str | None:
        data = result.get("data", result) if isinstance(result, dict) else None
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
        return None

    async def _record_agent_addresses(self, host: dict[str, Any], result: Any) -> None:
        """File what a guest agent reported as observed addresses (lane B2).

        The config is re-read for its NIC MACs, which is how an observed
        address learns which bridge it sits on. Best effort: a failure here
        leaves the previous observation, whose age says how old it is.
        """
        node = str(host.get("node") or "")
        vmid = int(host["proxmox_id"])
        config = await self._get_guest_config(node, "qemu", vmid)
        observed = parse_agent_interfaces(result, config)
        await self.repo.replace_host_addresses(str(host["id"]), (SOURCE_GUEST_AGENT,), observed)

    async def address_conflicts_for_start(
        self, host: dict[str, Any] | str, addresses: list[NicAddress] | None = None
    ) -> list[Holder]:
        """The RUNNING machines holding an address this guest would claim on start.

        The precondition the power controls (#781) call before a Start, or a
        Reboot that applies a pending ``ipconfig``: an empty list means nobody's
        address is taken. ``host`` is a host row or its id. Raises
        ``AddressCheckError`` when it cannot be established - refuse then.
        See ``addresses.AddressGuard``.
        """
        from .addresses import AddressCheckError

        row = await self.repo.get_host(host) if isinstance(host, str) else host
        if row is None:
            raise AddressCheckError(f"no host {host!r} in inventory")
        return await AddressGuard(self.repo, self.proxmox).address_conflicts_for_start(
            row, addresses
        )

    async def refresh_inventory(self, scope: str | None = None) -> dict[str, Any]:
        # `complete` starts FALSE and is earned. Callers subtract this sweep's
        # ids from what they already have to decide what is GONE, so a partial
        # answer that looks like a whole one makes them declare live machines
        # absent. Defaulting to False is the same fail-safe direction
        # `VerifyResult.state` takes: a path that forgets to establish
        # completeness reads as "not established" (#648 tranche 8).
        refreshed: dict[str, Any] = {
            "hosts": 0,
            "services": 0,
            "proxmox_host_ids": [],
            "complete": False,
        }
        # Did every listing this sweep depends on actually SUCCEED? The absent
        # sweep below deletes state on the strength of "I saw everything the
        # hypervisor reports", and that sentence is only true if nothing was
        # swallowed on the way. One node read failing - which is what a node
        # does while it reboots - used to be enough to mark every live guest on
        # it absent (#642).
        listings_complete = True
        # Rows retired this sweep because their VMID now belongs to another
        # machine (#706). They went absent THIS cycle just as surely as the ones
        # the absent sweep marks, but the sweep never sees them - they already
        # carry absent_since by the time it runs - so they are counted here.
        retired_ids: list[str] = []
        # "full" (and an empty string) is the word the MCP schema advertises for
        # "everything"; as a node name it would silently match nothing (#692).
        if scope is not None and scope.strip().lower() in ("", "full"):
            scope = None
        matched_scope = False

        if self.proxmox is None:
            # Nothing was asked, so nothing was established. Reported complete,
            # this told the reconciler that Proxmox reports NO hosts - and every
            # host in an agent-only install was written into the journal as
            # absent, every cycle.
            refreshed["error"] = "no Proxmox is configured, so no hypervisor was read"
            return refreshed

        try:
            # Template flag, pool, PVE tags and sizes for every guest, in the
            # one read Proxmox answers them all from (lane B1). Additive: a
            # failure here costs those facts for one cycle and nothing else -
            # it is not a listing the absent sweep depends on.
            resources, resources_read = await self._cluster_guest_resources()
            nodes = await self.proxmox.read("/nodes")
            node_list = nodes.get("data", nodes)
            if not isinstance(node_list, list):
                # An answer we cannot read is not "a cluster with no nodes".
                # Falling through with an empty list would take the sweep below
                # past an empty seen-set and mark the ENTIRE inventory absent
                # (#642).
                logger.warning("Could not read the cluster's node list: unexpected shape")
                listings_complete = False
                node_list = []

            for node_info in node_list:
                node_name = node_info.get("node") or node_info.get("name", "")
                if not node_name:
                    continue
                if scope and scope != node_name:
                    continue
                matched_scope = True

                # Read once: the primary IP and every address (lane B2) both
                # come out of the node's interface list.
                node_network = await self._read_node_network(node_name)
                node_ip = (
                    node_info.get("ip", "")
                    or self._first_node_address(node_network)
                    or await _guess_ip(node_name)
                    or self.proxmox_host
                    or ""
                )
                node_addresses = self._node_addresses(node_network or [])
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
                    node_row_id = str(existing["id"])
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
                    node_row_id = str(new_id)
                if node_network is not None:
                    # Only when the network read answered: an unanswered read
                    # keeps the last known set rather than erasing it.
                    await self.repo.replace_host_addresses(
                        node_row_id, (SOURCE_PVE_NODE,), node_addresses
                    )

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
                            g_key = _vmid_key(vmid)
                            g_facts = guest_facts(
                                guest,
                                resources.get((guest_type, g_key)) if g_key is not None else None,
                                resources_read,
                            )
                            g_config = await self._get_guest_config(node_name, guest_type, vmid)
                            g_description = (g_config or {}).get("description") or None
                            # The row that IS this machine: the one with its
                            # name, whatever order the rows of earlier
                            # occupants sort in. Failing that, the newest row
                            # still claiming the VMID is somebody else's.
                            g_rows = await self.repo.list_hosts_by_proxmox_id(vmid)
                            g_existing = next(
                                (r for r in g_rows if same_guest(r, g_hostname)), None
                            ) or next((r for r in g_rows if not r.get("absent_since")), None)
                            if g_existing and not same_guest(g_existing, g_hostname):
                                # A VMID is not an identity (#706). PVE hands
                                # a destroyed guest's number to the next VM
                                # created ANYWHERE - the operator's own
                                # included - and matching on the number alone
                                # folded that machine into the previous
                                # occupant's row: a friend's portal listed the
                                # operator's VM as theirs, could power it, and
                                # counted it in their quota. A different name
                                # at the same VMID is a different machine: the
                                # old row is retired (absent, never updated
                                # again) and the new one is discovered fresh.
                                await self.repo.retire_host(g_existing["id"])
                                retired_ids.append(str(g_existing["id"]))
                                logger.warning(
                                    "vmid %s is now %r, not %r: retired the old row and "
                                    "recorded the new occupant as discovered",
                                    vmid,
                                    g_hostname,
                                    g_existing.get("hostname"),
                                )
                                g_existing = None
                            if g_existing:
                                data = {
                                    "host_type": guest_type,
                                    # Where the guest IS now: a migrated guest
                                    # kept its old node, so every node-addressed
                                    # call went to the wrong node (#706).
                                    "node": node_name,
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
                                data.update(g_facts)
                                data.update(
                                    env_update(
                                        g_existing,
                                        g_facts,
                                        self.repo._pinned_fields(g_existing),
                                    )
                                )
                                await self.repo.update_host_from_automation(
                                    g_existing["id"], **data
                                )
                                refreshed["proxmox_host_ids"].append(g_existing["id"])
                                g_row_id = str(g_existing["id"])
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
                                await self.repo.update_host_from_automation(
                                    g_new_id, **g_facts, **env_update(None, g_facts, set())
                                )
                                refreshed["proxmox_host_ids"].append(g_new_id)
                                g_row_id = str(g_new_id)
                            if g_config is not None:
                                # What the guest's config claims, replaced as
                                # one set (lane B2). A template's ipconfig is a
                                # provisioning input, never a held address:
                                # it cannot run, so it records none.
                                # The template flag is the config's own, or
                                # the one the sync read for this guest (lane
                                # B1) - either is enough.
                                claims = (
                                    []
                                    if g_config.get("template") or g_facts.get("is_template")
                                    else parse_guest_config(g_config, guest_type)
                                )
                                await self.repo.replace_host_addresses(
                                    g_row_id, GUEST_CONFIG_SOURCES, claims
                                )
                            refreshed["hosts"] += 1
                    except Exception as e:
                        logger.warning("Failed to refresh %s on %s: %s", guest_type, node_name, e)
                        # The guests on this node are now UNSEEN, not gone.
                        listings_complete = False

                refreshed["hosts"] += 1

            # Everything Proxmox still reports has now been seen; anything
            # hypervisor-derived that was NOT is gone from the hypervisor (#445
            # A5). Only done for a FULL refresh: a scoped sync looks at one node,
            # so every guest on every other node would look absent.
            #
            # And only when every listing SUCCEEDED. The sweep is a write - it
            # stamps absent_since and blanks status/pve_status - so it may only
            # run off a complete picture. A swallowed listing failure left the
            # guests on that node out of proxmox_host_ids while the sweep ran
            # anyway, declaring live machines gone on a timer with no operator
            # action (#642). The scoped case above is the same hazard, already
            # guarded; this is the other half of it.
            if scope is None and listings_complete:
                marked = await self.repo.mark_hosts_absent_ids(
                    set(refreshed["proxmox_host_ids"]), _HYPERVISOR_SOURCES
                )
                # WHICH ones, so the journal can name the hosts that actually
                # went absent this cycle rather than every host the hypervisor
                # does not report - a manual host, a long-gone one (#712) - and
                # the occupants retired by a reused VMID among them (#706).
                went_absent = list(dict.fromkeys([*marked, *retired_ids]))
                refreshed["absent"] = len(went_absent)
                refreshed["absent_host_ids"] = went_absent
            elif scope is None:
                refreshed["absent_skipped"] = (
                    "a guest listing failed; nothing marked absent by the sweep"
                )
                logger.warning(
                    "Skipping the absent sweep: a guest listing failed, so the set of "
                    "guests actually seen is incomplete"
                )
            if retired_ids and "absent_host_ids" not in refreshed:
                # A retirement is a write that already happened, whatever the
                # sweep decided: say so on every path, not only a full one.
                refreshed["absent"] = len(retired_ids)
                refreshed["absent_host_ids"] = list(retired_ids)
            # A scoped sweep deliberately looks at one node, so it is complete
            # for what it promised; a full one is complete only if every listing
            # answered.
            refreshed["complete"] = listings_complete
            if scope and not matched_scope and listings_complete:
                # A scope that names no node refreshed NOTHING. Answering that
                # as a complete, successful sweep is the #692 lie in another
                # shape: the caller believes the inventory was re-pulled.
                refreshed["complete"] = False
                refreshed["error"] = f"no Proxmox node named {scope!r}"
        except Exception as e:
            # The sweep stopped part-way. `proxmox_host_ids` now holds whatever
            # was reached before the failure, which is not "everything the
            # hypervisor reports" - and it was returned looking exactly like a
            # finished sweep, so the caller synced "successfully" and the
            # reconciler wrote the rest of the fleet into the journal as absent.
            logger.warning("Failed to refresh proxmox inventory: %s", e)
            refreshed["error"] = str(e)[:500]

        return refreshed

    async def enrich_inventory(
        self, host_ids: list[str] | None = None, scope: str | None = None
    ) -> dict[str, int]:
        result = {"enriched": 0, "failed": 0, "skipped": 0, "unchanged": 0}
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
                # "Enriched" used to mean "looked at": `status` was rewritten
                # unconditionally, so `updates` was never empty and every host
                # examined was counted as enriched. The reconciler then wrote
                # `hosts_enriched: N` into the journal for a cycle that changed
                # nothing (#648 tranche 8).
                if await self._enrich_single_host(host):
                    result["enriched"] += 1
                else:
                    result["unchanged"] += 1
            except Exception as exc:
                logger.debug("Enrichment failed for %s: %s", host.get("id"), exc)
                result["failed"] += 1

        return result

    async def _enrich_single_host(self, host: dict[str, Any]) -> bool:
        """Enrich one host. Returns whether anything actually changed."""
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
            if matched is not None and matched != host.get("role"):
                updates["role"] = matched
                updates["role_source"] = "inferred"

        # IP enrichment
        current_ip = host.get("ip_address") or None
        new_ip: str | None = None
        ip_source_val: str | None = None

        node = host.get("node")
        vmid = host.get("proxmox_id")
        host_type = host.get("host_type")
        # The guest agent is asked once and answers two questions: the
        # primary IP (only when the operator has not pinned one) and every
        # address the guest holds right now (lane B2, always - a pinned IP
        # does not stop the guest holding others).
        agent_result: Any = None
        if (
            node
            and vmid
            and host_type == "qemu"
            and (ip_source != "user" or pve_status == "running")
        ):
            agent_result = await self._qemu_agent_interfaces(node, int(vmid))
            if agent_result is not None:
                try:
                    await self._record_agent_addresses(host, agent_result)
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    logger.debug("could not record agent addresses for %s: %s", host_id, exc)

        if ip_source != "user":
            if agent_result is not None:
                new_ip = self._first_agent_ipv4(agent_result)
                if new_ip:
                    ip_source_val = "pve"
            if not new_ip:
                new_ip = await _guess_ip(host.get("hostname", ""))
                if new_ip:
                    ip_source_val = "dns"
            if new_ip and (new_ip != current_ip or ip_source_val != ip_source):
                updates["ip_address"] = new_ip
                updates["ip_source"] = ip_source_val

        # Status derivation. Only when it MOVES: writing the same value back is
        # not enrichment, and counting it as such is what made the number a lie.
        derived = derive_status(pve_status, updates.get("ip_address") or current_ip)
        if derived != host.get("status"):
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
            # Every field the operator has pinned means nothing moved.
            return len(skipped) < len(updates)
        return False

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
        """Everything HomePilot knows about a target - and which parts it could
        not read.

        This fuses three sources. When the KB half raised, `kb_entries` stayed
        `[]` and the rendered document simply omitted the section, so "the KB
        said nothing about this host" and "the KB could not be asked" printed
        identically - the shape #642 exists to stamp out. `sources` now records
        the outcome of each half, so a hole is visible as a hole.
        """
        doc: dict[str, Any] = {
            "target": target,
            "hosts": [],
            "services": [],
            "kb_entries": [],
            "artifact_history": [],
            "sources": {},
        }

        try:
            # The WHOLE name (#701): a substring match put web10 on web1's page.
            hosts = await self.repo.db.fetchall(
                "SELECT * FROM hosts WHERE hostname = ? COLLATE NOCASE OR node = ? COLLATE NOCASE",
                [target, target],
            )
            for host in hosts:
                host_dict = dict(host)
                host_services = await self.repo.list_services(host_id=host_dict.get("id"))
                host_dict["services"] = [dict(s) for s in host_services]
                doc["hosts"].append(host_dict)
                doc["services"].extend(host_dict["services"])
            doc["sources"]["inventory"] = {"read": True}
        except Exception as e:
            logger.warning("Inventory read failed for %s: %s", target, e)
            doc["sources"]["inventory"] = {"read": False, "error": str(e)}

        if self.kb_service:
            try:
                kb_results = await self.kb_service.search(target, limit=20)
                doc["kb_entries"] = kb_results
                doc["sources"]["knowledge_base"] = {"read": True}
            except Exception as e:
                logger.warning("KB search failed for %s: %s", target, e)
                doc["sources"]["knowledge_base"] = {"read": False, "error": str(e)}
        else:
            doc["sources"]["knowledge_base"] = {
                "read": False,
                "error": "no KB service is wired into this instance",
            }

        try:
            # LIKE only narrows the candidates; the match is decided on the
            # PARSED target, field by field, as a whole name (#701). A substring
            # on the JSON text listed web10's artifacts on web1's page.
            candidates = await self.repo.db.fetchall(
                "SELECT id, kind, intent, status, created_at, target_json FROM artifacts "
                "WHERE target_json LIKE ? ESCAPE '\\' ORDER BY created_at DESC",
                [f"%{escape_like(target)}%"],
            )
            history: list[dict[str, Any]] = []
            for row in candidates:
                entry = dict(row)
                if _target_names(entry.pop("target_json", None), target):
                    history.append(entry)
                    if len(history) >= 20:
                        break
            doc["artifact_history"] = history
            doc["sources"]["artifact_history"] = {"read": True}
        except Exception as e:
            logger.warning("Artifact history read failed for %s: %s", target, e)
            doc["sources"]["artifact_history"] = {"read": False, "error": str(e)}

        return doc
