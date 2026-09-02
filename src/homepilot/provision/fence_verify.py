"""Is the guest fence ESTABLISHED, or merely written? Asked from inside the guest.

Every layer below this one reports the fence as a fact about configuration:
the NIC carries ``firewall=1``, the per-VM rules are on the tap, the datacenter
firewall is on, the node runs a stack that enforces. All of that was true of
the first real friend's guest on prod - and nobody had ever run a command
inside a guest and watched a packet towards the operator LAN go nowhere.
``configured`` is not ``established`` (#642), and the fence is the one
property of this product where the difference is a stranger on your LAN.

So a provision, once the guest is up, asks the guest itself: open a TCP
connection to an address HomePilot KNOWS is alive inside the isolated range
(the Proxmox host it just cloned the guest through), and to one it expects to
reach outside it (the guest subnet's gateway resolver, the guest's configured
nameserver). The verdict is built from what came back, and only from that:

* the isolated address ANSWERED (a SYN-ACK, or a RST) -> the fence does not
  hold. The provision fails and the guest is destroyed, exactly as when the
  rules could not be written - the guest is on the operator's LAN either way.
* the isolated address stayed silent AND a control answered -> VERIFIED. A
  DROP at the tap is precisely "no answer, while the network is fine".
* anything else -> UNVERIFIED, with the reason. Silence from both is a guest
  with no network yet, not a fence; EPERM is qemu-guest-agent's own SELinux
  confinement and says nothing about the wire; a guest with no python3 and no
  bash could not be asked. None of these is a verdict, so none is given one.

Nothing in here writes anything. The probe is a connect() with a four-second
timeout, run through the guest agent, and the guest's own words come back
through the same scrub every other guest output passes through.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# The connect() timeout INSIDE the guest, per target. A DROP produces exactly a
# timeout, so this is the time a verified fence costs; an answer, either kind,
# comes back in milliseconds.
PROBE_CONNECT_TIMEOUT_S = 4

# What the in-guest probe prints per target. One token, so the verdict is read
# off a word rather than parsed out of a sentence, and so a fake in the suite
# and a real guest are held to the same contract.
CONNECTED = "CONNECTED"  # the target completed the handshake
REFUSED = "REFUSED"  # the target sent a RST: no listener, but it was REACHED
TIMEOUT = "TIMEOUT"  # nothing came back at all
EPERM = "EPERM"  # the guest forbade the connect before a packet left
UNREACH = "UNREACH"  # the guest's own stack or a router said no route
NOTOOL = "NOTOOL"  # neither python3 nor bash: nothing could be asked
# Any other errno arrives as OTHER:<the guest's words>.
OTHER = "OTHER"

# Outcomes that mean the target was REACHED - the only two that settle a breach.
_REACHED = frozenset({CONNECTED, REFUSED})


class FenceVerdict(StrEnum):
    """What the probe ESTABLISHED about the fence, never what it hoped.

    VERIFIED   - a known-alive address inside the isolated range gave no
                 answer while an address outside it did.
    BREACHED   - an address inside the isolated range answered. The guest can
                 reach the operator's LAN; the provision must not hand it over.
    UNVERIFIED - nothing was established either way, and `detail` says why.
    """

    VERIFIED = "verified"
    BREACHED = "breached"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class ProbeTarget:
    """One address:port the guest is asked to connect to, and why."""

    host: str
    port: int
    role: str  # "isolated" or "control"
    label: str  # what it is, for the record: "the Proxmox host", "the guest gateway"

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class ProbeResult:
    target: ProbeTarget
    outcome: str  # one of the tokens above, or OTHER:<words>

    @property
    def reached(self) -> bool:
        return self.outcome in _REACHED

    def as_record(self) -> dict[str, Any]:
        return {
            "target": self.target.endpoint,
            "what": self.target.label,
            "outcome": self.outcome,
        }


def probe_script(targets: list[ProbeTarget]) -> str:
    """The shell the guest agent runs: one line `host:port TOKEN` per target.

    python3 first, because its errno is exact; bash's /dev/tcp second, reading
    the shell's own error text; and a guest with neither says NOTOOL for every
    target rather than failing in a way that looks like a network event.

    Targets are validated addresses and ports, so they are safe to interpolate:
    `ProbeTarget.host` is only ever built from an `ipaddress` object or a
    hostname that resolved, never from request input.
    """
    endpoints = " ".join(t.endpoint for t in targets)
    # Double quotes only inside the python source: the whole of it is handed
    # to the guest's shell inside single quotes.
    python = (
        "import socket,sys,errno\n"
        'names={errno.ECONNREFUSED:"REFUSED",errno.EPERM:"EPERM",errno.EACCES:"EPERM",'
        'errno.ENETUNREACH:"UNREACH",errno.EHOSTUNREACH:"UNREACH"}\n'
        "for t in sys.argv[1:]:\n"
        '    h,p=t.rsplit(":",1)\n'
        "    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
        f"    s.settimeout({PROBE_CONNECT_TIMEOUT_S})\n"
        "    try:\n"
        '        s.connect((h,int(p))); r="CONNECTED"\n'
        "    except socket.timeout:\n"
        '        r="TIMEOUT"\n'
        "    except OSError as e:\n"
        '        r=names.get(e.errno) or "OTHER:"+" ".join((e.strerror or str(e)).split())\n'
        "    finally:\n"
        "        s.close()\n"
        "    print(t,r)\n"
    )
    bash = (
        "for t in $HP_TARGETS; do "
        'h="${t%:*}"; p="${t##*:}"; '
        f'err=$(timeout {PROBE_CONNECT_TIMEOUT_S} bash -c "exec 3<>/dev/tcp/$h/$p" 2>&1); rc=$?; '
        'if [ "$rc" -eq 0 ]; then r=CONNECTED; '
        'elif [ "$rc" -eq 124 ]; then r=TIMEOUT; '
        'else case "$err" in '
        "*refused*) r=REFUSED;; "
        "*ermission*) r=EPERM;; "
        "*nreachable*|*route*) r=UNREACH;; "
        "*) r=\"OTHER:$(echo $err | tr -s ' ' | cut -c1-120)\";; "
        "esac; fi; "
        'echo "$t $r"; done'
    )
    return (
        f"HP_TARGETS='{endpoints}'; export HP_TARGETS; "
        "if command -v python3 >/dev/null 2>&1; then "
        f"python3 -c '{python}' $HP_TARGETS; "
        "elif command -v bash >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then "
        f"{bash}; "
        'else for t in $HP_TARGETS; do echo "$t NOTOOL"; done; fi'
    )


def parse_probe_output(out: str, targets: list[ProbeTarget]) -> list[ProbeResult]:
    """Match the guest's lines back to the targets. A target the guest said
    nothing about is recorded as OTHER, not invented as a timeout."""
    said: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] not in said:
            said[parts[0]] = parts[1].strip()
    results: list[ProbeResult] = []
    for target in targets:
        token = said.get(target.endpoint)
        if token is None:
            token = f"{OTHER}:the guest printed nothing for this target"
        elif token.split(":", 1)[0] not in {CONNECTED, REFUSED, TIMEOUT, EPERM, UNREACH, NOTOOL}:
            token = f"{OTHER}:{token[:120]}"
        results.append(ProbeResult(target=target, outcome=token))
    return results


def _inside(address: str, cidrs: tuple[str, ...] | list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


async def resolve_ipv4(host: str) -> str | None:
    """The IPv4 address a name resolves to, or None. A literal comes back as is."""
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, socket.AF_INET)
    except (OSError, ValueError):
        return None
    for info in infos:
        addr = info[4][0]
        if isinstance(addr, str):
            return addr
    return None


async def isolated_targets(
    proxmox_host: str | None, proxmox_port: int, isolate_cidrs: tuple[str, ...] | list[str]
) -> list[ProbeTarget]:
    """Addresses HomePilot KNOWS are alive inside the isolated range, or none.

    Today that is the Proxmox host: HomePilot cloned this very guest through
    it seconds ago, so a silent SYN towards it is not "the target is down".
    When the Proxmox host is not inside any isolated range (a hypervisor
    reachable only over a management VLAN that guests could never see anyway)
    there is nothing HomePilot can vouch for, and the fence stays unverified
    rather than being tested against an address that might simply be absent.

    TWO ports on that one host: the API port, which always listens, and 53,
    which does not. A fence is a DROP, so both stay silent behind it - and
    without it the API port completes a handshake while 53 answers with a
    reset, which proves the host was reached just as well. The second port is
    there for guests whose qemu-guest-agent may only open DNS: on an
    SELinux-enforcing image the agent runs confined and gets EPERM on
    arbitrary ports before a packet leaves, while tcp/53 is allowed - proven
    live on dev 2026-08-29. Without it every such image would read
    `unverified` forever.
    """
    if not proxmox_host or not isinstance(proxmox_host, str):
        return []
    address = await resolve_ipv4(proxmox_host)
    if address is None or not _inside(address, isolate_cidrs):
        return []
    targets = [
        ProbeTarget(host=address, port=proxmox_port, role="isolated", label="the Proxmox host")
    ]
    if proxmox_port != 53:
        targets.append(
            ProbeTarget(host=address, port=53, role="isolated", label="the Proxmox host, dns port")
        )
    return targets


def control_targets(
    gateway: str | None, nameserver: str | None, isolate_cidrs: tuple[str, ...] | list[str]
) -> list[ProbeTarget]:
    """Addresses the guest is EXPECTED to reach, so a silent isolated range can
    be told apart from a guest with no network at all.

    The guest gateway's resolver first (the fence ACCEPTs tcp/53 to it by
    design, and an SDN DHCP zone's dnsmasq listens there); the guest's own
    nameserver second, if it is not itself inside the fence.
    """
    out: list[ProbeTarget] = []
    if gateway:
        out.append(ProbeTarget(host=gateway, port=53, role="control", label="the guest gateway"))
    if nameserver and nameserver != gateway and not _inside(nameserver, isolate_cidrs):
        try:
            ipaddress.ip_address(nameserver)
        except ValueError:
            return out
        out.append(
            ProbeTarget(host=nameserver, port=53, role="control", label="the guest's nameserver")
        )
    return out


def judge(results: list[ProbeResult]) -> tuple[FenceVerdict, str]:
    """The verdict, and the sentence that justifies it, from the probe alone."""
    isolated = [r for r in results if r.target.role == "isolated"]
    controls = [r for r in results if r.target.role == "control"]
    if not isolated:
        return FenceVerdict.UNVERIFIED, (
            "No address HomePilot knows to be alive lies inside the isolated ranges, so "
            "nothing could be probed."
        )
    breached = [r for r in isolated if r.reached]
    if breached:
        hit = breached[0]
        how = "completed a TCP handshake" if hit.outcome == CONNECTED else "answered with a reset"
        return FenceVerdict.BREACHED, (
            f"The guest reached {hit.target.endpoint} ({hit.target.label}) inside the isolated "
            f"range: the connection {how}. The fence does not hold."
        )
    # A silence is only evidence next to an answer. Any isolated port that
    # timed out counts - the one the agent was allowed to try is the one that
    # can speak for the wire.
    silent = [r for r in isolated if r.outcome == TIMEOUT]
    if silent:
        probe = silent[0]
        answered = [r for r in controls if r.reached]
        if answered:
            ctrl = answered[0]
            return FenceVerdict.VERIFIED, (
                f"From inside the guest, {probe.target.endpoint} ({probe.target.label}) gave "
                f"no answer in {PROBE_CONNECT_TIMEOUT_S}s while {ctrl.target.endpoint} "
                f"({ctrl.target.label}) answered. The fence holds."
            )
        if not controls:
            return FenceVerdict.UNVERIFIED, (
                f"{probe.target.endpoint} ({probe.target.label}) gave no answer, but there "
                "was no address outside the fence to compare against, so a guest with no "
                "network yet would look the same."
            )
        seen = ", ".join(f"{r.target.endpoint} {r.outcome}" for r in controls)
        return FenceVerdict.UNVERIFIED, (
            f"{probe.target.endpoint} ({probe.target.label}) gave no answer, but neither "
            f"did anything outside the fence ({seen}). The guest may have no network yet; "
            "silence on both sides proves nothing about the fence."
        )
    # Nothing reached and nothing silent: every isolated probe failed to run
    # for a reason of its own. Name the most telling one.
    heads = {r.outcome.split(":", 1)[0]: r for r in isolated}
    if NOTOOL in heads:
        return FenceVerdict.UNVERIFIED, (
            "The guest has neither python3 nor bash, so nothing inside it could open a "
            "connection to check the fence."
        )
    if EPERM in heads:
        tried = ", ".join(r.target.endpoint for r in isolated)
        return FenceVerdict.UNVERIFIED, (
            f"The guest forbade the probe before a packet left it (EPERM on {tried}) - on "
            "an SELinux-enforcing image qemu-guest-agent runs confined and may not open "
            "arbitrary ports. That is a limit of the agent, not evidence about the wire."
        )
    if UNREACH in heads:
        probe = heads[UNREACH]
        return FenceVerdict.UNVERIFIED, (
            f"The guest reported no route towards {probe.target.endpoint} "
            f"({probe.target.label}). A missing route is not the tap fence, so nothing "
            "about the fence was established."
        )
    probe = isolated[0]
    words = probe.outcome.split(":", 1)[1] if ":" in probe.outcome else probe.outcome
    return FenceVerdict.UNVERIFIED, (
        f"The probe towards {probe.target.endpoint} ({probe.target.label}) ended with "
        f"'{words}', which is neither an answer nor a silence HomePilot can read."
    )


__all__ = [
    "CONNECTED",
    "EPERM",
    "NOTOOL",
    "OTHER",
    "PROBE_CONNECT_TIMEOUT_S",
    "REFUSED",
    "TIMEOUT",
    "UNREACH",
    "FenceVerdict",
    "ProbeResult",
    "ProbeTarget",
    "control_targets",
    "isolated_targets",
    "judge",
    "parse_probe_output",
    "probe_script",
]
