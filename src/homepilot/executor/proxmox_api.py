from __future__ import annotations

import logging
import re
import time
from typing import Any

import yaml

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.executor.jinja_utils import (
    _eval_skip_if,
    _interpolate,
    _interpolate_obj,
)

logger = logging.getLogger(__name__)

_SPEC_FENCE = "proxmox-api-spec"
_ROLLBACK_FENCE = "proxmox-api-rollback"


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
) -> dict[str, Any]:
    tag = _ROLLBACK_FENCE if rollback else _SPEC_FENCE
    steps = _extract_steps(body, tag)
    if steps is None:
        return {
            "success": False,
            "execution_log": f"No ```yaml {tag}``` block found",
            "failure_reason": "missing spec",
        }

    context: dict[str, Any] = {
        "target": target,
        "artifact": {
            "id": frontmatter.get("id", ""),
            "intent": frontmatter.get("intent", ""),
        },
    }

    if target.get("kind") == "cluster":
        pve_node = await _pick_cluster_node(proxmox)
        if pve_node:
            context["target"] = dict(target)
            context["target"]["node"] = pve_node

    log_lines: list[str] = []
    start = time.monotonic()
    applied_steps: list[dict[str, Any]] = []

    for step in steps:
        step_id = step.get("id", "unknown")
        step_log = f"[{step_id}]"

        path = _interpolate(step.get("path", ""), context)
        method = step.get("method", "GET").upper()
        do_body = step.get("body")
        if do_body is not None:
            do_body = _interpolate_obj(do_body, context)

        precheck = step.get("precheck")
        if precheck and not rollback:
            pre_method = precheck.get("method", "GET").upper()
            pre_path = _interpolate(precheck.get("path", ""), context)
            skip_if_expr = precheck.get("skip_if")

            step_log += f" precheck {pre_method} {pre_path}"
            try:
                resp = await proxmox.call(pre_method, pre_path)
                if skip_if_expr and _eval_skip_if(skip_if_expr, resp, context.get("target", {})):
                    step_log += " -> SKIPPED (precheck)"
                    log_lines.append(step_log)
                    continue
            except ProxmoxError as e:
                if pre_method == "GET" and e.status_code == 0:
                    step_log += f" -> precheck unreachable: {e}"
                    log_lines.append(step_log)
                    on_error = step.get("on_error", "halt")
                    if on_error == "halt":
                        elapsed = time.monotonic() - start
                        log_lines.append(f"duration={elapsed:.1f}s")
                        return {
                            "success": False,
                            "execution_log": "\n".join(log_lines),
                            "failure_reason": f"precheck unreachable for {step_id}",
                        }

        try:
            if method in ("POST", "PUT", "DELETE", "PATCH"):
                resp = await proxmox.call(method, path, body=do_body)
            else:
                resp = await proxmox.call(method, path)
            step_log += f" {method} {path} -> OK"
            applied_steps.append({"step": step_id, "status": "ok"})
        except ProxmoxError as e:
            step_log += f" {method} {path} -> ERROR {e.status_code}: {e.body[:200]}"
            on_error = step.get("on_error", "halt")
            if on_error == "halt":
                elapsed = time.monotonic() - start
                log_lines.append(step_log)
                log_lines.append(f"duration={elapsed:.1f}s")
                return {
                    "success": False,
                    "execution_log": "\n".join(log_lines),
                    "failure_reason": f"step {step_id} failed: {e}",
                }
            else:
                applied_steps.append({"step": step_id, "status": "error", "error": str(e)})

        log_lines.append(step_log)

    elapsed = time.monotonic() - start
    log_lines.append(f"duration={elapsed:.1f}s")
    return {"success": True, "execution_log": "\n".join(log_lines)}


async def _pick_cluster_node(proxmox: ProxmoxClient) -> str | None:
    try:
        nodes = await proxmox.read("/nodes")
        data = nodes.get("data", nodes)
        if isinstance(data, list):
            for n in data:
                if n.get("status") == "online" or n.get("node"):
                    val = n.get("node") or n.get("name")
                    return str(val) if val is not None else None
            if data:
                val = data[0].get("node") or data[0].get("name")
                return str(val) if val is not None else None
    except ProxmoxError:
        logger.warning("Failed to pick cluster node, executor will use default")
    return None
