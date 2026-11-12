"""Which inventory row is the machine HomePilot itself runs on (lane B1, review 02 step 3).

Nothing in the product knew. On dev the VM running the control plane sat in the
adoption queue as an ordinary discovered guest; on prod it had no agent and
nothing said so. A future "stop this VM" button (lane B3) without this fact can
switch HomePilot off from inside HomePilot.

HomePilot PROPOSES and an operator CONFIRMS, once (review Q3, recommendation B).
Auto-marking is refused on purpose: an address the control plane answers on can
belong to a NAT box or a proxy in front of it (the WireGuard `.10` case), and a
wrong self marker is worse than none - it would guard the wrong machine and
leave the real one unguarded.

The proposal weighs evidence, most deliberate first (``RANK_*``):

0. a guest holding the hub ADVERTISE address an operator configured;
1. the guest whose agent dials the hub from this container's DEFAULT GATEWAY.
   HomePilot ships as a container on a bridge network: the only address the
   process sees for itself is the bridge's (172.x), which no guest holds, and
   a default install sets no advertise address. But a connection from the
   container's gateway can only come from the docker host itself - so the
   agent on that connection runs on the machine HomePilot runs on (found on
   the 3.8.0-rc1 dev walk, where nothing else matched);
2. a guest holding the hub bind address;
3. a guest holding the address the operator's browser asked for (Host /
   X-Forwarded-Host) - weak: a reverse proxy in front makes it the proxy's;
4. a guest holding an address this process's hostname resolves to.

Addresses are read from lane B2's ``host_addresses`` table
(``addresses.host_addresses`` - the one address source). The guests sharing
the best rank are the candidates: exactly one is a proposal, several are a
question with no proposal, none is no proposal.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..runtime.blocking import run_blocking
from .addresses import host_addresses

logger = logging.getLogger(__name__)

# A name lookup must never hold the hosts page hostage.
_RESOLVE_TIMEOUT = 2.0

# settings key: the host id an operator said is NOT HomePilot's host, so the
# same proposal is not offered again (a different one still is).
DISMISSED_KEY = "self_guest_dismissed"

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)

_WILDCARDS = frozenset({"", "0.0.0.0", "::", "localhost"})  # nosec B104 - compared, never bound


# Evidence ranks, lowest wins. See the module docstring.
RANK_ADVERTISE = 0
RANK_COLOCATED_AGENT = 1
RANK_BIND = 2
RANK_PAGE = 3
RANK_HOSTNAME = 4


@dataclass(frozen=True)
class ControlPlaneAddress:
    """An IPv4 address the control plane is reached at, and why HomePilot thinks so."""

    address: str
    why: str
    rank: int = RANK_HOSTNAME


@dataclass(frozen=True)
class ColocatedAgent:
    """A connected agent that runs on the machine this container runs on."""

    agent_id: str
    hostname: str
    peer: str
    why: str


_ROUTE_FILE = Path("/proc/net/route")
# docker marks a container with /.dockerenv, podman with /run/.containerenv.
_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def default_gateway(route_file: Path = _ROUTE_FILE) -> str | None:
    """This process's IPv4 default gateway, from the kernel routing table."""
    try:
        lines = route_file.read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            raw = int(fields[2], 16)
        except ValueError:
            continue
        if raw == 0:
            continue
        # The table is in host byte order (little-endian on every platform
        # HomePilot ships for).
        return str(ipaddress.IPv4Address(raw.to_bytes(4, "little")))
    return None


def running_in_container(markers: tuple[Path, ...] = _CONTAINER_MARKERS) -> bool:
    """Whether this process runs in a container (the runtime leaves a marker file)."""
    return any(m.exists() for m in markers)


def colocated_agents(
    connected: list[dict[str, Any]], gateway: str | None, in_container: bool
) -> list[ColocatedAgent]:
    """Connected agents whose hub connection comes from this container's gateway.

    Only inside a container: there the gateway is the docker host's end of the
    bridge, so a connection from it was made ON the docker host. On a VM or
    bare metal the gateway is the LAN router, and an agent arriving from it is
    merely behind a NAT - no evidence of anything.
    """
    if not in_container or not gateway:
        return []
    out: list[ColocatedAgent] = []
    for a in connected:
        if str(a.get("peer") or "") == gateway:
            out.append(
                ColocatedAgent(
                    agent_id=str(a["agent_id"]),
                    hostname=str(a.get("hostname") or ""),
                    peer=gateway,
                    why=(
                        f"its agent connects to the hub from {gateway}, this container's "
                        "default gateway - i.e. from the docker host HomePilot runs on"
                    ),
                )
            )
    return out


def _host_part(value: str) -> str:
    v = (value or "").strip()
    v = v.removeprefix("http://").removeprefix("https://")
    v = v.split("/", 1)[0]
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    if v.count(":") == 1:
        v = v.split(":", 1)[0]
    return v


async def _resolve(name: str) -> list[str]:
    """IPv4 addresses for a name or a literal; [] when it does not resolve.

    Only a well-formed hostname is looked up: one of the names comes from the
    request's Host header, which the client chose.
    """
    try:
        return [str(ipaddress.IPv4Address(name))]
    except ipaddress.AddressValueError:
        pass
    if not _HOSTNAME_RE.match(name):
        return []
    try:
        infos = await asyncio.wait_for(
            run_blocking("net", socket.getaddrinfo, name, None, socket.AF_INET),
            timeout=_RESOLVE_TIMEOUT,
        )
    except (TimeoutError, OSError, UnicodeError) as exc:
        logger.debug("Could not resolve %r: %s", name, exc)
        return []
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


async def control_plane_addresses(
    settings: Any, request_host: str | None = None, forwarded_host: str | None = None
) -> list[ControlPlaneAddress]:
    """Where the control plane is reached, most deliberate source first.

    1. ``HP_AGENT_HUB_ADVERTISE_HOST`` - the address an operator told agents to
       dial, i.e. said this machine is reached at;
    2. the hub's bind address, when it is not a wildcard;
    3. the address the operator's browser reached this API on, and the host a
       reverse proxy says the browser asked for (X-Forwarded-Host) - only an IP
       or a name that resolves; a proxy in front makes this the proxy's
       address, which is why nothing is decided without the operator;
    4. this process's own hostname.

    Each carries its ``RANK_*``; the agent-from-the-gateway evidence (rank 1)
    is not an address and comes from ``colocated_agents``.

    Loopback addresses are dropped: every machine has them.
    """
    candidates: list[tuple[str, str, int]] = [
        (
            _host_part(getattr(settings, "agent_hub_advertise_host", "") or ""),
            "agent hub advertise address",
            RANK_ADVERTISE,
        ),
        (
            _host_part(getattr(settings, "agent_hub_host", "") or ""),
            "agent hub bind address",
            RANK_BIND,
        ),
        (_host_part(request_host or ""), "the address this page was opened at", RANK_PAGE),
        (
            _host_part((forwarded_host or "").split(",", 1)[0]),
            "the host the browser asked the proxy for (X-Forwarded-Host)",
            RANK_PAGE,
        ),
        (socket.gethostname(), "this process's own hostname", RANK_HOSTNAME),
    ]
    out: list[ControlPlaneAddress] = []
    seen: set[str] = set()
    for name, why, rank in candidates:
        if name.lower() in _WILDCARDS:
            continue
        for addr in await _resolve(name):
            if ipaddress.IPv4Address(addr).is_loopback or addr in seen:
                continue
            seen.add(addr)
            out.append(ControlPlaneAddress(addr, f"{why} ({name})" if name != addr else why, rank))
    return out


def _is_candidate(row: dict[str, Any]) -> bool:
    # A guest that exists and could be running HomePilot: not a template (it
    # cannot run), not gone, and a VM or container (a node runs guests, and a
    # manual host has no config to match).
    return (
        row.get("host_type") in ("qemu", "lxc")
        and not row.get("is_template")
        and not row.get("absent_since")
    )


async def self_guest_status(
    repo: Any,
    cp_addresses: list[ControlPlaneAddress],
    colocated: list[ColocatedAgent] | None = None,
) -> dict[str, Any]:
    """The confirmed self row (if any), and HomePilot's proposal when it is needed.

    A proposal is computed only when there is no confirmed row, or the
    confirmed row is gone from Proxmox (its machine was destroyed or its VMID
    reused, so the mark guards nothing).
    """
    confirmed = await repo.get_self_host()
    dismissed_row = await repo.get_setting(DISMISSED_KEY)
    dismissed = str(dismissed_row["value"]) if dismissed_row else None
    result: dict[str, Any] = {
        "confirmed": confirmed,
        "proposal": None,
        "candidates": [],
        "control_plane_addresses": [asdict(a) for a in cp_addresses],
        "colocated_agents": [asdict(a) for a in colocated or []],
        "dismissed_host_id": dismissed,
    }
    if confirmed is not None and not confirmed.get("absent_since"):
        return result

    # A guest's rank is that of the most deliberate evidence it matches (see
    # RANK_*). The advertise address an operator configured outranks the
    # address a browser happened to use (which may be a proxy's), so a proxy
    # guest matching only the latter does not make the real host ambiguous.
    cp_of = {a.address: a for a in cp_addresses}
    agent_evidence = {a.agent_id: a for a in colocated or []}
    rows = await repo.list_hosts(limit=1000, is_template=False)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not _is_candidate(row):
            continue
        evidence: list[tuple[int, str, str | None]] = []
        for h in await host_addresses(repo, row):
            cp = cp_of.get(h.address)
            if cp is not None:
                evidence.append(
                    (cp.rank, f"{h.detail} = {h.address}, which is {cp.why}", h.address)
                )
        agent = agent_evidence.get(str(row.get("agent_id") or ""))
        if agent is not None:
            evidence.append((RANK_COLOCATED_AGENT, f"{agent.hostname}: {agent.why}", None))
        if not evidence:
            continue
        rank, why, address = min(evidence, key=lambda e: e[0])
        candidates.append(
            {
                "host_id": row["id"],
                "hostname": row["hostname"],
                "proxmox_id": row.get("proxmox_id"),
                "node": row.get("node"),
                "address": address,
                "evidence": why,
                "rank": rank,
            }
        )
    candidates.sort(key=lambda c: (c["rank"], str(c["hostname"])))
    result["candidates"] = candidates
    if candidates:
        top = [c for c in candidates if c["rank"] == candidates[0]["rank"]]
        if len(top) == 1 and top[0]["host_id"] != dismissed:
            result["proposal"] = top[0]
    return result


__all__ = [
    "DISMISSED_KEY",
    "RANK_ADVERTISE",
    "RANK_BIND",
    "RANK_COLOCATED_AGENT",
    "RANK_HOSTNAME",
    "RANK_PAGE",
    "ColocatedAgent",
    "ControlPlaneAddress",
    "colocated_agents",
    "control_plane_addresses",
    "default_gateway",
    "running_in_container",
    "self_guest_status",
]
