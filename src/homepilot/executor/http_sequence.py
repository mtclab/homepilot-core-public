from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import Any

import httpx
import yaml

from homepilot.executor.jinja_utils import (
    InterpolationError,
    _eval_skip_if,
    _interpolate,
    _interpolate_obj,
    interpolation_context,
)
from homepilot.executor.skip_if import SkipIfUndecided, make_response_proxy

logger = logging.getLogger(__name__)

_SPEC_FENCE = "http-spec"
_ROLLBACK_FENCE = "http-rollback"

# See proxmox_api: 2xx/4xx answer about the resource, 5xx and transport failures
# answer about the service. Only the first kind is permission to skip - or not to
# skip - a mutating step (#642 A4).
_PRECHECK_ANSWERS_BELOW = 500


def _extract_steps(body: str, tag: str) -> list[dict[str, Any]] | None:
    pattern = re.compile(rf"```yaml\s+{tag}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    if not m:
        return None
    content = m.group(1).strip()
    parsed = yaml.safe_load(content)
    if not isinstance(parsed, dict) or "steps" not in parsed:
        return None
    steps: list[dict[str, Any]] = parsed["steps"]
    return steps


async def _resolve_credential(name: str, vault: Any) -> dict[str, Any]:
    from homepilot.vault.manager import VaultError, VaultManager

    vault_mgr: VaultManager = vault
    try:
        return await vault_mgr.get_secret(name)
    except VaultError:
        raise ExecutorError(f"Vault credential '{name}' not found") from None


class ExecutorError(Exception):
    pass


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    target: dict[str, Any],
    vault: object,
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

    context = interpolation_context(target, frontmatter)

    client_cache: dict[str, httpx.AsyncClient] = {}

    log_lines: list[str] = []
    start = time.monotonic()
    # `on_error: continue` keeps the sequence going; it does not make a step that
    # failed, or one that was refused by an undecided precheck, into a success
    # (#642 B5).
    unfinished: list[str] = []

    try:
        for step in steps:
            step_id = step.get("id", "unknown")
            step_log = f"[{step_id}]"
            on_error = step.get("on_error", "halt")

            cred_name = step.get("name", "")
            precheck = step.get("precheck")
            if precheck and not rollback:
                pre_cred_name = precheck.get("name", cred_name)
                pre_method = precheck.get("method", "GET").upper()
                try:
                    pre_path = _interpolate(precheck.get("path", ""), context)
                except InterpolationError as exc:
                    step_log += f" -> REFUSED: precheck path {exc}"
                    log_lines.append(step_log)
                    return {
                        "success": False,
                        "execution_log": "\n".join(log_lines),
                        "failure_reason": f"precheck for {step_id}: {exc}",
                    }
                skip_if_expr = precheck.get("skip_if")

                undecided: str | None = None
                pre_cred: dict[str, Any] = {}
                try:
                    pre_cred = await _resolve_credential(pre_cred_name, vault)
                except ExecutorError as e:
                    undecided = f"precheck credential '{pre_cred_name}' not found ({e})"

                if undecided is None:
                    base_url = pre_cred.get("base_url", "").rstrip("/")
                    headers = pre_cred.get("headers", {})
                    verify_tls = pre_cred.get("verify_tls", True)

                    client_key = f"{base_url}:{pre_cred_name}"
                    if client_key not in client_cache:
                        client_cache[client_key] = httpx.AsyncClient(
                            base_url=base_url, headers=headers, verify=verify_tls, timeout=30.0
                        )
                    pre_client = client_cache[client_key]

                    step_log += f" precheck {pre_method} {pre_path}"
                    try:
                        resp = await pre_client.request(method=pre_method, url=pre_path)
                        if resp.status_code >= _PRECHECK_ANSWERS_BELOW:
                            undecided = (
                                f"precheck answered HTTP {resp.status_code}, which says "
                                "nothing about the resource"
                            )
                        elif skip_if_expr:
                            proxy = make_response_proxy(resp)
                            if _eval_skip_if(skip_if_expr, proxy, context.get("target", {})):
                                step_log += " -> SKIPPED (precheck)"
                                log_lines.append(step_log)
                                continue
                    except SkipIfUndecided as e:
                        undecided = str(e)
                    except httpx.HTTPError as e:
                        undecided = f"precheck did not answer ({e})"

                if undecided is not None:
                    step_log += f" -> NOT RUN: {undecided}"
                    log_lines.append(step_log)
                    unfinished.append(step_id)
                    if on_error == "halt":
                        return {
                            "success": False,
                            "execution_log": "\n".join(log_lines),
                            "failure_reason": (
                                f"step {step_id} was not run: its precheck could not be "
                                f"decided ({undecided})"
                            ),
                        }
                    continue

            method = step.get("method", "GET").upper()
            try:
                path = _interpolate(step.get("path", ""), context)
                do_body = step.get("body")
                if do_body is not None:
                    do_body = _interpolate_obj(do_body, context)
            except InterpolationError as exc:
                step_log += f" -> REFUSED: {exc}"
                log_lines.append(step_log)
                return {
                    "success": False,
                    "execution_log": "\n".join(log_lines),
                    "failure_reason": f"step {step_id} could not be interpolated: {exc}",
                }

            try:
                cred = await _resolve_credential(cred_name, vault)
            except ExecutorError as e:
                step_log += f" credential '{cred_name}' not found"
                log_lines.append(step_log)
                unfinished.append(step_id)
                if on_error == "halt":
                    return {
                        "success": False,
                        "execution_log": "\n".join(log_lines),
                        "failure_reason": str(e),
                    }
                continue

            base_url = cred.get("base_url", "").rstrip("/")
            headers = cred.get("headers", {})
            verify_tls = cred.get("verify_tls", True)

            client_key = f"{base_url}:{cred_name}"
            if client_key not in client_cache:
                client_cache[client_key] = httpx.AsyncClient(
                    base_url=base_url, headers=headers, verify=verify_tls, timeout=30.0
                )
            client = client_cache[client_key]

            try:
                kwargs: dict[str, Any] = {}
                if do_body is not None and method in ("POST", "PUT", "PATCH"):
                    kwargs["json"] = do_body
                resp = await client.request(method=method, url=path, **kwargs)
                step_log += f" {method} {path} -> {resp.status_code}"
                if resp.status_code >= 400:
                    if on_error == "halt":
                        log_lines.append(step_log)
                        return {
                            "success": False,
                            "execution_log": "\n".join(log_lines),
                            "failure_reason": f"step {step_id}: HTTP {resp.status_code}",
                        }
                    unfinished.append(step_id)
            except httpx.HTTPError as e:
                step_log += f" {method} {path} -> ERROR: {e}"
                if on_error == "halt":
                    log_lines.append(step_log)
                    return {
                        "success": False,
                        "execution_log": "\n".join(log_lines),
                        "failure_reason": str(e),
                    }
                unfinished.append(step_id)

            log_lines.append(step_log)

    finally:
        # Every `halt` path used to `return` from inside the loop; three of them
        # skipped the close, leaking an httpx.AsyncClient and its pool on every
        # apply whose step named a missing vault credential (#388). A finally
        # covers every exit, including exceptions, and removes the three
        # duplicated close blocks that used to guard only some of them.
        for c in client_cache.values():
            with contextlib.suppress(Exception):
                await c.aclose()

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
