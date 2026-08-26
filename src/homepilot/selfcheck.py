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

from .common import redact_endpoint

logger = logging.getLogger(__name__)

# Bound on every probe, and therefore on the whole report (probes run
# concurrently). A probe that exceeds it degrades to "unknown" rather than
# holding the report - or the operator - open.
PROBE_TIMEOUT_SECONDS = 2.0

# The boot report is scheduled, not awaited, so startup is never delayed by it.
# The delay lets the agent-hub task finish binding before the report judges it: a
# report that races the thing it describes reads as a fault that is not there.
BOOT_REPORT_DELAY_SECONDS = 1.0

STATE_OFF = "off"
STATE_OK = "ok"
STATE_UNREACHABLE = "unreachable"
STATE_UNKNOWN = "unknown"


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
    probe: Callable[[], Awaitable[bool]] | None = None


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
        off=(
            "Artifact and task events are not forwarded anywhere because no events "
            "webhook is configured. Events are still recorded and shown in the UI."
        ),
        ok=(
            f"Events are posted to {target}, which is accepting connections. "
            "Whether the receiving workflow handles them is only proven by a real event."
        ),
        broken=(
            f"Events are dropped: the webhook configured at {target} is not accepting "
            "connections. Every artifact and task notification is lost."
        ),
        probe=probe if url else None,
    )


def _proxmox_subsystem(state: Any, settings: Any) -> Subsystem:
    host = (getattr(settings, "proxmox_host", "") or "").strip()
    client = getattr(state, "proxmox", None)
    target = redact_endpoint(host)

    async def probe() -> bool:
        if client is None:
            return False
        return bool(await client.test_connection())

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
        return hub is not None and bool(hub.is_listening())

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
        try:
            await vault.list_secrets()
        except Exception:
            return False
        return True

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
            "The vault is present but locked, so stored secrets cannot be read. "
            "Anything depending on them - the Proxmox token above all - will fail."
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


def _artifacts_remote_subsystem(state: Any, settings: Any) -> Subsystem:
    remote = (getattr(settings, "artifacts_remote", "") or "").strip()
    target = redact_endpoint(remote)

    # The scheduled push (#442 follow-up) records its outcome in the settings
    # table; report THAT, not a promise. A probe reading our own database is
    # bounded and honest - it verifies "the last push worked", which is the
    # only thing "mirrored" can truthfully mean.
    async def _last_push_succeeded() -> bool:
        repo = getattr(state, "repo", None)
        if repo is None:
            return False
        row = await repo.get_setting("archive_last_push_ok")
        # No row yet = the first push has not run; treat as ok-so-far rather
        # than alarming a fresh configure, the schedule will write the truth.
        return row is None or row.get("value") == "1"

    return Subsystem(
        name="artifacts_remote",
        label="the artifacts remote",
        configured=bool(remote),
        target=target,
        off=(
            "Artifacts live only in this instance's data directory; there is no "
            "off-box copy if the volume is lost."
        ),
        ok=(
            f"Artifacts are pushed to {target} on a schedule; the most recent "
            "push succeeded (or the first one has not run yet)."
        ),
        broken=(
            f"The last scheduled push to {target} FAILED - the off-box copy is "
            "stale. The error is recorded in the journal and the settings table "
            "(archive_last_push_error)."
        ),
        probe=_last_push_succeeded,
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
    ]


async def _evaluate(subsystem: Subsystem, timeout: float) -> dict[str, Any]:
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
