"""The one lockout guard every PVE firewall-options write passes (#600, #707).

#600 made the guest-network path enable the datacenter firewall safely, and
nothing else: a ``proxmox-api-sequence`` step ``PUT /cluster/firewall/options
{enable: 1}`` went straight to the wire, PVE defaulted the input policy to DROP,
and that is the exact node lockout #600 was filed about (#707). A guard that
lives in one caller protects one caller.

So the rule lives HERE, and the two clients HomePilot talks to PVE through call
it on the wire, for every write, whatever asked for it:

* ``ProxmoxClient.call`` (every artifact step, every HTTP write HomePilot makes)
  runs :func:`check_firewall_options_write` before any mutation of
  ``/cluster/firewall/options`` or ``/nodes/{node}/firewall/options``;
* ``PveSdnGateway.ensure_datacenter_firewall_enabled`` (the guest-network path)
  runs the same function before its own options write.

THE RULE. A write that would leave a firewall ENABLED with an input policy of
DROP or REJECT (PVE's default when ``policy_in`` is unset is DROP) is sent only
when a management-allow is VERIFIED on the cluster's own rules: for the SSH port
and the API port, an enabled ``in`` ACCEPT rule HomePilot can prove matches the
management source, with nothing that might drop that traffic ahead of it.
Anything less - a rule HomePilot cannot read, a security group whose contents it
does not resolve, an ipset or alias source, an interface restriction - is not
verification, and the write is REFUSED.

REFUSED, NOT FIXED (owner rule). HomePilot never adds, moves or loosens a rule
or a policy to make a write pass. An operator's firewall is theirs; the guard
says why it will not send the write and stops there.

Which source counts as "management": the address HomePilot itself reaches the
PVE API from (computed locally, never sent anywhere), because that is the one
management client HomePilot can name. When it cannot be determined - or it is a
loopback, which PVE always accepts and so proves nothing about anyone else -
only a rule with NO source restriction counts. PVE's own built-in allow for its
``management`` ipset is deliberately NOT relied on: that ipset is derived from
the cluster network, and the #600 lockout happened with it in place.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote

# Input policies that drop what no rule accepts. REJECT severs management
# exactly as DROP does - it only answers faster.
LOCKOUT_POLICIES = frozenset({"DROP", "REJECT"})
SSH_PORT = 22
DEFAULT_API_PORT = 8006

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class PveFirewallLockoutError(RuntimeError):
    """A firewall write HomePilot refuses to send, because it could sever management.

    Raised rather than sent to the wire: an enabled firewall with an input
    policy of DROP/REJECT has no management-allow rule PVE adds for you that
    HomePilot can rely on, so without a VERIFIED allow it locks out SSH and the
    API. HomePilot refuses to be the thing that does that.
    """


@dataclass(frozen=True)
class OptionsScope:
    """Which firewall-options object a path writes: the datacenter's or a node's."""

    kind: str  # "cluster" | "node"
    node: str = ""

    def describe(self) -> str:
        if self.kind == "cluster":
            return "the datacenter firewall"
        return f"node {self.node}'s firewall"


class FirewallReader(Protocol):
    """The reads the guard needs, from whichever client is about to write."""

    async def cluster_firewall_options(self) -> Mapping[str, Any]: ...

    async def cluster_firewall_rules(self) -> Sequence[Mapping[str, Any]]: ...

    async def node_firewall_options(self, node: str) -> Mapping[str, Any]: ...

    async def node_firewall_rules(self, node: str) -> Sequence[Mapping[str, Any]]: ...

    async def node_names(self) -> Sequence[str]: ...


# ── Small readers of PVE's vocabulary ────────────────────────────────────────


def fw_flag(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def policy_in(options: Mapping[str, Any]) -> str:
    """The input policy, upper-cased, defaulting to DROP when unset.

    The default is the load-bearing part: PVE treats an ENABLED firewall with no
    explicit ``policy_in`` as DROP, so "absent" is the lockout case, not a safe
    blank.
    """
    return str(options.get("policy_in", "") or "").strip().upper() or "DROP"


def _path_segments(path: str) -> list[str]:
    """A PVE API path as its segments, refusing dot segments outright.

    ``cluster/./firewall/options`` and ``nodes/x/../../cluster/firewall/options``
    are the options path under another spelling; a PVE API path never needs a
    dot segment, so a write that carries one is refused rather than resolved.
    """
    raw = path.split("?", 1)[0].split("#", 1)[0]
    segments = [unquote(s) for s in raw.split("/") if s != ""]
    if any(s in (".", "..") for s in segments):
        raise PveFirewallLockoutError(
            f"refusing a PVE write to {path!r}: a dot segment could address the firewall "
            "options under another name, and a PVE API path never needs one"
        )
    return segments


def firewall_options_scope(path: str) -> OptionsScope | None:
    """The firewall-options object a PVE API path (relative to ``/api2/json``) writes."""
    segments = _path_segments(path)
    if segments == ["cluster", "firewall", "options"]:
        return OptionsScope("cluster")
    if len(segments) == 4 and segments[0] == "nodes" and segments[2:] == ["firewall", "options"]:
        return OptionsScope("node", segments[1])
    return None


def addresses_firewall_options(url_path: str) -> bool:
    """Does a full URL path end at a PVE firewall-options object, under any prefix?

    For the http-sequence executor, whose base URL may or may not carry
    ``/api2/json``: it is a generic HTTP client, so it cannot run the guard and
    instead refuses to write PVE firewall options at all.
    """
    segments = [s.lower() for s in _path_segments(url_path)]
    if segments[-3:] == ["cluster", "firewall", "options"]:
        return True
    tail = ["firewall", "options"]
    return len(segments) >= 4 and segments[-4] == "nodes" and segments[-2:] == tail


def query_params(path: str) -> dict[str, Any]:
    """Parameters carried in the path's own query string (PVE reads those too)."""
    if "?" not in path:
        return {}
    return dict(parse_qsl(path.split("?", 1)[1].split("#", 1)[0], keep_blank_values=True))


def _deleted_keys(write: Mapping[str, Any]) -> set[str]:
    raw = write.get("delete")
    if raw is None:
        return set()
    if isinstance(raw, list | tuple):
        items: Iterable[Any] = raw
    else:
        items = str(raw).replace(";", ",").replace(" ", ",").split(",")
    return {str(item).strip() for item in items if str(item).strip()}


def effective_options(current: Mapping[str, Any], write: Mapping[str, Any]) -> dict[str, Any]:
    """The options as they will stand after PVE applies this PUT.

    PVE's options PUT sets what it is given and resets every key named in
    ``delete`` to its default; ``digest`` is a concurrency check, not a value.
    """
    after = dict(current)
    for key in _deleted_keys(write):
        after.pop(key, None)
    for key, value in write.items():
        if key in ("delete", "digest"):
            continue
        after[key] = value
    return after


def _touches(write: Mapping[str, Any], keys: Iterable[str]) -> bool:
    deleted = _deleted_keys(write)
    return any(key in write or key in deleted for key in keys)


def cluster_lockout_shaped(options: Mapping[str, Any]) -> bool:
    """Enabled with an input policy that drops what no rule accepts."""
    return fw_flag(options.get("enable"), default=False) and policy_in(options) in LOCKOUT_POLICIES


# ── The management-allow verification ────────────────────────────────────────


def _ports_of(spec: Any) -> set[int] | None:
    """The ports a ``dport``/``sport`` spec names, or None if it cannot be read.

    PVE takes ``22``, ``22,8006``, ``8000:9000`` (and service names, which are
    not resolved here - an unreadable spec is never treated as covering a port).
    """
    text = str(spec or "").strip()
    ports: set[int] = set()
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        low, sep, high = item.replace("-", ":").partition(":")
        try:
            if sep:
                a, b = int(low), int(high)
                if b - a > 70000:  # pragma: no cover - not a port range
                    return None
                ports.update(range(a, b + 1))
            else:
                ports.add(int(item))
        except ValueError:
            return None
    return ports


def _address_items(spec: Any) -> list[str]:
    return [s.strip() for s in str(spec or "").split(",") if s.strip()]


def _literal_contains(item: str, source: IpAddress) -> bool | None:
    """Does a literal address item contain the source? None when it is not a literal."""
    try:
        if "-" in item and "/" not in item:
            low_s, high_s = item.split("-", 1)
            low, high = ipaddress.ip_address(low_s.strip()), ipaddress.ip_address(high_s.strip())
            if low.version != source.version:
                return False
            return int(low) <= int(source) <= int(high)
        network = ipaddress.ip_network(item, strict=False)
    except ValueError:
        return None  # an alias, an ipset (+name), a dc/ or guest/ reference
    if network.version != source.version:
        return False
    return source in network


def _source_definitely_covers(spec: Any, source: IpAddress | None) -> bool:
    items = _address_items(spec)
    if not items:
        return True
    if source is None:
        return False
    return any(_literal_contains(item, source) is True for item in items)


def _source_may_cover(spec: Any, source: IpAddress | None) -> bool:
    items = _address_items(spec)
    if not items or source is None:
        return True
    return any(_literal_contains(item, source) is not False for item in items)


def _proto_is_tcp(rule: Mapping[str, Any]) -> bool:
    """Does the rule's protocol (unset = any) include TCP?"""
    proto = str(rule.get("proto", "") or "").strip().lower()
    return proto in ("", "tcp", "6")


def _definitely_accepts(rule: Mapping[str, Any], port: int, source: IpAddress | None) -> bool:
    """Is this rule an enabled ACCEPT that PROVABLY matches management on ``port``?"""
    if str(rule.get("type", "")).lower() != "in" or str(rule.get("action", "")).upper() != "ACCEPT":
        return False
    # An ACCEPT counts only when it says it is enabled; a rule with no `enable`
    # is not one HomePilot can prove the cluster compiles.
    if "enable" not in rule or not fw_flag(rule.get("enable")):
        return False
    # Restrictions HomePilot cannot evaluate from here make the rule unprovable.
    if any(str(rule.get(key, "") or "").strip() for key in ("iface", "dest", "sport", "icmp-type")):
        return False
    macro = str(rule.get("macro", "") or "").strip()
    if macro:
        covered = {SSH_PORT} if macro.lower() == "ssh" else set()
        return port in covered and _source_definitely_covers(rule.get("source"), source)
    if not _proto_is_tcp(rule):
        return False
    dport = str(rule.get("dport", "") or "").strip()
    if dport:
        ports = _ports_of(dport)
        if ports is None or port not in ports:
            return False
    return _source_definitely_covers(rule.get("source"), source)


def _may_block(rule: Mapping[str, Any], port: int, source: IpAddress | None) -> bool:
    """Could this rule drop management traffic on ``port`` before an allow is reached?"""
    kind = str(rule.get("type", "")).lower()
    if kind not in ("in", "group"):
        return False
    # A disabled rule is not compiled; a rule that does not SAY it is disabled
    # is assumed live, the fail-safe reading for something that may block.
    if "enable" in rule and not fw_flag(rule.get("enable")):
        return False
    if kind == "group":
        # A security group's own rules are not resolved here, so whether it
        # drops management is unknown - and unknown is not verified.
        return True
    action = str(rule.get("action", "")).upper()
    if action == "ACCEPT":
        return False
    if action not in ("DROP", "REJECT"):
        return True  # an action HomePilot does not model may well stop traffic
    if str(rule.get("macro", "") or "").strip():
        return True  # a macro's port set is not resolved here
    if not _proto_is_tcp(rule):
        return False
    dport = str(rule.get("dport", "") or "").strip()
    if dport:
        ports = _ports_of(dport)
        if ports is not None and port not in ports:
            return False
    return _source_may_cover(rule.get("source"), source)


def _ordered(rules: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    def key(pair: tuple[int, Mapping[str, Any]]) -> tuple[int, int]:
        index, rule = pair
        try:
            return (int(rule.get("pos", index)), index)
        except (TypeError, ValueError):
            return (index, index)

    return [rule for _, rule in sorted(enumerate(rules), key=key)]


def _first_verdict(
    chain: Sequence[Mapping[str, Any]], port: int, source: IpAddress | None
) -> str | None:
    """None when ``port`` is provably accepted on this chain; else why it is not."""
    for rule in chain:
        if _definitely_accepts(rule, port, source):
            return None
        if _may_block(rule, port, source):
            what = rule.get("comment") or rule.get("action") or rule.get("type")
            return (
                f"a rule that may drop it comes first (pos {rule.get('pos', '?')}: "
                f"{rule.get('type')} {rule.get('action', '')} {what!r})"
            )
    return "no enabled 'in' ACCEPT rule provably matches it"


def management_allow_problems(
    cluster_rules: Sequence[Mapping[str, Any]],
    node_rules: Mapping[str, Sequence[Mapping[str, Any]]],
    ports: Sequence[int],
    source: IpAddress | None,
) -> list[str]:
    """Why management is NOT provably allowed, or [] when it is.

    For every node and every management port, the allow must hold whichever way
    the host chain is assembled - the node's rules ahead of the datacenter's or
    behind them - so the answer never depends on an ordering assumption.
    """
    problems: list[str] = []
    who = f"from {source}" if source is not None else "from any source"
    cluster_chain = _ordered(cluster_rules)
    for node, rules in sorted(node_rules.items()):
        node_chain = _ordered(rules)
        for port in ports:
            for chain in (node_chain + cluster_chain, cluster_chain + node_chain):
                why = _first_verdict(chain, port, source)
                if why is not None:
                    problems.append(
                        f"node {node}: tcp/{port} {who} is not verified allowed - {why}"
                    )
                    break
    return problems


# ── The guard ────────────────────────────────────────────────────────────────


async def check_firewall_options_write(
    scope: OptionsScope,
    write: Mapping[str, Any],
    reader: FirewallReader,
    *,
    ports: Sequence[int] = (SSH_PORT, DEFAULT_API_PORT),
    source: IpAddress | None = None,
    current: Mapping[str, Any] | None = None,
) -> str | None:
    """Refuse a firewall-options write that would leave management unverified.

    Returns None when the write does not leave a lockout-shaped firewall (or
    does not touch the switch at all), a sentence naming what was verified when
    it does and the allow holds, and raises :class:`PveFirewallLockoutError`
    otherwise. Every read failure is a refusal: what could not be read was not
    verified.
    """
    switch_keys = ("enable", "policy_in") if scope.kind == "cluster" else ("enable",)
    if not _touches(write, switch_keys):
        return None
    what = scope.describe()

    try:
        cluster_before = dict(
            current
            if current is not None and scope.kind == "cluster"
            else await reader.cluster_firewall_options()
        )
    except Exception as exc:
        raise PveFirewallLockoutError(
            f"refusing to write {what}'s options: the datacenter firewall options could not be "
            f"read, so whether this write locks out management cannot be verified ({exc})"
        ) from exc

    if scope.kind == "cluster":
        after = effective_options(cluster_before, write)
        if not cluster_lockout_shaped(after):
            return None
        nodes_desc = "every node"
        try:
            nodes = list(await reader.node_names())
        except Exception as exc:
            raise PveFirewallLockoutError(
                f"refusing to leave {what} enabled with policy_in={policy_in(after)}: the "
                f"cluster's nodes could not be listed, so management cannot be verified ({exc})"
            ) from exc
        if not nodes:
            raise PveFirewallLockoutError(
                f"refusing to leave {what} enabled with policy_in={policy_in(after)}: the "
                "cluster listed no nodes, so management cannot be verified on any of them"
            )
        policy = policy_in(after)
    else:
        try:
            node_before = dict(
                current if current is not None else await reader.node_firewall_options(scope.node)
            )
        except Exception as exc:
            raise PveFirewallLockoutError(
                f"refusing to write {what}'s options: they could not be read, so whether this "
                f"write locks out management cannot be verified ({exc})"
            ) from exc
        after = effective_options(node_before, write)
        # PVE's node firewall is ON unless it says otherwise; it drops by the
        # DATACENTER's input policy once the datacenter switch is on.
        node_on = fw_flag(after.get("enable"), default=True)
        if not (node_on and cluster_lockout_shaped(cluster_before)):
            return None
        nodes = [scope.node]
        nodes_desc = f"node {scope.node}"
        policy = policy_in(cluster_before)

    try:
        cluster_rules = list(await reader.cluster_firewall_rules())
        node_rules = {node: list(await reader.node_firewall_rules(node)) for node in nodes}
    except Exception as exc:
        raise PveFirewallLockoutError(
            f"refusing to leave {what} enabled with policy_in={policy}: the firewall rules could "
            f"not be read, so a management-allow cannot be verified ({exc})"
        ) from exc

    problems = management_allow_problems(cluster_rules, node_rules, list(ports), source)
    if problems:
        raise PveFirewallLockoutError(
            f"refusing to leave {what} enabled with policy_in={policy}: management would not be "
            "verifiably reachable - " + "; ".join(problems) + ". HomePilot does not add or change "
            "firewall rules to make this pass: add an enabled 'in' ACCEPT rule for "
            f"tcp {', '.join(str(p) for p in ports)} from the management network ahead of any "
            "DROP/REJECT, or enable with policy_in=ACCEPT"
        )
    ports_text = "/".join(str(p) for p in ports)
    who = f"from {source}" if source is not None else "from any source"
    return f"management-allow verified on {nodes_desc}: tcp {ports_text} {who}"


# ── The management source ────────────────────────────────────────────────────


def _local_address_towards(host: str, port: int) -> IpAddress | None:
    """The local address the OS would use to reach ``host`` (a UDP connect sends nothing)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except OSError:
        return None
    for family, kind, proto, _canon, sockaddr in infos:
        with contextlib.suppress(OSError), socket.socket(family, kind, proto) as sock:
            sock.connect(sockaddr)
            address = ipaddress.ip_address(sock.getsockname()[0])
            if address.is_loopback or address.is_unspecified:
                # PVE always accepts loopback, so this proves nothing about the
                # management network: only an unrestricted allow will count.
                return None
            return address
    return None


async def management_source(host: str | None, port: int) -> IpAddress | None:
    """HomePilot's own address towards the PVE API, resolved off the event loop."""
    if not host:
        return None
    return await asyncio.to_thread(_local_address_towards, host, port)


__all__ = [
    "DEFAULT_API_PORT",
    "LOCKOUT_POLICIES",
    "SSH_PORT",
    "FirewallReader",
    "OptionsScope",
    "PveFirewallLockoutError",
    "addresses_firewall_options",
    "check_firewall_options_write",
    "cluster_lockout_shaped",
    "effective_options",
    "firewall_options_scope",
    "fw_flag",
    "management_allow_problems",
    "management_source",
    "policy_in",
    "query_params",
]
