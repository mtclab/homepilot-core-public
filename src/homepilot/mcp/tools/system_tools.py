"""System/infrastructure read-only tools: Proxmox API, HTTP calls, SSH file/exec."""

from __future__ import annotations

import json
import logging
import posixpath
from typing import Any

import httpx
from mcp.types import TextContent

from homepilot.adapters.agent import AgentAdapter
from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.mcp.tools.ssrf_guard import SSRFError, _PinnedTransport, validate_url
from homepilot.vault import VaultManager

logger = logging.getLogger(__name__)

_proxmox_read_prefixes = (
    "/nodes/",
    "/storage/",
    "/access/",
    "/cluster/",
    "/pools/",
    "/version",
    "/api2/json/version",
)


def _proxmox_path_allowed(path: str) -> bool:
    p = path.lstrip("/")
    for prefix in _proxmox_read_prefixes:
        pre = prefix.lstrip("/").rstrip("/")
        if p == pre or p.startswith(pre + "/"):
            return True
    return False


# Defense-in-depth allowlist for guest file reads at the MCP boundary. The host
# agent (hp_agent.file_ops) is the real enforcement point; this mirrors its read
# prefixes and adds a hard secret denylist so the control plane cannot even ask an
# agent to hand back the sensitive files the agent itself refuses.
_ALLOWED_READ_PREFIXES = (
    "/var/log/",
    "/etc/",
    "/opt/homepilot/",
    "/proc/",
    "/sys/",
    "/tmp/homepilot/",
    "/home/",
    "/usr/local/bin/",
)

# Secrets that are blocked regardless of prefix (some, like /etc/shadow, live
# under an allowed prefix).
_DENIED_READ_PATHS = ("/etc/shadow", "/etc/gshadow")
_DENIED_READ_BASENAMES = ("id_rsa", "id_ed25519")
_DENIED_READ_SUFFIXES = (".key", ".pem")


def _check_guest_read_path(path: str) -> None:
    """Raise ``ValueError`` if a guest file-read path is outside the read
    allowlist or hits the secret denylist (private keys, shadow files, process
    environments). Mirrors the Go/Python host agent's protections."""
    if ".." in path.split("/"):
        raise ValueError(f"path '{path}' contains parent traversal")
    norm = posixpath.normpath(path)
    if not norm.startswith("/"):
        raise ValueError(f"path '{path}' must be an absolute path")

    # Secret denylist — enforced regardless of the allowed prefix.
    base = posixpath.basename(norm)
    if norm in _DENIED_READ_PATHS:
        raise ValueError(f"path '{path}' is denied (sensitive file)")
    if base in _DENIED_READ_BASENAMES or norm.endswith(_DENIED_READ_SUFFIXES):
        raise ValueError(f"path '{path}' is denied (private key material)")
    parts = norm.split("/")
    if len(parts) >= 4 and parts[1] == "proc" and parts[-1] == "environ":
        raise ValueError(f"path '{path}' is denied (process environment)")

    if not norm.startswith(_ALLOWED_READ_PREFIXES):
        allowed = ", ".join(_ALLOWED_READ_PREFIXES)
        raise ValueError(f"path '{path}' not under an allowed read prefix ({allowed})")


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "proxmox_api_read",
        "description": (
            "GET-only Proxmox REST API call. Used to read configs, list resources, "
            "check status. Vault-resolved token. Cannot mutate state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Proxmox API path (e.g. /nodes/pve1/status)",
                },
                "query": {
                    "type": ["string", "null"],
                    "description": "Query string or null",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "http_call_read",
        "description": (
            "GET-only REST call to an adopted service (e.g. Authentik, Traefik). "
            "Vault-resolved credentials. Cannot mutate state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Service name as registered in vault (e.g. authentik-admin)",
                },
                "path": {
                    "type": "string",
                    "description": "API path on the service",
                },
                "query": {
                    "type": ["string", "null"],
                    "description": "Query string or null",
                },
            },
            "required": ["name", "path"],
        },
    },
    {
        "name": "read_file_on_guest",
        "description": "Read a file from a guest host via the connected hp-agent (agent hub).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Guest hostname",
                },
                "path": {
                    "type": "string",
                    "description": "Absolute file path on the guest",
                },
            },
            "required": ["host", "path"],
        },
    },
    {
        "name": "exec_on_guest_readonly",
        "description": (
            "Execute an allowlisted read-only command on a guest via the connected "
            "hp-agent (agent hub). "
            "Allowed commands: ls, ps, systemctl status, journalctl, "
            "dpkg, ip, ss, hostname, uname, df, free, uptime. "
            "For file reading, use read_file_on_guest instead (logs access)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Guest hostname",
                },
                "command": {
                    "type": "string",
                    "description": "Read-only command to execute",
                },
            },
            "required": ["host", "command"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
            },
            "required": ["exit_code", "stdout", "stderr"],
        },
    },
    {
        "name": "check_host_reachable",
        "description": (
            "Is a host reachable through a connected hp-agent RIGHT NOW? An artifact "
            "targeting a host with no connected agent fails at apply, so check this "
            "BEFORE proposing host-provision or shell-script work for it. Read-only: "
            "it inspects the hub's live registry and touches nothing on the host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Hostname to check"},
            },
            "required": ["host"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "reachable": {"type": "boolean"},
                "reason": {"type": "string"},
                "agent_version": {"type": ["string", "null"]},
            },
            "required": ["host", "reachable"],
        },
    },
]


async def handle_proxmox_api_read(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> list[TextContent]:
    proxmox: ProxmoxClient | None = ctx["proxmox"]
    if proxmox is None:
        raise RuntimeError("Proxmox not configured")
    path = arguments["path"]
    if not _proxmox_path_allowed(path):
        raise ValueError(f"path '{path}' not in read allowlist")
    if any(tok in path for tok in ("execute", "vnc", "term", "upload")):
        raise ValueError(f"path '{path}' contains forbidden token")
    query_str = arguments.get("query")
    query_obj = None
    if query_str:
        try:
            query_obj = json.loads(query_str)
        except json.JSONDecodeError:
            pairs = query_str.split("&")
            query_obj = {p.split("=")[0]: p.split("=", 1)[1] for p in pairs if "=" in p}

    result = await proxmox.read(path, query=query_obj)
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def handle_http_call_read(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> list[TextContent]:
    service_name = arguments["name"]
    path = arguments["path"]
    query_str = arguments.get("query")

    vault: VaultManager | None = ctx.get("vault")
    if vault is None:
        raise RuntimeError("Vault not unlocked — cannot resolve service credentials")

    creds = await vault.get_secret(service_name)

    base_url = creds.get("url", "")
    if not base_url:
        raise RuntimeError(f"service '{service_name}' has no URL in vault")
    if not base_url.startswith(("https://", "http://")):
        raise ValueError(f"invalid base_url for '{service_name}'")

    full_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    from homepilot.config import get_settings

    settings = get_settings()
    allowed_domains = [
        d.strip() for d in settings.allowed_http_domains.split(",") if d.strip()
    ] or None
    try:
        _, resolved_ips = await validate_url(full_url, allowed_domains=allowed_domains)
    except SSRFError as exc:
        raise ValueError(f"SSRF protection: {exc}") from exc

    headers: dict[str, str] = {}
    token = creds.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    params = None
    if query_str:
        try:
            params = json.loads(query_str)
        except json.JSONDecodeError:
            params = dict(p.split("=", 1) for p in query_str.split("&") if "=" in p)

    parsed_url = httpx.URL(full_url)
    transport = _PinnedTransport(resolved_ips=resolved_ips, port=parsed_url.port)
    async with httpx.AsyncClient(timeout=30.0, verify=True, transport=transport) as client:
        resp = await client.get(full_url, headers=headers, params=params)
        resp.raise_for_status()
        try:
            data = resp.json()
            return [TextContent(type="text", text=json.dumps(data, indent=2))]
        except ValueError:
            return [TextContent(type="text", text=resp.text)]


async def handle_read_file_on_guest(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> list[TextContent]:
    agent_adapter: AgentAdapter | None = ctx.get("agent_adapter")
    if agent_adapter is None:
        raise RuntimeError("no agent hub — host operations unavailable")
    host = arguments["host"]
    path = arguments["path"]
    _check_guest_read_path(path)
    content = await agent_adapter.read_file(host, path)
    return [TextContent(type="text", text=content)]


async def handle_exec_on_guest_readonly(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    agent_adapter: AgentAdapter | None = ctx.get("agent_adapter")
    if agent_adapter is None:
        raise RuntimeError("no agent hub — host operations unavailable")
    host = arguments["host"]
    command = arguments["command"]
    exit_code, stdout, stderr = await agent_adapter.exec_readonly(host, command)
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


async def handle_check_host_reachable(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Can a change actually REACH this host? (#427)

    An artifact targeting a host with no connected agent fails at apply, and the
    AI had no way to find that out before proposing - so it proposed work that
    could not run and learned about it from a failure.

    Answers from the hub's live registry rather than by touching the host: the
    question is whether the control plane can reach it, and a check that itself
    needs the host to be up cannot answer that.
    """
    host = arguments["host"]
    registry = ctx.get("agent_registry")
    if registry is None:
        from homepilot.app_state import get_agent_registry

        registry = get_agent_registry()
    if registry is None:
        return {
            "host": host,
            "reachable": False,
            "reason": "the agent hub is not running in this process, so reachability is unknown",
            "agent_version": None,
        }

    agent = registry.get_by_hostname(host)
    if agent is None:
        return {
            "host": host,
            "reachable": False,
            "reason": "no agent is connected for this host right now",
            "agent_version": None,
        }
    version = (agent.system_info or {}).get("agent_version")
    return {
        "host": host,
        "reachable": True,
        "reason": "an agent is connected",
        "agent_version": str(version) if version else None,
    }
