from __future__ import annotations

import logging
import re
import time
from typing import Any

import yaml

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.executor.jinja_utils import (
    InterpolationError,
    _eval_skip_if,
    _interpolate,
    _interpolate_obj,
    interpolation_context,
)
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

# A clone or a destroy of a large disk is minutes of real work, so the wait has
# to outlast it; the poll matches ProvisionService so one cluster sees one
# polling cadence.
DEFAULT_TASK_TIMEOUT_S = 600.0
DEFAULT_POLL_INTERVAL_S = 2.0


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
) -> str:
    """Block until the task this step spawned finishes; raise if it failed.

    Returns the text to append to the step log, or "" when the call was
    synchronous and there was nothing to wait for.
    """
    upid = ProxmoxClient.upid_of(resp)
    if not upid:
        return ""
    node = _node_for_task(context, upid)
    if not node:
        # Nothing to poll against. Say so rather than implying the task
        # finished - a silent skip here is the very bug this code fixes.
        return " (task not waited on: no node in the UPID or the target)"
    await proxmox.wait_for_task(node, upid, timeout_s=timeout_s, poll_interval=poll_interval)
    return " (task finished OK)"


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
    task_timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
) -> dict[str, Any]:
    tag = _ROLLBACK_FENCE if rollback else _SPEC_FENCE
    steps = _extract_steps(body, tag)
    if steps is None:
        return {
            "success": False,
            "execution_log": f"No ```yaml {tag}``` block found",
            "failure_reason": "missing spec",
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

    for step in steps:
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
            return {
                "success": False,
                "execution_log": "\n".join(log_lines),
                "failure_reason": f"step {step_id} could not be interpolated: {exc}",
            }

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
                return {
                    "success": False,
                    "execution_log": "\n".join(log_lines),
                    "failure_reason": f"precheck for {step_id} could not be interpolated: {exc}",
                }
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
                    return {
                        "success": False,
                        "execution_log": "\n".join(log_lines),
                        "failure_reason": (
                            f"step {step_id} was not run: its precheck could not be "
                            f"decided ({undecided})"
                        ),
                    }
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
            waited = await _await_pve_task(proxmox, context, resp, task_timeout_s, poll_interval)
            if waited:
                step_log += waited
        except ProxmoxError as e:
            step_log += f" {method} {path} -> ERROR {e.status_code}: {e.body[:200]}"
            if on_error == "halt":
                elapsed = time.monotonic() - start
                log_lines.append(step_log)
                log_lines.append(f"duration={elapsed:.1f}s")
                return {
                    "success": False,
                    "execution_log": "\n".join(log_lines),
                    "failure_reason": f"step {step_id} failed: {e}",
                }
            unfinished.append(step_id)

        log_lines.append(step_log)

    elapsed = time.monotonic() - start
    log_lines.append(f"duration={elapsed:.1f}s")
    if unfinished:
        return {
            "success": False,
            "execution_log": "\n".join(log_lines),
            "failure_reason": (
                "these steps did not complete (on_error: continue kept the "
                "sequence going, it did not make them succeed): " + ", ".join(unfinished)
            ),
        }
    return {"success": True, "execution_log": "\n".join(log_lines)}


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
