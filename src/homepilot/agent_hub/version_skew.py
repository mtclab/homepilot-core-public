"""Is a managed host running the agent binary this control plane shipped?

`dist.py` says the rest: enrolment serves the agent from the image, so an agent
matches the hub *that enrolled it* - and then nothing ever upgrades it and
nothing reports the gap. Dev ran a v3.6.6 agent against a 3.6.15 hub for weeks
with every surface green, so a fix that lived in the Go binary shipped, was
released, was deployed, and changed nothing at all on any managed host. The
version is recorded on every register; nobody compared it.

That is the same belief the whole of #648 keeps finding, one layer down: the
product reports a healthy fleet from a fact it never checked. This module is the
comparison, in ONE place, so the self-check and the host list cannot disagree
about it - three hand-rolled copies of a rule is how #631 happened.

Deliberately conservative: a version this cannot parse is `None` (unknown), never
`False` (fine). An unreadable version is exactly the case where quietly
answering "up to date" would be worst.
"""

from __future__ import annotations

import re
from typing import Any

# `v3.6.15`, `3.6.15`, `3.6.15-dirty`, `3.6.15+local` - take the numeric core and
# ignore whatever a build appended to it.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def parse(version: str | None) -> tuple[int, int, int] | None:
    """The numeric core of a version string, or None when it is not one."""
    if not version:
        return None
    match = _VERSION_RE.match(str(version).strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_behind(agent_version: str | None, control_version: str | None) -> bool | None:
    """Is the agent OLDER than the control plane? None when it cannot be told.

    An agent AHEAD of the control plane is not "behind" - it is a different
    problem (a downgraded control plane), and calling it up-to-date would be the
    same rounding-up this module exists to refuse. It is reported as unknown.
    """
    agent = parse(agent_version)
    control = parse(control_version)
    if agent is None or control is None:
        return None
    if agent == control:
        return False
    return True if agent < control else None


def control_plane_version() -> str:
    from homepilot import __version__

    return str(__version__)


def summarise(agents: list[dict[str, Any]], control_version: str | None = None) -> dict[str, Any]:
    """Fleet-wide skew, from the agent records the hub already holds.

    Only CONNECTED agents are judged: a host that is not here cannot be running
    anything, and counting it as outdated would push an operator to chase a
    machine whose actual problem is that it is offline. `AgentRegistry.
    list_connected` returns only live agents and carries no `connected` key, so
    ABSENT means connected here; only an explicit False is skipped.
    """
    control = control_version or control_plane_version()
    behind: list[str] = []
    unknown: list[str] = []
    matched = 0
    for agent in agents:
        if agent.get("connected") is False:
            continue
        info = agent.get("system_info") or {}
        version = info.get("agent_version") if isinstance(info, dict) else None
        verdict = is_behind(version, control)
        name = str(agent.get("hostname") or agent.get("agent_id") or "?")
        if verdict is True:
            behind.append(f"{name} ({version})")
        elif verdict is None:
            unknown.append(f"{name} ({version or 'no version reported'})")
        else:
            matched += 1
    return {
        "control_version": control,
        "connected": matched + len(behind) + len(unknown),
        "matched": matched,
        "behind": behind,
        "unknown": unknown,
    }
