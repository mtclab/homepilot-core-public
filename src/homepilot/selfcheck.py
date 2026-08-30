"""Startup self-check for the optional subsystems (ADR-004 S6).

The report exists for ONE distinction: a subsystem that is off by choice needs no
action, and a subsystem that is configured but unreachable needs one now.
Collapsing the two - which a bare "embeddings: no" does - is how a stock install
degrades silently and only says so at first use.

Every entry states the consequence in plain words, because "embeddings:
unreachable" tells an operator nothing about what stopped working.

Constraints, in force for every probe added here:
  * bounded - each probe is wrapped in ``PROBE_TIMEOUT_SECONDS`` and probes run
    concurrently, so the whole report is bounded by that same number;
  * non-blocking at boot - the boot report is scheduled, never awaited;
  * no secrets - only redacted targets (scheme, host, port) ever reach the
    report, so a token in a webhook path or a password in a git remote cannot
    leak through diagnostics.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .agent_hub import version_skew
from .common import redact_endpoint

logger = logging.getLogger(__name__)

# Bound on every probe, and therefore on the whole report (probes run
# concurrently). A probe that exceeds it degrades to "unknown" rather than
# holding the report - or the operator - open.
PROBE_TIMEOUT_SECONDS = 2.0

# Some probes cannot answer inside that. PVE DELAYS A REFUSED CREDENTIAL on
# purpose - measured at a steady ~3.0s against a live cluster, warm or cold -
# so a check whose whole job is to notice a refused token needs a budget larger
# than a healthy answer needs. Two seconds bought a report that could only ever
# see the good case, and reported the bad one as "unverified" (#648, found by
# driving 3.6.20 on dev). This is the ceiling for such a probe; the report is
# still bounded, just by a number that lets the answer arrive.
SLOW_PROBE_TIMEOUT_SECONDS = 8.0

# The boot report is scheduled, not awaited, so startup is never delayed by it.
# The delay lets the agent-hub task finish binding before the report judges it: a
# report that races the thing it describes reads as a fault that is not there.
BOOT_REPORT_DELAY_SECONDS = 1.0

STATE_OFF = "off"
STATE_OK = "ok"
STATE_UNREACHABLE = "unreachable"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProbeVerdict:
    """A probe's own answer when `reachable / not reachable` is too coarse.

    Added for the artifacts remote, where three outcomes are genuinely
    different: pushed recently, never pushed at all, and pushed once a long time
    ago. Folding the last two into "ok" is how "there is an off-box copy" got
    asserted about instances that had never made one.
    """

    state: str
    consequence: str


@dataclass(frozen=True)
class Subsystem:
    """One optional subsystem and everything needed to report on it honestly.

    ``probe`` is None for a subsystem with nothing to reach over the network; the
    ``ok`` consequence must then say what "ok" does and does not prove.
    """

    name: str
    label: str
    configured: bool
    target: str
    off: str
    ok: str
    broken: str
    probe: Callable[[], Awaitable[bool | ProbeVerdict]] | None = None
    # Seconds this probe may take, when the default is not enough to see the
    # answer it exists to see. None means the report-wide bound.
    timeout: float | None = None


async def _tcp_reachable(host: str, port: int) -> bool:
    """Whether something accepts a TCP connection at host:port.

    Used where an application-level probe would have a side effect - posting a
    synthetic event to an operator's webhook would fire their workflow - so the
    ``ok`` sentence must not claim more than "the port answers".
    """
    writer = None
    try:
        _reader, writer = await asyncio.open_connection(host, port)
        return True
    except OSError:
        # TimeoutError is an OSError subclass on 3.11+, so a connect that times
        # out inside the caller's bound lands here too.
        return False
    finally:
        if writer is not None:
            writer.close()


def _url_host_port(url: str) -> tuple[str, int] | None:
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return None
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return host, port


def mcp_transport_running(mcp_app: Any) -> bool:
    """Whether the mounted MCP app's session manager is ACTUALLY running.

    A mounted /mcp route alone is not proof the transport works: #382 had the
    mount present while its session manager was never started, so every request
    500'd. Shared with /health so both surfaces judge MCP the same way.
    """
    session_manager = getattr(getattr(mcp_app, "state", None), "session_manager", None)
    return getattr(session_manager, "_task_group", None) is not None


def agent_hub_listening(hub: Any) -> bool:
    """Whether the agent hub is ACTUALLY listening.

    The same lesson as `mcp_transport_running`, and shared with /health for the
    same reason: a hub OBJECT existing is not proof it bound. /health used to
    report `agent_hub: ok` from `agent_registry is not None` alone, so a hub
    that refused its transport - the case `agent_hub_disabled_reason` exists to
    describe - was reported healthy to the liveness probe an orchestrator acts
    on. Both surfaces now ask the hub itself.
    """
    return hub is not None and bool(hub.is_listening())


async def vault_unlocked(vault: Any) -> bool:
    """Whether the vault can actually be OPENED, not merely listed.

    The same lesson as `agent_hub_listening`, in the subsystem where it costs
    the most. Both surfaces probed with `list_secrets()`, which is
    `glob("*.age")` - a directory listing that cannot fail while the directory
    is readable. So "The vault is unlocked" was established by the presence of
    filenames, and a vault whose passphrase does not match its identity - the
    state a restore without the passphrase leaves behind - would have reported
    `ok` with every secret in it unreadable.

    `ensure_master_identity()` unwraps `master.protected` with the passphrase in
    force. It caches after the first success, so this costs one AES-GCM open per
    process and nothing thereafter.
    """
    try:
        await vault.ensure_master_identity()
    except Exception:
        return False
    return True


def _embeddings_subsystem(settings: Any) -> Subsystem:
    primary = (getattr(settings, "embedding_service_url", "") or "").strip()
    fallback = (getattr(settings, "embedding_fallback_url", "") or "").strip()
    url = primary or fallback
    model = (
        getattr(settings, "embedding_model", "")
        if primary
        else getattr(settings, "embedding_fallback_model", "")
    )
    target = redact_endpoint(url)

    async def probe() -> bool:
        from .kb.service import _call_embed_service

        vector = await _call_embed_service(url, model, "selfcheck", timeout=PROBE_TIMEOUT_SECONDS)
        return vector is not None

    return Subsystem(
        name="embeddings",
        label="the embedding service",
        configured=bool(url),
        target=target,
        off=(
            "KB search is keyword-only because no embedding service is configured. "
            "Searches still work; results are matched on words rather than meaning."
        ),
        ok=f"KB search ranks by vector similarity using the embedding service at {target}.",
        broken=(
            f"KB search is keyword-only: the embedding service configured at {target} "
            "did not return an embedding. Searches still work, with word matching only."
        ),
        probe=probe if url else None,
    )


def _events_webhook_subsystem(settings: Any) -> Subsystem:
    url = (getattr(settings, "events_webhook_url", "") or "").strip()
    target = redact_endpoint(url)
    host_port = _url_host_port(url) if url else None

    async def probe() -> bool:
        if host_port is None:
            return False
        return await _tcp_reachable(*host_port)

    return Subsystem(
        name="events_webhook",
        label="the events webhook",
        configured=bool(url),
        target=target,
        # The wording used to name only "artifact and task events", which stopped
        # being the whole truth when ADR-004 S5 routed alert_firing and
        # alert_resolved down this same channel. On a default install a FIRING
        # ALERT reaches a log line and a browser that happens to be open, and
        # nothing else - and, unlike artifacts and tasks, an alert leaves no
        # durable record to look at afterwards. That is the consequence this
        # line exists to state (#642, #648 tranche 5).
        off=(
            "Nothing is forwarded anywhere because no events webhook is configured. "
            "That includes ALERTS: a firing alert reaches the log and an open browser "
            "and nowhere else, and it leaves no record once it resolves. Artifact and "
            "task events are still recorded and shown in the UI."
        ),
        ok=(
            f"Events, including firing and resolved alerts, are posted to {target}, "
            "which is accepting connections. Whether the receiving workflow handles "
            "them is only proven by a real event."
        ),
        broken=(
            f"Events are dropped: the webhook configured at {target} is not accepting "
            "connections. Every artifact, task and ALERT notification is lost."
        ),
        probe=probe if url else None,
    )


def _proxmox_subsystem(state: Any, settings: Any) -> Subsystem:
    # The RESOLVED host (vault overrides env) is carried on the app state;
    # settings.proxmox_host is the env half only. Reading settings alone
    # reported "No hypervisor is configured" on an install whose Proxmox
    # address lives in the vault - while its inventory and provisioning were
    # working off that very address.
    host = (getattr(state, "proxmox_host", "") or "").strip()
    if not host:
        host = (getattr(settings, "proxmox_host", "") or "").strip()
    client = getattr(state, "proxmox", None)
    target = redact_endpoint(host)

    async def probe() -> bool | ProbeVerdict:
        if client is None:
            return False
        # ONE round trip, not two. `check_tokens` already asks both credentials
        # the same harmless question `test_connection` asks, so calling both put
        # three sequential requests inside a 2-second budget and the subsystem
        # timed out into `unknown` against a real cluster. The read verdict IS
        # the connection verdict.
        try:
            tokens = await client.check_tokens()
        except Exception:
            return ProbeVerdict(
                STATE_UNKNOWN,
                f"The Proxmox API at {target} answered its read token, but the write "
                "token could not be checked, so nothing is known about whether "
                "provisioning would work.",
            )
        if tokens.get("read", {}).get("ok") is False:
            # The API did not answer the read token at all: the existing
            # `broken` copy is the right sentence for that.
            return False
        write = tokens.get("write", {})
        if write.get("ok") is False:
            return ProbeVerdict(
                STATE_UNREACHABLE,
                f"The Proxmox API at {target} answers reads, but the WRITE token is "
                f"refused ({write.get('detail') or 'no detail'}). Inventory works and "
                "every mutation does not: provisioning, clones, snapshots and deletes "
                "will fail at the moment they are attempted. Re-paste the write token "
                "in Settings -> Proxmox.",
            )
        return ProbeVerdict(
            STATE_OK,
            f"The Proxmox API at {target} answered and both tokens authenticate; "
            "inventory and provisioning are available. (Authentication only - "
            "privileges are proven by the first real mutation.)",
        )

    return Subsystem(
        name="proxmox",
        label="the Proxmox API",
        configured=bool(host),
        target=target,
        off=(
            "No hypervisor is configured, so inventory stays empty and guest "
            "provisioning is unavailable. Add the address and token in Settings."
        ),
        ok=f"The Proxmox API at {target} answered; inventory and provisioning are available.",
        broken=(
            f"Proxmox is configured at {target} but HomePilot cannot use it - the token "
            "is missing or the API did not answer - so inventory and provisioning are "
            "unavailable."
        ),
        probe=probe if host else None,
        # A REFUSED PVE credential is answered slowly on purpose (~3s measured
        # live), and noticing a refused write token is the entire point of this
        # probe. At the default bound it could only ever see the healthy case.
        timeout=SLOW_PROBE_TIMEOUT_SECONDS,
    )


def _agent_hub_subsystem(state: Any, settings: Any) -> Subsystem:
    enabled = bool(getattr(settings, "agent_hub_enabled", False))
    host = getattr(settings, "agent_hub_host", "")
    port = getattr(settings, "agent_hub_port", 0)
    hub = getattr(state, "agent_hub", None)
    target = f"{host}:{port}"
    # A hub that refused its own transport check reports the refusal verbatim.
    # "Enabled but not listening" is true and useless; the operator needs the
    # sentence that names the setting to change (#468).
    disabled_reason = str(getattr(state, "agent_hub_disabled_reason", "") or "")

    async def probe() -> bool:
        return agent_hub_listening(hub)

    return Subsystem(
        name="agent_hub",
        label="the agent hub",
        configured=enabled,
        target=target,
        off=(
            "Managed hosts cannot connect, so host metrics, alerts and remote actions "
            "are unavailable."
        ),
        ok=f"The agent hub is listening on {target}; managed hosts can connect.",
        broken=(
            disabled_reason
            or (
                f"The agent hub is enabled but not listening on {target}. Enrolled hosts "
                "cannot connect, so metrics and remote actions stop."
            )
        ),
        probe=probe if enabled else None,
    )


def _vault_subsystem(state: Any) -> Subsystem:
    vault: Any = getattr(state, "vault", None)

    async def probe() -> bool:
        return await vault_unlocked(vault)

    return Subsystem(
        name="vault",
        label="the secret vault",
        configured=vault is not None,
        target="",
        off=(
            "Secrets have nowhere to live: the Proxmox token and the webhook secret "
            "cannot be stored, so they must be supplied through the environment."
        ),
        ok="The vault is unlocked; secrets are stored encrypted in the data directory.",
        broken=(
            "The vault is present but its identity cannot be unwrapped with the "
            "passphrase in force, so NO stored secret can be read. Anything depending "
            "on them - the Proxmox token above all - will fail. This is what a restore "
            "without the source host's passphrase leaves behind: set "
            "HP_VAULT_PASSPHRASE (or HP_VAULT_PASSPHRASE_FILE) to the passphrase that "
            "wrote vault/identities/master.protected."
        ),
        probe=probe if vault is not None else None,
    )


def _mcp_subsystem(state: Any) -> Subsystem:
    # The transport is always mounted and always authenticated: an MCP client
    # presents an API token minted in Settings -> Tokens (HP_MCP_TOKEN is only
    # the legacy static fallback), so there is no env var left to make it
    # "configured". What is worth reporting is whether it is actually running.
    mcp_app = getattr(state, "mcp_app", None)

    async def probe() -> bool:
        return mcp_transport_running(mcp_app)

    return Subsystem(
        name="mcp",
        label="the MCP transport",
        configured=mcp_app is not None,
        target="/mcp",
        off=(
            "The MCP tool surface is not mounted, so external agents cannot drive "
            "HomePilot over MCP."
        ),
        ok=(
            "MCP is mounted at /mcp and its transport is running. Clients "
            "authenticate with an API token from Settings -> Tokens."
        ),
        broken=(
            "The MCP transport is mounted but not running, so every /mcp request "
            "fails. Restart the backend and check the startup log."
        ),
        probe=probe if mcp_app is not None else None,
    )


def _age_seconds(timestamp: str) -> float | None:
    """Seconds since `timestamp`, or None if it is not one we wrote.

    `repository.now()` writes `%Y-%m-%dT%H:%M:%SZ`. An unparseable value means
    the caller must not claim an age rather than guessing one.
    """
    import datetime

    try:
        parsed = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.UTC
        )
    except (TypeError, ValueError):
        return None
    return (datetime.datetime.now(datetime.UTC) - parsed).total_seconds()


def _humanise(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _artifacts_remote_subsystem(state: Any, settings: Any) -> Subsystem:
    remote = (getattr(settings, "artifacts_remote", "") or "").strip()
    target = redact_endpoint(remote)

    interval = float(getattr(settings, "artifacts_push_interval_seconds", 3600) or 3600)
    # A push may legitimately be one cycle late (the reconciler waits 90s after
    # boot, and a cycle can be skipped by a slow git). Three of them is not
    # lateness, it is a mirror that stopped.
    stale_after = interval * 3

    # The scheduled push (#442 follow-up) records its outcome in the settings
    # table; report THAT, not a promise. A probe reading our own database is
    # bounded and honest - but it has to read the WHOLE record. The previous
    # version read only `archive_last_push_ok` and answered "ok" when the row
    # was ABSENT, so an instance that had never pushed anything - and one whose
    # push loop had been dead for a month, with `archive_last_push_at` sitting
    # unread beside it - both reported "the most recent push succeeded". The
    # off-box copy is the thing an operator loses their artifacts without; its
    # check may not round up.
    async def _push_verdict() -> bool | ProbeVerdict:
        repo = getattr(state, "repo", None)
        if repo is None:
            return False
        ok_row = await repo.get_setting("archive_last_push_ok")
        at_row = await repo.get_setting("archive_last_push_at")
        last_at = (at_row or {}).get("value") or ""
        # A recorded failure is a failure whether or not a timestamp came with
        # it: the fault outranks the bookkeeping.
        if ok_row is not None and ok_row.get("value") != "1":
            return False
        if ok_row is None or not last_at:
            return ProbeVerdict(
                STATE_UNKNOWN,
                f"No push to {target} has completed yet, so there is still no off-box "
                "copy. The first one runs about 90s after boot; if this persists, read "
                "the journal for `archive_push`.",
            )
        age = _age_seconds(last_at)
        if age is not None and age > stale_after:
            return ProbeVerdict(
                STATE_UNREACHABLE,
                f"The last SUCCESSFUL push to {target} was {_humanise(age)} ago, more "
                f"than three push intervals ({interval:g}s). Nothing has failed - the "
                "push has stopped running, so the off-box copy is that old. Check the "
                "backend is up and the `archive_push` reconciler is scheduled.",
            )
        when = f"{_humanise(age)} ago" if age is not None else f"at {last_at}"
        return ProbeVerdict(
            STATE_OK,
            f"Artifacts are pushed to {target} on a schedule; the last push succeeded {when}.",
        )

    return Subsystem(
        name="artifacts_remote",
        label="the artifacts remote",
        configured=bool(remote),
        target=target,
        off=(
            "Artifacts live only in this instance's data directory; there is no "
            "off-box copy if the volume is lost."
        ),
        ok=f"Artifacts are pushed to {target} on a schedule and the last push succeeded.",
        broken=(
            f"The last scheduled push to {target} FAILED - the off-box copy is "
            "stale. The error is recorded in the journal and the settings table "
            "(archive_last_push_error)."
        ),
        probe=_push_verdict,
    )


def _agent_versions_subsystem(state: Any, settings: Any) -> Subsystem:
    """Is the fleet running the agent binary this control plane shipped?

    Enrolment serves the agent out of this image, so a new agent matches the hub
    that enrolled it - and then nothing upgrades it and, until now, nothing
    reported the gap. Dev ran a v3.6.6 agent against a 3.6.15 hub for weeks with
    every surface green: a fix that lived in the Go binary was written, gated,
    released and deployed, and changed nothing at all on any managed host.
    `agent_hub` reporting `ok` is not this claim - it says hosts CAN connect,
    not that what connected is current. Named in `agent_hub/dist.py` as #648's
    tranche-1 follow-up; this is it.
    """
    enabled = bool(getattr(settings, "agent_hub_enabled", False))
    registry = getattr(state, "agent_registry", None)
    control = version_skew.control_plane_version()

    async def probe() -> bool | ProbeVerdict:
        if registry is None or not hasattr(registry, "list_connected"):
            return ProbeVerdict(
                STATE_UNKNOWN,
                "No agent registry is wired in, so the versions running on managed "
                "hosts cannot be read at all.",
            )
        summary = version_skew.summarise(list(registry.list_connected()), control)
        if summary["connected"] == 0:
            return ProbeVerdict(
                STATE_UNKNOWN,
                "No agent is connected, so nothing can be said about what versions the "
                "fleet is running.",
            )
        if summary["behind"]:
            names = ", ".join(summary["behind"])
            return ProbeVerdict(
                STATE_UNREACHABLE,
                f"{len(summary['behind'])} of {summary['connected']} connected agents are "
                f"OLDER than this control plane ({control}): {names}. Nothing upgrades an "
                "enrolled agent, so any fix that lives in the agent binary - including a "
                "security fix - has not reached those hosts however many releases ago it "
                "shipped. Re-run the installer on each, or forget and re-enrol the agent.",
            )
        if summary["unknown"]:
            names = ", ".join(summary["unknown"])
            return ProbeVerdict(
                STATE_UNKNOWN,
                f"{len(summary['unknown'])} connected agent(s) report a version this build "
                f"cannot compare with its own ({control}): {names}. Treat them as unproven "
                "rather than current.",
            )
        return ProbeVerdict(
            STATE_OK,
            f"All {summary['connected']} connected agents are running {control}, the same "
            "version as this control plane.",
        )

    return Subsystem(
        name="agent_versions",
        label="the agent versions in the fleet",
        configured=enabled,
        target="",
        off=(
            "No host runs an hp-agent, so there is no agent binary to be out of date - "
            "and no host metrics or remote actions either."
        ),
        ok=f"Every connected agent is running {control}, the same version as this control plane.",
        broken=(
            "At least one managed host is running an agent binary older than this control "
            "plane, so fixes shipped in the agent have not reached it."
        ),
        probe=probe if enabled else None,
    )


# What STOPS HAPPENING when one loop stops. "A reconciler is failing" tells an
# operator nothing about what they have lost, and the whole point of this report
# is the consequence in plain words. Keyed by the name each loop puts on its own
# ReconcilerResult; an unlisted loop falls back to a generic line rather than
# being left out of the verdict.
_RECONCILER_CONSEQUENCE: dict[str, str] = {
    "inventory": (
        "the inventory has stopped following the hypervisor: a new guest is never "
        "seen and a destroyed one keeps its row"
    ),
    "drift": (
        "no applied artifact is re-checked, so every drift verdict on the fleet is "
        "as old as the last cycle - and a fleet nothing checks stays green"
    ),
    "retention": "operational history is no longer pruned, so the database grows without bound",
    "db_integrity": (
        "nothing asks whether the database FILE is still sound, so /health cannot report corruption"
    ),
    "archive_push": "the artifact archive is no longer mirrored off-box",
    "apply": "approved changes are no longer applied automatically",
    "metrics_pruner": "old metrics are no longer pruned",
    "alert_evaluator": (
        "no alert rule is evaluated, so nothing raises an alert however bad it gets"
    ),
}

# A loop may legitimately be one cycle late. Three is not lateness - the same
# judgment the artifacts remote makes above, for the same reason.
_STALE_INTERVALS = 3
# Consecutive failed cycles before a loop counts as broken rather than unlucky.
_FAILURE_STREAK = 3


def _reconciler_consequence(name: str) -> str:
    return _RECONCILER_CONSEQUENCE.get(name, f"whatever `{name}` maintains stops being maintained")


def _reconcilers_subsystem(state: Any) -> Subsystem:
    """Are the scheduled loops that maintain the estate actually running?

    Nothing asked until #648 tranche 8. Every loop swallows its own exceptions
    into a log line, so a crashed reconciler and a healthy one look identical
    from every surface - and the drift loop looks BETTER dead, because a fleet
    nothing re-checks keeps its last green verdict forever. This reports what
    the scheduler has actually recorded, and it may not round up: a loop that
    has never completed a cycle is `unknown`, not `ok`.
    """

    scheduler = getattr(state, "reconciler_scheduler", None)
    registered: list[Any] = []
    if scheduler is not None and hasattr(scheduler, "status"):
        registered = list(scheduler.status())

    async def probe() -> bool | ProbeVerdict:
        statuses = registered
        failing = [s for s in statuses if s.consecutive_failures >= _FAILURE_STREAK]
        if failing:
            worst = failing[0]
            names = ", ".join(sorted(s.name for s in failing))
            return ProbeVerdict(
                STATE_UNREACHABLE,
                f"{len(failing)} reconciler(s) are failing every cycle ({names}). For "
                f"`{worst.name}`: {_reconciler_consequence(worst.name)}. Last error: "
                f"{worst.last_error or 'see the journal'}",
            )

        stale: list[Any] = []
        never: list[Any] = []
        for st in statuses:
            interval = st.interval_seconds
            budget = (interval or 0.0) * _STALE_INTERVALS + st.startup_delay
            if st.runs == 0:
                # Not yet due is not a fault: a loop with a 6h interval and a
                # 120s delay has honestly not run yet a minute after boot.
                age = _age_seconds(st.registered_at)
                if age is not None and interval is not None and age > budget:
                    never.append(st)
                continue
            age = _age_seconds(st.last_finished_at or st.registered_at)
            if age is not None and interval is not None and age > budget:
                stale.append(st)

        if never:
            names = ", ".join(sorted(s.name for s in never))
            worst = never[0]
            return ProbeVerdict(
                STATE_UNKNOWN,
                f"{len(never)} reconciler(s) have never completed a cycle although they "
                f"are long overdue ({names}). For `{worst.name}`: "
                f"{_reconciler_consequence(worst.name)}.",
            )
        if stale:
            names = ", ".join(sorted(s.name for s in stale))
            worst = stale[0]
            age = _age_seconds(worst.last_finished_at or worst.registered_at)
            when = _humanise(age) if age is not None else "a long time"
            return ProbeVerdict(
                STATE_UNREACHABLE,
                f"{len(stale)} reconciler(s) have stopped running ({names}). Nothing has "
                f"FAILED - `{worst.name}` last finished {when} ago, more than three of its "
                f"intervals, so {_reconciler_consequence(worst.name)}.",
            )
        ran = sum(1 for s in statuses if s.runs > 0)
        return ProbeVerdict(
            STATE_OK,
            f"All {len(statuses)} scheduled reconcilers are on time ({ran} have completed "
            "at least one cycle and none is failing).",
        )

    return Subsystem(
        name="reconcilers",
        label="the scheduled reconcilers",
        # "Configured" here means a scheduler exists AND something is registered
        # on it - which is what an operator is really asking. A process with no
        # loops registered is not a healthy one with nothing to do; it is an
        # estate nothing maintains, and that is the `off` arm below.
        configured=bool(registered),
        # These loops run in-process; there is no address to name, and a report
        # that invents one implies something an operator could go and check.
        target="",
        off=(
            "No reconciler is registered, so nothing maintains the estate on a timer: "
            "the inventory never follows the hypervisor, no applied artifact is ever "
            "re-checked for drift, and operational history is never pruned."
        ),
        ok="Every scheduled reconciler has run recently and none is failing.",
        broken=(
            "A scheduled reconciler has stopped. Whatever it maintains is no longer "
            "being maintained, and no other surface would have said so."
        ),
        probe=probe,
    )


def build_subsystems(state: Any, settings: Any) -> list[Subsystem]:
    """The optional subsystems of a HomePilot instance, in report order.

    ``state`` is anything carrying the live objects (``app.state`` in the running
    app), so this is usable from the lifespan, from a route, and from a test
    without a server.
    """
    return [
        _proxmox_subsystem(state, settings),
        _agent_hub_subsystem(state, settings),
        _vault_subsystem(state),
        _embeddings_subsystem(settings),
        _events_webhook_subsystem(settings),
        _mcp_subsystem(state),
        _artifacts_remote_subsystem(state, settings),
        _reconcilers_subsystem(state),
        _agent_versions_subsystem(state, settings),
    ]


async def _evaluate(subsystem: Subsystem, timeout: float) -> dict[str, Any]:
    # A subsystem may ask for longer than the report-wide bound when the answer
    # it exists to establish cannot arrive inside it.
    timeout = subsystem.timeout or timeout
    if not subsystem.configured:
        return {
            "name": subsystem.name,
            "configured": False,
            "state": STATE_OFF,
            "target": "",
            "consequence": subsystem.off,
        }

    entry: dict[str, Any] = {
        "name": subsystem.name,
        "configured": True,
        "target": subsystem.target,
    }
    if subsystem.probe is None:
        entry["state"] = STATE_OK
        entry["consequence"] = subsystem.ok
        return entry

    try:
        reachable = await asyncio.wait_for(subsystem.probe(), timeout=timeout)
    except TimeoutError:
        # Only the probe's own deadline lands here; a CancelledError from an
        # outer shutdown is a BaseException and stays uncaught, so a shutting-down
        # app is never held open by diagnostics.
        entry["state"] = STATE_UNKNOWN
        entry["consequence"] = (
            f"Could not check {subsystem.label} within {timeout:g}s. Its state is "
            "unverified - treat it as unproven, not as working."
        )
        return entry
    except Exception as exc:
        logger.debug("selfcheck probe %s raised: %s", subsystem.name, exc)
        entry["state"] = STATE_UNKNOWN
        entry["consequence"] = (
            f"Checking {subsystem.label} failed unexpectedly. Its state is unverified - "
            "treat it as unproven, not as working."
        )
        return entry

    if isinstance(reachable, ProbeVerdict):
        entry["state"] = reachable.state
        entry["consequence"] = reachable.consequence
        return entry

    entry["state"] = STATE_OK if reachable else STATE_UNREACHABLE
    entry["consequence"] = subsystem.ok if reachable else subsystem.broken
    return entry


async def run_selfcheck(
    subsystems: list[Subsystem], timeout: float = PROBE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Evaluate every subsystem concurrently under a per-probe timeout.

    Concurrent plus per-probe bounding is what keeps the whole report inside
    ``timeout`` no matter how many subsystems are added.
    """
    entries = await asyncio.gather(*(_evaluate(s, timeout) for s in subsystems))
    subsystem_list = list(entries)
    return {
        "timeout_seconds": timeout,
        "counts": {
            state: sum(1 for e in subsystem_list if e["state"] == state)
            for state in (STATE_OK, STATE_OFF, STATE_UNREACHABLE, STATE_UNKNOWN)
        },
        "subsystems": subsystem_list,
    }


async def selfcheck_report(
    state: Any, settings: Any, timeout: float = PROBE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """The report as of NOW, against the values actually in force.

    Persisted operator settings (#553 C2) are resolved first: a report built from
    the boot-time ``Settings`` would call a subsystem the operator configured
    from the product "off by choice", which is the exact collapse this file
    exists to prevent.
    """
    from .app_settings import effective_settings

    in_force = await effective_settings(state, settings)
    return await run_selfcheck(build_subsystems(state, in_force), timeout=timeout)


async def log_selfcheck(state: Any, settings: Any) -> dict[str, Any]:
    """Write the report to the log, one line per subsystem.

    Level carries the operator action: a broken subsystem is a WARNING because
    someone has to fix it, while one that is off was a choice and is INFO.
    """
    report = await selfcheck_report(state, settings)
    logger.info("Startup self-check - optional subsystems:")
    for entry in report["subsystems"]:
        line = "  %s: %s - %s"
        args = (entry["name"], entry["state"], entry["consequence"])
        if entry["state"] in (STATE_UNREACHABLE, STATE_UNKNOWN):
            logger.warning(line, *args)
        else:
            logger.info(line, *args)
    return report


def schedule_boot_selfcheck(state: Any, settings: Any) -> asyncio.Task[None]:
    """Run the boot report in the background so startup is not delayed by it."""

    async def _run() -> None:
        try:
            await asyncio.sleep(BOOT_REPORT_DELAY_SECONDS)
            await log_selfcheck(state, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Diagnostics must never take the app down with them.
            logger.warning("Startup self-check failed to run", exc_info=True)

    return asyncio.create_task(_run())
