"""The guest subnet HomePilot builds, fences and keeps honest (#553).

A friend's machine must reach the internet and nothing of the operator's. That
is two fences, on two different layers, and this module is the first of them:
the SDN zone / vnet / subnet the guest lives on, plus the vnet firewall that
describes what may leave it.

Four functions, deliberately separate, because three of them must be testable
without a cluster:

* :class:`DesiredGuestNetwork` - what the operator says the guest network is.
  Validated in full at construction: a gateway outside its own subnet or a DHCP
  range outside it produces a network nobody can use, and PVE will accept both.
* :func:`survey` - what the cluster currently says, in the cluster's own words.
  Reads only.
* :func:`plan` - desired minus current, as an ordered list of steps. EMPTY when
  the estate already matches, which is what makes an apply idempotent and a
  drift check possible; both read this one function, so they can never disagree.
* :func:`execute` - runs a plan and reports per step. No deletes in this slice:
  every step creates or updates, so a mistaken desired state cannot take an
  operator's existing zone away.

THE ENFORCEMENT CAVEAT, stated here because it is the most important sentence
in the file: vnet firewall rules are enforced only under the nftables
`proxmox-firewall` stack. A node running the LEGACY iptables firewall accepts
the rules, stores them, shows them - and does not apply them to vnet forward
traffic. On such a node the fence that actually holds is the PER-VM one applied
at provision time (`provision/service.py`), which is tap-level and enforced by
both stacks. The vnet rules are still written, because they are the correct
place for them and they become live the moment the stack is switched, and
:func:`survey` reports which stack the node is running so nothing here has to
guess.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# PVE stores a zone and a vnet name in an 8-character field. Longer names are
# refused by the cluster with a terse error; refusing here names the field.
_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,7}$")

# What the settings registry calls each part of the desired state (#553 C2).
KEYS = (
    "guest_network_zone",
    "guest_network_vnet",
    "guest_network_subnet",
    "guest_network_gateway",
    "guest_network_snat",
    "guest_network_dhcp",
    "guest_network_dhcp_range",
    "guest_network_dhcp_dns_server",
    "guest_network_isolate_cidrs",
)


class GuestNetworkError(ValueError):
    """A desired guest network that cannot be built as described."""


def validate_name(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not _NAME_RE.match(text):
        raise GuestNetworkError(
            f"{field_name} must be 1-8 characters, lower-case letters and digits, "
            f"starting with a letter (PVE stores it in an 8-character field); got {text!r}"
        )
    return text


def validate_network(value: str, field_name: str) -> ipaddress.IPv4Network:
    text = str(value or "").strip()
    try:
        net = ipaddress.ip_network(text, strict=True)
    except ValueError as exc:
        raise GuestNetworkError(
            f"{field_name} must be an IPv4 CIDR like 198.51.100.0/24: {exc}"
        ) from exc
    if not isinstance(net, ipaddress.IPv4Network):
        raise GuestNetworkError(f"{field_name} must be IPv4; got {text!r}")
    return net


def validate_address(value: str, field_name: str) -> ipaddress.IPv4Address:
    text = str(value or "").strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError as exc:
        raise GuestNetworkError(f"{field_name} must be an IPv4 address: {exc}") from exc
    if not isinstance(addr, ipaddress.IPv4Address):
        raise GuestNetworkError(f"{field_name} must be IPv4; got {text!r}")
    return addr


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class DesiredGuestNetwork:
    """The guest network as the operator describes it.

    Every field is validated here rather than at the cluster, because the
    cluster's refusals are terse and its ACCEPTANCES are worse: PVE will happily
    store a gateway that is not inside its own subnet, and the first anybody
    hears of it is a guest with no route.

    ``dhcp_range`` is the operator-facing form ``"<start>-<end>"``; PVE's own
    ``start-address=...,end-address=...`` spelling is built in :func:`plan`, so
    the shape an operator types and the shape the API takes cannot drift.
    """

    zone: str
    vnet: str
    subnet_cidr: str
    gateway: str
    snat: bool = True
    dhcp: bool = True
    dhcp_range: str = ""
    dhcp_dns_server: str = ""
    isolate_cidrs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "zone", validate_name(self.zone, "zone"))
        object.__setattr__(self, "vnet", validate_name(self.vnet, "vnet"))
        subnet = validate_network(self.subnet_cidr, "subnet_cidr")
        object.__setattr__(self, "subnet_cidr", str(subnet))

        gateway = validate_address(self.gateway, "gateway")
        if gateway not in subnet:
            raise GuestNetworkError(
                f"gateway {gateway} is not inside subnet {subnet}: a guest given "
                "this gateway would have no route off its own wire"
            )
        object.__setattr__(self, "gateway", str(gateway))
        object.__setattr__(self, "snat", _flag(self.snat))
        object.__setattr__(self, "dhcp", _flag(self.dhcp))

        raw_range = str(self.dhcp_range or "").strip()
        if raw_range:
            start, end = parse_range(raw_range)
            for addr, which in ((start, "start"), (end, "end")):
                if addr not in subnet:
                    raise GuestNetworkError(
                        f"dhcp_range {which} address {addr} is not inside subnet {subnet}"
                    )
            if int(end) < int(start):
                raise GuestNetworkError(f"dhcp_range ends before it starts: {start} - {end}")
            if start <= gateway <= end:
                raise GuestNetworkError(
                    f"dhcp_range {start}-{end} contains the gateway {gateway}; "
                    "DHCP would hand a guest the router's own address"
                )
            object.__setattr__(self, "dhcp_range", f"{start}-{end}")
        elif self.dhcp:
            raise GuestNetworkError(
                "dhcp is on but no dhcp_range is set: dnsmasq has nothing to hand out. "
                "Set guest_network_dhcp_range, or turn DHCP off."
            )

        dns = str(self.dhcp_dns_server or "").strip()
        if dns:
            object.__setattr__(
                self, "dhcp_dns_server", str(validate_address(dns, "dhcp_dns_server"))
            )

        cidrs: list[str] = []
        for raw in self.isolate_cidrs:
            text = str(raw or "").strip()
            if not text:
                continue
            cidrs.append(str(validate_network(text, "isolate_cidrs")))
        object.__setattr__(self, "isolate_cidrs", tuple(cidrs))

    @property
    def dhcp_range_param(self) -> list[str]:
        """The ``dhcp-range`` value PVE takes: a list of one range string."""
        if not self.dhcp_range:
            return []
        start, end = self.dhcp_range.split("-", 1)
        return [f"start-address={start},end-address={end}"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "vnet": self.vnet,
            "subnet_cidr": self.subnet_cidr,
            "gateway": self.gateway,
            "snat": self.snat,
            "dhcp": self.dhcp,
            "dhcp_range": self.dhcp_range,
            "dhcp_dns_server": self.dhcp_dns_server,
            "isolate_cidrs": list(self.isolate_cidrs),
        }


def parse_range(raw: str) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]:
    parts = [p.strip() for p in raw.split("-")]
    if len(parts) != 2 or not all(parts):
        raise GuestNetworkError(
            f"dhcp_range must be '<start>-<end>', e.g. 198.51.100.100-198.51.100.199; got {raw!r}"
        )
    return validate_address(parts[0], "dhcp_range start"), validate_address(
        parts[1], "dhcp_range end"
    )


# ── The fence ────────────────────────────────────────────────────────────────
# ONE description of what a guest may do, used twice: as vnet forward rules
# (correct place, enforced only under nftables) and as per-VM tap rules at
# provision time (enforced today, on both stacks). Two hand-written copies
# would be two fences that disagree, and the one that disagrees quietly is the
# one that lets traffic through.


def fence_rules(desired: DesiredGuestNetwork, direction: str) -> list[dict[str, Any]]:
    """The ordered rule set that isolates a guest from the operator's LAN.

    Order is the whole meaning: the ACCEPTs for DHCP and DNS to the gateway come
    FIRST, because the DROPs below them include the gateway itself. Reverse them
    and a fenced guest cannot get an address or resolve a name.
    """
    rules: list[dict[str, Any]] = []
    gateway = desired.gateway
    if desired.dhcp:
        rules.append(
            {
                "type": direction,
                "action": "ACCEPT",
                "proto": "udp",
                "dport": "67:68",
                "dest": gateway,
                "enable": 1,
                "comment": "DHCP from the guest subnet's own dnsmasq",
            }
        )
    for proto in ("udp", "tcp"):
        rules.append(
            {
                "type": direction,
                "action": "ACCEPT",
                "proto": proto,
                "dport": "53",
                "dest": gateway,
                "enable": 1,
                "comment": "DNS to the guest subnet's own resolver",
            }
        )
    for cidr in desired.isolate_cidrs:
        rules.append(
            {
                "type": direction,
                "action": "DROP",
                "dest": cidr,
                "enable": 1,
                "comment": f"guests never reach {cidr}",
            }
        )
    rules.append(
        {
            "type": direction,
            "action": "DROP",
            "dest": f"{desired.gateway}/32",
            "enable": 1,
            "comment": "the gateway is a router for guests, not a host they talk to",
        }
    )
    return rules


def _rule_identity(rule: dict[str, Any]) -> tuple[str, ...]:
    """What makes two firewall rules the same rule. Comments and position do not."""
    return (
        str(rule.get("type", "")),
        str(rule.get("action", "")),
        str(rule.get("proto", "")),
        str(rule.get("dport", "")),
        str(rule.get("dest", "")),
    )


# ── Survey ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuestNetworkSurvey:
    """What the cluster says right now. Nothing here is interpreted."""

    zones: list[dict[str, Any]] = field(default_factory=list)
    vnets: list[dict[str, Any]] = field(default_factory=list)
    subnets: list[dict[str, Any]] = field(default_factory=list)
    vnet_firewall_options: dict[str, Any] = field(default_factory=dict)
    vnet_firewall_rules: list[dict[str, Any]] = field(default_factory=list)
    node: str = ""
    nftables: bool | None = None
    pending: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def zone(self, name: str) -> dict[str, Any] | None:
        return next((z for z in self.zones if str(z.get("zone", "")) == name), None)

    def vnet(self, name: str) -> dict[str, Any] | None:
        return next((v for v in self.vnets if str(v.get("vnet", "")) == name), None)

    def subnet(self, cidr: str) -> dict[str, Any] | None:
        # PVE names a subnet `<zone>-<cidr with / as ->`, and also returns the
        # plain `cidr` field. Match on the field, never on the composed id.
        return next((s for s in self.subnets if str(s.get("cidr", "")) == cidr), None)

    @property
    def firewall_stack(self) -> str:
        if self.nftables is None:
            return "unknown"
        return "nftables" if self.nftables else "legacy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "zones": self.zones,
            "vnets": self.vnets,
            "subnets": self.subnets,
            "vnet_firewall_options": self.vnet_firewall_options,
            "vnet_firewall_rules": self.vnet_firewall_rules,
            "node": self.node,
            "firewall_stack": self.firewall_stack,
            "pending": self.pending,
            "errors": self.errors,
        }


async def survey(
    gateway: Any,
    desired: DesiredGuestNetwork,
    node: str = "",
) -> GuestNetworkSurvey:
    """Read the cluster's SDN and firewall state for ONE desired network.

    Reads only, and every read is allowed to fail on its own: an operator whose
    token cannot see the node's firewall options should still be told what the
    zones and vnets look like. A read that failed lands in ``errors`` and leaves
    its field empty, which :func:`plan` reads as "not established" rather than
    as "absent" wherever the difference matters.
    """
    zones: list[dict[str, Any]] = []
    vnets: list[dict[str, Any]] = []
    subnets: list[dict[str, Any]] = []
    fw_options: dict[str, Any] = {}
    fw_rules: list[dict[str, Any]] = []
    pending: list[str] = []
    errors: list[str] = []
    nftables: bool | None = None

    try:
        zones = await gateway.list_zones()
    except Exception as exc:
        errors.append(f"could not list the SDN zones: {exc}")

    try:
        vnets = await gateway.list_vnets()
    except Exception as exc:
        errors.append(f"could not list the SDN vnets: {exc}")

    for row in list(zones) + list(vnets):
        state = str(row.get("state") or row.get("pending") or "").strip()
        if state and state not in ("", "0"):
            name = str(row.get("zone") or row.get("vnet") or "?")
            pending.append(f"{name}: {state}")

    vnet_exists = any(str(v.get("vnet", "")) == desired.vnet for v in vnets)
    if vnet_exists:
        try:
            subnets = await gateway.list_subnets(desired.vnet)
        except Exception as exc:
            errors.append(f"could not read the subnets of vnet {desired.vnet}: {exc}")
        try:
            fw_options = await gateway.vnet_firewall_options(desired.vnet)
        except Exception as exc:
            errors.append(f"could not read the firewall options of vnet {desired.vnet}: {exc}")
        try:
            fw_rules = await gateway.vnet_firewall_rules(desired.vnet)
        except Exception as exc:
            errors.append(f"could not read the firewall rules of vnet {desired.vnet}: {exc}")

    resolved_node = str(node or "").strip()
    if not resolved_node:
        try:
            node_rows = await gateway.list_nodes()
            names = sorted(str(r.get("node", "")) for r in node_rows if r.get("node"))
            resolved_node = names[0] if names else ""
        except Exception as exc:
            errors.append(f"could not list the cluster's nodes: {exc}")

    if resolved_node:
        try:
            options = await gateway.node_firewall_options(resolved_node)
            # The flag is ABSENT on a legacy node rather than false, so its
            # absence is the answer - but only once the read itself succeeded.
            nftables = _flag(options.get("nftables", 0))
        except Exception as exc:
            errors.append(f"could not read the firewall options of node {resolved_node}: {exc}")

    return GuestNetworkSurvey(
        zones=zones,
        vnets=vnets,
        subnets=subnets,
        vnet_firewall_options=fw_options,
        vnet_firewall_rules=fw_rules,
        node=resolved_node,
        nftables=nftables,
        pending=pending,
        errors=errors,
    )


# ── Plan ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    """One cluster operation the plan would perform, named in words an operator reads.

    ``op`` names a method on the PVE gateway - never an HTTP method and a path.
    HomePilot does not own PVE's endpoints (the estate's ``proxmox_mcp`` library
    does), so a plan step that carried a path would be a second copy of one, in
    the layer least likely to be updated when PVE changes it.
    """

    id: str
    description: str
    op: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "op": self.op,
            "params": self.params,
        }


@dataclass(frozen=True)
class Plan:
    """The ordered steps, plus anything that stops the plan being runnable.

    A blocker is NOT a step that failed: it is a state of the cluster that this
    slice refuses to resolve by itself, because resolving it would mean deleting
    or repurposing something an operator built. Execute refuses a plan that
    carries one, rather than doing the first half of it.
    """

    steps: tuple[Step, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def converged(self) -> bool:
        return not self.steps and not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "blockers": list(self.blockers),
            "converged": self.converged,
        }


def _zone_needs_update(row: dict[str, Any], desired: DesiredGuestNetwork) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    want_dhcp = "dnsmasq" if desired.dhcp else ""
    have_dhcp = str(row.get("dhcp", "") or "")
    if want_dhcp and have_dhcp != want_dhcp:
        changes["dhcp"] = want_dhcp
    if desired.dhcp and str(row.get("ipam", "") or "") != "pve":
        changes["ipam"] = "pve"
    return changes


def _subnet_needs_update(row: dict[str, Any], desired: DesiredGuestNetwork) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if str(row.get("gateway", "") or "") != desired.gateway:
        changes["gateway"] = desired.gateway
    if _flag(row.get("snat", 0)) != desired.snat:
        changes["snat"] = 1 if desired.snat else 0
    have_range = row.get("dhcp-range") or row.get("dhcp_range") or []
    if isinstance(have_range, str):
        have_range = [have_range]
    want_range = desired.dhcp_range_param
    if _normalised_ranges(have_range) != _normalised_ranges(want_range):
        changes["dhcp-range"] = want_range
    if desired.dhcp_dns_server and str(row.get("dhcp-dns-server", "") or "") != (
        desired.dhcp_dns_server
    ):
        changes["dhcp-dns-server"] = desired.dhcp_dns_server
    return changes


def _normalised_ranges(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or []:
        parts = sorted(p.strip() for p in str(value).split(",") if p.strip())
        out.append(",".join(parts))
    return sorted(out)


def plan(desired: DesiredGuestNetwork, current: GuestNetworkSurvey) -> Plan:
    """Desired minus current, in the order the cluster must be told.

    EMPTY when the estate already matches, and that is the load-bearing
    property: the apply is idempotent because this returns nothing to do, and
    the drift check is honest because it asks this same question. Nothing here
    talks to a cluster, so every branch is testable without one.
    """
    steps: list[Step] = []
    firewall_steps: list[Step] = []
    blockers: list[str] = []

    zone_row = current.zone(desired.zone)
    if zone_row is None:
        steps.append(
            Step(
                id="create-zone",
                description=(
                    f"create SDN zone {desired.zone} (simple"
                    + (", dnsmasq DHCP, PVE IPAM)" if desired.dhcp else ")")
                ),
                op="create_zone",
                params=_zone_params(desired),
            )
        )
    else:
        zone_type = str(zone_row.get("type", "") or "")
        if zone_type and zone_type != "simple":
            blockers.append(
                f"zone {desired.zone} already exists with type {zone_type!r}, not 'simple'. "
                "HomePilot will not repurpose a zone somebody else built - pick another "
                "zone name, or remove that zone on the cluster first."
            )
        else:
            changes = _zone_needs_update(zone_row, desired)
            if changes:
                steps.append(
                    Step(
                        id="update-zone",
                        description=(
                            f"update SDN zone {desired.zone}: "
                            + ", ".join(f"{k}={v}" for k, v in sorted(changes.items()))
                        ),
                        op="update_zone",
                        params={"zone": desired.zone, **changes},
                    )
                )

    vnet_row = current.vnet(desired.vnet)
    if vnet_row is None:
        steps.append(
            Step(
                id="create-vnet",
                description=f"create vnet {desired.vnet} in zone {desired.zone}",
                op="create_vnet",
                params={
                    "vnet": desired.vnet,
                    "zone": desired.zone,
                    "alias": "HomePilot guest network",
                },
            )
        )
    else:
        vnet_zone = str(vnet_row.get("zone", "") or "")
        if vnet_zone and vnet_zone != desired.zone:
            blockers.append(
                f"vnet {desired.vnet} already exists in zone {vnet_zone!r}, not "
                f"{desired.zone!r}. Moving a vnet between zones takes its guests off "
                "the wire, so HomePilot will not do it - pick another vnet name."
            )

    if not blockers:
        subnet_row = current.subnet(desired.subnet_cidr) if vnet_row is not None else None
        if subnet_row is None:
            steps.append(
                Step(
                    id="create-subnet",
                    description=(
                        f"create subnet {desired.subnet_cidr} on vnet {desired.vnet} "
                        f"(gateway {desired.gateway}, SNAT "
                        f"{'on' if desired.snat else 'off'})"
                    ),
                    op="create_subnet",
                    params={"vnet": desired.vnet, **_subnet_params(desired)},
                )
            )
        else:
            changes = _subnet_needs_update(subnet_row, desired)
            if changes:
                steps.append(
                    Step(
                        id="update-subnet",
                        description=(
                            f"update subnet {desired.subnet_cidr}: "
                            + ", ".join(f"{k}={v}" for k, v in sorted(changes.items()))
                        ),
                        op="update_subnet",
                        params={
                            "vnet": desired.vnet,
                            "subnet": desired.subnet_cidr,
                            **changes,
                        },
                    )
                )

        # Firewall steps are collected separately and ordered AFTER apply-sdn:
        # PVE's vnet firewall API validates the vnet against the APPLIED SDN
        # config, so options written while the vnet is still pending are
        # refused with "invalid vnet" (found on the first live apply - the
        # zone/vnet/subnet were created and the firewall step then 500'd).
        firewall_steps = _firewall_steps(desired, current, vnet_row is not None)

    # apply-sdn commits whatever is pending - the steps above, or leftovers
    # from an earlier run that failed before its own apply.
    if steps or current.pending:
        steps.append(
            Step(
                id="apply-sdn",
                description=(
                    "apply the pending SDN configuration (this is what writes it to "
                    "the nodes; DHCP needs dnsmasq installed there)"
                ),
                op="apply_sdn",
                params={},
            )
        )
    steps.extend(firewall_steps)

    if blockers:
        # A blocked plan carries NO steps. Execute would refuse to run them
        # anyway, but a plan an operator reads must not promise work that will
        # never happen - "3 steps pending" next to "this cannot proceed" is two
        # answers to one question.
        return Plan(steps=(), blockers=tuple(blockers))
    return Plan(steps=tuple(steps), blockers=())


def _zone_params(desired: DesiredGuestNetwork) -> dict[str, Any]:
    body: dict[str, Any] = {"zone": desired.zone, "type": "simple"}
    if desired.dhcp:
        body["dhcp"] = "dnsmasq"
        body["ipam"] = "pve"
    return body


def _subnet_params(desired: DesiredGuestNetwork) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subnet": desired.subnet_cidr,
        "type": "subnet",
        "gateway": desired.gateway,
        "snat": 1 if desired.snat else 0,
    }
    if desired.dhcp_range_param:
        body["dhcp-range"] = desired.dhcp_range_param
    if desired.dhcp_dns_server:
        body["dhcp-dns-server"] = desired.dhcp_dns_server
    return body


def _firewall_steps(
    desired: DesiredGuestNetwork,
    current: GuestNetworkSurvey,
    vnet_exists: bool,
) -> list[Step]:
    """The vnet firewall half of the plan.

    Written even on a legacy-stack node, where the cluster stores it and does
    not enforce it. That is deliberate: it is the correct place for the rule,
    the per-VM fence carries the enforcement today, and a rule that only appears
    the day somebody switches stacks is a rule nobody wrote.
    """
    steps: list[Step] = []
    if not desired.isolate_cidrs:
        return steps

    options = current.vnet_firewall_options if vnet_exists else {}
    want_options: dict[str, Any] = {"enable": 1, "policy_forward": "ACCEPT"}
    have_enable = _flag(options.get("enable", 0)) if vnet_exists else False
    have_policy = str(options.get("policy_forward", "") or "") if vnet_exists else ""
    if not have_enable or have_policy != "ACCEPT":
        steps.append(
            Step(
                id="vnet-firewall-options",
                description=(
                    f"turn the vnet firewall on for {desired.vnet} with a forward "
                    "policy of ACCEPT (the DROPs below are what fences it)"
                ),
                op="set_vnet_firewall_options",
                params={"vnet": desired.vnet, **want_options},
            )
        )

    have_rules = current.vnet_firewall_rules if vnet_exists else []
    existing = {_rule_identity(r) for r in have_rules}
    for position, rule in enumerate(fence_rules(desired, "forward")):
        if _rule_identity(rule) in existing:
            continue
        steps.append(
            Step(
                id=f"vnet-firewall-rule-{position}",
                description=(
                    f"vnet {desired.vnet} forward rule {position}: {rule['action']} "
                    f"{rule.get('proto', 'any')} -> {rule.get('dest', 'any')}"
                ),
                op="create_vnet_firewall_rule",
                params={"vnet": desired.vnet, **rule, "pos": position},
            )
        )
    return steps


# ── Execute ──────────────────────────────────────────────────────────────────


async def execute(gateway: Any, steps: list[Step] | tuple[Step, ...]) -> dict[str, Any]:
    """Run a plan step by step and say exactly what happened to each one.

    Stops at the first failure and repeats the cluster's own words, because a
    PVE refusal ("dnsmasq is not installed") is the sentence an operator can act
    on and no paraphrase of it is. Steps that never ran are reported as not
    attempted rather than left out - a log that simply ends is a log that looks
    like it finished.
    """
    results: list[dict[str, Any]] = []
    log_lines: list[str] = []
    steps = list(steps)

    for index, step in enumerate(steps):
        try:
            operation = getattr(gateway, step.op, None)
            if operation is None:
                raise GuestNetworkError(f"the PVE gateway has no operation {step.op!r}")
            said = await operation(**step.params)
        except Exception as exc:
            detail = str(exc)
            results.append({"id": step.id, "status": "failed", "detail": detail})
            log_lines.append(f"{step.id}: FAILED - {detail}")
            for skipped in steps[index + 1 :]:
                results.append({"id": skipped.id, "status": "not_attempted", "detail": ""})
                log_lines.append(f"{skipped.id}: not attempted")
            return {
                "success": False,
                "execution_log": "\n".join(log_lines),
                "failure_reason": f"{step.id}: {detail}",
                "steps": results,
            }
        # The library answers in a sentence ("SDN zone 'guest' created"); that
        # is the cluster's side of the story and it goes in the log next to
        # ours, rather than being thrown away for a tick.
        detail = str(said or step.description)
        results.append({"id": step.id, "status": "done", "detail": detail})
        log_lines.append(f"{step.id}: {step.description} -> {detail}")

    if not steps:
        log_lines.append("nothing to do: the cluster already matches the desired guest network")
    return {
        "success": True,
        "execution_log": "\n".join(log_lines),
        "steps": results,
    }


# ── The desired state this instance carries (#553 C2 settings) ───────────────


async def desired_from_settings(source: Any = None) -> DesiredGuestNetwork | None:
    """The desired guest network from the operator settings, or None.

    None means "this instance has not described a guest network", which is the
    honest answer for a fresh install and the state every consumer must handle:
    no card contents, no plan, no fence at provision time. An INVALID stored
    combination raises, because silently ignoring it would fence nothing while
    the settings page says otherwise.
    """
    from ..app_settings import REGISTRY, SettingsResolver, bound_resolver, resolver_from_state

    resolver: SettingsResolver | None
    if isinstance(source, SettingsResolver):
        resolver = source
    elif source is not None:
        resolver = resolver_from_state(source) or bound_resolver()
    else:
        resolver = bound_resolver()
    if resolver is None:
        return None

    values: dict[str, Any] = {}
    for key in KEYS:
        try:
            values[key] = await resolver.value(key)
        except Exception:  # pragma: no cover - a bad row never breaks a read path
            logger.warning("Could not resolve %s; treating it as unset", key)
            values[key] = REGISTRY[key].parse("") if key in REGISTRY else ""

    subnet = str(values.get("guest_network_subnet") or "").strip()
    gateway = str(values.get("guest_network_gateway") or "").strip()
    if not subnet or not gateway:
        # A zone/vnet name with nowhere to put them is not a guest network.
        return None

    return DesiredGuestNetwork(
        zone=str(values.get("guest_network_zone") or ""),
        vnet=str(values.get("guest_network_vnet") or ""),
        subnet_cidr=subnet,
        gateway=gateway,
        snat=_flag(values.get("guest_network_snat", 1)),
        dhcp=_flag(values.get("guest_network_dhcp", 1)),
        dhcp_range=str(values.get("guest_network_dhcp_range") or ""),
        dhcp_dns_server=str(values.get("guest_network_dhcp_dns_server") or ""),
        isolate_cidrs=tuple(split_cidrs(values.get("guest_network_isolate_cidrs"))),
    )


def split_cidrs(raw: Any) -> list[str]:
    """The isolate list as stored: comma or whitespace separated CIDRs."""
    if raw is None:
        return []
    if isinstance(raw, list | tuple):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [part.strip() for part in re.split(r"[,\s]+", str(raw)) if part.strip()]


# ── The report both surfaces serve (#553: GET /admin/guest-network, MCP) ─────


def gateway_for(proxmox: Any) -> Any:
    """The PVE gateway for a HomePilot Proxmox client, or None.

    One line, in one place, because every caller here (the report, the
    executor, the drift check, the provision fence) must build it the same way
    - and because a test hands a fake gateway straight to `survey`/`execute`
    instead, which is exactly what makes those testable without a cluster.
    """
    from ..adapters.pve_sdn import gateway_from

    return gateway_from(proxmox)


async def guest_network_report(state: Any, proxmox: Any = None) -> dict[str, Any]:
    """Survey, desired and plan in one read, for the API and the MCP tool alike.

    ONE function so the console and the assistant cannot describe the estate
    differently. Every "we could not" is a stated field rather than an
    exception: an instance with no guest network configured, and an instance
    whose Proxmox is not wired up, are both legitimate states to report.
    """
    result: dict[str, Any] = {
        "configured": False,
        "desired": None,
        "survey": None,
        "plan": None,
        "detail": "",
        "enforcement": "",
    }

    try:
        desired = await desired_from_settings(state)
    except GuestNetworkError as exc:
        result["detail"] = (
            f"The stored guest-network settings do not describe a usable network: {exc}"
        )
        return result

    if desired is None:
        result["detail"] = (
            "No guest network is configured on this instance. Set "
            "guest_network_subnet and guest_network_gateway on Settings -> "
            "Subsystems -> Guest network."
        )
        return result

    result["configured"] = True
    result["desired"] = desired.to_dict()

    if proxmox is None:
        from ..app_settings import _proxmox_from

        proxmox = _proxmox_from(state)
    gateway = gateway_for(proxmox)
    if gateway is None:
        result["detail"] = (
            "Proxmox is not configured on this instance, so the cluster could not be "
            "surveyed. The desired network above is what an apply would build."
        )
        return result

    node = ""
    try:
        from ..app_settings import resolver_from_state

        resolver = resolver_from_state(state)
        if resolver is not None:
            node = str(await resolver.value("provision_default_node") or "")
    except Exception:  # pragma: no cover - the survey resolves a node itself
        node = ""

    current = await survey(gateway, desired, node)
    the_plan = plan(desired, current)
    result["survey"] = current.to_dict()
    result["plan"] = the_plan.to_dict()
    result["enforcement"] = enforcement_note(current)
    if the_plan.converged:
        result["detail"] = "The cluster matches the desired guest network."
    elif the_plan.blockers:
        result["detail"] = "; ".join(the_plan.blockers)
    else:
        result["detail"] = (
            f"{len(the_plan.steps)} step(s) pending. They ship as a guest-network "
            "artifact: propose, approve with the code, apply."
        )
    return result


def enforcement_note(current: GuestNetworkSurvey) -> str:
    """What the vnet firewall rules are actually worth on THIS node's stack."""
    if current.firewall_stack == "nftables":
        return (
            f"Node {current.node} runs the nftables proxmox-firewall, so the vnet "
            "forward rules below are enforced. The per-VM rules HomePilot writes at "
            "provision time fence each guest as well."
        )
    if current.firewall_stack == "legacy":
        return (
            f"Node {current.node} runs the LEGACY iptables firewall, which stores vnet "
            "firewall rules but does not enforce them on vnet forward traffic. The "
            "fence that holds today is the per-VM rule set HomePilot writes at "
            "provision time; the vnet rules become live if the node is switched to "
            "the nftables proxmox-firewall."
        )
    return (
        "Which firewall stack the node runs could not be read, so whether the vnet "
        "forward rules are enforced is unknown. The per-VM rules written at provision "
        "time are enforced by both stacks."
    )


__all__ = [
    "KEYS",
    "DesiredGuestNetwork",
    "GuestNetworkError",
    "GuestNetworkSurvey",
    "Plan",
    "Step",
    "desired_from_settings",
    "enforcement_note",
    "execute",
    "fence_rules",
    "gateway_for",
    "guest_network_report",
    "plan",
    "split_cidrs",
    "survey",
]
