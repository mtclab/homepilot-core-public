from __future__ import annotations

import logging
import re
import shlex
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from homepilot.adapters.agent import is_pve_node

if TYPE_CHECKING:
    from homepilot.adapters import HostAdapter

logger = logging.getLogger(__name__)


def _extract_spec(body: str, tag: str = "ansible-spec") -> str | None:
    pattern = re.compile(rf"```yaml\s+{tag}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    return m.group(1).strip() if m else None


def _resolve_target_host(target: dict[str, Any]) -> str:
    host: Any = target.get("host") or target.get("node", "")
    return str(host)


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    target: dict[str, Any],
    host_adapter: object,
    repo: object | None = None,
    pve_nodes: list[str] | None = None,
    rollback: bool = False,
) -> dict[str, Any]:
    ssh_adapter: HostAdapter = cast("HostAdapter", host_adapter)

    tag = "ansible-rollback" if rollback else "ansible-spec"
    spec_yaml = _extract_spec(body, tag)
    if spec_yaml is None:
        return {
            "success": False,
            "execution_log": f"No ```yaml {tag}``` block found",
            "failure_reason": "missing spec",
        }

    try:
        playbook = yaml.safe_load(spec_yaml)
    except yaml.YAMLError as e:
        return {
            "success": False,
            "execution_log": f"Invalid YAML in {tag}: {e}",
            "failure_reason": "invalid yaml",
        }

    if not isinstance(playbook, list):
        return {
            "success": False,
            "execution_log": "Ansible playbook must be a YAML list",
            "failure_reason": "invalid playbook",
        }

    host = _resolve_target_host(target)
    if not host:
        return {
            "success": False,
            "execution_log": "No target host resolved",
            "failure_reason": "missing host",
        }

    # Refuse to run a playbook against a Proxmox hypervisor node. This was
    # `hasattr(ssh_adapter, "_validate_guest_only")` guarding a call to a method
    # that no longer exists (it is `_check_guest_only` now), so the hasattr was
    # always False and the guard NEVER RAN - a dead safety check, not dead code.
    # It also caught GuestHostError from adapters.ssh while the agent adapter
    # raises the one from adapters.agent (#388).
    if pve_nodes and is_pve_node(host, pve_nodes):
        return {
            "success": False,
            "execution_log": f"PVE node '{host}' - use the Proxmox API instead",
            "failure_reason": "forbidden target",
        }

    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$", host):
        return {
            "success": False,
            "execution_log": f"invalid hostname: {host}",
            "failure_reason": "hostname contains invalid characters",
        }

    inventory_content = (
        f"[target]\n"
        # The jump server was decommissioned in #327; the ProxyCommand here
        # pointed at a host that no longer resolves (#388). NOTE: this does
        # not make the ansible path work - it is structurally dead (temp
        # inventory written on the control plane, ansible-playbook run on the
        # remote host where neither the files nor ansible exist). That is the
        # first item of #388 and needs a real transport design.
        f"{host} ansible_connection=ssh\n"
    )

    variables_section = _extract_variables(body)
    extra_vars_args: list[str] = []
    if variables_section:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, prefix="hp-vars-"
        ) as vf:
            vf.write(variables_section)
            extra_vars_args = ["-e", f"@{shlex.quote(vf.name)}"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, prefix="hp-inv-"
    ) as invf:
        invf.write(inventory_content)
        inventory_path = invf.name

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, prefix="hp-play-"
    ) as pf:
        pf.write(spec_yaml)
        playbook_path = pf.name

    import time

    log_lines: list[str] = []
    start = time.monotonic()

    try:
        if not rollback:
            inv = shlex.quote(inventory_path)
            play = shlex.quote(playbook_path)
            cmd = f"ansible-playbook --check --diff -i {inv} {play}"
            for a in extra_vars_args:
                cmd += f" {a}"
            log_lines.append(f"$ {cmd}")
            rc, stdout, stderr = await ssh_adapter.exec(host, cmd, timeout=120)
            log_lines.append(f"[dry-run] exit={rc}")
            log_lines.append(f"[dry-run] stdout:\n{stdout}")
            if stderr:
                log_lines.append(f"[dry-run] stderr:\n{stderr}")
            if rc != 0:
                return {
                    "success": False,
                    "execution_log": "\n".join(log_lines),
                    "failure_reason": f"dry-run failed with rc={rc}",
                }

        cmd = f"ansible-playbook -i {shlex.quote(inventory_path)} {shlex.quote(playbook_path)}"
        for a in extra_vars_args:
            cmd += f" {a}"
        log_lines.append(f"$ {cmd}")
        rc, stdout, stderr = await ssh_adapter.exec(host, cmd, timeout=300)
        log_lines.append(f"exit={rc}")
        log_lines.append(f"stdout:\n{stdout}")
        if stderr:
            log_lines.append(f"stderr:\n{stderr}")

        elapsed = time.monotonic() - start
        log_lines.append(f"duration={elapsed:.1f}s")

        if rc == 0:
            return {"success": True, "execution_log": "\n".join(log_lines)}
        else:
            return {
                "success": False,
                "execution_log": "\n".join(log_lines),
                "failure_reason": f"ansible-playbook exited with rc={rc}",
            }
    except Exception as e:
        elapsed = time.monotonic() - start
        log_lines.append(f"duration={elapsed:.1f}s")
        log_lines.append(f"error: {e}")
        return {"success": False, "execution_log": "\n".join(log_lines), "failure_reason": str(e)}
    finally:
        Path(inventory_path).unlink(missing_ok=True)
        Path(playbook_path).unlink(missing_ok=True)
        if variables_section and extra_vars_args:
            import os

            os.unlink(extra_vars_args[1][1:])


def _extract_variables(body: str) -> str | None:
    variables_match = re.search(r"## Variables\s*\n(.*?)(?=\n## |\Z)", body, re.DOTALL)
    if not variables_match:
        return None
    content = variables_match.group(1).strip()
    yaml_match = re.search(r"```yaml\s*\n(.*?)```", content, re.DOTALL)
    if yaml_match:
        return yaml_match.group(1)
    try:
        yaml.safe_load(content)
        return content
    except yaml.YAMLError:
        return None
