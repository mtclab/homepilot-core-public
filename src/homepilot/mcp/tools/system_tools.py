"""System/infrastructure read-only tools: Proxmox API, HTTP calls, SSH file/exec."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from mcp.types import TextContent

from homepilot.adapters.agent import AgentAdapter
from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.adapters.readonly_policy import read_path_refusal
from homepilot.mcp.tools.host_param import host_arg, host_properties, with_host_warning
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


# Path segments the read-tier passthrough never forwards, wherever they sit.
#
# `agent` is the QEMU guest-agent namespace (/nodes/<n>/qemu/<vmid>/agent/...).
# It is not a read of the hypervisor: `agent/file-read` returns a file from
# INSIDE the guest - /etc/homepilot/agent.env, which holds the fleet enrolment
# token and which read_file_on_guest refuses by name - and `agent/exec-status`
# returns the output of a command someone else ran. It sits under the allowed
# /nodes/ prefix, so before #716 this tool relayed it unchanged. The product's
# own guest-agent reads (inventory's network-get-interfaces) go through the
# ProxmoxClient directly and are unaffected.
_PROXMOX_DENIED_SEGMENTS = frozenset({"agent"})

# How many rounds of percent-decoding a path may need before it is stable. A
# path still changing after this many rounds is refused rather than guessed at.
_MAX_PATH_DECODE_ROUNDS = 4


def _proxmox_path_refusal(path: str) -> str | None:
    """Why ``path`` may not be sent to the Proxmox API, or ``None`` if it may.

    The checks run on the DECODED path split into segments, not on the raw
    string: a percent-encoded, doubled or dotted segment (``%61gent``,
    ``agent%2Ffile-read``, ``//agent``, ``status/../agent``) must not reach a
    namespace the literal spelling is refused for.
    """
    decoded = path
    for _ in range(_MAX_PATH_DECODE_ROUNDS):
        step = unquote(decoded)
        if step == decoded:
            break
        decoded = step
    else:
        return f"path '{path}' is encoded too many times to check"
    if "?" in decoded or "#" in decoded:
        return f"path '{path}' carries a query or fragment; pass the query separately"
    segments = [seg for seg in decoded.split("/") if seg]
    if any(seg in (".", "..") for seg in segments):
        return f"path '{path}' contains a relative segment"
    if any(seg.lower() in _PROXMOX_DENIED_SEGMENTS for seg in segments):
        return (
            f"path '{path}' reaches the guest agent API, which reads inside the "
            "guest rather than the hypervisor"
        )
    joined = "/".join(segments)
    for prefix in _proxmox_read_prefixes:
        pre = prefix.lstrip("/").rstrip("/")
        if joined == pre or joined.startswith(pre + "/"):
            return None
    return f"path '{path}' not in read allowlist"


def _proxmox_path_allowed(path: str) -> bool:
    return _proxmox_path_refusal(path) is None


def _check_guest_read_path(path: str) -> None:
    """Raise ``ValueError`` if the READ tier may not read ``path`` off a guest.

    Default-deny under the policy shared with the host agent
    (adapters/readonly_policy.json, #691): logs, unit files, a few identity
    files and what HomePilot itself wrote are readable; private keys, shadow
    files, PVE and HomePilot credentials, /root, process environments and
    dotfile credential stores are denied even under an allowed prefix. The agent
    re-checks the symlink-resolved path and is the enforcement point; this is
    the control plane declining to ask. ``cat`` through exec_on_guest_readonly
    passes through the same check.
    """
    refusal = read_path_refusal(path)
    if refusal is not None:
        raise ValueError(refusal)


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "proxmox_api_read",
        "description": (
            "GET-only Proxmox REST API call. Used to read configs, list resources, "
            "check status. Vault-resolved token. Cannot mutate state. The guest "
            "agent namespace (/nodes/<node>/qemu/<vmid>/agent/...) is refused: it "
            "reads inside the guest, not the hypervisor."
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
        "description": (
            "Read a file from a guest host via the connected hp-agent (agent hub). "
            "Default-deny: logs (/var/log), systemd unit files, a few identity files "
            "(os-release, hostname, hosts, resolv.conf, /proc/meminfo...) and what "
            "HomePilot wrote (/etc/homepilot, /opt/homepilot, ...). Keys, shadow "
            "files, PVE/HomePilot credentials and /root are always refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **host_properties("Guest hostname"),
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
            "Positive-only allowlist shared with the agent: hostname, uptime, uname, "
            "free, df, ls, ps (no 'e' environment flag), ip <object> [show|list|get], "
            "ss (no -K/-D/-F/-N), systemctl status|is-active|is-enabled|is-failed, "
            "journalctl with read flags only (no -f, no flush/rotate/vacuum), "
            "dpkg -l|-s|-S|-L, cat of a path read_file_on_guest would serve. "
            "For file reading, use read_file_on_guest instead (logs access)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **host_properties("Guest hostname"),
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
                **host_properties("Hostname to check"),
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
    refusal = _proxmox_path_refusal(path)
    if refusal is not None:
        raise ValueError(refusal)
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
    # The caller supplies only the path; the host is the one the operator
    # registered in the vault. A path that would move the request to another
    # host or port is refused before anything is resolved.
    registered = urlparse(base_url)
    target = urlparse(full_url)
    if (target.hostname, target.port) != (registered.hostname, registered.port):
        raise ValueError(f"path for '{service_name}' must stay on the registered host")
    from homepilot.config import get_settings

    settings = get_settings()
    allowed_domains = [
        d.strip() for d in settings.allowed_http_domains.split(",") if d.strip()
    ] or None
    try:
        # #688: the registered host may live on the LAN - that is what the
        # tool advertises (adopted services) - so its LAN addresses are
        # accepted; any other host, and loopback/metadata for every host,
        # stay refused.
        _, resolved_ips = await validate_url(
            full_url,
            allowed_domains=allowed_domains,
            private_host=registered.hostname,
        )
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
    # No `warning` channel here: this handler answers with the FILE'S CONTENT,
    # and appending a deprecation line to a file the caller asked to read would
    # corrupt the answer. The schema carries the deprecation for this one.
    host, _warning = host_arg(arguments)
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
    host, warning = host_arg(arguments)
    command = arguments["command"]
    exit_code, stdout, stderr = await agent_adapter.exec_readonly(host, command)
    return with_host_warning({"exit_code": exit_code, "stdout": stdout, "stderr": stderr}, warning)


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
    host, warning = host_arg(arguments)
    registry = ctx.get("agent_registry")
    if registry is None:
        from homepilot.app_state import get_agent_registry

        registry = get_agent_registry()
    if registry is None:
        return with_host_warning(
            {
                "host": host,
                "reachable": False,
                "reason": (
                    "the agent hub is not running in this process, so reachability is unknown"
                ),
                "agent_version": None,
            },
            warning,
        )

    agent = registry.get_by_hostname(host)
    if agent is None:
        return with_host_warning(
            {
                "host": host,
                "reachable": False,
                "reason": "no agent is connected for this host right now",
                "agent_version": None,
            },
            warning,
        )
    version = (agent.system_info or {}).get("agent_version")
    return with_host_warning(
        {
            "host": host,
            "reachable": True,
            "reason": "an agent is connected",
            "agent_version": str(version) if version else None,
        },
        warning,
    )
