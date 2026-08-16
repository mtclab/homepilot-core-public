from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from homepilot.artifacts.models import (
    HostProvisionSpec,
    ServiceState,
    parse_host_provision_spec,
)

if TYPE_CHECKING:
    from homepilot.adapters.agent import AgentAdapter

logger = logging.getLogger(__name__)


def _resolve_host(target: dict[str, Any]) -> str:
    host: Any = target.get("host") or target.get("node", "")
    return str(host)


def _pve_guard(host: str, pve_nodes: list[str] | None) -> str | None:
    if not pve_nodes:
        return None
    host_lower = host.lower().strip()
    for node in pve_nodes:
        if host_lower == node.lower().strip():
            return f"host-provision forbidden for PVE node '{host}'"
    return None


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    target: dict[str, Any],
    host_adapter: object,
    pve_nodes: list[str] | None = None,
    rollback: bool = False,
) -> dict[str, Any]:
    """Apply a declarative ``host-provision`` artifact.

    Parses the ``host-provision-spec`` from the body and drives the native B1
    provisioning actions (``install_package`` / ``manage_service`` /
    ``write_config``) on the target host in order. Aggregated success is true iff
    every item succeeded; on the first hard failure (an adapter raising) it stops
    and reports the reason. Partial application is acceptable and logged — the
    underlying actions are idempotent, so a re-apply resumes.

    Rollback is NOT supported for this kind: reverting an install / a service
    state / a config write has no single trivial inverse (the prior package set
    and file bytes are not captured), so a ``rollback=True`` call is a documented
    no-op rather than a guessed, possibly destructive undo.
    """
    if rollback:
        return {
            "success": True,
            "execution_log": "host-provision does not support rollback (no-op)",
        }

    agent: AgentAdapter = cast("AgentAdapter", host_adapter)

    try:
        spec = parse_host_provision_spec(body)
    except ValueError as exc:
        return {
            "success": False,
            "execution_log": f"spec error: {exc}",
            "failure_reason": str(exc),
        }

    target_kind = target.get("kind", "")
    if target_kind in ("node", "cluster"):
        return {
            "success": False,
            "execution_log": f"host-provision forbidden for target.kind={target_kind}",
            "failure_reason": "host-provision cannot target PVE nodes or cluster",
        }

    host = _resolve_host(target)
    if not host:
        return {
            "success": False,
            "execution_log": "No target host resolved",
            "failure_reason": "missing host",
        }

    forbidden = _pve_guard(host, pve_nodes)
    if forbidden:
        return {
            "success": False,
            "execution_log": forbidden,
            "failure_reason": "forbidden target",
        }

    log_lines: list[str] = []
    start = time.monotonic()

    try:
        await _apply_spec(agent, host, spec, log_lines)
    except Exception as exc:  # hard failure from an adapter action
        log_lines.append(f"FAILED: {exc}")
        return {
            "success": False,
            "execution_log": "\n".join(log_lines),
            "failure_reason": str(exc),
        }

    elapsed = time.monotonic() - start
    log_lines.append(f"duration={elapsed:.1f}s")
    return {"success": True, "execution_log": "\n".join(log_lines)}


async def check_drift(
    agent: AgentAdapter,
    host: str,
    spec: HostProvisionSpec,
) -> dict[str, Any]:
    """Read-only drift probe for a host-provision spec. Mutates NOTHING.

    Uses only ``exec_readonly`` (dpkg / systemctl status probes) and ``read_file``
    to compare the host's observed state against the declared desired state.
    Returns ``{"drifted": bool, "drifted_items": [...], "log": str}``. An item is
    drifted when the package is not installed, a service's active/enabled status
    does not match the desired state, or a config file's on-host bytes differ from
    the spec content (a missing/unreadable file counts as drifted)."""
    drifted_items: list[str] = []
    log_lines: list[str] = []

    for name in spec.packages:
        rc, stdout, _ = await agent.exec_readonly(host, f"dpkg -s {name}")
        installed = rc == 0 and "install ok installed" in stdout
        if not installed:
            drifted_items.append(f"package:{name}")
        log_lines.append(f"package {name}: installed={installed}")

    for svc in spec.services:
        state = svc.state.value if isinstance(svc.state, ServiceState) else str(svc.state)
        if state in ("started", "restarted", "stopped"):
            _, stdout, _ = await agent.exec_readonly(host, f"systemctl is-active {svc.name}")
            active = stdout.strip() == "active"
            want_active = state in ("started", "restarted")
            ok = active == want_active
            log_lines.append(f"service {svc.name} is-active={stdout.strip()} want={state}")
        else:  # enabled / disabled
            _, stdout, _ = await agent.exec_readonly(host, f"systemctl is-enabled {svc.name}")
            observed = stdout.strip()
            ok = observed == state
            log_lines.append(f"service {svc.name} is-enabled={observed} want={state}")
        if not ok:
            drifted_items.append(f"service:{svc.name}")

    for cfg in spec.config_files:
        try:
            on_host = await agent.read_file(host, cfg.path)
            matches = on_host == cfg.content
        except Exception:
            matches = False
        if not matches:
            drifted_items.append(f"config:{cfg.path}")
        log_lines.append(f"config {cfg.path}: matches={matches}")

    return {
        "drifted": len(drifted_items) > 0,
        "drifted_items": drifted_items,
        "log": "\n".join(log_lines),
    }


async def _apply_spec(
    agent: AgentAdapter,
    host: str,
    spec: HostProvisionSpec,
    log_lines: list[str],
) -> None:
    for name in spec.packages:
        result = await agent.install_package(host, name)
        log_lines.append(
            f"package {name}: changed={result.get('changed')} {result.get('detail', '')}".rstrip()
        )
    for svc in spec.services:
        state = svc.state.value if isinstance(svc.state, ServiceState) else str(svc.state)
        result = await agent.manage_service(host, svc.name, state)
        log_lines.append(
            f"service {svc.name} -> {state}: "
            f"changed={result.get('changed')} {result.get('detail', '')}".rstrip()
        )
    for cfg in spec.config_files:
        result = await agent.write_config(host, cfg.path, cfg.content, cfg.mode)
        log_lines.append(
            f"config {cfg.path} (mode {cfg.mode}): "
            f"changed={result.get('changed')} {result.get('detail', '')}".rstrip()
        )
