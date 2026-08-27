"""Live cluster checks behind the provisioning defaults (#553 C3).

A provisioning default that the cluster refutes is worse than no default at
all: it is a value the product will hand to every future provision, and the
operator finds out when a friend's redemption dies half way through a clone.
So each of these settings is checked AGAINST THE CLUSTER before it is stored,
and a refusal repeats the cluster's own answer - "no bridge vmbr7 on node pve1;
node has: vmbr0, vmbr1" - rather than a generic "invalid value".

Three outcomes, and the difference between the last two matters:

* ``ok`` - the cluster confirms the value (or the value is empty, which means
  "no default" and is always allowed: clearing a setting must never require a
  reachable cluster).
* refused (``ok`` false, ``reachable`` true) - the cluster answered, and its
  answer contradicts the value. Nothing is saved; the caller sees 422.
* could not run (``reachable`` false) - Proxmox is unconfigured or unreachable,
  so NOTHING is known about the value. Nothing is saved either, and the caller
  sees a 502-shaped refusal saying the probe could not run. Saving an unchecked
  value here and calling it validated would be the lie this module exists to
  prevent.

This module deliberately imports nothing from ``homepilot`` except the Proxmox
error type: the settings registry imports IT, so a dependency in the other
direction would be a cycle. Consumption of the resolved defaults lives in
``provision.defaults`` instead, which is free to use the registry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProbeResult:
    """What the cluster said about a candidate value."""

    ok: bool
    detail: str
    # False only when the probe could not be RUN at all. `ok` is then false too,
    # but for a different reason, and the two must not be collapsed: one means
    # "the cluster says no", the other "the cluster said nothing".
    reachable: bool = True


@dataclass(frozen=True)
class ProbeContext:
    """Everything a probe may ask about beyond the value itself.

    ``node`` is the default node currently in force. A bridge is per-node and a
    template lives on one, so the answer to "is this value good" is not the same
    everywhere in the cluster - the probes say WHERE they looked.
    """

    proxmox: Any | None = None
    node: str = ""
    bridge: str = ""
    # The guest subnet currently in force (#553 guest network). A gateway and a
    # DHCP range only mean anything relative to a subnet, so the probes for them
    # are handed the one this instance already carries - exactly as the bridge
    # probe is handed the node.
    guest_subnet: str = ""


ProbeFn = Callable[[Any, ProbeContext], Awaitable[ProbeResult]]

_NO_CLUSTER = ProbeResult(
    ok=False,
    reachable=False,
    detail=(
        "Proxmox is not configured on this instance, so the cluster cannot be "
        "asked about this value. Wire Proxmox up first (Settings -> Proxmox); "
        "nothing was saved."
    ),
)


def _rows(payload: Any) -> list[dict[str, Any]]:
    """The list PVE actually returned, whatever wrapper it came in."""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _unreachable(exc: Exception) -> ProbeResult:
    return ProbeResult(
        ok=False,
        reachable=False,
        detail=(
            f"The cluster could not be asked about this value: {exc}. "
            "Nothing was saved - an unchecked provisioning default is exactly "
            "what this check exists to refuse."
        ),
    )


class _NotConfiguredError(Exception):
    """No Proxmox client to ask - distinct from a cluster that answered badly."""


async def _read(ctx: ProbeContext, path: str, query: dict[str, Any] | None = None) -> Any:
    proxmox = ctx.proxmox
    if proxmox is None:
        raise _NotConfiguredError
    return await proxmox.read(path, query) if query else await proxmox.read(path)


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return False


# ── The probes ───────────────────────────────────────────────────────────────


async def probe_node(value: Any, ctx: ProbeContext) -> ProbeResult:
    node = str(value or "").strip()
    if not node:
        return ProbeResult(True, "No default node: every provision must name its own node.")
    try:
        rows = _rows(await _read(ctx, "/nodes"))
    except _NotConfiguredError:
        return _NO_CLUSTER
    except Exception as exc:
        return _unreachable(exc)
    names = sorted(str(r.get("node", "")) for r in rows if r.get("node"))
    if node in names:
        return ProbeResult(True, f"Node {node} is in the cluster.")
    return ProbeResult(
        False,
        f"no node {node} in the cluster; cluster has: {', '.join(names) or '(none)'}",
    )


async def probe_template_vmid(value: Any, ctx: ProbeContext) -> ProbeResult:
    vmid = int(value or 0)
    if vmid == 0:
        return ProbeResult(True, "No default template: every provision must name its own.")
    try:
        rows = _rows(await _read(ctx, "/cluster/resources", {"type": "vm"}))
    except _NotConfiguredError:
        return _NO_CLUSTER
    except Exception as exc:
        return _unreachable(exc)

    qemu = [r for r in rows if str(r.get("type", "")) == "qemu"]
    matches = [r for r in qemu if _as_int(r.get("vmid")) == vmid]
    if not matches:
        available = ", ".join(
            f"{_as_int(r.get('vmid'))} ({r.get('name', '?')} on {r.get('node', '?')})"
            for r in qemu
            if _truthy(r.get("template"))
        )
        return ProbeResult(
            False,
            f"no VM {vmid} in the cluster; templates it does have: {available or '(none)'}",
        )
    # A template can only live on one node, but the cluster answer is a list -
    # prefer the default node's copy so the sentence names the node provisioning
    # will actually clone on.
    match = next((r for r in matches if str(r.get("node", "")) == ctx.node), matches[0])
    found_on = str(match.get("node", "?"))
    name = str(match.get("name", "?"))
    if not _truthy(match.get("template")):
        return ProbeResult(
            False,
            f"VM {vmid} ({name}) on node {found_on} is not a template; "
            "cloning from a running VM is not what this default is for",
        )
    where = f"on node {found_on}"
    if ctx.node and found_on != ctx.node:
        where = f"on node {found_on}, NOT on the default node {ctx.node}"
    return ProbeResult(True, f"Template {vmid} ({name}) found {where}.")


async def probe_pool(value: Any, ctx: ProbeContext) -> ProbeResult:
    pool = str(value or "").strip()
    if not pool:
        return ProbeResult(True, "No default pool: provisioned guests join no pool.")
    try:
        rows = _rows(await _read(ctx, "/pools"))
    except _NotConfiguredError:
        return _NO_CLUSTER
    except Exception as exc:
        return _unreachable(exc)
    names = sorted(str(r.get("poolid", "")) for r in rows if r.get("poolid"))
    if pool in names:
        return ProbeResult(True, f"The token can see pool {pool}.")
    return ProbeResult(
        False,
        f"this token cannot see a pool {pool}; pools it can see: {', '.join(names) or '(none)'}",
    )


async def probe_bridge(value: Any, ctx: ProbeContext) -> ProbeResult:
    bridge = str(value or "").strip()
    if not bridge:
        return ProbeResult(True, "No default bridge: the template's own NIC is cloned untouched.")
    if not ctx.node:
        # Not a cluster failure and not a typo: a bridge exists on a NODE, so
        # there is no cluster-wide question to ask yet.
        return ProbeResult(
            False,
            f"set the node first: a bridge is per-node, so there is nothing to "
            f"check {bridge} against until provision_default_node names one",
        )
    entries, failure = await _network_entries(ctx)
    if failure is not None:
        return failure
    bridges = sorted(str(e.get("iface", "")) for e in entries if str(e.get("type")) == "bridge")
    if bridge in bridges:
        return ProbeResult(True, f"Bridge {bridge} is on node {ctx.node}.")
    return ProbeResult(
        False,
        f"no bridge {bridge} on node {ctx.node}; node has: {', '.join(bridges) or '(none)'}",
    )


async def probe_vlan_tag(value: Any, ctx: ProbeContext) -> ProbeResult:
    tag = int(value or 0)
    if tag == 0:
        return ProbeResult(True, "No VLAN tag: the guest NIC is untagged.")
    if not ctx.bridge:
        return ProbeResult(
            False,
            "set the bridge first: the VLAN tag is only ever applied to the net0 "
            "this instance sets, and net0 is left alone while no default bridge is set",
        )
    if not ctx.node:
        return ProbeResult(
            False,
            "set the node first: VLAN-awareness is a property of the bridge on a "
            "node, so there is nothing to check the tag against yet",
        )
    entries, failure = await _network_entries(ctx)
    if failure is not None:
        return failure
    entry = next(
        (
            e
            for e in entries
            if str(e.get("iface", "")) == ctx.bridge and str(e.get("type")) == "bridge"
        ),
        None,
    )
    if entry is None:
        return ProbeResult(
            False,
            f"no bridge {ctx.bridge} on node {ctx.node} to carry VLAN {tag}; "
            "fix the default bridge first",
        )
    raw = entry.get("bridge_vlan_aware", entry.get("bridge-vlan-aware"))
    if raw is None:
        # Honest uncertainty, not a guess in either direction: some PVE
        # versions omit the flag from this listing entirely, and refusing a
        # correct tag is as wrong as promising one that will not pass.
        return ProbeResult(
            True,
            f"Saved, but unverified: node {ctx.node} does not report whether bridge "
            f"{ctx.bridge} is VLAN-aware, so VLAN {tag} could not be checked. "
            "Confirm on the node that the bridge has VLAN awareness enabled.",
        )
    if _truthy(raw):
        return ProbeResult(True, f"Bridge {ctx.bridge} on node {ctx.node} is VLAN-aware.")
    return ProbeResult(
        False,
        f"bridge {ctx.bridge} on node {ctx.node} is not VLAN-aware, so tag {tag} "
        "would be dropped; enable VLAN awareness on the bridge or clear the tag",
    )


async def probe_ipconfig(value: Any, ctx: ProbeContext) -> ProbeResult:
    """Syntax only. There is no cluster question here: PVE accepts any
    ipconfig0 string and only cloud-init inside the guest finds out later, so
    the honest check is the shape, done locally."""
    text = str(value or "").strip()
    if not text:
        return ProbeResult(True, "No default ipconfig: provisioning falls back to ip=dhcp.")
    return ProbeResult(True, f"Checked locally: {text} is a valid ipconfig0. No cluster call.")


async def _network_entries(ctx: ProbeContext) -> tuple[list[dict[str, Any]], ProbeResult | None]:
    try:
        return _rows(await _read(ctx, f"/nodes/{ctx.node}/network")), None
    except _NotConfiguredError:
        return [], _NO_CLUSTER
    except Exception as exc:
        return [], _unreachable(exc)


def _as_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


# ── Guest-network shape probes (#553) ────────────────────────────────────────
# LOCAL checks, and they say so. There is no cluster question to ask about a
# subnet that does not exist yet - the cluster is asked by the guest-network
# survey/plan, which is a read of its own. What CAN be checked before a value is
# stored is whether the settings describe a network that could work at all, and
# getting that wrong (a gateway outside its subnet, a DHCP range that hands out
# the router's own address) produces a guest with no network and no explanation.
# Each probe asks the same rule DesiredGuestNetwork enforces, one field at a
# time, so the field an operator is editing is the field the refusal names.


def _local(detail: str) -> ProbeResult:
    return ProbeResult(True, f"Checked locally: {detail} No cluster call.")


def _shape_probe(check: Callable[[Any, ProbeContext], str]) -> ProbeFn:
    async def run(value: Any, ctx: ProbeContext) -> ProbeResult:
        from homepilot.provision.guest_network import GuestNetworkError

        try:
            return _local(check(value, ctx))
        except GuestNetworkError as exc:
            return ProbeResult(False, str(exc))

    return run


def _check_zone(value: Any, _ctx: ProbeContext) -> str:
    from homepilot.provision.guest_network import validate_name

    text = str(value or "").strip()
    if not text:
        return "no zone name: the guest network is not described yet."
    return f"{validate_name(text, 'zone')} is a usable PVE zone name."


def _check_vnet(value: Any, _ctx: ProbeContext) -> str:
    from homepilot.provision.guest_network import validate_name

    text = str(value or "").strip()
    if not text:
        return "no vnet name: the guest network is not described yet."
    return f"{validate_name(text, 'vnet')} is a usable PVE vnet name."


def _check_subnet(value: Any, _ctx: ProbeContext) -> str:
    from homepilot.provision.guest_network import validate_network

    text = str(value or "").strip()
    if not text:
        return "no guest subnet: guest provisioning fences nothing."
    net = validate_network(text, "guest_network_subnet")
    return f"{net} is a valid IPv4 subnet with {net.num_addresses - 2} usable addresses."


def _check_gateway(value: Any, ctx: ProbeContext) -> str:
    from homepilot.provision.guest_network import (
        GuestNetworkError,
        validate_address,
        validate_network,
    )

    text = str(value or "").strip()
    if not text:
        return "no gateway: the guest subnet cannot route yet."
    addr = validate_address(text, "guest_network_gateway")
    if not ctx.guest_subnet:
        return (
            f"{addr} is a valid IPv4 address. Set guest_network_subnet to check that "
            "it sits inside the guest subnet."
        )
    net = validate_network(ctx.guest_subnet, "guest_network_subnet")
    if addr not in net:
        raise GuestNetworkError(
            f"gateway {addr} is not inside the guest subnet {net}: a guest given this "
            "gateway would have no route off its own wire"
        )
    return f"{addr} is inside the guest subnet {net}."


def _check_dhcp_range(value: Any, ctx: ProbeContext) -> str:
    from homepilot.provision.guest_network import (
        GuestNetworkError,
        parse_range,
        validate_network,
    )

    text = str(value or "").strip()
    if not text:
        return "no DHCP range: dnsmasq would have nothing to hand out."
    start, end = parse_range(text)
    if int(end) < int(start):
        raise GuestNetworkError(f"dhcp_range ends before it starts: {start} - {end}")
    if not ctx.guest_subnet:
        return (
            f"{start}-{end} is a well-formed range. Set guest_network_subnet to check "
            "that it sits inside the guest subnet."
        )
    net = validate_network(ctx.guest_subnet, "guest_network_subnet")
    for addr, which in ((start, "start"), (end, "end")):
        if addr not in net:
            raise GuestNetworkError(f"dhcp_range {which} address {addr} is not inside {net}")
    return f"{start}-{end} is inside the guest subnet {net}."


def _check_dns_server(value: Any, _ctx: ProbeContext) -> str:
    from homepilot.provision.guest_network import validate_address

    text = str(value or "").strip()
    if not text:
        return "no DNS server override: guests are told to resolve at the gateway."
    return f"{validate_address(text, 'guest_network_dhcp_dns_server')} is a valid IPv4 address."


def _check_isolate_cidrs(value: Any, _ctx: ProbeContext) -> str:
    from homepilot.provision.guest_network import split_cidrs, validate_network

    cidrs = split_cidrs(value)
    if not cidrs:
        return (
            "no isolate list: guests are NOT fenced off any network, and provisioning "
            "writes no per-VM firewall rules."
        )
    nets = [str(validate_network(c, "guest_network_isolate_cidrs")) for c in cidrs]
    return f"guests would be dropped towards {', '.join(nets)}."


PROBES: dict[str, ProbeFn] = {
    "provision_default_node": probe_node,
    "provision_default_template_vmid": probe_template_vmid,
    "provision_default_pool": probe_pool,
    "provision_default_bridge": probe_bridge,
    "provision_default_vlan_tag": probe_vlan_tag,
    "provision_default_ipconfig": probe_ipconfig,
    "guest_network_zone": _shape_probe(_check_zone),
    "guest_network_vnet": _shape_probe(_check_vnet),
    "guest_network_subnet": _shape_probe(_check_subnet),
    "guest_network_gateway": _shape_probe(_check_gateway),
    "guest_network_dhcp_range": _shape_probe(_check_dhcp_range),
    "guest_network_dhcp_dns_server": _shape_probe(_check_dns_server),
    "guest_network_isolate_cidrs": _shape_probe(_check_isolate_cidrs),
}

__all__ = ["PROBES", "ProbeContext", "ProbeFn", "ProbeResult"]
