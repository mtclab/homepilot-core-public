from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from homepilot.adapters import HostAdapter

logger = logging.getLogger(__name__)

_SPEC_FENCE = "shell-spec"
_ROLLBACK_FENCE = "shell-rollback"
_IDEMPOTENCE_HEADING = "## Idempotence preamble"


def _extract_script(body: str, tag: str = _SPEC_FENCE) -> str | None:
    pattern = re.compile(rf"```bash\s+{tag}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    return m.group(1).strip() if m else None


def _validate_idempotence_preamble(body: str) -> str | None:
    idx = body.find(_IDEMPOTENCE_HEADING)
    if idx == -1:
        return "Missing '## Idempotence preamble' section"
    after = body[idx + len(_IDEMPOTENCE_HEADING) :]
    next_heading = re.search(r"^##\s+", after, re.MULTILINE)
    preamble = after[: next_heading.start()].strip() if next_heading else after.strip()
    if not preamble:
        return "Idempotence preamble is empty"
    return None


_SCRIPT_DIR = "/opt/homepilot"


def _remote_script_path(artifact_id: str, rollback: bool) -> str:
    """Metachar-free destination under the HP-controlled write prefix.

    A piped heredoc (``cat <<EOF | bash``) is rejected by the agent allowlist's
    shell-metacharacter filter, so a shell-script can never reach a managed host
    that way. Instead the body is shipped as a file under ``/opt/homepilot`` and
    run with a metachar-free ``bash <path>`` (allowlisted, privileged-only). The
    path is stable per (artifact, mode) so a re-apply overwrites rather than
    accumulates; concurrent applies of one artifact are already serialised by the
    task lifecycle. The id is sanitised to the allowlist's ``[a-zA-Z0-9_./-]``.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", artifact_id) or "artifact"
    suffix = "rollback" if rollback else "apply"
    return f"{_SCRIPT_DIR}/hp-{safe}-{suffix}.sh"


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    target: dict[str, Any],
    host_adapter: object,
    pve_nodes: list[str] | None = None,
    rollback: bool = False,
) -> dict[str, Any]:

    ssh_adapter: HostAdapter = cast("HostAdapter", host_adapter)

    tag = _ROLLBACK_FENCE if rollback else _SPEC_FENCE
    script = _extract_script(body, tag)
    if script is None:
        return {
            "success": False,
            "execution_log": f"No ```bash {tag}``` block found",
            "failure_reason": "missing spec",
        }

    if not rollback:
        preamble_error = _validate_idempotence_preamble(body)
        if preamble_error:
            return {
                "success": False,
                "execution_log": preamble_error,
                "failure_reason": preamble_error,
            }

    target_kind = target.get("kind", "")
    if target_kind in ("node", "cluster"):
        return {
            "success": False,
            "execution_log": f"shell-script forbidden for target.kind={target_kind}",
            "failure_reason": "shell-script cannot target PVE nodes or cluster",
        }

    host = target.get("host") or target.get("node", "")
    if not host:
        return {
            "success": False,
            "execution_log": "No target host resolved",
            "failure_reason": "missing host",
        }

    if pve_nodes:
        host_lower = host.lower().strip()
        for node in pve_nodes:
            if host_lower == node.lower().strip():
                return {
                    "success": False,
                    "execution_log": f"shell-script forbidden for PVE node '{host}'",
                    "failure_reason": "forbidden target",
                }

    log_lines: list[str] = []
    start = time.monotonic()

    label = "rollback" if rollback else "script"
    remote_path = _remote_script_path(str(frontmatter.get("id", "artifact")), rollback)

    # Ship the body as a file under the HP write prefix, then run it with a
    # metachar-free `bash <path>` the agent allowlist accepts (privileged-only).
    try:
        await ssh_adapter.write_file(host, remote_path, script)
    except Exception as e:
        return {
            "success": False,
            "execution_log": f"host write error: {e}",
            "failure_reason": str(e),
        }
    log_lines.append(f"$ write {remote_path}  # {label}")

    command = f"bash {remote_path}"
    log_lines.append(f"$ {command}")
    try:
        rc, stdout, stderr = await ssh_adapter.exec(host, command, timeout=300)
    except Exception as e:
        return {
            "success": False,
            "execution_log": f"host exec error: {e}",
            "failure_reason": str(e),
        }

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
            "failure_reason": f"script exited with rc={rc}",
        }
