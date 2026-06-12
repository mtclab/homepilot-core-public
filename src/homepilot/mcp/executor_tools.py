"""Executor-only tools — NOT exposed via MCP. Called by ArtifactExecutor during apply/revoke."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from homepilot.adapters.agent import AgentAdapter
from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import ArtifactStatus
from homepilot.vault import VaultManager

logger = logging.getLogger(__name__)


async def proxmox_api(
    client: ProxmoxClient,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    node: str | None = None,
) -> dict[str, Any]:
    result = await client.call(method=method, path=path, body=body)
    return result


async def http_call(
    name: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    vault: VaultManager | None = None,
) -> dict[str, Any]:
    if vault is None:
        raise ValueError(f"Vault locked — cannot resolve credentials for service '{name}'")

    creds = await vault.get_secret(name)
    base_url = creds.get("url", "")
    headers: dict[str, str] = {}
    token = creds.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=30.0, verify=True) as http:
        resp = await http.request(
            method=method.upper(),
            url=f"{base_url}{path}",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        try:
            result: dict[str, Any] = resp.json()
            return result
        except ValueError:
            return {"status_code": resp.status_code, "text": resp.text}


async def exec_on_guest(
    ssh_adapter: AgentAdapter,
    host: str,
    command: str,
    timeout: int = 30,
) -> dict[str, Any]:
    exit_code, stdout, stderr = await ssh_adapter.exec(host=host, command=command, timeout=timeout)
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


async def write_file_on_guest(
    ssh_adapter: AgentAdapter,
    host: str,
    path: str,
    content: str,
) -> dict[str, Any]:
    result: dict[str, Any] = await ssh_adapter.write_file(host=host, path=path, content=content)
    return result


async def snapshot_vm(
    client: ProxmoxClient,
    node: str,
    vmid: int,
    name: str = "hp-pre-apply",
) -> dict[str, Any]:
    result = await client.snapshot(node=node, vmid=vmid, name=name)
    return result


async def apply_artifact(
    lifecycle: ArtifactLifecycle,
    artifact_id: str,
    approved_by: str = "cli",
) -> dict[str, Any]:
    fm, body = lifecycle.store.read(artifact_id)
    current_status = fm.get("status")

    if current_status not in (ArtifactStatus.APPROVED.value, ArtifactStatus.APPLIED.value):
        return {
            "success": False,
            "error": f"Artifact {artifact_id} is {current_status}, expected approved or applied",
        }

    kind = fm.get("kind", "")
    spec_body = body
    target = fm.get("target", {})
    log_entries: list[str] = []

    if kind == "kb-note":
        return {"success": True, "id": artifact_id, "status": "already-applied"}

    executor_funcs = {
        "proxmox-api-sequence": _apply_proxmox_sequence,
        "http-sequence": _apply_http_sequence,
        "shell-script": _apply_shell_script,
        "composite": _apply_composite,
        "ansible-playbook": _apply_ansible_playbook,
    }

    apply_fn = executor_funcs.get(kind)
    if apply_fn is None:
        return {"success": False, "error": f"Unknown artifact kind: {kind}"}

    try:
        result = await apply_fn(fm, spec_body, target, lifecycle)
        log_entries.append(f"Apply succeeded for {artifact_id}")
        if result:
            log_entries.append(json.dumps(result, default=str))
    except Exception as e:
        log_entries.append(f"Apply failed for {artifact_id}: {e}")
        await lifecycle.mark_failed(artifact_id, reason=str(e), partial_log="\n".join(log_entries))
        return {"success": False, "id": artifact_id, "error": str(e)}

    execution_log = "\n".join(log_entries)
    await lifecycle.mark_applied(artifact_id, execution_log=execution_log)
    return {"success": True, "id": artifact_id, "status": "applied"}


async def revoke_artifact(
    lifecycle: ArtifactLifecycle,
    artifact_id: str,
    user: str = "cli",
    reason: str | None = None,
) -> dict[str, Any]:
    await lifecycle.revoke(artifact_id, user=user, reason=reason)

    _fm, body = lifecycle.store.read(artifact_id)

    rollback_spec = None
    for line in body.split("\n"):
        if line.strip().startswith("## Rollback"):
            rollback_spec = True
            break

    return {
        "success": True,
        "id": artifact_id,
        "status": "revoked",
        "has_rollback": rollback_spec is not None,
    }


async def _apply_proxmox_sequence(
    frontmatter: dict[str, Any], body: str, target: dict[str, Any], lifecycle: ArtifactLifecycle
) -> dict[str, Any]:
    from homepilot.executor.proxmox_api import execute as proxmox_execute

    proxmox = lifecycle._proxmox_client()
    vault = lifecycle._vault()
    return await proxmox_execute(frontmatter, body, target, proxmox, vault)


async def _apply_http_sequence(
    frontmatter: dict[str, Any], body: str, target: dict[str, Any], lifecycle: ArtifactLifecycle
) -> dict[str, Any]:
    from homepilot.executor.http_sequence import execute as http_execute

    vault = lifecycle._vault()
    return await http_execute(frontmatter, body, target, vault)


async def _apply_shell_script(
    frontmatter: dict[str, Any], body: str, target: dict[str, Any], lifecycle: ArtifactLifecycle
) -> dict[str, Any]:
    from homepilot.executor.shell_script import execute as shell_execute

    ssh = lifecycle._ssh_adapter()
    pve_nodes = lifecycle._pve_nodes()
    return await shell_execute(frontmatter, body, target, ssh, pve_nodes)


async def _apply_composite(
    frontmatter: dict[str, Any], body: str, target: dict[str, Any], lifecycle: ArtifactLifecycle
) -> dict[str, Any]:
    from homepilot.executor.composite import execute as composite_execute

    return await composite_execute(frontmatter, body, lifecycle, lifecycle._executor())


async def _apply_ansible_playbook(
    frontmatter: dict[str, Any], body: str, target: dict[str, Any], lifecycle: ArtifactLifecycle
) -> dict[str, Any]:
    from homepilot.executor.ansible import execute as ansible_execute

    ssh = lifecycle._ssh_adapter()
    pve_nodes = lifecycle._pve_nodes()
    return await ansible_execute(frontmatter, body, target, ssh, repo=None, pve_nodes=pve_nodes)
