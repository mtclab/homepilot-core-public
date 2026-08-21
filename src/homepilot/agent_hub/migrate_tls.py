"""Move an already-enrolled fleet onto TLS without visiting a single host (#468).

An install that predates TLS-by-default keeps plaintext, because flipping the
listener under agents that dial plaintext strands every one of them (see
``tls_mode``). Keeping plaintext forever is not the answer either, and the
manual route is worse than it sounds: the agent reads ``HP_AGENT_TLS`` and
``HP_AGENT_TLS_PIN`` from ``/etc/homepilot/agent.env``, written once at
enrolment, so "enable TLS" would mean editing that file on every managed host -
LXC and bare metal included - and restarting each agent.

The agent channel already reaches every one of those hosts. So the hub pushes
the new transport down it, each agent persists it and redials over TLS, and the
listener flips only once the fleet has actually arrived. ADR-004's rule is that
an operator supplies the Proxmox address and token and nothing else; "ssh to
twelve boxes to change a config file" fails that rule loudly.

The ordering is the whole design:

1. The certificate is generated FIRST, so there is a real fingerprint to hand
   out. It is generated, not served - the listener stays plaintext throughout.
2. Every connected agent is told to adopt it. Each persists the pin, acks on the
   connection it is still holding, then drops and redials.
3. The listener flips only when the agents that could be reached have adopted
   it, and NEVER while an agent is known-but-offline unless the operator says
   so in as many words - that agent would come back to a door it cannot open.

A flip is therefore never a surprise: it is the operator's decision, made with
the list of who would be stranded in front of them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .tls_mode import MODE_TLS, SETTING_KEY

logger = logging.getLogger(__name__)


class MigrationRefusedError(Exception):
    """The migration cannot proceed, with a reason meant for an operator."""


async def plan_migration(repo: Any, registry: Any) -> dict[str, Any]:
    """Who would move, and who would be left behind, without changing anything.

    Offered on its own because the flip is irreversible from the fleet's point
    of view: an operator should be able to see the stranding list before doing
    anything, not discover it in a report afterwards.
    """
    connected = {a["agent_id"]: a for a in registry.list_connected()} if registry else {}
    known = await repo.list_agents()

    offline = [
        {"agent_id": row["agent_id"], "hostname": row["hostname"]}
        for row in known
        if row["agent_id"] not in connected and not row.get("revoked_at")
    ]
    return {
        "reachable": [
            {"agent_id": agent_id, "hostname": agent.get("hostname", "")}
            for agent_id, agent in connected.items()
        ],
        "unreachable": offline,
        "can_flip_cleanly": not offline,
    }


async def migrate_fleet_to_tls(
    repo: Any,
    registry: Any,
    hub: Any,
    data_dir: str | Path,
    *,
    settings: Any,
    force: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Push TLS to every reachable agent and flip the listener when it is safe.

    Returns a report naming every agent and what happened to it. Raises
    :class:`MigrationRefusedError` when flipping would strand agents and ``force`` was
    not given - refusing with the list is more useful than a partial success an
    operator has to reconstruct.
    """
    from .selfconfig import certificate_fingerprint, ensure_hub_certificate

    if hub is None or registry is None:
        raise MigrationRefusedError(
            "The agent hub is not running, so there is no fleet to migrate."
        )
    if hub.tls_enabled:
        raise MigrationRefusedError("The hub is already serving TLS; there is nothing to migrate.")

    plan = await plan_migration(repo, registry)
    if plan["unreachable"] and not force:
        names = ", ".join(f"{a['hostname']} ({a['agent_id'][:8]})" for a in plan["unreachable"])
        raise MigrationRefusedError(
            f"{len(plan['unreachable'])} enrolled agent(s) are not connected and cannot "
            f"be told about the new transport: {names}. They would come back to a hub "
            "they cannot speak to. Bring them online and retry, or repeat with "
            "force=true to migrate without them - they will need re-enrolling by hand."
        )

    # Generate (do NOT serve) the certificate: the agents need its fingerprint
    # while they are still talking plaintext. ensure_hub_certificate reuses an
    # existing pair, so a retried migration hands out the same pin.
    cert_path, _key_path = ensure_hub_certificate(
        Path(data_dir),
        extra_hosts=(settings.agent_hub_advertise_host, settings.agent_hub_host),
    )
    fingerprint = certificate_fingerprint(cert_path)

    adopted: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for agent in plan["reachable"]:
        agent_id = agent["agent_id"]
        try:
            await hub.send_action(
                agent_id,
                "set_transport",
                {"tls": True, "pin": fingerprint},
                timeout=timeout,
            )
        except Exception as exc:
            # One agent refusing must not abort the fleet: the report names it,
            # and the flip decision below accounts for it.
            logger.warning("agent %s did not adopt the TLS transport: %s", agent_id, exc)
            failed.append({**agent, "error": str(exc)})
        else:
            adopted.append(agent)

    if failed and not force:
        names = ", ".join(f"{a['hostname']} ({a['agent_id'][:8]})" for a in failed)
        raise MigrationRefusedError(
            f"{len(failed)} agent(s) did not adopt the new transport: {names}. The hub is "
            "still serving plaintext and the fleet is unchanged. Fix those agents and "
            "retry, or repeat with force=true to flip without them."
        )

    # Only now is the decision recorded. The listener itself changes on the next
    # start: rebinding a live socket underneath connected agents is the kind of
    # in-place surgery this whole issue is about avoiding, and the agents have
    # already been told to expect TLS - they retry until they find it.
    await repo.set_setting(SETTING_KEY, MODE_TLS)
    logger.warning(
        "Fleet TLS migration: %d agent(s) adopted the hub certificate %s. The hub serves "
        "TLS from its next start - restart the backend to complete the move.",
        len(adopted),
        fingerprint[:16],
    )

    return {
        "fingerprint": fingerprint,
        "adopted": adopted,
        "failed": failed,
        "unreachable": plan["unreachable"],
        "hub_tls_active_after_restart": True,
        "detail": (
            f"{len(adopted)} agent(s) hold the hub's certificate. The hub serves TLS from "
            "its next start; restart the backend to complete the migration."
        ),
    }
