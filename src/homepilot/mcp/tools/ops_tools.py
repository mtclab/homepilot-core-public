"""Operations read tools (MCP read parity, wave 1): tasks, overview, audit, admin.

Each handler calls the SAME repo/service the matching management route calls -
`TaskRepository`, `dashboard.service.build_summary`,
`Repository.query_audit_log`, `selfcheck_report`, `proxmox_settings_report` - so
the console and the assistant cannot answer the same question differently.

`get_proxmox_settings` reports how this instance is WIRED to Proxmox and never
returns a token value: the report says whether a token is configured and where
it came from, and the secret itself stays in the vault.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_tasks",
        "description": (
            "The task history: every apply, replay, revoke and provision this install "
            "has run, newest first, with its status, action, artifact_id and "
            "timestamps. Optionally filtered to one artifact. `total` is the true "
            "count for the filter, not the page size. Use get_task_result for one "
            "task's error and execution log. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": ["string", "null"],
                    "description": "Only this artifact's tasks, or null for all",
                },
                "limit": {"type": "integer", "description": "Page size (1-1000, default 50)"},
                "offset": {"type": "integer", "description": "Rows to skip (default 0)"},
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    {
        # Mutator (MCP<->API parity, wave 2).
        "name": "cancel_task",
        "description": (
            "Cancel an in-flight task by its id and mark the record 'cancelled' so it "
            "stops blocking future actions. A provision cancel reaches the "
            "ProvisionService that owns the clone (stopping and cleaning it up); an "
            "apply/replay/revoke cancel reaches the task runner. Cancelling an already "
            "finished task is a no-op that returns its current status. An unknown id is "
            "an error. Returns the resulting task row."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task's id"},
            },
            "required": ["task_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
        },
    },
    {
        "name": "get_dashboard_summary",
        "description": (
            "The operator overview, computed from the estate itself: inventory counts "
            "and coverage, drift (in-spec, drifted, and how many were never checked), "
            "artifacts and tasks by status, agents known vs connected, firing alert "
            "count and metrics retention, plus the first-run onboarding checklist and "
            "whether it is complete. Current-state aggregates only - use "
            "get_host_metrics_series for history. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "onboarding": {"type": "object"},
                "inventory": {"type": "object"},
                "drift": {"type": "object"},
                "artifacts": {"type": "object"},
                "tasks": {"type": "object"},
                "agents": {"type": "object"},
                "metrics": {"type": "object"},
            },
            "required": ["inventory", "drift", "artifacts", "tasks", "agents"],
        },
    },
    {
        "name": "get_audit_log",
        "description": (
            "The control plane's audit trail: what was done, to which artifact and "
            "host, by which actor, from which source, newest first. Filterable by "
            "action, artifact_id, target_host, source, or free text over all of them. "
            "`total` counts every matching entry, not the page. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": ["string", "null"]},
                "artifact_id": {"type": ["string", "null"]},
                "target_host": {"type": ["string", "null"]},
                "source": {"type": ["string", "null"]},
                "q": {
                    "type": ["string", "null"],
                    "description": "Free text over artifact, host, command, actor",
                },
                "limit": {"type": "integer", "description": "Page size (1-10000, default 100)"},
                "offset": {"type": "integer", "description": "Rows to skip (default 0)"},
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    {
        "name": "get_selfcheck",
        "description": (
            "What each optional subsystem is doing and what that costs: Proxmox, the "
            "agent hub, the vault, embeddings, the events webhook, the MCP transport "
            "and the artifacts remote. Each entry says whether it is off by choice, "
            "reachable, unreachable, or unverified, and states the CONSEQUENCE in "
            "plain words. Targets are redacted to scheme/host/port; no secret is "
            "included. Computed live, so it describes now rather than boot."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "subsystems": {"type": "array", "items": {"type": "object"}},
            },
        },
    },
    {
        "name": "get_proxmox_settings",
        "description": (
            "How this instance is wired to Proxmox: host, port, verify_ssl, whether a "
            "read token and a write token are configured and where each came from "
            "(vault, env, or reused read token), and whether the API answers right "
            "now. NO TOKEN VALUE IS EVER RETURNED - only whether one exists. Read-only: "
            "it cannot change the settings or store a token."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer"},
                "verify_ssl": {"type": "boolean"},
                "token_configured": {"type": "boolean"},
                "token_source": {"type": "string"},
                "write_token_configured": {"type": "boolean"},
                "write_token_source": {"type": "string"},
                "write_token_is_separate": {"type": "boolean"},
                "connection_status": {"type": "string"},
            },
            "required": ["host", "token_configured", "connection_status"],
        },
    },
    # ── Admin tier (MCP<->API parity, wave 3). ──────────────────────────────────
    {
        "name": "test_proxmox_connection",
        "description": (
            "Probe the CONFIGURED Proxmox connection (stored host/port and vault/env "
            "token) and report whether the API answers, with its version on success. "
            "Takes no token: it only tests the credentials already stored, so no secret "
            "travels over MCP. Stores nothing and changes no live client. Admin only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["status", "message"],
        },
    },
    {
        "name": "delete_auth_token",
        "description": (
            "Revoke an API token by its prefix (as shown in the token registry). "
            "Returns whether a token was removed; an unknown prefix is an error. Admin "
            "only - this is a credential-management action."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "The token's prefix"},
            },
            "required": ["prefix"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string"},
                "deleted": {"type": "boolean"},
            },
            "required": ["prefix", "deleted"],
        },
    },
]


def _state(ctx: dict[str, Any]) -> Any:
    """The live-object bundle the state-shaped reports read.

    Prefers the real AppState the process was built from. When the context does
    not carry one - the stdio bootstrap builds its objects itself - a namespace
    over the same context keys stands in, so both transports answer identically
    instead of one of them raising.
    """
    state = ctx.get("app_state")
    if state is not None:
        return state
    return SimpleNamespace(
        settings=ctx.get("settings"),
        repo=ctx.get("repo"),
        database=ctx.get("database"),
        vault=ctx.get("vault"),
        proxmox=ctx.get("proxmox"),
        kb_service=ctx.get("kb_service"),
        agent_hub=getattr(ctx.get("agent_registry"), "hub_server", None),
        agent_registry=ctx.get("agent_registry"),
        metrics_repo=ctx.get("metrics_repo"),
        mcp_app=None,
        agent_hub_disabled_reason="",
    )


async def handle_list_tasks(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    task_repo = ctx.get("task_repo")
    if task_repo is None:
        raise RuntimeError("Task repository not configured")
    artifact_id = arguments.get("artifact_id")
    raw_limit = arguments.get("limit")
    limit = 50 if raw_limit is None else max(1, min(1000, int(raw_limit)))
    offset = max(0, int(arguments.get("offset") or 0))
    items = await task_repo.list_tasks(artifact_id, limit=limit, offset=offset)
    total = await task_repo.count_tasks(artifact_id)
    return {"items": items, "total": total}


async def handle_cancel_task(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Cancel a task through the SAME shared callable POST /tasks/{id}/cancel uses,
    so a provision cancel reaches ProvisionService and everything else the runner."""
    from homepilot.tasks.router import perform_task_cancel

    task_repo = ctx.get("task_repo")
    if task_repo is None:
        raise RuntimeError("Task repository not configured")
    task_id = str(arguments["task_id"])
    result = await perform_task_cancel(
        task_id,
        task_repo=task_repo,
        provision_service=ctx.get("provision_service"),
        task_runner=ctx.get("task_runner"),
        guest_template_service=ctx.get("guest_template_service"),
    )
    if result is None:
        raise ValueError(f"Task not found: {task_id}")
    return result


async def handle_get_dashboard_summary(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from homepilot.dashboard.service import build_summary

    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    return await build_summary(repo)


async def handle_get_audit_log(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    raw_limit = arguments.get("limit")
    limit = 100 if raw_limit is None else max(1, min(10000, int(raw_limit)))
    filters = {
        "action": arguments.get("action"),
        "artifact_id": arguments.get("artifact_id"),
        "target_host": arguments.get("target_host"),
        "source": arguments.get("source"),
        "q": arguments.get("q"),
    }
    items = await repo.query_audit_log(
        limit=limit,
        offset=max(0, int(arguments.get("offset") or 0)),
        **filters,
    )
    # The same filters feed the count: a total that ignored the search would
    # report "50 of 4000" for a 50-row search.
    total = await repo.count_audit_log(**filters)
    return {"items": items, "total": total}


async def handle_get_selfcheck(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.config import get_settings
    from homepilot.selfcheck import selfcheck_report

    state = _state(ctx)
    settings = getattr(state, "settings", None) or ctx.get("settings") or get_settings()
    result: dict[str, Any] = await selfcheck_report(state, settings)
    return result


async def handle_get_proxmox_settings(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    from homepilot.admin.router import proxmox_settings_report

    return await proxmox_settings_report(_state(ctx))


# ── Admin-tier handlers (wave 3). ─────────────────────────────────────────────


async def handle_test_proxmox_connection(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Probe the stored Proxmox connection through the SAME helper POST
    /admin/settings/proxmox/test uses. No token is accepted over MCP: the empty
    config makes it test only the credentials already stored."""
    from homepilot.admin.router import ProxmoxConfigIn, probe_proxmox_connection

    return await probe_proxmox_connection(_state(ctx), ProxmoxConfigIn())


async def handle_delete_auth_token(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Revoke an API token by prefix, exactly as DELETE /auth/tokens/{prefix} does:
    look up the row, delete it (which commits), and report the outcome."""
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    prefix = str(arguments["prefix"]).strip()
    row = await repo.get_token_by_prefix(prefix)
    if row is None:
        raise ValueError(f"Token not found: {prefix}")
    await repo.delete_token(row["id"])
    return {"prefix": prefix, "deleted": True}
