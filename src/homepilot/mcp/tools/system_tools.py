"""System/infrastructure read-only tools: Proxmox API, HTTP calls, SSH file/exec."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from mcp.types import TextContent

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.adapters.ssh import SSHAdapter
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
        "description": "Read a file from a guest host via SFTP through the jump server.",
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
            "Execute a whitelisted read-only command on a guest via SSH. "
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
    ssh_adapter: SSHAdapter | None = ctx.get("ssh_adapter")
    if ssh_adapter is None:
        raise RuntimeError("SSH not configured — jump server unavailable")
    host = arguments["host"]
    path = arguments["path"]
    content = await ssh_adapter.read_file(host, path)
    return [TextContent(type="text", text=content)]


async def handle_exec_on_guest_readonly(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    ssh_adapter: SSHAdapter | None = ctx.get("ssh_adapter")
    if ssh_adapter is None:
        raise RuntimeError("SSH not configured — jump server unavailable")
    host = arguments["host"]
    command = arguments["command"]
    exit_code, stdout, stderr = await ssh_adapter.exec_readonly(host, command)
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}
