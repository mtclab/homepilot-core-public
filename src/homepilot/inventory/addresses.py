"""Every address a host holds, where each one came from, and who else holds it.

Two incidents this exists for. A stopped VM was started whose cloud-init
``ipconfig0`` named an address a RUNNING VM already used, and it silently took
that address over on the LAN. Another VM shared a control-plane address with a
VPN box for days. HomePilot knew both machines' configs and said nothing,
because ``hosts.ip_address`` holds ONE address per host, guessed from the guest
agent or DNS, with no idea where it came from or how old it is.

So addresses become a table (``host_addresses``), one row per address per
source, and three questions are answered over it:

* **Configured vs configured** (``find_conflicts``): two machines whose
  configs claim one address on the same wire - running or not, because the one
  that starts second takes it over. Raised as a critical Overview line.
* **Configured vs observed** (``find_conflicts``): an address configured on one
  machine is held right now by another - reported by that machine's guest agent
  or, once an active probe exists, answered over ARP by a MAC that is not the
  configured guest's.
* **May this guest start?** (``AddressGuard.address_conflicts_for_start``): the
  start precondition the power controls call. It reads the cluster LIVE - the
  table is up to one reconciler cycle old, and "the holder was started a minute
  ago" is exactly the case that matters - and an incomplete read REFUSES rather
  than answers, the stance ``provision/ip_allocation.py`` already takes.

Provenance (``source``):

``qemu-ipconfig``  a VM's cloud-init ``ipconfigN`` (static ``ip=``/``ip6=``)
``lxc-net``        a container's ``netN`` ``ip=``/``ip6=``
``pve-node``       a Proxmox node's own bridge address
``manual``         an address an operator typed (a manual device, or a pinned IP)
``guest-agent``    what a running VM's qemu-guest-agent reports, with a MAC
``arp``            an active neighbour probe (see ``NeighbourProbe``; no probe
                   ships yet - the seam is here and tested with a fake)

The first four are CONFIGURED claims; the last two are OBSERVED, carry an
``observed_at`` that ages, and only count while fresh.

A "wire" is a (bridge, VLAN tag) pair taken from the NIC the address sits on.
The same address on two different bridges is two different networks - the
guest vnet and the operator LAN can both have a .10 - and must not be called a
clash. An address whose wire is not known (a manual device, an address with no
matching NIC) is compared against every wire, because not knowing where a
device sits is no evidence that it is elsewhere.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SOURCE_QEMU_IPCONFIG = "qemu-ipconfig"
SOURCE_LXC_NET = "lxc-net"
SOURCE_PVE_NODE = "pve-node"
SOURCE_MANUAL = "manual"
SOURCE_GUEST_AGENT = "guest-agent"
SOURCE_ARP = "arp"

# A configured claim holds the address whether or not anybody is answering for
# it right now: it is what the machine WILL use the next time it boots.
CONFIGURED_SOURCES: tuple[str, ...] = (
    SOURCE_QEMU_IPCONFIG,
    SOURCE_LXC_NET,
    SOURCE_PVE_NODE,
    SOURCE_MANUAL,
)
OBSERVED_SOURCES: tuple[str, ...] = (SOURCE_GUEST_AGENT, SOURCE_ARP)
ALL_SOURCES: tuple[str, ...] = CONFIGURED_SOURCES + OBSERVED_SOURCES
# What a Proxmox guest's config states; replaced as one set per config read.
GUEST_CONFIG_SOURCES: tuple[str, ...] = (SOURCE_QEMU_IPCONFIG, SOURCE_LXC_NET)

# An observation older than this says where the address WAS. The inventory
# reconciler re-reads every 5 minutes, so three missed cycles is stale.
OBSERVED_FRESH = timedelta(minutes=15)

# Interfaces that exist only inside one machine. Every docker host has a
# 172.17.0.1 on docker0; two of them are not a clash on anybody's LAN.
_HOST_LOCAL_IFACE_RE = re.compile(
    r"^(lo|docker\d*|br-[0-9a-f]+|veth.*|virbr\d*|cni\d*|podman\d*|flannel.*|cali.*|vxlan.*)$"
)
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")
_NET_KEY_RE = re.compile(r"^net(\d+)$")
_IPCONFIG_KEY_RE = re.compile(r"^ipconfig(\d+)$")


class AddressCheckError(RuntimeError):
    """The address check could not be completed, so its answer is unknown.

    Callers REFUSE on this: "nobody else holds it" is a conclusion only a
    complete read of the cluster can draw.
    """


@dataclass(frozen=True)
class NicAddress:
    """One address on one interface, with where the claim came from."""

    address: str
    source: str
    iface: str = ""
    prefixlen: int | None = None
    mac: str | None = None
    bridge: str | None = None
    vlan: int | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "prefixlen": self.prefixlen,
            "iface": self.iface,
            "mac": self.mac,
            "bridge": self.bridge,
            "vlan": self.vlan,
            "source": self.source,
        }


# ── parsing ──────────────────────────────────────────────────────────────────


def _kv(line: Any) -> dict[str, str]:
    """A PVE config line (`k=v,k=v`) as an ordered dict, tolerantly."""
    out: dict[str, str] = {}
    for part in str(line or "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip().lower()] = value.strip()
    return out


def normalise_address(value: Any) -> str | None:
    """A bare, canonical IP address worth comparing, or None.

    Loopback, link-local, multicast and unspecified addresses are never a claim
    on a shared wire, so they come back None and are not recorded.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.split("/", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return None
    return str(ip)


def _prefixlen(value: str) -> int | None:
    if "/" not in value:
        return None
    try:
        return int(value.split("/", 1)[1])
    except ValueError:
        return None


def normalise_mac(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw.upper() if _MAC_RE.match(raw) else None


def _nic_mac(nic: Mapping[str, str]) -> str | None:
    """The MAC on a NIC line: LXC says `hwaddr=`, qemu says `<model>=<mac>`."""
    if nic.get("hwaddr"):
        return normalise_mac(nic["hwaddr"])
    for value in nic.values():
        mac = normalise_mac(value)
        if mac:
            return mac
    return None


def _vlan(nic: Mapping[str, str]) -> int | None:
    try:
        return int(nic["tag"]) if nic.get("tag") else None
    except ValueError:
        return None


def _static_addresses(line: Mapping[str, str]) -> list[tuple[str, int | None]]:
    out: list[tuple[str, int | None]] = []
    for key in ("ip", "ip6"):
        raw = line.get(key, "")
        address = normalise_address(raw)
        if address is not None:
            out.append((address, _prefixlen(raw)))
    return out


def parse_guest_config(config: Mapping[str, Any], guest_type: str) -> list[NicAddress]:
    """Every static address a guest's config claims, per NIC, with MAC and wire.

    ``dhcp``, ``auto`` and ``manual`` claim nothing and are skipped: an address
    a DHCP server hands out is observed (guest agent), not configured.
    """
    out: list[NicAddress] = []
    nics: dict[str, dict[str, str]] = {}
    for key, value in config.items():
        match = _NET_KEY_RE.match(str(key))
        if match is not None:
            nics[match.group(1)] = _kv(value)
    if guest_type == "lxc":
        for index, nic in sorted(nics.items()):
            for address, prefixlen in _static_addresses(nic):
                out.append(
                    NicAddress(
                        address=address,
                        prefixlen=prefixlen,
                        iface=nic.get("name") or f"net{index}",
                        mac=_nic_mac(nic),
                        bridge=nic.get("bridge") or None,
                        vlan=_vlan(nic),
                        source=SOURCE_LXC_NET,
                    )
                )
        return out
    for key, value in config.items():
        match = _IPCONFIG_KEY_RE.match(str(key))
        if match is None:
            continue
        index = match.group(1)
        nic = nics.get(index, {})
        for address, prefixlen in _static_addresses(_kv(value)):
            out.append(
                NicAddress(
                    address=address,
                    prefixlen=prefixlen,
                    iface=f"net{index}",
                    mac=_nic_mac(nic) if nic else None,
                    bridge=(nic.get("bridge") or None) if nic else None,
                    vlan=_vlan(nic) if nic else None,
                    source=SOURCE_QEMU_IPCONFIG,
                )
            )
    return sorted(out, key=lambda a: (a.iface, a.address))


def config_nics(config: Mapping[str, Any]) -> dict[str, tuple[str, str | None, int | None]]:
    """MAC -> (netN, bridge, vlan) for every NIC with a MAC on a guest config."""
    out: dict[str, tuple[str, str | None, int | None]] = {}
    for key, value in config.items():
        match = _NET_KEY_RE.match(str(key))
        if match is None:
            continue
        nic = _kv(value)
        mac = _nic_mac(nic)
        if mac:
            out[mac] = (f"net{match.group(1)}", nic.get("bridge") or None, _vlan(nic))
    return out


def effective_pending_config(pending: Any) -> dict[str, Any]:
    """The config a guest will boot with, from PVE's ``/pending`` listing.

    Each entry is ``{key, value[, pending][, delete]}``: ``pending`` is the
    value queued for the next boot, ``delete`` marks a key queued for removal.
    A Start or a Reboot applies both, so both are what the address check must
    look at - checking ``value`` alone would let a reboot apply a pending
    ``ipconfig0`` nobody compared against anything.
    """
    rows = pending.get("data", pending) if isinstance(pending, Mapping) else pending
    out: dict[str, Any] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, Mapping) or "key" not in row:
            continue
        if row.get("delete"):
            continue
        out[str(row["key"])] = row["pending"] if "pending" in row else row.get("value")
    return out


def parse_agent_interfaces(
    payload: Any, config: Mapping[str, Any] | None = None
) -> list[NicAddress]:
    """What a running VM's guest agent says it holds, per interface.

    The agent knows interface names and MACs, not bridges. The MAC is matched
    against the config's NICs to learn which wire an address is on; an
    interface whose MAC is on no configured NIC keeps an unknown wire and is
    listed but never compared (it is internal to the machine, or the config
    could not be read).
    """
    data = payload.get("data", payload) if isinstance(payload, Mapping) else payload
    if isinstance(data, Mapping):
        data = data.get("result", data)
    if not isinstance(data, list):
        return []
    nics = config_nics(config or {})
    out: list[NicAddress] = []
    seen: set[tuple[str, str]] = set()
    for iface in data:
        if not isinstance(iface, Mapping):
            continue
        name = str(iface.get("name") or "")
        if _HOST_LOCAL_IFACE_RE.match(name):
            continue
        mac = normalise_mac(iface.get("hardware-address"))
        _netn, bridge, vlan = nics.get(mac or "", ("", None, None))
        for entry in iface.get("ip-addresses") or []:
            if not isinstance(entry, Mapping):
                continue
            address = normalise_address(entry.get("ip-address"))
            if address is None or (name, address) in seen:
                continue
            seen.add((name, address))
            prefix = entry.get("prefix")
            out.append(
                NicAddress(
                    address=address,
                    prefixlen=int(prefix) if isinstance(prefix, int) else None,
                    iface=name,
                    mac=mac,
                    bridge=bridge,
                    vlan=vlan,
                    source=SOURCE_GUEST_AGENT,
                )
            )
    return out


def same_wire(
    a_bridge: str | None, a_vlan: int | None, b_bridge: str | None, b_vlan: int | None
) -> bool:
    """Could two addresses be on one broadcast domain? Unknown means yes."""
    if not a_bridge or not b_bridge:
        return True
    return a_bridge == b_bridge and (a_vlan or None) == (b_vlan or None)


# ── who holds what ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Holder:
    """A machine holding an address, as the operator needs it named."""

    address: str
    hostname: str
    source: str
    host_id: str | None = None
    proxmox_id: int | None = None
    node: str | None = None
    host_type: str | None = None
    pve_status: str | None = None
    iface: str = ""
    mac: str | None = None
    bridge: str | None = None
    vlan: int | None = None
    observed_at: str | None = None

    @property
    def label(self) -> str:
        """`web-01 (VM 101, running)` - what a person matches against Proxmox."""
        return f"{self.hostname} ({describe_kind(self)})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "hostname": self.hostname,
            "label": self.label,
            "source": self.source,
            "host_id": self.host_id,
            "proxmox_id": self.proxmox_id,
            "node": self.node,
            "host_type": self.host_type,
            "pve_status": self.pve_status,
            "iface": self.iface,
            "mac": self.mac,
            "bridge": self.bridge,
            "vlan": self.vlan,
            "observed_at": self.observed_at,
        }


def describe_kind(h: Holder) -> str:
    status = h.pve_status if h.pve_status in ("running", "stopped", "paused") else None
    if h.host_type == "qemu" and h.proxmox_id is not None:
        what = f"VM {h.proxmox_id}"
    elif h.host_type == "lxc" and h.proxmox_id is not None:
        what = f"CT {h.proxmox_id}"
    elif h.host_type == "node" or h.source == SOURCE_PVE_NODE:
        what = "Proxmox node"
    elif h.host_id is None:
        what = "not in inventory"
    else:
        what = "device"
    if h.source == SOURCE_ARP and h.mac:
        what += f", MAC {h.mac}"
    return f"{what}, {status}" if status else what


def _holder_from_row(row: Mapping[str, Any]) -> Holder:
    vmid = row.get("proxmox_id")
    return Holder(
        address=str(row["address"]),
        hostname=str(row.get("hostname") or row.get("mac") or "unknown"),
        source=str(row["source"]),
        host_id=row.get("host_id"),
        proxmox_id=int(vmid) if vmid is not None else None,
        node=row.get("node"),
        host_type=row.get("host_type"),
        pve_status=row.get("pve_status"),
        iface=str(row.get("iface") or ""),
        mac=row.get("mac"),
        bridge=row.get("bridge"),
        vlan=row.get("vlan"),
        observed_at=row.get("observed_at"),
    )


@dataclass
class AddressConflict:
    """One address that more than one machine holds or will hold."""

    address: str
    kind: str  # "configured" | "observed"
    holders: list[Holder] = field(default_factory=list)

    @property
    def host_ids(self) -> list[str]:
        return list(dict.fromkeys(h.host_id for h in self.holders if h.host_id))

    @property
    def summary(self) -> str:
        configured = [h for h in self.holders if h.source in CONFIGURED_SOURCES]
        observed = [h for h in self.holders if h.source in OBSERVED_SOURCES]
        names = _names(configured)
        if self.kind == "configured":
            return f"{self.address} is configured on {len(names)} machines: {_and(names)}"
        holders = _names(observed)
        return (
            f"{self.address} is configured on {_and(names)} but held by {_and(holders)}"
            if names
            else f"{self.address} is held by {_and(holders)}"
        )

    @property
    def remedy(self) -> str:
        if self.kind == "configured":
            return (
                "Whichever starts second takes the address over on the LAN. Give one of "
                "them another address (its cloud-init ipconfig, or the container's net "
                "ip=) and restart it."
            )
        return (
            "Two machines are answering for one address now. Change the address on the "
            "one that should not hold it, or stop it, before the other is started."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "kind": self.kind,
            "severity": "critical",
            "summary": self.summary,
            "remedy": self.remedy,
            "host_ids": self.host_ids,
            "holders": [h.as_dict() for h in self.holders],
        }


def _names(holders: Iterable[Holder]) -> list[str]:
    seen: dict[str, str] = {}
    for h in holders:
        seen.setdefault(h.host_id or f"mac:{h.mac}:{h.hostname}", h.label)
    return list(seen.values())


def _and(names: Sequence[str]) -> str:
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _fresh(row: Mapping[str, Any], now: datetime) -> bool:
    ts = _parse_ts(row.get("observed_at"))
    return ts is not None and now - ts <= OBSERVED_FRESH


def find_conflicts(
    rows: Iterable[Mapping[str, Any]], now: datetime | None = None
) -> list[AddressConflict]:
    """Every address more than one machine claims, from ``host_addresses`` rows.

    ``rows`` are address rows joined with their host (hostname, proxmox_id,
    node, host_type, pve_status, absent_since). A host Proxmox no longer
    reports holds nothing: its rows stay for the record, but a destroyed guest
    cannot take anybody's address, and dev carried four such ghosts on one
    address that would otherwise all "clash".

    Adoption is deliberately NOT a filter. Two guests fighting over an address
    break the LAN whether or not anybody has adopted either of them.
    """
    now = now or datetime.now(UTC)
    live = [r for r in rows if not r.get("absent_since")]
    configured: dict[str, list[Mapping[str, Any]]] = {}
    for r in live:
        if r["source"] in CONFIGURED_SOURCES:
            configured.setdefault(str(r["address"]), []).append(r)

    conflicts: dict[str, AddressConflict] = {}

    # Configured vs configured: distinct machines, overlapping wires.
    for address, claims in configured.items():
        involved: dict[int, Mapping[str, Any]] = {}
        for i, a in enumerate(claims):
            for b in claims[i + 1 :]:
                if a["host_id"] == b["host_id"]:
                    continue
                if same_wire(a.get("bridge"), a.get("vlan"), b.get("bridge"), b.get("vlan")):
                    involved[id(a)] = a
                    involved[id(b)] = b
        if involved:
            conflicts[address] = AddressConflict(
                address=address,
                kind="configured",
                holders=_dedupe([_holder_from_row(r) for r in involved.values()]),
            )

    # Configured vs observed: an address configured on A, held right now by B.
    macs_by_host: dict[str, set[str]] = {}
    for r in live:
        if r.get("mac") and r["source"] != SOURCE_ARP:
            macs_by_host.setdefault(str(r["host_id"]), set()).add(str(r["mac"]).upper())
    for r in live:
        if r["source"] not in OBSERVED_SOURCES or not _fresh(r, now):
            continue
        address = str(r["address"])
        claims = configured.get(address, [])
        if r["source"] == SOURCE_GUEST_AGENT:
            if r.get("pve_status") != "running" or not r.get("bridge"):
                # A guest-agent read from a guest that has since stopped, or an
                # interface on no configured NIC, says nothing about the wire.
                continue
            others = [
                c
                for c in claims
                if c["host_id"] != r["host_id"]
                and same_wire(c.get("bridge"), c.get("vlan"), r.get("bridge"), r.get("vlan"))
            ]
            if not others:
                continue
            observed_holder = _holder_from_row(r)
        else:
            # ARP: the row sits on the host whose configured address was probed
            # and carries the MAC that answered. A MAC that is not one of that
            # host's own NICs is somebody else.
            mac = str(r.get("mac") or "").upper()
            own = macs_by_host.get(str(r["host_id"]), set())
            if not mac or mac in own:
                continue
            others = [c for c in claims if c["host_id"] == r["host_id"]]
            if not others:
                continue
            observed_holder = _arp_holder(r, mac, live)
        existing = conflicts.get(address)
        if existing is None:
            conflicts[address] = AddressConflict(
                address=address,
                kind="observed",
                holders=_dedupe([*map(_holder_from_row, others), observed_holder]),
            )
        else:
            existing.holders = _dedupe([*existing.holders, observed_holder])
    return [conflicts[a] for a in sorted(conflicts, key=_address_sort_key)]


def _arp_holder(row: Mapping[str, Any], mac: str, live: Sequence[Mapping[str, Any]]) -> Holder:
    """Who answered: the inventory machine with that MAC, if there is one."""
    owner = next(
        (
            r
            for r in live
            if str(r.get("mac") or "").upper() == mac
            and r["source"] != SOURCE_ARP
            and r["host_id"] != row["host_id"]
        ),
        None,
    )
    if owner is not None:
        return _holder_from_row(
            {
                **owner,
                "address": row["address"],
                "source": SOURCE_ARP,
                "mac": mac,
                "observed_at": row.get("observed_at"),
            }
        )
    return Holder(
        address=str(row["address"]),
        hostname=f"unknown device {mac}",
        source=SOURCE_ARP,
        mac=mac,
        observed_at=row.get("observed_at"),
    )


def _dedupe(holders: Iterable[Holder]) -> list[Holder]:
    out: dict[tuple[Any, ...], Holder] = {}
    for h in holders:
        out.setdefault((h.host_id, h.mac if h.host_id is None else None, h.source, h.iface), h)
    return list(out.values())


def _address_sort_key(address: str) -> tuple[int, int]:
    ip = ipaddress.ip_address(address)
    return (ip.version, int(ip))


# ── the active probe seam ────────────────────────────────────────────────────


@dataclass(frozen=True)
class NeighbourAnswer:
    """One address and the MAC that answered for it on the wire."""

    address: str
    mac: str


class NeighbourProbe(Protocol):
    """Asks the LAN who answers for a set of addresses (ARP / ND).

    NOT IMPLEMENTED YET, deliberately. The control plane runs in a container on
    a bridge network: its ARP table holds the docker gateway and nothing else,
    and giving it host networking to ping the LAN is a new privilege. The
    review's recommendation (02 Q5, option B) is to ask the agent on the
    self-guest - HomePilot's own VM - to run ``ip neigh`` after one ping per
    address, which needs that guest identified (lane B1) and two read-only
    policy patterns in the agent. Until then ``NO_PROBE`` answers None and the
    observed check runs on guest-agent data alone. ``record_probe_answers``
    and ``find_conflicts`` already handle the answers, tested with a fake.
    """

    async def answers(self, addresses: Sequence[str]) -> list[NeighbourAnswer] | None:
        """The MACs that answered, or None when no probe could be run."""
        ...


class _NoProbe:
    async def answers(self, addresses: Sequence[str]) -> list[NeighbourAnswer] | None:
        return None


NO_PROBE: NeighbourProbe = _NoProbe()


async def record_probe_answers(repo: Any, probe: NeighbourProbe) -> int | None:
    """Ask ``probe`` about every configured address and store what answered.

    Each answer is filed under the host the address is CONFIGURED on, carrying
    the MAC that answered; ``find_conflicts`` then names a MAC that is not that
    host's own. Returns how many answers were stored, None if no probe ran.
    """
    rows = await repo.list_address_rows()
    by_address: dict[str, list[str]] = {}
    for r in rows:
        if r["source"] in CONFIGURED_SOURCES and not r.get("absent_since"):
            by_address.setdefault(str(r["address"]), []).append(str(r["host_id"]))
    if not by_address:
        return 0
    answers = await probe.answers(sorted(by_address, key=_address_sort_key))
    if answers is None:
        return None
    per_host: dict[str, list[NicAddress]] = {}
    for answer in answers:
        address = normalise_address(answer.address)
        mac = normalise_mac(answer.mac)
        if address is None or mac is None:
            continue
        for host_id in by_address.get(address, []):
            per_host.setdefault(host_id, []).append(
                NicAddress(address=address, mac=mac, source=SOURCE_ARP)
            )
    for host_id in {h for hosts in by_address.values() for h in hosts}:
        await repo.replace_host_addresses(host_id, (SOURCE_ARP,), per_host.get(host_id, []))
    return sum(len(v) for v in per_host.values())


# ── the live guard (start / provision preconditions) ─────────────────────────


class AddressGuard:
    """Live answers to "who else holds these addresses?" for actions about to run.

    Reads the cluster, not the table: a guest started a minute ago is in no
    table yet, and that guest is the one a Start would collide with. Every read
    that fails raises ``AddressCheckError``; a partial scan never answers.
    """

    def __init__(self, repo: Any, proxmox: Any) -> None:
        self.repo = repo
        self.proxmox = proxmox

    async def target_addresses(self, host: Mapping[str, Any]) -> list[NicAddress]:
        """The addresses ``host`` will hold once it boots: its config plus pending."""
        node, vmid, guest_type = _guest_ref(host)
        if self.proxmox is None:
            raise AddressCheckError(
                "no Proxmox is configured, so the guest's config cannot be read"
            )
        try:
            pending = await self.proxmox.read(f"/nodes/{node}/{guest_type}/{vmid}/pending")
        except Exception as exc:  # any failure means "not known": the caller must refuse
            raise AddressCheckError(
                f"could not read the config of {guest_type} {vmid} on {node} ({exc}), so "
                "the addresses it would claim are not known"
            ) from exc
        return parse_guest_config(effective_pending_config(pending), guest_type)

    async def holders(
        self,
        addresses: Sequence[NicAddress],
        *,
        include_stopped: bool,
        exclude_vmid: int | None = None,
    ) -> list[Holder]:
        """Every machine other than ``exclude_vmid`` that holds one of ``addresses``.

        ``include_stopped=False`` is the Start question (a stopped holder cannot
        be hijacked right now); True is the provision question (a new guest must
        not be built onto an address another guest's config already names).
        Templates never run and are skipped. Manual devices and Proxmox nodes
        come from the table - they are not guests the cluster can be asked
        about - and so do fresh guest-agent observations of running guests.
        """
        wanted = {a.address: a for a in addresses}
        if not wanted:
            return []
        if self.proxmox is None:
            raise AddressCheckError(
                "no Proxmox is configured, so which guests hold an address cannot be asked"
            )
        try:
            listing = await self.proxmox.read("/cluster/resources", {"type": "vm"})
        except Exception as exc:  # any failure means "not known": the caller must refuse
            raise AddressCheckError(
                f"could not list the cluster's guests to see who holds the address ({exc})"
            ) from exc
        rows = listing.get("data", listing) if isinstance(listing, Mapping) else listing
        if not isinstance(rows, list):
            raise AddressCheckError(
                "the cluster's guest list came back in a shape that cannot be read"
            )

        out: list[Holder] = []
        running_vmids: set[int] = set()
        for row in rows:
            if not isinstance(row, Mapping) or row.get("template"):
                continue
            guest_type = str(row.get("type") or "")
            node = str(row.get("node") or "")
            try:
                vmid = int(row.get("vmid"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if guest_type not in ("qemu", "lxc") or not node:
                continue
            status = str(row.get("status") or "unknown")
            if status == "running":
                running_vmids.add(vmid)
            if vmid == exclude_vmid or (status != "running" and not include_stopped):
                continue
            # A RUNNING holder holds the addresses it booted with: PVE's plain
            # GET answers with pending changes already applied, so a running
            # guest with a queued ipconfig change would be judged by an address
            # it does not hold yet - and the one it does hold would go unseen.
            # A stopped holder (the provision question) claims what it will
            # boot with, which is exactly the plain GET (#781).
            query = {"current": 1} if status == "running" else None
            try:
                config = await self.proxmox.read(f"/nodes/{node}/{guest_type}/{vmid}/config", query)
            except Exception as exc:  # any failure leaves the scan partial: refuse
                raise AddressCheckError(
                    f"could not read the config of {guest_type} {vmid} on {node} ({exc}), "
                    "so whether it holds the address is not known"
                ) from exc
            data = config.get("data", config) if isinstance(config, Mapping) else {}
            claims = parse_guest_config(data if isinstance(data, Mapping) else {}, guest_type)
            name = str(row.get("name") or (data or {}).get("name") or f"{guest_type}-{vmid}")
            for claim in claims:
                target = wanted.get(claim.address)
                if target is None or not same_wire(
                    target.bridge, target.vlan, claim.bridge, claim.vlan
                ):
                    continue
                inv = await self._inventory_row(vmid, name)
                out.append(
                    Holder(
                        address=claim.address,
                        hostname=name,
                        source=claim.source,
                        host_id=inv.get("id") if inv else None,
                        proxmox_id=vmid,
                        node=node,
                        host_type=guest_type,
                        pve_status=status,
                        iface=claim.iface,
                        mac=claim.mac,
                        bridge=claim.bridge,
                        vlan=claim.vlan,
                    )
                )

        now = datetime.now(UTC)
        for r in await self.repo.list_address_rows():
            if r.get("absent_since"):
                continue
            target = wanted.get(str(r["address"]))
            if target is None or not same_wire(
                target.bridge, target.vlan, r.get("bridge"), r.get("vlan")
            ):
                continue
            vmid = r.get("proxmox_id")
            if r["source"] in (SOURCE_PVE_NODE, SOURCE_MANUAL):
                if vmid is not None and int(vmid) == exclude_vmid:
                    continue
                # An operator-pinned address on a GUEST is that guest's claim;
                # it holds the address only while the guest runs.
                pinned_on_stopped_guest = (
                    vmid is not None
                    and r.get("host_type") in ("qemu", "lxc")
                    and int(vmid) not in running_vmids
                )
                if pinned_on_stopped_guest and not include_stopped:
                    continue
                out.append(_holder_from_row(r))
            elif r["source"] == SOURCE_GUEST_AGENT:
                if vmid is None or int(vmid) == exclude_vmid or int(vmid) not in running_vmids:
                    continue
                if _fresh(r, now):
                    out.append(_holder_from_row({**r, "pve_status": "running"}))
        return _dedupe_by_machine(out)

    async def address_conflicts_for_start(
        self, host: Mapping[str, Any], addresses: Sequence[NicAddress] | None = None
    ) -> list[Holder]:
        """The RUNNING machines that already hold an address ``host`` would claim.

        Empty means the Start (or a Reboot applying a pending ``ipconfig``) takes
        nobody's address. ``addresses`` overrides what is read from the guest's
        own config + pending changes, for a caller that is about to write a new
        one. Raises ``AddressCheckError`` when the answer cannot be established
        - the caller refuses then, it never guesses.
        """
        _node, vmid, _type = _guest_ref(host)
        claims = list(addresses) if addresses is not None else await self.target_addresses(host)
        return await self.holders(claims, include_stopped=False, exclude_vmid=vmid)

    async def _inventory_row(self, vmid: int, name: str) -> dict[str, Any] | None:
        from .service import same_guest

        rows = await self.repo.list_hosts_by_proxmox_id(vmid)
        return next((dict(r) for r in rows if same_guest(r, name)), None)


def _dedupe_by_machine(holders: Iterable[Holder]) -> list[Holder]:
    out: dict[tuple[Any, ...], Holder] = {}
    for h in holders:
        key = (h.address, h.host_id or h.proxmox_id or h.hostname)
        # The configured claim names the machine best; keep the first seen.
        out.setdefault(key, h)
    return list(out.values())


def _guest_ref(host: Mapping[str, Any]) -> tuple[str, int, str]:
    node = str(host.get("node") or "")
    guest_type = str(host.get("host_type") or "")
    vmid = host.get("proxmox_id")
    if not node or vmid is None or guest_type not in ("qemu", "lxc"):
        raise AddressCheckError(
            f"{host.get('hostname') or 'this host'} is not a Proxmox guest with a known "
            "node and VMID, so its addresses cannot be read from the cluster"
        )
    return node, int(vmid), guest_type


# ── the per-host read seam (lane B1's self-guest match) ──────────────────────
#
# Lane B1 matched the control plane's own addresses against each guest through
# ``host_addresses`` and, before this table existed, assembled the answer live
# from the guest config plus ``hosts.ip_address``. That made two address
# sources that could disagree. The table is now the ONE source: the refresh
# fills the config claims, the enrich pass the guest-agent observations, and an
# operator-typed address is mirrored in as ``manual``. ``hosts.ip_address`` on
# its own (a DNS or agent guess with no provenance) is not evidence here.


@dataclass(frozen=True)
class HostAddress:
    """One address a host holds, and where HomePilot learned it."""

    address: str
    # One of ``ALL_SOURCES`` (``qemu-ipconfig``, ``guest-agent``, ...).
    source: str
    # Which interface and source said so, worded for evidence: ``net0 (qemu-ipconfig)``.
    detail: str


# What a host CLAIMS about itself. An ``arp`` row is filed under the host the
# address is configured on but may name another machine's MAC, so it is
# evidence of a clash, not of who holds the address.
_SELF_EVIDENCE_SOURCES: tuple[str, ...] = (*CONFIGURED_SOURCES, SOURCE_GUEST_AGENT)


async def host_addresses(repo: Any, host: Mapping[str, Any]) -> list[HostAddress]:
    """Every address the ``host_addresses`` table holds for ``host``, configured first.

    De-duplicated by address (the first, most deliberate source wins); ordering
    is ``Repository.list_host_addresses``'s: configured claims, then manual,
    then guest-agent observations.
    """
    seen: set[str] = set()
    out: list[HostAddress] = []
    for row in await repo.list_host_addresses(str(host["id"])):
        source = str(row["source"])
        address = str(row["address"])
        if source not in _SELF_EVIDENCE_SOURCES or address in seen:
            continue
        seen.add(address)
        iface = str(row.get("iface") or "")
        out.append(HostAddress(address, source, f"{iface} ({source})" if iface else source))
    return out


__all__ = [
    "ALL_SOURCES",
    "CONFIGURED_SOURCES",
    "GUEST_CONFIG_SOURCES",
    "NO_PROBE",
    "OBSERVED_FRESH",
    "OBSERVED_SOURCES",
    "AddressCheckError",
    "AddressConflict",
    "AddressGuard",
    "Holder",
    "HostAddress",
    "NeighbourAnswer",
    "NeighbourProbe",
    "NicAddress",
    "config_nics",
    "effective_pending_config",
    "find_conflicts",
    "host_addresses",
    "normalise_address",
    "parse_agent_interfaces",
    "parse_guest_config",
    "record_probe_answers",
    "same_wire",
]
