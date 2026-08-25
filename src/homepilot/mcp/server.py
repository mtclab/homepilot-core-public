from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from contextvars import ContextVar
from typing import Any

from mcp.server import Server
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from homepilot.app_state import create_app_state
from homepilot.config import get_settings

from ..portal.repository import InviteRepository
from .tools.agent_tools import (
    TOOL_DEFINITIONS as AGENT_TOOL_DEFS,
)
from .tools.agent_tools import (
    handle_close_enrolment_window,
    handle_exec_on_host,
    handle_forget_agent,
    handle_get_agent,
    handle_get_agent_audit,
    handle_get_enrolment_window,
    handle_list_agents,
    handle_migrate_agents_tls,
    handle_open_enrolment_window,
    handle_revoke_agent,
    handle_write_file_on_host,
)
from .tools.artifact_tools import (
    TOOL_DEFINITIONS as ARTIFACT_TOOL_DEFS,
)
from .tools.artifact_tools import (
    handle_apply_artifact,
    handle_approve_artifact,
    handle_check_artifact_drift,
    handle_get_artifact,
    handle_get_artifact_status,
    handle_get_fleet_drift,
    handle_get_task_result,
    handle_plan_artifact,
    handle_preview_artifact,
    handle_propose_artifact,
    handle_query_artifacts,
    handle_reject_artifact,
    handle_replay_artifact,
    handle_revoke_artifact,
)
from .tools.guest_tools import (
    TOOL_DEFINITIONS as GUEST_TOOL_DEFS,
)
from .tools.guest_tools import (
    handle_provision_guest,
    handle_query_guests,
    handle_revoke_guest_invite,
    handle_set_guest_quota,
)
from .tools.inventory_tools import (
    TOOL_DEFINITIONS as INVENTORY_TOOL_DEFS,
)
from .tools.inventory_tools import (
    handle_add_host,
    handle_adopt_host,
    handle_bulk_host_action,
    handle_delete_host,
    handle_enrich_inventory,
    handle_get_environment_doc,
    handle_get_host,
    handle_ignore_host,
    handle_query_inventory,
    handle_refresh_inventory,
    handle_update_host,
)
from .tools.kb_tools import (
    TOOL_DEFINITIONS as KB_TOOL_DEFS,
)
from .tools.kb_tools import (
    handle_delete_kb_doc,
    handle_get_kb_doc,
    handle_get_kb_embedding_status,
    handle_ingest_kb,
    handle_list_kb,
    handle_record_fact,
    handle_reindex_kb,
    handle_search_kb,
    handle_update_kb_doc,
)
from .tools.monitoring_tools import (
    TOOL_DEFINITIONS as MONITORING_TOOL_DEFS,
)
from .tools.monitoring_tools import (
    handle_create_alert_rule,
    handle_delete_alert_rule,
    handle_get_host_metrics,
    handle_get_host_metrics_series,
    handle_get_monitoring_alerts,
    handle_list_alert_rules,
    handle_update_alert_rule,
)
from .tools.ops_tools import (
    TOOL_DEFINITIONS as OPS_TOOL_DEFS,
)
from .tools.ops_tools import (
    handle_cancel_task,
    handle_delete_auth_token,
    handle_get_audit_log,
    handle_get_dashboard_summary,
    handle_get_proxmox_settings,
    handle_get_selfcheck,
    handle_list_tasks,
    handle_test_proxmox_connection,
)
from .tools.system_tools import (
    TOOL_DEFINITIONS as SYSTEM_TOOL_DEFS,
)
from .tools.system_tools import (
    handle_check_host_reachable,
    handle_exec_on_guest_readonly,
    handle_http_call_read,
    handle_proxmox_api_read,
    handle_read_file_on_guest,
)

_mcp_token_scope_var: ContextVar[str] = ContextVar("_mcp_token_scope_var", default="full")
_mcp_caller_id_var: ContextVar[str] = ContextVar("_mcp_caller_id_var", default="mcp-stdio")

logger = logging.getLogger(__name__)

_Handler = Callable[
    [dict[str, Any], dict[str, Any]],
    Coroutine[Any, Any, list[TextContent] | dict[str, Any]],
]

_TOOL_DEFINITIONS: list[dict[str, Any]] = (
    INVENTORY_TOOL_DEFS
    + SYSTEM_TOOL_DEFS
    + KB_TOOL_DEFS
    + ARTIFACT_TOOL_DEFS
    + GUEST_TOOL_DEFS
    + AGENT_TOOL_DEFS
    + MONITORING_TOOL_DEFS
    + OPS_TOOL_DEFS
)

_MUTATING_TOOLS = frozenset(
    {
        "propose_artifact",
        "approve_artifact",
        "record_fact",
        # Standard mutators (MCP<->API parity, wave 2), at `full` scope. A
        # read_only token is denied all of these by _handle_tool.
        "cancel_task",
        "add_host",
        "adopt_host",
        "ignore_host",
        "update_host",
        "delete_host",
        "enrich_inventory",
        "bulk_host_action",
        # KB: PUT /kb/{doc_id} is API `write`, so update_kb_doc is `full`. The
        # DELETE/ingest/reindex routes are API `admin` -> _ADMIN_TOOLS (wave 3).
        "update_kb_doc",
        # reject only marks a proposal rejected (no host impact), so unlike
        # approve it does not collapse the review model - allowed at full scope.
        "reject_artifact",
        # apply/replay execute an ALREADY-APPROVED artifact. Human approval is
        # the gate and cannot be given over MCP, so these are sanctioned
        # execution, not an unreviewed change.
        "apply_artifact",
        "replay_artifact",
        # revoke rolls an APPLIED artifact back through the task runner. API
        # DELETE /artifacts/{id} is require_scope('write'), so this is `full`, not
        # admin - it needs an approved/applied artifact (the runner enforces the
        # transition) and cannot grant approval, exactly like apply/replay.
        "revoke_artifact",
    }
)

# Every tool an MCP token needs the `admin` scope to call. These mirror API
# routes guarded by require_scope("admin"): a `full` MCP token is refused them by
# _handle_tool, an `admin` token passes. The tier<->scope invariant
# (tests/test_mcp_read_parity.py::TestMcpTierMatchesApiScope) holds each of these
# at exactly its route's admin scope.
_ADMIN_TOOLS = frozenset(
    {
        # Guest management (#442). GET /admin/guests and the guest quota/invite
        # writes are all API admin, so the whole set sits at the admin tier (wave
        # 3 - they were escalation debt at read_only/full before it existed).
        "query_guests",
        "set_guest_quota",
        "revoke_guest_invite",
        # Guest provisioning (owner decision 2026-08-25): POST /guests/provision is
        # API admin; it clones a Proxmox template into a running guest.
        "provision_guest",
        # KB admin: delete a doc, ingest sources, reindex, and the embedding
        # status read (an API-admin GET, so admin-tier even though it is a read).
        "delete_kb_doc",
        "ingest_kb",
        "reindex_kb",
        "get_kb_embedding_status",
        # Monitoring: alert-rule create/update/delete are all API admin.
        "create_alert_rule",
        "update_alert_rule",
        "delete_alert_rule",
        # Agent fleet, security-relaxing / host-mutating admin ops.
        "open_enrolment_window",
        "close_enrolment_window",
        "revoke_agent",
        "forget_agent",
        "migrate_agents_tls",
        "exec_on_host",
        "write_file_on_host",
        # Admin settings / credentials.
        "test_proxmox_connection",
        "delete_auth_token",
    }
)

# Tools no MCP token may ever reach, whatever its scope. Enforced in two places
# below — delisted from list_tools() and hard-refused in call_tool().
#
# approve_artifact USED to live here (#385): a single shared MCP token approving
# its own proposal collapses the propose->human-approve model. The human-relay
# approval mechanism replaced that blanket ban — approve_artifact is now exposed
# but gated by a per-artifact approval code the assistant cannot see (generated
# at propose, returned by NO MCP read, relayed by a human), so a valid code is
# proof a human decided and the assistant still cannot self-approve. The set is
# kept (empty) as the backstop for any future truly-forbidden tool.
_MCP_FORBIDDEN_TOOLS: frozenset[str] = frozenset()

# Every tool an MCP token with `read_only` scope may call. Nothing here writes,
# so nothing here appears in _MUTATING_TOOLS. The read surface is held at parity
# with the management API's GET routes by tests/test_mcp_read_parity.py: a new
# GET route must gain a tool here or an explicit, reasoned exclusion.
_READ_ONLY_TOOLS = frozenset(
    {
        "query_inventory",
        "refresh_inventory",
        "get_environment_doc",
        "query_artifacts",
        "get_artifact_status",
        "search_kb",
        "proxmox_api_read",
        "http_call_read",
        "read_file_on_guest",
        "exec_on_guest_readonly",
        "check_artifact_drift",
        "get_artifact",
        "get_task_result",
        "check_host_reachable",
        # query_guests moved to _ADMIN_TOOLS in wave 3 (GET /admin/guests is API
        # admin); it is no longer a read_only tool.
        # Read parity with the management API, wave 1.
        "list_agents",
        "get_agent",
        "get_agent_audit",
        "get_enrolment_window",
        "list_alert_rules",
        "get_monitoring_alerts",
        "get_host_metrics",
        "get_host_metrics_series",
        "get_host",
        "list_tasks",
        "get_dashboard_summary",
        "get_audit_log",
        "list_kb",
        "get_kb_doc",
        # get_kb_embedding_status is NOT here: GET /kb/embedding-status is API
        # `admin`, so wave 3 places it in _ADMIN_TOOLS (an admin-tier read).
        "get_fleet_drift",
        "get_selfcheck",
        "get_proxmox_settings",
        # Wave 2: plan/preview are POST routes but API `read`-scoped and change no
        # host, so read_only is their exact tier (they compute a plan/diff only).
        "plan_artifact",
        "preview_artifact",
    }
)


async def _bootstrap() -> dict[str, Any]:
    settings = get_settings()
    state = await create_app_state(settings)

    lifecycle = state.artifact_lifecycle
    proxmox = state.proxmox
    vault = state.vault

    # Host ops route through the agent hub (the SSH/jump transport was removed).
    from homepilot.adapters.agent import AgentAdapter

    agent_adapter = (
        AgentAdapter(hub_server=state.agent_hub) if state.agent_hub is not None else None
    )

    lifecycle._proxmox = proxmox
    lifecycle._ssh = agent_adapter
    lifecycle._vault_mgr = vault
    if proxmox:
        try:
            pve_nodes_data = await proxmox.read("/nodes")
            pve_node_list = [
                n.get("node") or n.get("name", "")
                for n in (
                    pve_nodes_data.get("data", pve_nodes_data)
                    if isinstance(pve_nodes_data, dict)
                    else pve_nodes_data
                )
            ]
            lifecycle._pve_nodes_list = pve_node_list
        except Exception:  # bootstrap must not crash, falls back to empty node list
            lifecycle._pve_nodes_list = []

    from homepilot.executor.orchestrator import ArtifactExecutor

    if proxmox and vault and agent_adapter is not None:
        executor = ArtifactExecutor(
            store=state.artifact_store,
            lifecycle=lifecycle,
            repo=state.repo,
            proxmox=proxmox,
            vault=vault,
            pve_nodes=lifecycle._pve_nodes_list or [],
            agent=agent_adapter,
        )
        lifecycle._executor_ref = executor

    from homepilot.app_state import get_agent_registry
    from homepilot.inventory.service import InventoryService
    from homepilot.reconciler import DriftReconciler
    from homepilot.tasks.repository import TaskRepository

    inventory_service = InventoryService(
        state.repo,
        proxmox=proxmox,
        kb_service=state.kb_service,
        proxmox_host=settings.proxmox_host,
    )

    executor_ref = getattr(lifecycle, "_executor_ref", None)
    drift_reconciler = DriftReconciler(
        store=state.artifact_store,
        repo=state.repo,
        executor=executor_ref,
    )

    # One TaskRepository, shared by the read tools, the task runner and the
    # provision service, so a cancel/apply started over MCP sees the same rows
    # the read tools report.
    task_repo = TaskRepository(state.database)

    # Wave 2 mutators (apply/replay/cancel) need the SAME machinery the HTTP app
    # builds. The runner and provision service are constructed exactly as main.py
    # does (apply_reconciler is built only when an executor exists), so an apply
    # over MCP runs through the one engine, not a weaker second path.
    from homepilot.provision.service import ProvisionService
    from homepilot.reconciler.apply import ApplyReconciler
    from homepilot.tasks.runner import TaskRunner

    apply_reconciler = (
        ApplyReconciler(store=state.artifact_store, repo=state.repo, executor=executor_ref)
        if executor_ref is not None
        else None
    )
    task_runner = TaskRunner(
        repo=task_repo,
        lifecycle=lifecycle,
        executor=executor_ref,
        apply_reconciler=apply_reconciler,
        store=state.artifact_store,
    )
    provision_service = ProvisionService(
        proxmox=proxmox,
        task_repo=task_repo,
        repo=state.repo,
    )

    return {
        "settings": settings,
        "repo": state.repo,
        "database": state.database,
        "artifacts_dir": state.artifacts_dir,
        "lifecycle": lifecycle,
        "store": state.artifact_store,
        "vault": vault,
        "proxmox": proxmox,
        "agent_adapter": agent_adapter,
        "kb_service": state.kb_service,
        # The environment doc's real implementation lives on the service; the MCP
        # handler used to re-render a lesser version of it without the KB (#427).
        # Built HERE rather than read off `state`: the MCP process has no FastAPI
        # app, which is where the API's instance lives.
        "inventory_service": inventory_service,
        "drift_reconciler": drift_reconciler,
        # Guest management (#442): the assistant can see and budget guests.
        "invite_repo": InviteRepository(state.database) if state.database else None,
        # Task outcomes live here. Without it an agent can start an apply and
        # never learn whether it worked (#427).
        "task_repo": task_repo,
        # Wave 2: the runner and provision service the apply/replay/cancel
        # mutators dispatch to. Both contexts (this one and main.py's HTTP one)
        # must carry them or those tools work over one transport and fail on the
        # other (the same trap TestBothTransportsCarryTheSameToolContext guards).
        "task_runner": task_runner,
        "provision_service": provision_service,
        # The hub's live registry, for the reachability check. None when the hub
        # is not running in this process, which the handler reports as "unknown"
        # rather than as "reachable".
        "agent_registry": get_agent_registry(),
        # Read parity with the management API, wave 1: the metric store behind
        # /monitoring, and the state bundle the selfcheck and Proxmox-settings
        # reports read. Both contexts (this one and main.py's HTTP one) must
        # carry them or the same tool answers differently per transport.
        "metrics_repo": state.metrics_repo,
        "app_state": state,
    }


def _mcp_caller_id() -> str:
    token = get_access_token()
    if token is not None:
        return token.client_id or "mcp-http"
    return _mcp_caller_id_var.get()


_TOOL_HANDLERS: dict[str, _Handler] = {
    "query_inventory": handle_query_inventory,
    "refresh_inventory": handle_refresh_inventory,
    "get_environment_doc": handle_get_environment_doc,
    "query_artifacts": handle_query_artifacts,
    "get_artifact": handle_get_artifact,
    "get_task_result": handle_get_task_result,
    "check_host_reachable": handle_check_host_reachable,
    "search_kb": handle_search_kb,
    "proxmox_api_read": handle_proxmox_api_read,
    "http_call_read": handle_http_call_read,
    "read_file_on_guest": handle_read_file_on_guest,
    "exec_on_guest_readonly": handle_exec_on_guest_readonly,
    "record_fact": handle_record_fact,
    "propose_artifact": handle_propose_artifact,
    "check_artifact_drift": handle_check_artifact_drift,
    "approve_artifact": handle_approve_artifact,
    "get_artifact_status": handle_get_artifact_status,
    "query_guests": handle_query_guests,
    "set_guest_quota": handle_set_guest_quota,
    "revoke_guest_invite": handle_revoke_guest_invite,
    "provision_guest": handle_provision_guest,
    # Read parity with the management API, wave 1. Every one is read-only and
    # calls the same repo/service its management route calls.
    "list_agents": handle_list_agents,
    "get_agent": handle_get_agent,
    "get_agent_audit": handle_get_agent_audit,
    "get_enrolment_window": handle_get_enrolment_window,
    "list_alert_rules": handle_list_alert_rules,
    "get_monitoring_alerts": handle_get_monitoring_alerts,
    "get_host_metrics": handle_get_host_metrics,
    "get_host_metrics_series": handle_get_host_metrics_series,
    "get_host": handle_get_host,
    "list_tasks": handle_list_tasks,
    "get_dashboard_summary": handle_get_dashboard_summary,
    "get_audit_log": handle_get_audit_log,
    "list_kb": handle_list_kb,
    "get_kb_doc": handle_get_kb_doc,
    "get_fleet_drift": handle_get_fleet_drift,
    "get_selfcheck": handle_get_selfcheck,
    "get_proxmox_settings": handle_get_proxmox_settings,
    # Standard mutators (MCP<->API parity, wave 2). Each calls the same
    # repo/service/runner its management route calls, and each is in
    # _MUTATING_TOOLS so a read_only token is denied.
    "cancel_task": handle_cancel_task,
    "add_host": handle_add_host,
    "adopt_host": handle_adopt_host,
    "ignore_host": handle_ignore_host,
    "update_host": handle_update_host,
    "delete_host": handle_delete_host,
    "enrich_inventory": handle_enrich_inventory,
    "bulk_host_action": handle_bulk_host_action,
    "update_kb_doc": handle_update_kb_doc,
    "plan_artifact": handle_plan_artifact,
    "preview_artifact": handle_preview_artifact,
    "reject_artifact": handle_reject_artifact,
    "apply_artifact": handle_apply_artifact,
    "replay_artifact": handle_replay_artifact,
    "revoke_artifact": handle_revoke_artifact,
    # Admin tier (MCP<->API parity, wave 3). Each mirrors an API route guarded by
    # require_scope("admin") and calls the SAME repo/service/runner that route
    # calls; _handle_tool refuses every one of them a non-admin MCP token.
    "delete_kb_doc": handle_delete_kb_doc,
    "ingest_kb": handle_ingest_kb,
    "reindex_kb": handle_reindex_kb,
    "get_kb_embedding_status": handle_get_kb_embedding_status,
    "create_alert_rule": handle_create_alert_rule,
    "update_alert_rule": handle_update_alert_rule,
    "delete_alert_rule": handle_delete_alert_rule,
    "open_enrolment_window": handle_open_enrolment_window,
    "close_enrolment_window": handle_close_enrolment_window,
    "revoke_agent": handle_revoke_agent,
    "forget_agent": handle_forget_agent,
    "migrate_agents_tls": handle_migrate_agents_tls,
    "exec_on_host": handle_exec_on_host,
    "write_file_on_host": handle_write_file_on_host,
    "test_proxmox_connection": handle_test_proxmox_connection,
    "delete_auth_token": handle_delete_auth_token,
}


async def _handle_tool(
    name: str, arguments: dict[str, Any], ctx: dict[str, Any]
) -> list[TextContent] | dict[str, Any]:
    mcp_scope: str | None = ctx.get("_mcp_token_scope") or _mcp_token_scope_var.get()
    # The ladder is read_only < full < admin. An admin tool needs an admin token;
    # a mutating (full) tool needs full OR admin; a read tool needs nothing more
    # than read_only. Admin is checked first so a `full` token is refused an admin
    # tool with the precise reason, not the generic write-scope one.
    if name in _ADMIN_TOOLS and mcp_scope != "admin":
        raise ValueError(f"Tool '{name}' requires admin scope — '{mcp_scope}' token denied")
    if name in _MUTATING_TOOLS and mcp_scope == "read_only":
        raise ValueError(f"Tool '{name}' requires write scope — read-only token denied")

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    result = await handler(arguments, ctx)
    return result


async def _on_list_tools(_ctx: Any, _params: Any) -> ListToolsResult:
    # mcp 2.x handler: registered via the Server(on_list_tools=...) constructor
    # kwarg instead of the removed @server.list_tools() decorator.
    return ListToolsResult(
        tools=[
            Tool(
                name=t["name"],
                description=t["description"],
                input_schema=t["inputSchema"],
                output_schema=t.get("outputSchema"),
            )
            for t in _TOOL_DEFINITIONS
            if t["name"] not in _MCP_FORBIDDEN_TOOLS
        ]
    )


async def _on_call_tool(_ctx: Any, params: Any) -> CallToolResult:
    # mcp 2.x handler (Server(on_call_tool=...)). `params` is a
    # CallToolRequestParams; results are wrapped in a CallToolResult, and errors
    # surface as is_error=True (the 1.x server did this wrapping for us).
    name = params.name
    arguments = params.arguments or {}
    if name in _MCP_FORBIDDEN_TOOLS:
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(f"Tool '{name}' is not available over the MCP transport."),
                )
            ],
            is_error=True,
        )
    ctx = await _server_context.snapshot()
    ctx["_mcp_token_scope"] = _mcp_token_scope_var.get()
    ctx["_mcp_caller_id"] = _mcp_caller_id_var.get()
    try:
        result = await _handle_tool(name, arguments, ctx)
    except ValueError as exc:
        return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
    if isinstance(result, dict):
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))],
            structured_content=result,
        )
    return CallToolResult(content=list(result))


def create_server() -> Server:
    return Server(
        "homepilot-mcp",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


class _ServerContext:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __bool__(self) -> bool:
        return bool(self._data)

    def clear(self) -> None:
        self._data.clear()

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def keys(self) -> Any:
        return self._data.keys()

    def values(self) -> Any:
        return self._data.values()

    def items(self) -> Any:
        return self._data.items()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return dict(self._data)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._data[key] = value

    async def async_update(self, values: dict[str, Any]) -> None:
        async with self._lock:
            self._data.update(values)


_server_context = _ServerContext()


async def run_server() -> None:
    ctx = await _bootstrap()
    await _server_context.async_update(ctx)

    srv = create_server()
    async with stdio_server() as (read_stream, write_stream):
        try:
            await srv.run(read_stream, write_stream, srv.create_initialization_options())
        finally:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(asyncio.sleep(0.5))

            proxmox = ctx.get("proxmox")
            if proxmox:
                await proxmox.close()
            database = ctx.get("database")
            if database:
                await database.close()

            _server_context.clear()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    asyncio.run(run_server())


def create_http_app(srv: Server) -> Any:
    import contextlib
    import hmac as _hmac

    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
    from mcp.server.auth.provider import AccessToken
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Mount
    from starlette.types import ASGIApp

    mcp_token = os.environ.get("HP_MCP_TOKEN", "").strip()
    # HP_MCP_TOKEN_SCOPE selects the tool tier this HTTP MCP transport grants,
    # mirroring the API scope ladder read < write < admin:
    #   "read_only" -> read tools only
    #   "full"      -> reads + full mutators (the default), NOT admin tools
    #   "admin"     -> everything except the _MCP_FORBIDDEN_TOOLS set
    # Enforced per call in _handle_tool.
    mcp_scope = os.environ.get("HP_MCP_TOKEN_SCOPE", "").strip() or "full"

    session_manager = StreamableHTTPSessionManager(
        app=srv,
        stateless=False,
        session_idle_timeout=1800,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: ASGIApp) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("HomePilot MCP HTTP server ready at /mcp")
            yield
        for key in ("proxmox", "database"):
            obj = _server_context.get(key)
            if obj:
                with contextlib.suppress(Exception):
                    await obj.close()

    starlette_app: Starlette = Starlette(
        routes=[Mount("/", app=session_manager.handle_request)],
        lifespan=lifespan,
    )
    # Exposed so a host that MOUNTS this app (main.py) can (a) drive this app's
    # lifespan — Starlette does not run a mounted sub-app's lifespan, so without
    # this the session manager never starts (#382) — and (b) report real MCP
    # health by inspecting whether the session manager task group is running.
    starlette_app.state.session_manager = session_manager

    if mcp_token:

        class _TokenVerifier:
            async def verify_token(self, token: str) -> AccessToken | None:
                # Security note: MCP token is a static HMAC-compared value from
                # HP_MCP_TOKEN env var. No expiry, no rotation — rotate by
                # changing the env var and restarting the process.
                if not _hmac.compare_digest(token, mcp_token):
                    return None
                return AccessToken(token=token, client_id="mcp-http", scopes=[])

        class _RequireAuthenticated(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: Any) -> Any:
                if not request.user.is_authenticated:
                    return Response("Unauthorized", status_code=401)
                return await call_next(request)

        class _InjectContext:
            """ASGI middleware: sets per-request contextvars for MCP scope/caller.

            Uses raw ASGI (not BaseHTTPMiddleware) to ensure contextvars
            propagate correctly to downstream handlers without task isolation.

            Uses the token pattern for contextvars so values are scoped
            to the request lifecycle and never persist across requests.
            """

            def __init__(self, app: ASGIApp) -> None:
                self.app = app

            async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
                if scope["type"] in ("http", "websocket"):
                    request = Request(scope, receive)
                    if hasattr(request, "user") and request.user.is_authenticated:
                        client_id = (
                            request.user.client_id
                            if hasattr(request.user, "client_id") and request.user.client_id
                            else "mcp-http"
                        )
                        caller_token = _mcp_caller_id_var.set(client_id)
                        scope_token = _mcp_token_scope_var.set(mcp_scope)
                        try:
                            await self.app(scope, receive, send)
                        finally:
                            _mcp_caller_id_var.reset(caller_token)
                            _mcp_token_scope_var.reset(scope_token)
                        return
                await self.app(scope, receive, send)

        starlette_app.add_middleware(_InjectContext)
        starlette_app.add_middleware(AuthContextMiddleware)
        starlette_app.add_middleware(_RequireAuthenticated)
        starlette_app.add_middleware(
            AuthenticationMiddleware, backend=BearerAuthBackend(_TokenVerifier())
        )

    return starlette_app


async def run_server_http(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    # Hard startup guard (#385): the HTTP transport binds a network port
    # (0.0.0.0 by default). Auth is only attached when HP_MCP_TOKEN is set, so
    # binding without it would expose the full MCP tool surface unauthenticated.
    # Refuse to start rather than serve an open control plane.
    if not os.environ.get("HP_MCP_TOKEN", "").strip():
        raise RuntimeError(
            "Refusing to start the HTTP MCP transport without HP_MCP_TOKEN: the "
            "bind would be unauthenticated. Set HP_MCP_TOKEN and restart."
        )

    ctx = await _bootstrap()
    await _server_context.async_update(ctx)
    srv = create_server()

    app = create_http_app(srv)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server_instance = uvicorn.Server(config)
    await server_instance.serve()


def main_http(host: str = "0.0.0.0", port: int = 8000) -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    asyncio.run(run_server_http(host=host, port=port))


if __name__ == "__main__":
    main()
