from __future__ import annotations

import logging
import re
import time
from typing import Any

import yaml

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError, ProxmoxTaskStillRunningError
from homepilot.executor.jinja_utils import (
    InterpolationError,
    _eval_skip_if,
    _interpolate,
    _interpolate_obj,
    interpolation_context,
)
from homepilot.executor.pve_warnings import explain_warning
from homepilot.executor.skip_if import SkipIfUndecided, make_pve_response_proxy

logger = logging.getLogger(__name__)

_SPEC_FENCE = "proxmox-api-spec"
_ROLLBACK_FENCE = "proxmox-api-rollback"

# A precheck answers when the API tells us something ABOUT THE RESOURCE. 2xx and
# 4xx do that ("here it is" / "there is no such thing"). A 5xx or a transport
# failure tells us about the API, not about the resource, so it settles nothing -
# and "I could not tell whether this was already applied" must never become
# "apply it" (#642 A4). Proved live on dev 3.6.14: a precheck that answered HTTP
# 500 fell straight through to its step, and the log did not even mention it.
_PRECHECK_ANSWERS_BELOW = 500

# How long a step FOLLOWS the PVE task it spawned before it stops watching
# (#774). One flat 600 s was the whole policy, and a vzdump of three guests
# (~13 min) outlived it: the artifact was marked failed while PVE went on to
# finish `OK` with every archive on disk. So the wait is per operation - the
# kinds of work that routinely run long get a long one - and a step may name its
# own with `timeout_s`. Whatever the wait, running out of it is NOT a failure:
# the step reports the task as still running and the run's outcome as unknown
# (see `execute`). The poll matches ProvisionService so one cluster sees one
# polling cadence.
DEFAULT_TASK_TIMEOUT_S = 600.0
DEFAULT_POLL_INTERVAL_S = 2.0
# The ceiling a step's own `timeout_s` may ask for. A day is longer than any
# single PVE task an artifact has a business waiting on; past that the step has
# stopped being "wait for this" and become "leave a worker blocked forever".
MAX_TASK_TIMEOUT_S = 24 * 3600.0
# Backups and restores move whole disks through a storage; hours are normal.
_BACKUP_TASK_TIMEOUT_S = 6 * 3600.0
# Copying or relocating a disk (clone, move, migrate, template, download).
_DISK_TASK_TIMEOUT_S = 2 * 3600.0
_OPERATION_TASK_TIMEOUTS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"/vzdump$"), _BACKUP_TASK_TIMEOUT_S),
    (
        re.compile(r"/(clone|move_disk|move_volume|migrate|template|download-url)$"),
        _DISK_TASK_TIMEOUT_S,
    ),
)


def step_timeout_error(value: Any) -> str | None:
    """Why a step's `timeout_s` is not acceptable, or None when it is (or absent).

    Shared by propose-time validation and the executor, so an artifact refused
    at apply is one that could not have been proposed either.
    """
    if value is None:
        return None
    # bool is an int in Python; `timeout_s: true` is a typo, not a duration.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"timeout_s must be a number of seconds, got {value!r}"
    if not 0 < value <= MAX_TASK_TIMEOUT_S:
        return (
            f"timeout_s must be above 0 and at most {MAX_TASK_TIMEOUT_S:.0f} seconds, got {value}"
        )
    return None


def task_timeout_for(
    step: dict[str, Any],
    method: str,
    path: str,
    body: Any,
    override: float | None = None,
) -> float:
    """How long this step follows its PVE task (#774).

    The step's own `timeout_s` wins; then a caller's override for the whole run;
    then the operation's default; then the general default.
    """
    declared = step.get("timeout_s")
    if declared is not None:
        return float(declared)
    if override is not None:
        return override
    bare = path.split("?", 1)[0].rstrip("/")
    for pattern, seconds in _OPERATION_TASK_TIMEOUTS:
        if pattern.search(bare):
            return seconds
    # A restore is a POST that creates a guest FROM an archive: same path as a
    # plain create, told apart by its body (`archive` for qemu, `restore` for lxc).
    if method == "POST" and isinstance(body, dict) and ("archive" in body or body.get("restore")):
        return _BACKUP_TASK_TIMEOUT_S
    return DEFAULT_TASK_TIMEOUT_S


def _node_for_task(context: dict[str, Any], upid: str) -> str | None:
    """The node whose task list holds this UPID.

    A UPID is `UPID:<node>:...`, so it names its own node - which is the right
    source, because a step may address a node other than the target's.
    """
    parts = upid.split(":")
    if len(parts) > 1 and parts[1]:
        return parts[1]
    node = (context.get("target") or {}).get("node")
    return str(node) if node else None


async def _await_pve_task(
    proxmox: ProxmoxClient,
    context: dict[str, Any],
    resp: Any,
    timeout_s: float,
    poll_interval: float,
) -> tuple[str, list[dict[str, Any]]]:
    """Block until the task this step spawned finishes; raise if it failed.

    Returns the text to append to the step log ("" when the call was
    synchronous and there was nothing to wait for) and the task's warnings,
    explained where recognised. A task still running when `timeout_s` runs out
    raises ProxmoxTaskStillRunningError, which `execute` reports as an unknown
    outcome - never as a failure (#774).
    """
    upid = ProxmoxClient.upid_of(resp)
    if not upid:
        return "", []
    node = _node_for_task(context, upid)
    if not node:
        # Nothing to poll against. Say so rather than implying the task
        # finished - a silent skip here is the very bug this code fixes.
        return " (task not waited on: no node in the UPID or the target)", []
    status = await proxmox.wait_for_task(
        node, upid, timeout_s=timeout_s, poll_interval=poll_interval
    )
    count = int(status.get("warnings") or 0) if isinstance(status, dict) else 0
    if not count:
        return " (task finished OK)", []
    # "WARNINGS: 1" alone told the owner nothing about a backup that was fine
    # (2026-11-08). The task log names each warning; read it, best-effort - a
    # log that cannot be read is said so, never dropped.
    try:
        lines = await proxmox.task_warning_lines(node, upid)
    except ProxmoxError as exc:
        lines = []
        logger.warning("Could not read the warnings of PVE task %s: %s", upid, exc)
    warnings = [explain_warning(line) for line in lines]
    if not warnings:
        warnings = [
            {
                "text": f"PVE reported {count} warning(s) but its task log could not be read",
                "explanation": None,
                "harmless": None,
            }
        ]
    suffix = f" (task finished OK with {count} warning{'s' if count != 1 else ''})"
    for w in warnings:
        suffix += f"\n    WARN: {w['text']}"
    return suffix, warnings


# Parameters of a PVE config write that are not config keys.
_CONFIG_META_PARAMS = frozenset({"delete", "digest", "revert", "skiplock", "background_delay"})


def _is_config_write(method: str, path: str) -> bool:
    return method in ("PUT", "POST") and path.split("?", 1)[0].rstrip("/").endswith("/config")


async def _reread_config_write(
    proxmox: ProxmoxClient, path: str, body: Any
) -> tuple[list[str], list[str]] | None:
    """Which keys of a FAILED config write PVE took anyway (#796).

    PVE applies a config write key by key: ``delete=scsi2,hostpci0`` removed
    scsi2 and then refused hostpci0 with a 500, and the run said "failed" as if
    nothing had changed. Reading the config back after the failure is the only
    way to know. ``GET .../config`` answers with pending changes applied, which
    is exactly "what the write was accepted as". Returns (applied, not_applied),
    or None when the config cannot be read (the caller then says so).
    """
    if not isinstance(body, dict):
        return None
    try:
        resp = await proxmox.call("GET", path.split("?", 1)[0])
    except ProxmoxError:
        logger.warning("Could not re-read %s after a failed write", path, exc_info=True)
        return None
    config = resp.get("data", resp) if isinstance(resp, dict) else None
    if not isinstance(config, dict):
        return None
    applied: list[str] = []
    not_applied: list[str] = []
    raw_delete = body.get("delete") or []
    deletes = (
        [k.strip() for k in raw_delete.split(",")]
        if isinstance(raw_delete, str)
        else [str(k).strip() for k in raw_delete]
    )
    for key in (k for k in deletes if k):
        (applied if key not in config else not_applied).append(key)
    for key, value in body.items():
        if key in _CONFIG_META_PARAMS:
            continue
        (applied if key in config and str(config[key]) == str(value) else not_applied).append(key)
    return applied, not_applied


def describe_partial(partial: dict[str, Any]) -> str:
    """The sentence a partly applied run is reported with (#796).

    ``partly applied: steps a, b done; step c failed: <why>; not run: d`` - and
    when the failed step was a config write that PVE took in part, which keys
    went through and which did not.
    """
    parts: list[str] = []
    done = partial.get("steps_done") or []
    if done:
        parts.append(f"step{'s' if len(done) != 1 else ''} {', '.join(done)} done")
    for failed in partial.get("steps_failed") or []:
        applied = partial.get("keys_applied") or []
        if applied and partial.get("step_reread") == failed:
            not_applied = partial.get("keys_not_applied") or []
            text = f"step {failed} changed {', '.join(applied)} before it failed"
            if not_applied:
                text += f" (not applied: {', '.join(not_applied)})"
        else:
            text = f"step {failed} failed"
        parts.append(text)
    sentence = "partly applied: " + "; ".join(parts)
    if partial.get("error"):
        sentence += f": {partial['error']}"
    not_run = partial.get("steps_not_run") or []
    if not_run:
        sentence += f"; not run: {', '.join(not_run)}"
    return sentence


def _with_partial(
    result: dict[str, Any],
    *,
    done: list[str],
    failed: list[str],
    error: str | None,
    not_run: list[str],
    warnings: list[dict[str, Any]],
    reread: tuple[str, list[str], list[str]] | None = None,
) -> dict[str, Any]:
    """Finish a failed run's result with what it DID change (#796).

    A run that changed nothing keeps its plain failure. One that completed
    steps before the failure - or whose failed config write PVE took in part
    (`reread` = step, applied keys, not-applied keys) - carries `partial_apply`
    and says "partly applied: ..." instead of a bare "failed".
    """
    keys_applied = reread[1] if reread else []
    if done or keys_applied:
        partial: dict[str, Any] = {
            "steps_done": list(done),
            "steps_failed": list(failed),
            "steps_not_run": list(not_run),
            "error": error,
        }
        if reread:
            partial["step_reread"] = reread[0]
            partial["keys_applied"] = list(reread[1])
            partial["keys_not_applied"] = list(reread[2])
        result["partial_apply"] = partial
        result["failure_reason"] = describe_partial(partial)
    if warnings:
        result["warnings"] = list(warnings)
    return result


def _extract_steps(body: str, tag: str) -> list[dict[str, Any]] | None:
    pattern = re.compile(rf"```yaml\s+{tag}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    if not m:
        return None
    content = m.group(1).strip()
    parsed = yaml.safe_load(content)
    if not isinstance(parsed, dict) or "steps" not in parsed:
        return None
    steps_raw: list[dict[str, Any]] = parsed["steps"]
    return steps_raw


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    target: dict[str, Any],
    proxmox: ProxmoxClient,
    vault: Any | None = None,
    rollback: bool = False,
    task_timeout_s: float | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
) -> dict[str, Any]:
    """Run a proxmox-api-spec (or its rollback) step by step.

    `task_timeout_s` overrides the per-operation wait for every step that does
    not name its own `timeout_s`; None (the default) means "per operation".

    Three outcomes, not two (#774): `success` True; `success` False with a
    `failure_reason`; or `success` False with `unsettled_task` set - a PVE task
    this run spawned was STILL RUNNING when its wait ran out, so whether the
    change happened is unknown. That last one must never be recorded as failed.
    """
    tag = _ROLLBACK_FENCE if rollback else _SPEC_FENCE
    steps = _extract_steps(body, tag)
    if steps is None:
        return {
            "success": False,
            "execution_log": f"No ```yaml {tag}``` block found",
            "failure_reason": "missing spec",
        }
    # Every step's wait is checked BEFORE the first one runs: refusing step 3 for
    # a bad `timeout_s` after steps 1-2 changed the cluster would leave it half
    # done for a reason that was knowable up front.
    for step in steps:
        bad_timeout = step_timeout_error(step.get("timeout_s"))
        if bad_timeout:
            step_id = step.get("id", "unknown")
            return {
                "success": False,
                "execution_log": f"[{step_id}] -> REFUSED: {bad_timeout}",
                "failure_reason": f"step {step_id}: {bad_timeout}",
            }

    context = interpolation_context(target, frontmatter)

    if target.get("kind") == "cluster":
        pve_node = await _pick_cluster_node(proxmox)
        if pve_node:
            context["target"]["node"] = pve_node

    log_lines: list[str] = []
    start = time.monotonic()
    # Steps that did NOT do what they were asked. `on_error: continue` means
    # "keep going", never "pretend it worked": an artifact that reports `applied`
    # over a step that failed or that was refused by an undecided precheck is the
    # lie this review exists to remove (#642 B5).
    unfinished: list[str] = []
    # Steps that ran and did what they were asked (#796): the record of what a
    # failed run had already changed. A step its precheck skipped is not here -
    # it changed nothing.
    done: list[str] = []
    warnings_all: list[dict[str, Any]] = []

    def _later(index: int) -> list[str]:
        return [str(later.get("id", "unknown")) for later in steps[index + 1 :]]

    for index, step in enumerate(steps):
        step_id = step.get("id", "unknown")
        step_log = f"[{step_id}]"
        on_error = step.get("on_error", "halt")

        try:
            path = _interpolate(step.get("path", ""), context)
            method = step.get("method", "GET").upper()
            do_body = step.get("body")
            if do_body is not None:
                do_body = _interpolate_obj(do_body, context)
        except InterpolationError as exc:
            step_log += f" -> REFUSED: {exc}"
            log_lines.append(step_log)
            elapsed = time.monotonic() - start
            log_lines.append(f"duration={elapsed:.1f}s")
            return _with_partial(
                {
                    "success": False,
                    "execution_log": "\n".join(log_lines),
                    "failure_reason": f"step {step_id} could not be interpolated: {exc}",
                },
                done=done,
                failed=[str(step_id)],
                error=f"could not be interpolated: {exc}",
                not_run=_later(index),
                warnings=warnings_all,
            )

        precheck = step.get("precheck")
        if precheck and not rollback:
            try:
                pre_method = precheck.get("method", "GET").upper()
                pre_path = _interpolate(precheck.get("path", ""), context)
            except InterpolationError as exc:
                step_log += f" -> REFUSED: precheck path {exc}"
                log_lines.append(step_log)
                elapsed = time.monotonic() - start
                log_lines.append(f"duration={elapsed:.1f}s")
                return _with_partial(
                    {
                        "success": False,
                        "execution_log": "\n".join(log_lines),
                        "failure_reason": (
                            f"precheck for {step_id} could not be interpolated: {exc}"
                        ),
                    },
                    done=done,
                    failed=[str(step_id)],
                    error=f"its precheck could not be interpolated: {exc}",
                    not_run=_later(index),
                    warnings=warnings_all,
                )
            skip_if_expr = precheck.get("skip_if")

            step_log += f" precheck {pre_method} {pre_path}"
            undecided: str | None = None
            proxy: Any = None
            try:
                resp = await proxmox.call(pre_method, pre_path)
                proxy = make_pve_response_proxy(200, resp)
            except ProxmoxError as e:
                if e.status_code and e.status_code < _PRECHECK_ANSWERS_BELOW:
                    # A 4xx IS an answer about the resource; let the expression
                    # decide on it (`status_code == 200` correctly says "no").
                    proxy = make_pve_response_proxy(e.status_code, {"data": None})
                    step_log += f" -> HTTP {e.status_code}"
                else:
                    undecided = f"precheck did not answer ({e})"

            if undecided is None and skip_if_expr:
                try:
                    if _eval_skip_if(skip_if_expr, proxy, context.get("target", {})):
                        step_log += " -> SKIPPED (precheck)"
                        log_lines.append(step_log)
                        continue
                except SkipIfUndecided as exc:
                    undecided = str(exc)

            if undecided is not None:
                # The whole point of a precheck is to stand between the plan and
                # the change. One that did not answer does not get to wave the
                # change through.
                step_log += f" -> NOT RUN: {undecided}"
                log_lines.append(step_log)
                unfinished.append(step_id)
                if on_error == "halt":
                    elapsed = time.monotonic() - start
                    log_lines.append(f"duration={elapsed:.1f}s")
                    return _with_partial(
                        {
                            "success": False,
                            "execution_log": "\n".join(log_lines),
                            "failure_reason": (
                                f"step {step_id} was not run: its precheck could not be "
                                f"decided ({undecided})"
                            ),
                        },
                        done=done,
                        failed=[str(step_id)],
                        error=f"its precheck could not be decided ({undecided})",
                        not_run=_later(index),
                        warnings=warnings_all,
                    )
                continue

        try:
            if method in ("POST", "PUT", "DELETE", "PATCH"):
                resp = await proxmox.call(method, path, body=do_body)
            else:
                resp = await proxmox.call(method, path)
            step_log += f" {method} {path} -> OK"
            # A PVE call that spawns a worker answers with a UPID and returns
            # IMMEDIATELY - the work has only been ACCEPTED. Logging "OK" and
            # moving on made every sequence race the cluster: a stop step was
            # reported done while the guest was still shutting down, and the
            # destroy step behind it got "VM 101 is running - destroy failed",
            # leaving the guest on the node (seen live on dev, #629/#626). It
            # also hid outright failure: a task that dies asynchronously was
            # still logged as OK. Wait for the task and report what it did.
            wait_s = task_timeout_for(step, method, path, do_body, task_timeout_s)
            waited, step_warnings = await _await_pve_task(
                proxmox, context, resp, wait_s, poll_interval
            )
            if waited:
                step_log += waited
            warnings_all.extend(step_warnings)
            done.append(str(step_id))
        except ProxmoxTaskStillRunningError as e:
            # The cluster ACCEPTED the work and is still doing it. That is not a
            # failure (#774: a vzdump reported failed at 600 s finished OK with
            # every archive written), and it is not a success either. Stop here
            # whatever `on_error` says - the next step would race a task that
            # still holds the guest (#629) - and hand back WHICH task, so the
            # outcome can be settled from PVE once it ends.
            step_log += f" (task STILL RUNNING after {e.waited_s:.0f}s: {e.upid}; outcome unknown)"
            log_lines.append(step_log)
            elapsed = time.monotonic() - start
            log_lines.append(f"duration={elapsed:.1f}s")
            not_run = [str(later.get("id", "unknown")) for later in steps[index + 1 :]]
            reason = (
                f"outcome unknown: step {step_id}'s PVE task {e.upid} was still running "
                f"when the wait ended after {e.waited_s:.0f}s"
            )
            if not_run:
                reason += "; not run yet: " + ", ".join(not_run)
            return {
                "success": False,
                "execution_log": "\n".join(log_lines),
                "failure_reason": reason,
                "unsettled_task": {
                    "node": e.node,
                    "upid": e.upid,
                    "step": str(step_id),
                    "waited_s": e.waited_s,
                    "steps_not_run": not_run,
                    # Steps that already did not complete under on_error:
                    # continue. However the task ends, they keep the verdict
                    # from being "applied".
                    "steps_unfinished": list(unfinished),
                },
            }
        except ProxmoxError as e:
            step_log += f" {method} {path} -> ERROR {e.status_code}: {e.body[:200]}"
            reread: tuple[str, list[str], list[str]] | None = None
            if _is_config_write(method, path):
                # PVE may have taken part of this write before refusing the
                # rest (#796); only reading it back can say which part.
                keys = await _reread_config_write(proxmox, path, do_body)
                if keys is None:
                    step_log += " (could not re-read the config to see what was applied)"
                else:
                    reread = (str(step_id), keys[0], keys[1])
                    if keys[0]:
                        step_log += f" (re-read: applied {', '.join(keys[0])}"
                        step_log += f"; not applied {', '.join(keys[1])})" if keys[1] else ")"
            if on_error == "halt":
                elapsed = time.monotonic() - start
                log_lines.append(step_log)
                log_lines.append(f"duration={elapsed:.1f}s")
                return _with_partial(
                    {
                        "success": False,
                        "execution_log": "\n".join(log_lines),
                        "failure_reason": f"step {step_id} failed: {e}",
                    },
                    done=done,
                    failed=[str(step_id)],
                    error=str(e),
                    not_run=_later(index),
                    warnings=warnings_all,
                    reread=reread,
                )
            unfinished.append(step_id)

        log_lines.append(step_log)

    elapsed = time.monotonic() - start
    log_lines.append(f"duration={elapsed:.1f}s")
    if unfinished:
        return _with_partial(
            {
                "success": False,
                "execution_log": "\n".join(log_lines),
                "failure_reason": (
                    "these steps did not complete (on_error: continue kept the "
                    "sequence going, it did not make them succeed): " + ", ".join(unfinished)
                ),
            },
            done=done,
            failed=[str(u) for u in unfinished],
            error=None,
            not_run=[],
            warnings=warnings_all,
        )
    result: dict[str, Any] = {"success": True, "execution_log": "\n".join(log_lines)}
    if warnings_all:
        result["warnings"] = warnings_all
    return result


async def _pick_cluster_node(proxmox: ProxmoxClient) -> str | None:
    try:
        nodes = await proxmox.read("/nodes")
        data = nodes.get("data", nodes)
        if isinstance(data, list):
            # `status == "online" or n.get("node")` made the status test DEAD:
            # every row carries a `node` key, so the right operand was always
            # truthy and the first row won whatever state it was in. A
            # cluster-scoped apply could be routed at an offline node (#642).
            for n in data:
                if str(n.get("status") or "").strip().lower() == "online":
                    val = n.get("node") or n.get("name")
                    if val is not None:
                        return str(val)
            # No node SAID it was online. Falling back to the first row would be
            # the same guess in a quieter voice, so say nothing instead and let
            # the caller use the target's own node.
            logger.warning(
                "No cluster node reported itself online; not guessing one for a cluster target"
            )
            return None
    except ProxmoxError:
        logger.warning("Failed to pick cluster node, executor will use default")
    return None
