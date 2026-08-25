"""Agent-fleet tools: reads (wave 1) + admin-tier fleet changes (wave 3).

Every handler answers from / acts through the SAME registry/repo call the
matching management route uses, so the console and the assistant cannot report
or change different fleets. The read tools (list_agents, get_agent,
get_agent_audit, get_enrolment_window) are read-tier; the fleet-CHANGING tools
below (open/close_enrolment_window, revoke_agent, forget_agent,
migrate_agents_tls, exec_on_host, write_file_on_host) mirror API routes guarded
by require_scope("admin") and are held at the admin MCP tier.

Deliberately absent, and staying absent:

* the hub token (GET /agents/token) and the bootstrap mint - a shared secret
  that enrols machines must not travel through an MCP transcript;
* the installer one-liner and the agent binary (GET /agents/dist*) - the
  one-liner embeds the enrolment token, and binary bytes are not an MCP shape;
* install (POST /agents/install) - provisioning-adjacent, deferred with the
  provisioning surface.
"""

from __future__ import annotations

from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_agents",
        "description": (
            "The whole hp-agent fleet: every agent this install knows, with its live "
            "connection state overlaid on the stored record. Each entry carries "
            "agent_id, hostname, connected (true only if the hub holds a live link "
            "right now), system_info, last_heartbeat, and - when it is not connected - "
            "last_error and last_error_at saying why. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "agents": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "connected": {"type": "integer"},
            },
            "required": ["agents", "total", "connected"],
        },
    },
    {
        "name": "get_agent",
        "description": (
            "One CONNECTED agent by agent_id or by hostname: agent_id, hostname, "
            "system_info, state, connected_at, last_heartbeat. Answers from the hub's "
            "live registry, so an agent that is enrolled but not connected right now "
            "is reported as not found - use list_agents to see stored agents that are "
            "currently offline. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "The agent's id"},
                "hostname": {
                    "type": "string",
                    "description": "The host's name, as an alternative to agent_id",
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "hostname": {"type": "string"},
                "system_info": {"type": "object"},
                "state": {"type": "object"},
                "connected_at": {"type": "string"},
                "last_heartbeat": {"type": "string"},
            },
            "required": ["agent_id", "hostname"],
        },
    },
    {
        "name": "get_agent_audit",
        "description": (
            "The agent hub's audit trail: enrolment attempts, rejections, revocations "
            "and command dispatches, newest first, with the caller recorded for each. "
            "Optionally filtered to one agent_id or one action (e.g. "
            '"register_rejected"). Read-only.'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many entries to return (1-1000, default 100)",
                },
                "agent_id": {"type": ["string", "null"], "description": "Only this agent's trail"},
                "action": {"type": ["string", "null"], "description": "Only this action"},
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "entries": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["entries", "total"],
        },
    },
    {
        "name": "get_enrolment_window",
        "description": (
            "Whether the shared fleet token can enrol a host this install has never "
            "seen right now: open/closed, when it expires, and fleet_empty - an "
            "install with no agents enrols its first host whether or not a window is "
            "open. READ ONLY: this tool cannot open or close the window."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "open": {"type": "boolean"},
                "expires_at": {"type": ["string", "null"]},
                "seconds_remaining": {"type": "integer"},
                "fleet_empty": {"type": "boolean"},
            },
            "required": ["open", "fleet_empty"],
        },
    },
    # ── Admin tier (MCP<->API parity, wave 3). Each mirrors an API route guarded
    # by require_scope("admin): opening the fleet to new hosts, revoking/forgetting
    # an agent, the TLS migration, and the raw host exec/write RPCs. All need an
    # admin MCP token. ──────────────────────────────────────────────────────────
    {
        "name": "open_enrolment_window",
        "description": (
            "Open (or extend) the enrolment window so the shared fleet token can enrol "
            "a host this install has never seen, for `minutes` (1-1440, default 15). "
            "This is the whole exposure a leaked shared token would otherwise carry "
            "permanently, so it is time-boxed and audited. Returns the window state. "
            "Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "description": "How long the window stays open (1-1440, default 15)",
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {"open": {"type": "boolean"}},
            "required": ["open"],
        },
    },
    {
        "name": "close_enrolment_window",
        "description": (
            "Close the enrolment window now, so the shared fleet token can no longer "
            "enrol a new host. Idempotent. Returns the window state. Admin only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {"open": {"type": "boolean"}},
            "required": ["open"],
        },
    },
    {
        "name": "revoke_agent",
        "description": (
            "Revoke an agent's per-agent credential by agent_id and close its live "
            "channel now, so a compromised or decommissioned agent cannot keep or "
            "regain its exec/write link until it is re-enrolled. An agent with no "
            "active credential is an error. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "The agent's id"},
            },
            "required": ["agent_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "revoked": {"type": "boolean"},
                "channel_closed": {"type": "boolean"},
            },
            "required": ["agent_id", "revoked"],
        },
    },
    {
        "name": "forget_agent",
        "description": (
            "Forget a decommissioned agent entirely: revoke its credential first, then "
            "delete its stored record so it stops being counted as known. REFUSES while "
            "the agent is connected - revoke it or stop it first. An unknown agent is an "
            "error. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "The agent's id"},
            },
            "required": ["agent_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "forgotten": {"type": "boolean"},
            },
            "required": ["agent_id", "forgotten"],
        },
    },
    {
        "name": "migrate_agents_tls",
        "description": (
            "Push the hub's certificate to the fleet and flip every agent's transport "
            "to TLS. REFUSES (an error) when it would strand agents that are not "
            "connected, unless force=true is passed to accept that those will need "
            "re-enrolling by hand. Returns the migration result. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Flip even though offline agents get stranded (default false)",
                },
            },
        },
        "outputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "exec_on_host",
        "description": (
            "Run an arbitrary command on a managed host through the connected hp-agent "
            "(agent hub) and return exit_code, stdout and stderr. This is the "
            "UNRESTRICTED host exec - unlike exec_on_guest_readonly it is not limited to "
            "an allowlist - so it is admin-tier and audited. PVE hypervisor nodes are "
            "refused (use the Proxmox API for those)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Managed host's name"},
                "command": {"type": "string", "description": "Command to run"},
                "timeout": {"type": "integer", "description": "Seconds (default 30)"},
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
        "name": "write_file_on_host",
        "description": (
            "Write a file on a managed host through the connected hp-agent (agent hub). "
            "The agent enforces its own write allowlist and symlink safety; identical "
            "content is a no-op. Returns before/after content hashes and whether it "
            "changed. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Managed host's name"},
                "path": {"type": "string", "description": "Absolute file path on the host"},
                "content": {"type": "string", "description": "New file content"},
            },
            "required": ["host", "path", "content"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"changed": {"type": "boolean"}},
            "required": ["changed"],
        },
    },
]


def _registry(ctx: dict[str, Any]) -> Any:
    registry = ctx.get("agent_registry")
    if registry is None:
        from homepilot.app_state import get_agent_registry

        registry = get_agent_registry()
    if registry is None:
        raise RuntimeError("Agent hub not enabled — there is no fleet to read")
    return registry


async def handle_list_agents(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.agent_hub.router import fleet_listing

    agents = await fleet_listing(_registry(ctx), ctx.get("repo"))
    return {
        "agents": agents,
        "total": len(agents),
        "connected": sum(1 for a in agents if a.get("connected")),
    }


async def handle_get_agent(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.agent_hub.router import agent_detail

    registry = _registry(ctx)
    agent_id = str(arguments.get("agent_id") or "").strip()
    hostname = str(arguments.get("hostname") or "").strip()
    if not agent_id and not hostname:
        raise ValueError("pass agent_id or hostname")

    agent = registry.get(agent_id) if agent_id else registry.get_by_hostname(hostname)
    if not agent:
        raise ValueError(f"Agent not connected: {agent_id or hostname}")
    return agent_detail(agent)


async def handle_get_agent_audit(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    registry = _registry(ctx)
    raw_limit = arguments.get("limit")
    limit = 100 if raw_limit is None else max(1, min(1000, int(raw_limit)))
    entries = await registry.audit_log.query_persisted(
        limit=limit,
        agent_id=arguments.get("agent_id"),
        action=arguments.get("action"),
    )
    return {"entries": entries, "total": len(entries)}


async def handle_get_enrolment_window(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from homepilot.agent_hub.enrolment_window import payload

    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Enrolment window store not available")
    result: dict[str, Any] = await payload(repo)
    return result


# ── Admin-tier handlers (wave 3). Each calls the SAME lower-level function the
# matching agent-hub route calls, so the console and the assistant change the
# fleet identically. Every one is refused a non-admin MCP token by _handle_tool. ─


def _require_repo(ctx: dict[str, Any]) -> Any:
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    return repo


def _audit_fleet_change(ctx: dict[str, Any], action: str, detail: str) -> None:
    """Record who changed the fleet, best-effort (a missing hub must not block
    the operation), mirroring the router's _audit_window."""
    import contextlib

    registry = ctx.get("agent_registry")
    audit_log = getattr(registry, "audit_log", None)
    if audit_log is None:
        return
    with contextlib.suppress(Exception):  # audit is best-effort
        audit_log.log(
            agent_id="",
            action=action,
            command_or_path=detail,
            result="success",
            caller=str(ctx.get("_mcp_caller_id") or "mcp"),
        )


async def handle_open_enrolment_window(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from homepilot.agent_hub.enrolment_window import (
        DEFAULT_WINDOW_MINUTES,
        MAX_WINDOW_MINUTES,
        open_window,
        payload,
    )

    repo = _require_repo(ctx)
    raw = arguments.get("minutes")
    minutes = DEFAULT_WINDOW_MINUTES if raw is None else int(raw)
    if minutes < 1 or minutes > MAX_WINDOW_MINUTES:
        raise ValueError(f"minutes must be between 1 and {MAX_WINDOW_MINUTES}")
    result = await open_window(repo, minutes)
    _audit_fleet_change(ctx, "enrolment_window_opened", f"minutes={result['minutes']}")
    out: dict[str, Any] = await payload(repo)
    return out


async def handle_close_enrolment_window(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from homepilot.agent_hub.enrolment_window import close_window, payload

    repo = _require_repo(ctx)
    await close_window(repo)
    _audit_fleet_change(ctx, "enrolment_window_closed", "")
    out: dict[str, Any] = await payload(repo)
    return out


async def handle_revoke_agent(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = _require_repo(ctx)
    agent_id = str(arguments["agent_id"]).strip()
    revoked = await repo.revoke_agent_credential(agent_id)
    if not revoked:
        raise ValueError(f"No active credential to revoke for agent {agent_id}")
    # Evict AFTER the credential is dead, so the agent cannot win a reconnect race
    # against its own revocation (mirrors POST /agents/{id}/revoke).
    registry = ctx.get("agent_registry")
    evicted = False
    if registry is not None:
        evicted = registry.disconnect(
            agent_id, "credential revoked by an operator; the live channel was closed"
        )
    return {"agent_id": agent_id, "revoked": True, "channel_closed": evicted}


async def handle_forget_agent(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    import contextlib

    repo = _require_repo(ctx)
    agent_id = str(arguments["agent_id"]).strip()
    registry = ctx.get("agent_registry")
    live = {a["agent_id"] for a in registry.list_connected()} if registry is not None else set()
    if agent_id in live:
        raise ValueError(
            f"Agent {agent_id} is connected right now. Revoke it or stop the agent "
            "first - removing a live agent would pull its credential out from under "
            "an open connection."
        )
    # Revoke first, then delete: if the delete fails, the credential is already
    # dead rather than the reverse (mirrors DELETE /agents/{agent_id}).
    await repo.revoke_agent_credential(agent_id)
    deleted = await repo.delete_agent(agent_id)
    if not deleted:
        raise ValueError(f"Agent {agent_id} not found")
    if registry is not None:
        with contextlib.suppress(Exception):
            registry.unregister(agent_id)
    return {"agent_id": agent_id, "forgotten": True}


async def handle_migrate_agents_tls(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from homepilot.agent_hub.migrate_tls import MigrationRefusedError, migrate_fleet_to_tls

    repo = _require_repo(ctx)
    registry = ctx.get("agent_registry")
    if registry is None:
        raise RuntimeError("Agent hub not enabled — there is no fleet to migrate")
    settings = ctx.get("settings")
    if settings is None:
        settings = getattr(ctx.get("app_state"), "settings", None)
    if settings is None:
        raise RuntimeError("Control plane not fully initialised")
    force = bool(arguments.get("force", False))
    try:
        result: dict[str, Any] = await migrate_fleet_to_tls(
            repo,
            registry,
            registry.hub_server,
            settings.data_dir,
            settings=settings,
            force=force,
        )
    except MigrationRefusedError as exc:
        # The 409 the route returns: well-formed, but the fleet is not in a state
        # where flipping is safe. The message names every agent involved.
        raise ValueError(str(exc)) from exc
    return result


async def handle_exec_on_host(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    adapter = ctx.get("agent_adapter")
    if adapter is None:
        raise RuntimeError("no agent hub — host operations unavailable")
    host = str(arguments["host"])
    command = str(arguments["command"])
    timeout = int(arguments.get("timeout", 30))
    # adapter.exec keeps the PVE-node guard; the agent enforces its own protections.
    # This is the raw exec the API's POST /agents/host/exec runs - no allowlist.
    exit_code, stdout, stderr = await adapter.exec(host, command, timeout=timeout)
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


async def handle_write_file_on_host(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    adapter = ctx.get("agent_adapter")
    if adapter is None:
        raise RuntimeError("no agent hub — host operations unavailable")
    host = str(arguments["host"])
    path = str(arguments["path"])
    content = str(arguments["content"])
    # adapter.write_file keeps the PVE-node guard; the agent enforces the write
    # allowlist and symlink safety (the real enforcement point), so this does not
    # bypass the path guard - it mirrors POST /agents/host/write-file.
    result: dict[str, Any] = await adapter.write_file(host, path, content)
    return result
