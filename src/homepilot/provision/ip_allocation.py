"""HomePilot hands the guest its address itself (#630).

The incident this exists for: prod's SDN guest network has no DHCP server.
PVE's simple zone serves DHCP through dnsmasq, dnsmasq is not installed on the
node, and installing it is a node mutation the operator refuses. Provisioning
wrote ``ipconfig0=ip=dhcp`` anyway, so the first real guest booted with a
link-local address and nothing said a word: the clone succeeded, the fence was
written, the VM started, and the task record said "succeeded".

So the product stops depending on a server it does not run. When the guest is
going onto the guest network's own vnet and nobody has asked for a particular
address, HomePilot picks one out of the guest subnet and writes it into
cloud-init.

Two decisions carry the design:

* **The claimed set is a LIVE SCAN of the cluster, never a table.** A
  bookkeeping table has to be kept true by every path that destroys a guest -
  including `qm destroy` typed into the node's shell, which HomePilot will
  never see. Reading what the cluster's own guest configs say makes a destroyed
  guest's address free the moment the guest is gone, and makes an address an
  operator typed into the PVE UI by hand a claim this allocator respects.
* **A scan that could not complete REFUSES rather than allocates.** A partial
  view of who holds what is exactly how two guests end up on one address, and
  an address conflict is a fault that surfaces days later as "the network is
  flaky". A refusal surfaces now, before anything is cloned.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Any

from .guest_network import DesiredGuestNetwork

logger = logging.getLogger(__name__)

# The first addresses of the subnet are left to infrastructure: the gateway
# lives there, and so does whatever the operator puts next to it. An allocator
# that hands out .2 the day before somebody wires up a resolver there is an
# allocator nobody trusts.
INFRA_HOST_FLOOR = 10

# `ipconfig0` as PVE stores it: comma-separated key=value, and the one this
# module cares about is `ip=<addr>/<prefix>`. `ip=dhcp` and the v6 spellings
# carry no IPv4 claim and are skipped rather than mis-parsed.
_IPV4_CIDR_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})$")

# A guest NIC line: `virtio=AA:BB:...,bridge=innkeep,firewall=1`.
_NET_KEY_RE = re.compile(r"^net(\d+)$")
_IPCONFIG_KEY_RE = re.compile(r"^ipconfig(\d+)$")


class AddressAllocationError(RuntimeError):
    """The guest's address could not be decided. Nothing has been built yet."""


class SubnetExhaustedError(AddressAllocationError):
    """Every usable address in the guest subnet is already claimed."""


@dataclass(frozen=True)
class AllocatedAddress:
    """One address, and the cloud-init lines that put it on a guest."""

    address: str
    prefixlen: int
    gateway: str
    nameserver: str = ""

    @property
    def ipconfig0(self) -> str:
        return f"ip={self.address}/{self.prefixlen},gw={self.gateway}"


def _kv(line: Any) -> dict[str, str]:
    """A PVE config line (`k=v,k=v`) as a dict, tolerantly."""
    out: dict[str, str] = {}
    for part in str(line or "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip().lower()] = value.strip()
    return out


def address_in(value: Any) -> ipaddress.IPv4Address | None:
    """The IPv4 address a config line claims, or None if it claims none.

    Handles both spellings a guest config uses: a qemu guest's ``ipconfigN``
    (``ip=198.51.100.5/24,gw=...``) and an LXC guest's ``netN``
    (``name=eth0,bridge=innkeep,ip=198.51.100.5/24``). ``ip=dhcp``, ``manual``
    and the IPv6 forms claim no IPv4 address and come back None.
    """
    raw = _kv(value).get("ip", "")
    match = _IPV4_CIDR_RE.match(raw)
    if match is None:
        return None
    try:
        return ipaddress.IPv4Address(match.group(1))
    except ipaddress.AddressValueError:  # pragma: no cover - the regex checked
        return None


def claimed_in_config(config: dict[str, Any], vnet: str) -> set[ipaddress.IPv4Address]:
    """Every IPv4 address this guest's config claims ON the given bridge.

    Per NIC, not per guest: a guest with one leg on the operator LAN and one on
    the guest vnet must contribute only the guest-vnet address, or the
    allocator would refuse addresses that were never on this wire.
    """
    claimed: set[ipaddress.IPv4Address] = set()
    for key, value in config.items():
        index = _NET_KEY_RE.match(str(key))
        if index is None:
            continue
        nic = _kv(value)
        if nic.get("bridge", "") != vnet:
            continue
        # LXC states the address on the NIC itself; qemu states it on the
        # matching cloud-init ipconfig line. Both are read, because both are
        # ways a guest on this wire holds an address.
        lxc_addr = address_in(value)
        if lxc_addr is not None:
            claimed.add(lxc_addr)
        qemu_addr = address_in(config.get(f"ipconfig{index.group(1)}"))
        if qemu_addr is not None:
            claimed.add(qemu_addr)
    return claimed


async def claimed_addresses(proxmox: Any, vnet: str) -> set[ipaddress.IPv4Address]:
    """Every address held by a guest attached to ``vnet``, read from the cluster.

    Raises rather than returning a partial answer: see the module docstring.
    """
    if proxmox is None:
        raise AddressAllocationError(
            "Proxmox is not configured, so the cluster cannot be asked which "
            "addresses are already in use; nothing was provisioned"
        )
    try:
        listing = await proxmox.read("/cluster/resources", {"type": "vm"})
    except Exception as exc:
        raise AddressAllocationError(
            f"could not list the cluster's guests to see which addresses are taken: {exc}"
        ) from exc

    rows = listing.get("data", listing) if isinstance(listing, dict) else listing
    if not isinstance(rows, list):
        rows = []

    claimed: set[ipaddress.IPv4Address] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        node = str(row.get("node") or "")
        guest_type = str(row.get("type") or "")
        vmid_raw = row.get("vmid")
        if not node or guest_type not in ("qemu", "lxc") or vmid_raw is None:
            continue
        try:
            vmid = int(vmid_raw)
        except (TypeError, ValueError):  # pragma: no cover - PVE sends ints
            continue
        # Templates are scanned too. A template cannot run, so its address is
        # not strictly in use - but it is the address every guest cloned from
        # it inherits until cloud-init is rewritten, and skipping one address
        # is cheaper than a collision.
        try:
            config = await proxmox.get_vm_config(node, vmid, guest_type=guest_type)
        except Exception as exc:
            raise AddressAllocationError(
                f"could not read the config of guest {vmid} on {node} ({exc}), so the "
                "set of addresses already in use is not known; refusing to allocate "
                "one rather than risk handing out an address a guest already holds"
            ) from exc
        if isinstance(config, dict):
            claimed |= claimed_in_config(config, vnet)
    return claimed


def pick_address(
    network: DesiredGuestNetwork,
    claimed: set[ipaddress.IPv4Address],
    floor: int = INFRA_HOST_FLOOR,
) -> ipaddress.IPv4Address:
    """The lowest free host address at or above the infra floor.

    Lowest-first, deliberately: an operator reading `qm config` down a rack
    wants the addresses to be the small, contiguous, boring ones, and a
    lowest-first allocator over a live scan reuses a destroyed guest's address
    instead of drifting up the subnet forever.
    """
    subnet = ipaddress.IPv4Network(network.subnet_cidr)
    gateway = ipaddress.IPv4Address(network.gateway)
    base = int(subnet.network_address)
    # `.hosts()` already excludes the network and broadcast addresses.
    for host in subnet.hosts():
        if int(host) - base < floor:
            continue
        if host == gateway or host in claimed:
            continue
        return host
    raise SubnetExhaustedError(
        f"no free address left in the guest subnet {subnet}: "
        f"{len(claimed)} address(es) are already claimed by guests on {network.vnet}, "
        f"and .1-.{floor - 1} are reserved for infrastructure. Widen "
        "guest_network_subnet, or remove a guest, before provisioning another."
    )


async def allocate_address(
    proxmox: Any,
    network: DesiredGuestNetwork,
    nameserver: str = "",
) -> AllocatedAddress:
    """Pick this guest's address out of the guest subnet. Raises, never guesses."""
    claimed = await claimed_addresses(proxmox, network.vnet)
    chosen = pick_address(network, claimed)
    prefixlen = ipaddress.IPv4Network(network.subnet_cidr).prefixlen
    logger.info(
        "Allocated %s/%s on %s (%d address(es) already claimed)",
        chosen,
        prefixlen,
        network.vnet,
        len(claimed),
    )
    return AllocatedAddress(
        address=str(chosen),
        prefixlen=prefixlen,
        gateway=network.gateway,
        nameserver=str(nameserver or "").strip(),
    )


__all__ = [
    "INFRA_HOST_FLOOR",
    "AddressAllocationError",
    "AllocatedAddress",
    "SubnetExhaustedError",
    "allocate_address",
    "claimed_addresses",
    "claimed_in_config",
    "pick_address",
]
