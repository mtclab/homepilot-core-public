from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from homepilot.artifacts.models import (
    HostProvisionSpec,
    ServiceState,
    parse_host_provision_spec,
)
from homepilot.executor.secrets import SecretResolutionError, redact, resolve

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
    pre_state: list[dict[str, Any]] | None = None,
    vault: object | None = None,
) -> dict[str, Any]:
    """Apply a declarative ``host-provision`` artifact.

    Parses the ``host-provision-spec`` from the body and drives the native B1
    provisioning actions (``install_package`` / ``manage_service`` /
    ``write_config``) on the target host in order. Aggregated success is true iff
    every item succeeded; on the first hard failure (an adapter raising) it stops
    and reports the reason. Partial application is acceptable and logged — the
    underlying actions are idempotent, so a re-apply resumes.

    Rollback captures the host's PRIOR state before applying and puts back what
    it can (#426). What it cannot invert with the agent's verbs - removing a
    package that was not installed, deleting a config file that did not exist -
    is REPORTED rather than guessed at, because a wrong guess here deletes things.
    """
    agent: AgentAdapter = cast("AgentAdapter", host_adapter)

    if rollback:
        return await _rollback(agent, target, pre_state, pve_nodes)

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

    # Resolve `{{ vault.name.field }}` in the CONFIG CONTENT, in memory, right
    # before it is written (#505). Without this the only way to ship a credential
    # in a config file was to commit it to the artifact git store.
    secret_values: list[str] = []
    try:
        spec, secret_values = await _resolve_spec_secrets(spec, vault)
    except SecretResolutionError as exc:
        return {
            "success": False,
            "execution_log": str(exc),
            "failure_reason": str(exc),
        }

    log_lines: list[str] = []
    start = time.monotonic()

    # Capture BEFORE mutating. This is the whole reason a host-provision rollback
    # can exist at all: nothing else records what the host looked like, and after
    # the apply the prior bytes are gone.
    captured = await capture_pre_state(agent, host, spec)

    try:
        await _apply_spec(agent, host, spec, log_lines)
    except Exception as exc:  # hard failure from an adapter action
        log_lines.append(f"FAILED: {exc}")
        # The capture rides back even on failure: a PARTIAL apply is exactly the
        # case where an operator most needs to put the host back.
        return {
            "success": False,
            "execution_log": redact("\n".join(log_lines), secret_values),
            "failure_reason": redact(str(exc), secret_values),
            "pre_state": captured,
        }

    elapsed = time.monotonic() - start
    log_lines.append(f"duration={elapsed:.1f}s")
    return {
        "success": True,
        "execution_log": redact("\n".join(log_lines), secret_values),
        "pre_state": captured,
    }


async def _resolve_spec_secrets(
    spec: HostProvisionSpec, vault: object | None
) -> tuple[HostProvisionSpec, list[str]]:
    """Resolve vault references in the spec's config contents.

    Only config CONTENT is resolved: a package name or a service name is not a
    place a credential belongs, and resolving there would let a reference decide
    which package gets installed.
    """
    values: list[str] = []
    for cfg in spec.config_files:
        resolved, found = await resolve(cfg.content, vault)
        if found:
            cfg.content = resolved
            values.extend(found)
    return spec, values


async def capture_pre_state(
    agent: AgentAdapter,
    host: str,
    spec: HostProvisionSpec,
) -> list[dict[str, Any]]:
    """Record what the host looks like BEFORE the spec is applied (#426).

    This is strictly more than :func:`probe` records: a plan only needs to say
    "this file differs", while an inverse needs the bytes that were there and the
    mode they had. Reading them after the apply is impossible, so it happens here
    or not at all.

    Mutates nothing. A read that fails is recorded as "unknown" rather than as
    "absent": rolling back to a guess is how an undo deletes something.
    """
    captured: list[dict[str, Any]] = []

    for name in spec.packages:
        installed = await _package_installed(agent, host, name)
        captured.append({"kind": "package", "name": name, "was_installed": installed})

    for svc in spec.services:
        state = svc.state.value if isinstance(svc.state, ServiceState) else str(svc.state)
        # Capture BOTH axes regardless of which one the spec sets: applying
        # "enabled" can start a unit as a side effect, and an inverse that only
        # knows about the axis it was asked for puts back half the change.
        _, active_out, _ = await agent.exec_readonly(host, f"systemctl is-active {svc.name}")
        _, enabled_out, _ = await agent.exec_readonly(host, f"systemctl is-enabled {svc.name}")
        captured.append(
            {
                "kind": "service",
                "name": svc.name,
                "desired": state,
                "was_active": active_out.strip(),
                "was_enabled": enabled_out.strip(),
            }
        )

    for cfg in spec.config_files:
        entry: dict[str, Any] = {"kind": "config", "name": cfg.path}
        try:
            entry["prior_content"] = await agent.read_file(host, cfg.path)
            entry["existed"] = True
            rc, mode_out, _ = await agent.exec_readonly(host, f"stat -c %a {cfg.path}")
            entry["prior_mode"] = mode_out.strip() if rc == 0 else None
        except Exception as exc:
            # This docstring has always promised "unknown" here, and the code
            # wrote `existed: False` - the one value that means the opposite.
            # The apply then overwrote the file as a FIRST write and the revoke
            # said "created by this artifact", so the bytes that were there went
            # away with nothing recording that they had ever existed. The agent's
            # read fails for plenty of reasons that are not absence: permission,
            # a file over the hub's frame budget, a denied path.
            entry["prior_content"] = None
            entry["prior_mode"] = None
            if _read_says_absent(exc):
                entry["existed"] = False
            else:
                entry["existed"] = None
                entry["read_error"] = str(exc)[:300]
        captured.append(entry)

    return captured


# The agent answers a genuinely missing file with this exact phrase
# (agent/go/fileops.go). Everything else it can raise - "permission denied",
# "path not in allowed read prefixes", "file is N bytes; the agent hub accepts at
# most ..." - is a failure to look, not a finding that there is nothing there.
_ABSENT_MARKERS = ("file not found",)


def _read_says_absent(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _ABSENT_MARKERS)


async def _package_installed(agent: AgentAdapter, host: str, name: str) -> bool | None:
    """True / False / None-for-unknown, from one `dpkg -s`.

    `dpkg -s` exits 1 for "not installed" and also for its own errors, so
    `rc == 0 and "install ok installed" in stdout` quietly turned every kind of
    failure into "absent" - and absent is the answer that makes the plan install
    something (#642 A6).
    """
    rc, stdout, stderr = await agent.exec_readonly(host, f"dpkg -s {name}")
    if rc == 0:
        return "install ok installed" in stdout
    if "not installed" in (stdout + stderr).lower():
        return False
    return None


async def _rollback(
    agent: AgentAdapter,
    target: dict[str, Any],
    pre_state: list[dict[str, Any]] | None,
    pve_nodes: list[str] | None,
) -> dict[str, Any]:
    """Put back what was captured, and report what could not be put back.

    The agent's verbs are `install_package`, `manage_service` and `write_config`.
    There is no package removal and no file deletion, so two inverses are simply
    not expressible today:

    * a package that was ABSENT before the apply stays installed;
    * a config file that did NOT EXIST before the apply stays on disk.

    Both are reported by name. Guessing at them - `apt-get remove`, `rm` - is how
    an undo takes out a dependency or a file somebody else wrote.
    """
    if not pre_state:
        return {
            "success": False,
            "execution_log": "no pre-apply state was captured for this artifact",
            "failure_reason": "nothing captured to roll back to",
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
        return {"success": False, "execution_log": forbidden, "failure_reason": "forbidden target"}

    log_lines: list[str] = []
    not_reversible: list[str] = []

    for item in pre_state:
        kind = item.get("kind")
        name = str(item.get("name", ""))
        if kind == "package":
            if item.get("was_installed"):
                log_lines.append(f"package {name}: was already installed, left in place")
            else:
                not_reversible.append(f"package {name} (installed by this artifact)")
        elif kind == "service":
            prior_active = str(item.get("was_active") or "")
            prior_enabled = str(item.get("was_enabled") or "")
            for value, wanted in (
                (prior_active, "started" if prior_active == "active" else "stopped"),
                (prior_enabled, prior_enabled if prior_enabled in ("enabled", "disabled") else ""),
            ):
                if not value or not wanted:
                    continue
                result = await agent.manage_service(host, name, wanted)
                log_lines.append(
                    f"service {name} -> {wanted}: changed={result.get('changed')}".rstrip()
                )
        elif kind == "config":
            if item.get("existed") and item.get("prior_content") is not None:
                mode = str(item.get("prior_mode") or "0644")
                await agent.write_config(host, name, str(item["prior_content"]), mode)
                log_lines.append(f"config {name}: restored {len(item['prior_content'])} bytes")
            elif item.get("existed") is None:
                # NOT "created by this artifact". Whatever was there was
                # overwritten and never read, so the honest answer names that
                # rather than implying the file is new.
                reason = item.get("read_error") or "the prior content was never read"
                not_reversible.append(
                    f"config {name} (its prior content was never established: {reason})"
                )
            else:
                not_reversible.append(f"config {name} (created by this artifact)")

    if not_reversible:
        log_lines.append("NOT reversed: " + "; ".join(not_reversible))
        return {
            "success": False,
            "execution_log": "\n".join(log_lines),
            "failure_reason": ("the agent has no verb to undo these: " + "; ".join(not_reversible)),
        }
    return {"success": True, "execution_log": "\n".join(log_lines)}


async def probe(
    agent: AgentAdapter,
    host: str,
    spec: HostProvisionSpec,
) -> list[dict[str, Any]]:
    """Compare a host against a spec, item by item. Mutates NOTHING.

    ONE probe engine, two views: :func:`check_drift` answers "is this host out of
    spec" and the approval plan answers "what would applying this change", and
    both read the results of this function. Writing the second engine separately
    is how #423 happened - two apply/revoke paths that disagreed - and drift
    saying "in spec" while the plan promises changes would be the same defect
    wearing a different hat.

    Each item is ``{kind, name, desired, observed, changes, established, id}``.
    ``changes`` means applying the spec would alter this item; ``established``
    says whether the host actually ANSWERED. The two are not the same, and
    conflating them is #642 A6: an unreadable file, a service whose state could
    not be read and a package whose query failed all read as "not in the desired
    state", so a plan promised to install, restart and overwrite on a host that
    was fine and drift painted it red. ``observed`` is what the host reports, in
    the host's own words, so a plan can show an operator the before and after
    rather than a boolean.
    """
    items: list[dict[str, Any]] = []

    for name in spec.packages:
        installed = await _package_installed(agent, host, name)
        items.append(
            {
                "kind": "package",
                "id": f"package:{name}",
                "name": name,
                "desired": "installed",
                "observed": (
                    "could not be read"
                    if installed is None
                    else ("installed" if installed else "absent")
                ),
                "changes": installed is not True,
                "established": installed is not None,
                "log": f"package {name}: installed={installed}",
            }
        )

    for svc in spec.services:
        state = svc.state.value if isinstance(svc.state, ServiceState) else str(svc.state)
        if state in ("started", "restarted", "stopped"):
            _, stdout, _ = await agent.exec_readonly(host, f"systemctl is-active {svc.name}")
            observed = stdout.strip()
            active = observed == "active"
            want_active = state in ("started", "restarted")
            ok = active == want_active
            log = f"service {svc.name} is-active={observed} want={state}"
        else:  # enabled / disabled
            _, stdout, _ = await agent.exec_readonly(host, f"systemctl is-enabled {svc.name}")
            observed = stdout.strip()
            ok = observed == state
            log = f"service {svc.name} is-enabled={observed} want={state}"
        items.append(
            {
                "kind": "service",
                "id": f"service:{svc.name}",
                "name": svc.name,
                "desired": state,
                "observed": observed or "could not be read",
                "changes": not ok,
                # systemctl always names a state it could determine ("active",
                # "inactive", "unknown", "failed"). An EMPTY answer means the
                # command did not run, not that the unit is stopped.
                "established": bool(observed),
                "log": log,
            }
        )

    for cfg in spec.config_files:
        established = True
        try:
            on_host = await agent.read_file(host, cfg.path)
            matches = on_host == cfg.content
            # "absent" and "different" are different problems to an operator:
            # one is a first write, the other overwrites bytes already there.
            observed = "matches" if matches else "differs"
        except Exception as exc:
            matches = False
            if _read_says_absent(exc):
                observed = "absent"
            else:
                observed = f"could not be read: {str(exc)[:120]}"
                established = False
        items.append(
            {
                "kind": "config",
                "id": f"config:{cfg.path}",
                "name": cfg.path,
                "desired": f"{len(cfg.content)} bytes, mode {cfg.mode}",
                "observed": observed,
                "changes": not matches,
                "established": established,
                "log": f"config {cfg.path}: {observed}",
            }
        )

    return items


async def check_drift(
    agent: AgentAdapter,
    host: str,
    spec: HostProvisionSpec,
) -> dict[str, Any]:
    """Read-only drift probe for a host-provision spec. Mutates NOTHING.

    A view over :func:`probe`: an item is drifted when applying the spec would
    change it, and UNESTABLISHED when the host never answered. Returns
    ``{"drifted": bool, "drifted_items": [...], "unknown_items": [...],
    "log": str}`` - the third key is what stops "I could not read this file"
    being filed as "this file has drifted", and what lets the verifier answer
    UNKNOWN instead of a colour it did not earn (#425, #642 A6).
    """
    items = await probe(agent, host, spec)
    drifted_items = [item["id"] for item in items if item["changes"] and item.get("established")]
    unknown_items = [item["id"] for item in items if not item.get("established")]
    return {
        "drifted": len(drifted_items) > 0,
        "drifted_items": drifted_items,
        "unknown_items": unknown_items,
        "log": "\n".join(str(item["log"]) for item in items),
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
