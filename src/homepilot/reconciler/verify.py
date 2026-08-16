from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from homepilot.artifacts.models import ArtifactKind, ArtifactStatus
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.repository import Repository

logger = logging.getLogger(__name__)

_ansible_semaphore = asyncio.Semaphore(3)

_MAX_VERIFY_DEPTH = 10

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")


@dataclass
class VerifyResult:
    artifact_id: str
    drifted: bool = False
    verification_log: str = ""
    details: dict[str, Any] = field(default_factory=dict)


async def verify_artifact(
    artifact_id: str,
    repo: Repository,
    store: ArtifactStore,
    executor: Any | None = None,
    _depth: int = 0,
) -> VerifyResult:
    if _depth > _MAX_VERIFY_DEPTH:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="max recursion depth exceeded",
            details={"reason": "max_depth"},
        )

    try:
        fm, body = store.read(artifact_id)
    except FileNotFoundError:
        raise

    kind_str = fm.get("kind", "")
    try:
        kind = ArtifactKind(kind_str)
    except ValueError:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"unknown kind: {kind_str}",
            details={"reason": "unknown_kind"},
        )

    status_str = fm.get("status", "")
    try:
        status = ArtifactStatus(status_str)
    except ValueError:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"unknown status: {status_str}",
            details={"reason": "unknown_status"},
        )

    if status != ArtifactStatus.APPLIED:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"not applied (status={status_str})",
            details={"reason": "not_applied"},
        )

    if kind == ArtifactKind.KB_NOTE:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="kb-note: unverifiable, skipped",
            details={"reason": "kb_note_skipped"},
        )

    if executor is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no executor available",
            details={"reason": "no_executor"},
        )

    if kind == ArtifactKind.ANSIBLE_PLAYBOOK:
        return await _verify_ansible(artifact_id, fm, body, executor)
    elif kind == ArtifactKind.PROXMOX_API_SEQUENCE:
        return await _verify_proxmox_api(artifact_id, fm, body, executor)
    elif kind == ArtifactKind.HTTP_SEQUENCE:
        return await _verify_http_sequence(artifact_id, fm, body, executor)
    elif kind == ArtifactKind.COMPOSITE:
        return await _verify_composite(artifact_id, fm, body, store, repo, executor, _depth)
    elif kind == ArtifactKind.SHELL_SCRIPT:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="shell-script: unverifiable",
            details={"reason": "shell_script_unverifiable"},
        )
    elif kind == ArtifactKind.HOST_PROVISION:
        return await _verify_host_provision(artifact_id, fm, body, executor)
    else:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"unhandled kind: {kind.value}",
            details={"reason": "unknown_kind"},
        )


def _extract_spec(body: str, tag: str) -> str | None:
    pattern = re.compile(rf"```yaml\s+{tag}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    return m.group(1).strip() if m else None


def _resolve_host(fm: dict[str, Any]) -> str:
    target: dict[str, Any] = fm.get("target", {})
    host: Any = target.get("host") or target.get("node", "")
    return str(host)


def _ansible_output_has_changes(output: str) -> bool:
    recap_match = re.search(r"changed=(\d+)", output)
    if recap_match:
        return int(recap_match.group(1)) > 0
    return "changed" in output.lower() and "changed=0" not in output


async def _verify_ansible(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    spec_yaml = _extract_spec(body, "ansible-spec")
    if spec_yaml is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no ansible-spec found",
            details={"reason": "no_spec"},
        )

    host = _resolve_host(fm)
    if not host:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no target host resolved",
            details={"reason": "no_host"},
        )

    if not _HOSTNAME_RE.match(host):
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"invalid hostname: {host!r}",
            details={"reason": "invalid_host"},
        )

    pve_nodes: list[str] = getattr(executor, "pve_nodes", []) or []
    if pve_nodes:
        from homepilot.adapters.ssh import GuestHostError

        try:
            executor.ssh._validate_guest_only(host, pve_nodes)
        except GuestHostError as exc:
            return VerifyResult(
                artifact_id=artifact_id,
                drifted=False,
                verification_log=str(exc),
                details={"reason": "forbidden_host"},
            )

    inventory_content = (
        f"[target]\n"
        f"{host} ansible_connection=ssh "
        f"ansible_ssh_common_args='-o ProxyCommand=jump-server-internal'\n"
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, prefix="hp-drift-inv-"
    ) as invf:
        invf.write(inventory_content)
        inventory_path = invf.name

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, prefix="hp-drift-play-"
    ) as pf:
        pf.write(spec_yaml)
        playbook_path = pf.name

    try:
        async with _ansible_semaphore:
            try:
                inv = shlex.quote(inventory_path)
                play = shlex.quote(playbook_path)
                cmd = f"ansible-playbook --check --diff -i {inv} {play}"
                _, stdout, _ = await asyncio.wait_for(
                    executor.ssh.exec(host, cmd, timeout=60),
                    timeout=60,
                )
            except TimeoutError:
                return VerifyResult(
                    artifact_id=artifact_id,
                    drifted=False,
                    verification_log="ansible check timed out",
                    details={"reason": "timeout"},
                )

            changed = _ansible_output_has_changes(stdout)
            return VerifyResult(
                artifact_id=artifact_id,
                drifted=changed,
                verification_log=stdout[:2000],
                details={"changed": changed},
            )
    except Exception as exc:
        logger.warning("ansible drift check failed for %s: %s", artifact_id, exc)
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"ansible check error: {exc}",
            details={"reason": "error"},
        )
    finally:
        Path(inventory_path).unlink(missing_ok=True)
        Path(playbook_path).unlink(missing_ok=True)


async def _verify_proxmox_api(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    from homepilot.executor.proxmox_api import _extract_steps as extract_proxmox_steps

    steps = extract_proxmox_steps(body, "proxmox-api-spec")
    if steps is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no proxmox-api-spec found",
            details={"reason": "no_spec"},
        )

    target: dict[str, Any] = fm.get("target", {})
    context: dict[str, Any] = {
        "target": target,
        "artifact": {"id": fm.get("id", ""), "intent": fm.get("intent", "")},
    }

    if target.get("kind") == "cluster":
        from homepilot.executor.proxmox_api import _pick_cluster_node

        try:
            pve_node = await _pick_cluster_node(executor.proxmox)
        except (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError):
            pve_node = None
        if pve_node:
            context["target"] = dict(target)
            context["target"]["node"] = pve_node

    from homepilot.executor.jinja_utils import _eval_skip_if, _interpolate

    drifted_steps: list[str] = []
    skipped_steps: list[str] = []

    for step in steps:
        step_id = step.get("id", "unknown")
        precheck = step.get("precheck")

        if step.get("method", "GET").upper() == "GET" and not precheck:
            skipped_steps.append(step_id)
            continue

        if precheck:
            pre_method = precheck.get("method", "GET").upper()
            pre_path = _interpolate(precheck.get("path", ""), context)
            skip_if_expr = precheck.get("skip_if")

            if not skip_if_expr:
                skipped_steps.append(step_id)
                continue

            try:
                resp = await executor.proxmox.call(pre_method, pre_path)
                if _eval_skip_if(skip_if_expr, resp, context.get("target", {})):
                    skipped_steps.append(step_id)
                    continue
                else:
                    drifted_steps.append(step_id)
                    continue
            except (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError):
                skipped_steps.append(step_id)
                continue
        else:
            skipped_steps.append(step_id)
            continue

    drifted = len(drifted_steps) > 0
    verification_log = f"drifted_steps={drifted_steps}, skipped_steps={skipped_steps}"
    return VerifyResult(
        artifact_id=artifact_id,
        drifted=drifted,
        verification_log=verification_log[:2000],
        details={"drifted_steps": drifted_steps, "skipped_steps": skipped_steps},
    )


async def _verify_http_sequence(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    import httpx

    from homepilot.executor.http_sequence import (
        ExecutorError,
        _resolve_credential,
    )
    from homepilot.executor.http_sequence import (
        _extract_steps as extract_http_steps,
    )
    from homepilot.executor.jinja_utils import _eval_skip_if, _interpolate
    from homepilot.executor.skip_if import make_response_proxy

    steps = extract_http_steps(body, "http-spec")
    if steps is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no http-spec found",
            details={"reason": "no_spec"},
        )

    target: dict[str, Any] = fm.get("target", {})
    context: dict[str, Any] = {
        "target": target,
        "artifact": {"id": fm.get("id", ""), "intent": fm.get("intent", "")},
    }

    client_cache: dict[str, httpx.AsyncClient] = {}
    drifted_steps: list[str] = []
    skipped_steps: list[str] = []

    try:
        for step in steps:
            step_id = step.get("id", "unknown")
            precheck = step.get("precheck")
            cred_name = step.get("name", "")

            if precheck:
                pre_cred_name = precheck.get("name", cred_name)
                pre_method = precheck.get("method", "GET").upper()
                pre_path = _interpolate(precheck.get("path", ""), context)
                skip_if_expr = precheck.get("skip_if")

                if not skip_if_expr:
                    skipped_steps.append(step_id)
                    continue

                try:
                    pre_cred = await _resolve_credential(pre_cred_name, executor.vault)
                except ExecutorError:
                    skipped_steps.append(step_id)
                    continue

                base_url = pre_cred.get("base_url", "").rstrip("/")
                headers = pre_cred.get("headers", {})
                verify_tls = pre_cred.get("verify_tls", True)

                client_key = f"{base_url}:{pre_cred_name}"
                if client_key not in client_cache:
                    client_cache[client_key] = httpx.AsyncClient(
                        base_url=base_url,
                        headers=headers,
                        verify=verify_tls,
                        timeout=30.0,
                    )
                pre_client = client_cache[client_key]

                try:
                    resp = await pre_client.request(method=pre_method, url=pre_path)
                    proxy = make_response_proxy(resp)
                    if _eval_skip_if(skip_if_expr, proxy, context.get("target", {})):
                        skipped_steps.append(step_id)
                        continue
                    else:
                        drifted_steps.append(step_id)
                        continue
                except httpx.HTTPError:
                    skipped_steps.append(step_id)
                    continue

            method = step.get("method", "GET").upper()
            if method == "GET":
                skipped_steps.append(step_id)
                continue

            try:
                cred = await _resolve_credential(cred_name, executor.vault)
            except ExecutorError:
                skipped_steps.append(step_id)
                continue

            base_url = cred.get("base_url", "").rstrip("/")
            headers = cred.get("headers", {})
            verify_tls = cred.get("verify_tls", True)

            client_key = f"{base_url}:{cred_name}"
            if client_key not in client_cache:
                client_cache[client_key] = httpx.AsyncClient(
                    base_url=base_url,
                    headers=headers,
                    verify=verify_tls,
                    timeout=30.0,
                )
            client = client_cache[client_key]

            path = _interpolate(step.get("path", ""), context)
            try:
                resp = await client.request(method=method, url=path)
                proxy = make_response_proxy(resp)
                skip_if_expr = step.get("skip_if")
                if skip_if_expr and _eval_skip_if(skip_if_expr, proxy, context.get("target", {})):
                    skipped_steps.append(step_id)
                    continue
                else:
                    drifted_steps.append(step_id)
                    continue
            except httpx.HTTPError:
                skipped_steps.append(step_id)
                continue
    finally:
        for c in client_cache.values():
            with contextlib.suppress(Exception):
                await c.aclose()

    drifted = len(drifted_steps) > 0
    verification_log = f"drifted_steps={drifted_steps}, skipped_steps={skipped_steps}"
    return VerifyResult(
        artifact_id=artifact_id,
        drifted=drifted,
        verification_log=verification_log[:2000],
        details={"drifted_steps": drifted_steps, "skipped_steps": skipped_steps},
    )


async def _verify_host_provision(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    from homepilot.artifacts.models import parse_host_provision_spec
    from homepilot.executor.host_provision import check_drift

    try:
        spec = parse_host_provision_spec(body)
    except ValueError as exc:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"no valid host-provision-spec: {exc}",
            details={"reason": "no_spec"},
        )

    host = _resolve_host(fm)
    if not host:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no target host resolved",
            details={"reason": "no_host"},
        )
    if not _HOSTNAME_RE.match(host):
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"invalid hostname: {host!r}",
            details={"reason": "invalid_host"},
        )

    pve_nodes: list[str] = getattr(executor, "pve_nodes", []) or []
    if any(host.lower().strip() == n.lower().strip() for n in pve_nodes):
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"host-provision forbidden for PVE node '{host}'",
            details={"reason": "forbidden_host"},
        )

    result = await check_drift(executor.host_adapter, host, spec)
    drifted_items: list[str] = result["drifted_items"]
    return VerifyResult(
        artifact_id=artifact_id,
        drifted=bool(result["drifted"]),
        verification_log=(result["log"] or "all items in desired state")[:2000],
        details={"drifted_items": drifted_items},
    )


async def _verify_composite(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    store: ArtifactStore,
    repo: Repository,
    executor: Any,
    depth: int,
) -> VerifyResult:
    from homepilot.artifacts.models import extract_composite_steps

    steps = extract_composite_steps(body)
    if not steps:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no composite-spec found",
            details={"reason": "no_spec"},
        )

    drifted_subs: list[str] = []
    skipped_subs: list[str] = []

    for step in steps:
        sub_id = step.get("artifact", "")
        if not sub_id:
            skipped_subs.append(step.get("id", "unknown"))
            continue

        try:
            sub_result = await verify_artifact(
                sub_id,
                repo,
                store,
                executor,
                _depth=depth + 1,
            )
        except FileNotFoundError:
            skipped_subs.append(sub_id)
            continue
        except (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError):
            skipped_subs.append(sub_id)
            continue

        if sub_result.drifted:
            drifted_subs.append(sub_id)

    drifted = len(drifted_subs) > 0
    verification_log = f"drifted_subs={drifted_subs}, skipped_subs={skipped_subs}"
    return VerifyResult(
        artifact_id=artifact_id,
        drifted=drifted,
        verification_log=verification_log[:2000],
        details={"drifted_subs": drifted_subs, "skipped_subs": skipped_subs},
    )
