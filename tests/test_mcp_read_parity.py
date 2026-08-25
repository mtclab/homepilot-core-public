"""MCP read parity with the management API (wave 1).

The MCP tool surface was far short of what the management portal can do: an
assistant could not list the fleet, read a host, see an alert, read the audit
trail, or look at the overview an operator reads first. This module holds the
gates that closed that gap and keep it closed.

Three kinds of gate live here:

* **Parity gate** - ``test_every_management_get_route_is_reachable_or_excluded``
  walks the REAL app's GET routes and demands each one either map to a
  registered MCP tool or appear in ``EXCLUDED_GET_ROUTES`` with a stated reason.
  Teeth: add a GET route with neither, or delete a tool, and it fails naming the
  route. ``test_no_stale_entries_in_the_parity_map`` is its other half - a map
  entry for a route that no longer exists fails too, so the map cannot rot into
  a list of comforting lies.

* **Same-answer gates** - each read tool is called next to the HTTP route it
  mirrors, over the SAME database, and the two answers are compared. This is the
  property that matters: not "the handler returned something" but "the console
  and the assistant describe the same estate".

* **Secret gate** - ``TestProxmoxSettingsNeverLeakTheToken`` puts a known token
  value in the vault and asserts it appears NOWHERE in what the tool returns.
  Teeth: return `token` instead of `token_configured` from
  ``proxmox_settings_report`` and it fails.

Every tool added in this wave is READ-ONLY and must therefore work under a
``read_only`` MCP token; ``TestReadOnlyScopeCanCallEveryReadTool`` proves it by
driving the real scope check in ``_handle_tool``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import (
    _ADMIN_TOOLS,
    _MUTATING_TOOLS,
    _READ_ONLY_TOOLS,
    _TOOL_HANDLERS,
    _handle_tool,
    _mcp_token_scope_var,
)
from homepilot.metrics.repository import MetricsRepository
from homepilot.tasks.repository import TaskRepository

pytestmark = pytest.mark.asyncio


# ── The parity map ───────────────────────────────────────────────────────────
# Every authenticated GET route of the management API, and the MCP tool that
# answers the same question. Data, not code: the gate below reads it.

ROUTE_TO_TOOL: dict[str, str] = {
    # Admin
    "/admin/guests": "query_guests",
    "/admin/selfcheck": "get_selfcheck",
    "/admin/settings/proxmox": "get_proxmox_settings",
    # Agent fleet
    "/agents/": "list_agents",
    "/agents/audit": "get_agent_audit",
    "/agents/enrolment-window": "get_enrolment_window",
    "/agents/{agent_id}": "get_agent",
    # get_agent takes agent_id OR hostname, which is exactly what this route is.
    "/agents/hostname/{hostname}": "get_agent",
    # "is there a live channel to this host" - the question check_host_reachable
    # was built for, answered from the same registry.
    "/agents/hostname/{hostname}/connected": "check_host_reachable",
    # Artifacts
    "/artifacts": "query_artifacts",
    "/artifacts/{artifact_id}": "get_artifact",
    "/artifacts/drift": "get_fleet_drift",
    # Audit
    "/audit": "get_audit_log",
    # Dashboard. /dashboard/config carries one field - metrics_retention_days -
    # and the summary already returns it under `metrics.retention_days`, so a
    # second tool would be a second name for one number.
    "/dashboard/summary": "get_dashboard_summary",
    "/dashboard/config": "get_dashboard_summary",
    # Inventory
    "/inventory": "query_inventory",
    "/inventory/{host_id}": "get_host",
    "/inventory/{host_id}/doc": "get_environment_doc",
    # KB
    "/kb": "list_kb",
    "/kb/search": "search_kb",
    "/kb/{doc_id}": "get_kb_doc",
    # GET /kb/embedding-status is API `admin` (a read the API reserves for admin);
    # get_kb_embedding_status is an admin-tier tool, so it maps here and the tier
    # gate holds it at admin.
    "/kb/embedding-status": "get_kb_embedding_status",
    # Monitoring
    "/monitoring/rules": "list_alert_rules",
    "/monitoring/alerts": "get_monitoring_alerts",
    "/monitoring/hosts/{hostname}/latest": "get_host_metrics",
    "/monitoring/hosts/{hostname}/series": "get_host_metrics_series",
    # Tasks
    "/tasks": "list_tasks",
    "/tasks/{task_id}": "get_task_result",
}

EXCLUDED_GET_ROUTES: dict[str, str] = {
    # ── Secrets. Owner rule: these never travel over MCP. ────────────────────
    "/agents/token": (
        "returns the hub's shared enrolment token - a credential that provisions "
        "machines must not appear in an MCP transcript"
    ),
    "/agents/dist/install-agent.sh": (
        "the installer one-liner embeds the enrolment token, so serving it over "
        "MCP is serving the token"
    ),
    # ── Not an MCP shape. ────────────────────────────────────────────────────
    "/agents/dist": "an agent-binary download manifest; the bytes are not an MCP payload",
    "/agents/dist/hp-agent-linux-{arch}": "raw binary bytes, not an MCP payload",
    "/artifacts/events/stream": (
        "a Server-Sent Events stream - an open connection, not a request/response tool"
    ),
    # ── Deferred to a later wave, deliberately. ──────────────────────────────
    "/agents/migrate-tls": (
        "the PREVIEW (GET) half of the fleet TLS migration. Its perform side "
        "(migrate_agents_tls) ships in wave 3, but the preview is deliberately not "
        "mirrored: it is an admin fleet-cert diagnostic the assistant can read "
        "indirectly via list_agents, and adding a preview tool it would rarely use "
        "is scope the wave did not enumerate"
    ),
    "/agents/install/{host_id}": (
        "install eligibility - the read half of POST /agents/install, which is "
        "itself provisioning-adjacent and excluded. It only means anything paired "
        "with the install it gates, so it ships with the provisioning surface"
    ),
    "/agents/test/adapter": (
        "a diagnostic that EXECUTES a command on a connected host; despite being "
        "a GET it is not a read, and exec_on_guest_readonly is the read-scoped way "
        "to run a command"
    ),
    "/auth/tokens": (
        "the API-token registry - a credential surface. It belongs to the admin "
        "scope tier, not to a read tool available to every MCP client"
    ),
}


def _authenticated_get_routes() -> list[str]:
    """Every GET path template of the real app that is not public."""
    from homepilot.main import _PUBLIC_ROUTES, _walk_api_routes, app

    paths: list[str] = []
    for path, route, _deps in _walk_api_routes(list(app.routes)):
        if "GET" not in route.methods:
            continue
        if ("GET", path) in _PUBLIC_ROUTES:
            continue
        paths.append(path)
    return sorted(set(paths))


class TestParityGate:
    async def test_the_route_walk_finds_the_management_api(self) -> None:
        """Guard the guard: an empty walk would make the gate vacuous."""
        routes = _authenticated_get_routes()
        assert len(routes) > 30, f"the route walk found almost nothing: {routes}"
        assert "/inventory" in routes and "/agents/" in routes

    async def test_every_management_get_route_is_reachable_or_excluded(self) -> None:
        unmapped = [
            path
            for path in _authenticated_get_routes()
            if path not in ROUTE_TO_TOOL and path not in EXCLUDED_GET_ROUTES
        ]
        assert not unmapped, (
            "management GET route(s) with no MCP tool and no stated exclusion: "
            f"{unmapped}. Add a read tool, or add an entry to EXCLUDED_GET_ROUTES "
            "saying why an assistant must not have this."
        )

    async def test_every_mapped_tool_is_registered(self) -> None:
        missing = sorted({tool for tool in ROUTE_TO_TOOL.values() if tool not in _TOOL_HANDLERS})
        assert not missing, f"the parity map names tool(s) the server does not serve: {missing}"

    async def test_no_stale_entries_in_the_parity_map(self) -> None:
        """A map entry for a route that no longer exists is a lie about coverage."""
        live = set(_authenticated_get_routes())
        stale = sorted((set(ROUTE_TO_TOOL) | set(EXCLUDED_GET_ROUTES)) - live)
        assert not stale, f"parity map names route(s) the API no longer has: {stale}"

    async def test_no_route_maps_to_a_mutating_tool(self) -> None:
        """A read route must never be served by something that writes."""
        offenders = sorted({tool for tool in ROUTE_TO_TOOL.values() if tool in _MUTATING_TOOLS})
        assert not offenders, f"GET route(s) mapped to mutating tool(s): {offenders}"

    async def test_every_mapped_get_tool_is_a_non_writing_tool(self) -> None:
        """A GET route must be served by a tool that does not write - a read_only
        tool for an API `read` route, or an admin-tier READ tool for an API-admin
        GET (e.g. get_kb_embedding_status). Never a mutator. The exact tier match
        is enforced by TestMcpTierMatchesApiScope; here we forbid the write set."""
        offenders = sorted({tool for tool in ROUTE_TO_TOOL.values() if tool in _MUTATING_TOOLS})
        assert not offenders, f"GET route(s) served by a mutating tool: {offenders}"
        unclassified = sorted(
            {
                tool
                for tool in ROUTE_TO_TOOL.values()
                if tool not in _READ_ONLY_TOOLS and tool not in _ADMIN_TOOLS
            }
        )
        assert not unclassified, (
            f"tool(s) answering a GET route are neither read_only nor admin: {unclassified}"
        )


# ── The mutation parity map (wave 2) ─────────────────────────────────────────
# The same discipline as the GET map above, for the management API's
# POST/PATCH/PUT/DELETE routes: every one either maps to an MCP mutator tool or
# appears in EXCLUDED_MUTATION_ROUTES with a stated reason. This is the
# anti-regression spine - a new write route with neither fails the gate, and a
# removed tool fails it too.

MUTATION_ROUTE_TO_TOOL: dict[tuple[str, str], str] = {
    # Tasks
    ("POST", "/tasks/{task_id}/cancel"): "cancel_task",
    # Inventory
    ("POST", "/inventory"): "add_host",
    ("POST", "/inventory/{host_id}/adopt"): "adopt_host",
    ("POST", "/inventory/{host_id}/ignore"): "ignore_host",
    ("PATCH", "/inventory/{host_id}"): "update_host",
    ("DELETE", "/inventory/{host_id}"): "delete_host",
    ("POST", "/inventory/enrich"): "enrich_inventory",
    ("POST", "/inventory/bulk"): "bulk_host_action",
    ("POST", "/inventory/refresh"): "refresh_inventory",
    # KB. record_fact writes a note/policy, which is exactly what both POST /kb
    # (record_fact route) and POST /kb/notes (create_note route) do - they call
    # the same KBService.record_fact underneath, so one tool covers both.
    ("POST", "/kb"): "record_fact",
    ("POST", "/kb/notes"): "record_fact",
    # PUT /kb/{doc_id} is API `write` -> full tier.
    ("PUT", "/kb/{doc_id}"): "update_kb_doc",
    # KB admin (wave 3): delete/ingest/reindex are API `admin` -> admin tier.
    ("DELETE", "/kb/{doc_id}"): "delete_kb_doc",
    ("POST", "/kb/ingest"): "ingest_kb",
    ("POST", "/kb/reindex"): "reindex_kb",
    # Monitoring admin (wave 3): rule create/update/delete are API `admin`.
    ("POST", "/monitoring/rules"): "create_alert_rule",
    ("PATCH", "/monitoring/rules/{rule_id}"): "update_alert_rule",
    ("DELETE", "/monitoring/rules/{rule_id}"): "delete_alert_rule",
    # Agent fleet admin (wave 3). exec_on_host/write_file_on_host mirror the
    # host-addressed RPCs (POST /agents/host/*); the by-agent-id variants are
    # excluded below because these hostname-addressed tools cover them.
    ("POST", "/agents/enrolment-window"): "open_enrolment_window",
    ("DELETE", "/agents/enrolment-window"): "close_enrolment_window",
    ("POST", "/agents/{agent_id}/revoke"): "revoke_agent",
    ("DELETE", "/agents/{agent_id}"): "forget_agent",
    ("POST", "/agents/migrate-tls"): "migrate_agents_tls",
    ("POST", "/agents/host/exec"): "exec_on_host",
    ("POST", "/agents/host/write-file"): "write_file_on_host",
    # Admin settings / credentials (wave 3).
    ("POST", "/admin/settings/proxmox/test"): "test_proxmox_connection",
    ("DELETE", "/auth/tokens/{prefix}"): "delete_auth_token",
    # Artifacts
    ("POST", "/artifacts"): "propose_artifact",
    # approve is now REACHABLE over MCP (human-relay approval, #385 follow-up):
    # POST /artifacts/{id}/approve is require_scope('write') -> full tier, exactly
    # matching approve_artifact's tier. The EXTRA gate is a per-artifact approval
    # code a human relays (never returned by any MCP read), not a tier change, so
    # the tier<->scope invariant still holds.
    ("POST", "/artifacts/{artifact_id}/approve"): "approve_artifact",
    ("POST", "/artifacts/{artifact_id}/apply"): "apply_artifact",
    # plan/preview are API `read`-scoped POSTs -> read_only tools (exact tier).
    ("POST", "/artifacts/{artifact_id}/plan"): "plan_artifact",
    ("POST", "/artifacts/{artifact_id}/preview"): "preview_artifact",
    ("POST", "/artifacts/{artifact_id}/reject"): "reject_artifact",
    ("POST", "/artifacts/{artifact_id}/replay"): "replay_artifact",
    # DELETE /artifacts/{id} is API require_scope('write') -> full tier (it rolls
    # an applied artifact back through the runner; it cannot grant approval).
    ("DELETE", "/artifacts/{artifact_id}"): "revoke_artifact",
    # Guest management (#442). These routes are API `admin`; wave 3 moved the tools
    # to the admin tier so tool and route now match (no exemption).
    ("POST", "/admin/guests/quota"): "set_guest_quota",
    ("POST", "/admin/guests/invites/{prefix}/revoke"): "revoke_guest_invite",
    # Guest provisioning (owner decision 2026-08-25): POST /guests/provision is API
    # admin; it clones a Proxmox template into a running guest.
    ("POST", "/guests/provision"): "provision_guest",
}

EXCLUDED_MUTATION_ROUTES: dict[tuple[str, str], str] = {
    # ── Operator-only reset of a coded-approval lock. Not mirrored over MCP. ──
    ("POST", "/artifacts/{artifact_id}/approval-code/reset"): (
        "clears the brute-force LOCK on an artifact's approval code - the very lock "
        "that stops an MCP caller guessing the code. Exposing the reset over MCP "
        "would let the assistant undo its own lock-out, so it is a present-human "
        "action at the web UI / `hp artifacts reset-approval` only."
    ),
    # ── Secret mints. Owner rule: a minted token never travels over MCP. ─────
    ("POST", "/admin/guests/invites"): (
        "minting a guest invite returns a fresh credential; a token an MCP transcript "
        "must never carry. Deferred to the secret-mint wave."
    ),
    # ── Secret/credential configuration - kept OUT of MCP by owner secret rules. ─
    ("PUT", "/admin/settings/proxmox"): (
        "PERMANENTLY excluded (owner 2026-08-25): set_proxmox_settings takes a live PVE "
        "API token as INPUT, which would land in the AI transcript - a secret-in-transcript "
        "vector the estate's secret rule forbids. Not a deferral. test_proxmox_connection "
        "is the exposed, no-secret way to check the wiring."
    ),
    ("POST", "/admin/reload-secrets"): (
        "PERMANENTLY excluded (owner 2026-08-25): reload_secrets is Request/app.state-coupled "
        "and SWAPS the live Proxmox client; the MCP process has no FastAPI app to reload, so "
        "mirroring it would risk HTTP and MCP pointing at different clients. App-state coupling, "
        "not a deferral."
    ),
    # ── Secret mints. Owner rule: a minted credential never travels over MCP. ──
    (
        "POST",
        "/agents/bootstrap",
    ): "enrolment/bootstrap that issues agent credentials - a secret mint",
    # ── Provisioning-adjacent. ────────────────────────────────────────────────
    ("POST", "/agents/install"): (
        "installs an agent on a host (provisioning-adjacent); ships with the "
        "provisioning surface, not this wave"
    ),
    # The by-agent-id raw host RPCs. The hostname-addressed exec_on_host /
    # write_file_on_host tools (mapped above to POST /agents/host/*) cover these,
    # keeping the MCP surface hostname-based like the other host tools. The
    # read-file RPCs are not mirrored at all: read_file_on_guest already reads
    # files at read tier WITH a secret denylist, and the raw admin variant would
    # strip that guard while calling the same adapter.read_file - a needless
    # secret-exfil surface for nothing gained.
    ("POST", "/agents/host/read-file"): (
        "raw host file read; read_file_on_guest is the guarded read tool - not mirrored"
    ),
    ("POST", "/agents/{agent_id}/exec"): (
        "by-agent-id raw exec; exec_on_host (hostname-addressed) covers it"
    ),
    ("POST", "/agents/{agent_id}/read-file"): (
        "by-agent-id raw file read; read_file_on_guest is the guarded read tool - not mirrored"
    ),
    ("POST", "/agents/{agent_id}/write-file"): (
        "by-agent-id raw file write; write_file_on_host (hostname-addressed) covers it"
    ),
}


def _authenticated_mutating_routes() -> list[tuple[str, str]]:
    """Every (method, path) of the real app that writes and is not public."""
    from homepilot.main import _PUBLIC_ROUTES, _walk_api_routes, app

    out: list[tuple[str, str]] = []
    for path, route, _deps in _walk_api_routes(list(app.routes)):
        for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
            if (method, path) in _PUBLIC_ROUTES:
                continue
            out.append((method, path))
    return sorted(set(out))


class TestMutationParityGate:
    async def test_the_route_walk_finds_the_write_surface(self) -> None:
        """Guard the guard: an empty walk would make the gate vacuous."""
        routes = _authenticated_mutating_routes()
        assert len(routes) > 20, f"the write-route walk found almost nothing: {routes}"
        assert ("POST", "/artifacts/{artifact_id}/apply") in routes
        assert ("PATCH", "/inventory/{host_id}") in routes

    async def test_every_management_write_route_is_reachable_or_excluded(self) -> None:
        unmapped = [
            r
            for r in _authenticated_mutating_routes()
            if r not in MUTATION_ROUTE_TO_TOOL and r not in EXCLUDED_MUTATION_ROUTES
        ]
        assert not unmapped, (
            "management write route(s) with no MCP tool and no stated exclusion: "
            f"{unmapped}. Add a mutator tool, or add an entry to "
            "EXCLUDED_MUTATION_ROUTES saying why an assistant must not have this."
        )

    async def test_every_mapped_mutator_is_registered(self) -> None:
        missing = sorted(
            {tool for tool in MUTATION_ROUTE_TO_TOOL.values() if tool not in _TOOL_HANDLERS}
        )
        assert not missing, f"the mutation map names tool(s) the server does not serve: {missing}"

    async def test_no_stale_entries_in_the_mutation_map(self) -> None:
        """A map entry for a write route that no longer exists is a lie about coverage."""
        live = set(_authenticated_mutating_routes())
        stale = sorted((set(MUTATION_ROUTE_TO_TOOL) | set(EXCLUDED_MUTATION_ROUTES)) - live)
        assert not stale, f"mutation map names route(s) the API no longer has: {stale}"

    # Read-tier tools that answer a POST route. Each is justified:
    # - refresh_inventory: POST /inventory/refresh is API `write`, but wave 1
    #   classified the tool read-only on purpose - pulling fresh data is a read
    #   the assistant should always be able to do, and it writes only HomePilot's
    #   own mirror of what the hypervisor already reports.
    # - plan_artifact / preview_artifact: their POST routes are API `read`-scoped
    #   and change no host, so read_only is their EXACT tier (they compute a
    #   plan/diff only) - not really an exemption, just a read tool on a POST.
    _READ_CLASSIFIED_WRITE_TOOLS = frozenset(
        {"refresh_inventory", "plan_artifact", "preview_artifact"}
    )

    async def test_every_mapped_mutator_is_actually_mutating(self) -> None:
        """A write/admin route must be served by a tool a read_only token cannot
        call - a `full` mutator or an admin-tier tool - except the named
        read-classified exemptions (plan/preview/refresh, which mirror API `read`
        POST routes)."""
        offenders = sorted(
            {
                tool
                for tool in MUTATION_ROUTE_TO_TOOL.values()
                if tool not in _MUTATING_TOOLS
                and tool not in _ADMIN_TOOLS
                and tool not in self._READ_CLASSIFIED_WRITE_TOOLS
            }
        )
        assert not offenders, f"write route(s) mapped to a read-only tool: {offenders}"

    async def test_the_read_classified_exemptions_really_are_read_tools(self) -> None:
        """Guard the exemption: each named exemption must genuinely be a read tool,
        so the escape hatch cannot be used to smuggle a real mutator past the gate."""
        for tool in self._READ_CLASSIFIED_WRITE_TOOLS:
            assert tool in _READ_ONLY_TOOLS, f"{tool} is exempted but is not a read-only tool"
            assert tool not in _MUTATING_TOOLS, f"{tool} is exempted but is also mutating"

    async def test_approve_is_exposed_at_full_tier_and_not_forbidden(self) -> None:
        """The human-relay approval mechanism: approve_artifact is now a REACHABLE
        MCP tool at full tier (matching its write-scoped route), gated by a code a
        human relays rather than by a blanket transport ban. It is no longer in
        the forbidden set nor the excluded-route map."""
        from homepilot.mcp.server import _MCP_FORBIDDEN_TOOLS, _MUTATING_TOOLS

        assert "approve_artifact" not in _MCP_FORBIDDEN_TOOLS
        assert "approve_artifact" in _MUTATING_TOOLS  # full tier == route's 'write'
        assert (
            MUTATION_ROUTE_TO_TOOL[("POST", "/artifacts/{artifact_id}/approve")]
            == "approve_artifact"
        )
        assert ("POST", "/artifacts/{artifact_id}/approve") not in EXCLUDED_MUTATION_ROUTES


# ── The tier<->scope anti-escalation gate (the spine) ────────────────────────
# The API scope ladder is read < write < admin, which maps to the MCP tier
# ladder read_only < full < admin. An MCP tool's tier MUST equal the API scope
# of the route it mirrors: a tool WEAKER than its route is an escalation (a
# lesser token doing what the API reserves for a greater one), a tool STRONGER
# is a needless over-restriction. This gate resolves each route's real
# require_scope from the app's dependency tree and demands an exact match for
# every (tool, route) pair in BOTH parity maps.

# API scope -> the MCP tier that maps to it exactly.
_API_SCOPE_TO_MCP_TIER = {"read": "read_only", "write": "full", "admin": "admin"}
_SCOPE_RANK = {"read": 0, "write": 1, "admin": 2}


def _iter_dependants(dep: Any) -> Any:
    yield dep
    for sub in dep.dependencies:
        yield from _iter_dependants(sub)


def _route_required_scope(route: Any, include_deps: tuple[Any, ...]) -> str | None:
    """The strongest scope any dependency of this route enforces, or None.

    Reads the scope string require_scope() records on its check callable, so this
    is the route's REAL required scope - not a guess from the path."""
    from fastapi.dependencies.utils import get_dependant

    from homepilot.auth.deps import REQUIRED_SCOPE_ATTR

    scopes: set[str] = set()
    for sub in _iter_dependants(route.dependant):
        sc = getattr(sub.call, REQUIRED_SCOPE_ATTR, None)
        if sc:
            scopes.add(sc)
    for dep in include_deps:
        call = getattr(dep, "dependency", None)
        if call is None:
            continue
        sc = getattr(call, REQUIRED_SCOPE_ATTR, None)
        if sc:
            scopes.add(sc)
        try:
            for sub in _iter_dependants(get_dependant(path="/", call=call)):
                sc = getattr(sub.call, REQUIRED_SCOPE_ATTR, None)
                if sc:
                    scopes.add(sc)
        except Exception:  # pragma: no cover - a dependency FastAPI cannot analyse
            pass
    if not scopes:
        return None
    return max(scopes, key=lambda s: _SCOPE_RANK.get(s, -1))


def _route_scope_index() -> dict[tuple[str, str], str | None]:
    from homepilot.main import _walk_api_routes, app

    idx: dict[tuple[str, str], str | None] = {}
    for path, route, deps in _walk_api_routes(list(app.routes)):
        scope = _route_required_scope(route, deps)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            idx[(method, path)] = scope
    return idx


def _mcp_tier(tool: str) -> str | None:
    if tool in _ADMIN_TOOLS:
        return "admin"
    if tool in _MUTATING_TOOLS:
        return "full"
    if tool in _READ_ONLY_TOOLS:
        return "read_only"
    return None


# Tools whose MCP tier deliberately does NOT equal their route's API scope. Each
# MUST be justified, and TestMcpTierMatchesApiScope proves every entry is a
# genuine mismatch (so the escape hatch cannot rot into a rubber stamp).
_TIER_EXEMPTIONS: dict[str, str] = {
    # Design: a POST that only re-pulls HomePilot's own mirror of what the
    # hypervisor already reports is deliberately read-tier, so the assistant can
    # always refresh even though the route is write-scoped. This is the ONLY
    # remaining exemption: wave 3 added the admin tier and loosened the fleet/ops
    # reads to API `read`, dissolving all the wave-1/#442 escalation debt that
    # used to live here.
    "refresh_inventory": "write route, deliberately read-classified (re-pull only)",
}


def _all_tool_route_pairs() -> list[tuple[str, tuple[str, str], str | None]]:
    """Every (tool, (method, path), api_scope) pair across BOTH parity maps."""
    idx = _route_scope_index()
    pairs: list[tuple[str, tuple[str, str], str | None]] = []
    for path, tool in ROUTE_TO_TOOL.items():
        pairs.append((tool, ("GET", path), idx.get(("GET", path))))
    for (method, path), tool in MUTATION_ROUTE_TO_TOOL.items():
        pairs.append((tool, (method, path), idx.get((method, path))))
    return pairs


def _tier_mismatches(tier_fn: Any, exemptions: set[str]) -> list[str]:
    """The core of the gate: every (tool, route) pair whose MCP tier does not
    equal its route's API scope, minus the named exemptions. Shared by the real
    gate and its teeth test so the teeth exercise the SAME logic."""
    out: list[str] = []
    for tool, route, scope in _all_tool_route_pairs():
        if scope is None:
            out.append(f"{tool} @ {route}: route has no resolvable scope")
            continue
        expected = _API_SCOPE_TO_MCP_TIER[scope]
        actual = tier_fn(tool)
        if actual != expected and tool not in exemptions:
            out.append(
                f"{tool} @ {route}: API scope '{scope}' wants MCP tier "
                f"'{expected}', tool is '{actual}'"
            )
    return out


class TestMcpTierMatchesApiScope:
    """No MCP tool may sit at a tier weaker OR stronger than its route's API
    scope. This is the gate that makes the tiers trustworthy."""

    async def test_the_scope_resolver_actually_resolves(self) -> None:
        """Guard the guard: if scope resolution returned None everywhere the gate
        below would be vacuous."""
        idx = _route_scope_index()
        assert idx.get(("POST", "/inventory")) == "write"
        assert idx.get(("GET", "/inventory")) == "read"
        # A route that is genuinely admin (KB delete), and one Part B loosened to
        # read (the token-redacted Proxmox wiring), so the resolver is exercised in
        # both directions.
        assert idx.get(("DELETE", "/kb/{doc_id}")) == "admin"
        assert idx.get(("GET", "/admin/settings/proxmox")) == "read"

    async def test_every_pair_maps_its_tier_to_its_scope_exactly(self) -> None:
        mismatches = _tier_mismatches(_mcp_tier, set(_TIER_EXEMPTIONS))
        assert not mismatches, (
            "MCP tier <-> API scope mismatch(es) - escalation or over-restriction:\n"
            + "\n".join(mismatches)
        )

    async def test_every_exemption_is_a_real_mismatch(self) -> None:
        """A stale exemption (for a pair that actually matches now) would silently
        weaken the gate, so each exemption must still name a genuine mismatch."""
        by_tool_scope: dict[str, set[str | None]] = {}
        for tool, _route, scope in _all_tool_route_pairs():
            by_tool_scope.setdefault(tool, set()).add(scope)
        for tool in _TIER_EXEMPTIONS:
            assert tool in by_tool_scope, f"exemption {tool} maps to no route in either map"
            scopes = by_tool_scope[tool]
            actual = _mcp_tier(tool)
            really_mismatched = any(
                sc is not None and _API_SCOPE_TO_MCP_TIER[sc] != actual for sc in scopes
            )
            assert really_mismatched, (
                f"exemption {tool} is stale: its tier '{actual}' already matches its "
                f"route scope(s) {scopes} - remove it from _TIER_EXEMPTIONS"
            )

    async def test_the_gate_has_teeth(self) -> None:
        """Flip one non-exempt tool to the wrong tier and the SAME gate logic must
        catch it (both directions)."""

        def escalated(tool: str) -> str | None:
            # add_host is API write -> full. Pretend it were shipped read_only:
            # a normal read_only MCP token could then add hosts (escalation).
            return "read_only" if tool == "add_host" else _mcp_tier(tool)

        caught = _tier_mismatches(escalated, set(_TIER_EXEMPTIONS))
        assert any("add_host" in m for m in caught), "the gate missed a real escalation"

        def over_restricted(tool: str) -> str | None:
            # get_host is API read -> read_only. Pretend it were shipped full:
            # a read_only token could no longer read a host (over-restriction).
            return "full" if tool == "get_host" else _mcp_tier(tool)

        caught2 = _tier_mismatches(over_restricted, set(_TIER_EXEMPTIONS))
        assert any("get_host" in m for m in caught2), "the gate missed an over-restriction"


# ── Shared estate: one database, seeded once, read by both surfaces ──────────


class _FakeAgent:
    def __init__(self, agent_id: str, hostname: str) -> None:
        from datetime import UTC, datetime

        self.agent_id = agent_id
        self.hostname = hostname
        self.system_info = {"agent_version": "9.9.9", "arch": "amd64"}
        self.state = {"load": 0.2}
        self.connected_at = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        self.last_heartbeat = datetime(2026, 8, 1, 10, 5, tzinfo=UTC)


class _FakeAuditLog:
    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def query_persisted(
        self, limit: int = 100, agent_id: str | None = None, action: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._repo.query_agent_audit(limit=limit, agent_id=agent_id, action=action)
        return [dict(r) for r in rows]


class _FakeRegistry:
    """The live hub registry, holding exactly what a test dials in."""

    def __init__(self, repo: Any, agents: list[_FakeAgent]) -> None:
        self._agents = {a.agent_id: a for a in agents}
        self.audit_log = _FakeAuditLog(repo)
        self.hub_server = None

    def list_connected(self) -> list[dict[str, Any]]:
        return [
            {
                "agent_id": a.agent_id,
                "hostname": a.hostname,
                "system_info": a.system_info,
                "state": a.state,
                "connected_at": a.connected_at.isoformat(),
                "last_heartbeat": a.last_heartbeat.isoformat(),
                "stale_seconds": 0,
            }
            for a in self._agents.values()
        ]

    def get(self, agent_id: str) -> _FakeAgent | None:
        return self._agents.get(agent_id)

    def get_by_hostname(self, hostname: str) -> _FakeAgent | None:
        for a in self._agents.values():
            if a.hostname == hostname:
                return a
        return None

    def is_connected(self, hostname: str) -> bool:
        return self.get_by_hostname(hostname) is not None


AGENT_ID = "agent-parity-1"
HOSTNAME = "hp-parity-host"


@pytest.fixture
async def estate(tmp_path: Path):
    """A small but real estate: host + agent + task + audit + KB doc + metrics."""
    db = Database(str(tmp_path / "parity.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    task_repo = TaskRepository(db)
    metrics_repo = MetricsRepository(db)

    host_id = await repo.create_host(
        hostname=HOSTNAME, host_type="vm", role="app", cpu_cores=4, managed=True
    )
    await db.execute(
        "INSERT INTO agents (agent_id, hostname, connected, system_info) VALUES (?, ?, 1, ?)",
        (AGENT_ID, HOSTNAME, json.dumps({"agent_version": "9.9.9", "arch": "amd64"})),
    )
    await db.execute(
        "UPDATE hosts SET agent_id = ? WHERE id = ?",
        (AGENT_ID, host_id),
    )
    await db.conn.commit()

    await repo.log_agent_audit(
        agent_id=AGENT_ID, hostname=HOSTNAME, action="register", target="parity seed"
    )
    await repo.log_audit(
        user_id="operator", source="cli", action="artifact_applied", target_host=HOSTNAME
    )
    task_id = await task_repo.create_task(artifact_id="2026-08-01-parity", action="apply")
    doc_id = await repo.create_doc_metadata(
        source="kb:parity",
        title="Parity note",
        content="what this host is for",
        kind="note",
        target=HOSTNAME,
    )
    # Recent timestamps on purpose: a real series query asks for the last N
    # hours, and samples older than the window are outside the product's answer.
    now_ts = int(time.time())
    await metrics_repo.insert_samples(
        HOSTNAME,
        AGENT_ID,
        [("cpu.percent", now_ts - 120, 12.5), ("cpu.percent", now_ts - 60, 13.5)],
    )
    rule = await metrics_repo.create_rule(
        name="cpu high",
        metric="cpu.percent",
        comparison="gt",
        threshold=90.0,
        for_seconds=0,
        host_filter="*",
        enabled=True,
    )

    registry = _FakeRegistry(repo, [_FakeAgent(AGENT_ID, HOSTNAME)])

    yield SimpleNamespace(
        db=db,
        repo=repo,
        task_repo=task_repo,
        metrics_repo=metrics_repo,
        registry=registry,
        host_id=str(host_id),
        task_id=task_id,
        doc_id=doc_id,
        rule_id=rule["id"],
    )
    await db.close()


@pytest.fixture
def api(estate, monkeypatch):
    """The management API, mounted over the same estate the MCP context reads."""
    from homepilot.agent_hub.router import router as agents_router
    from homepilot.audit.router import router as audit_router
    from homepilot.dashboard.router import router as dashboard_router
    from homepilot.inventory.router import router as inventory_router
    from homepilot.inventory.service import InventoryService
    from homepilot.kb.router import router as kb_router
    from homepilot.metrics.router import router as metrics_router
    from homepilot.tasks.router import router as tasks_router

    monkeypatch.setattr(
        "homepilot.app_state.get_agent_registry", lambda: estate.registry, raising=False
    )

    application = FastAPI()
    application.include_router(agents_router)
    application.include_router(audit_router, prefix="/audit")
    application.include_router(inventory_router, prefix="/inventory")
    application.include_router(tasks_router, prefix="/tasks")
    application.include_router(kb_router, prefix="/kb")
    application.include_router(dashboard_router)
    application.include_router(metrics_router)
    application.state.repo = estate.repo
    application.state.task_repo = estate.task_repo
    application.state.metrics_repo = estate.metrics_repo
    application.state.agent_registry = estate.registry
    application.state.inventory_service = InventoryService(estate.repo)
    application.state.kb_service = None
    application.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    return application


@pytest.fixture
def ctx(estate):
    from homepilot.inventory.service import InventoryService

    return {
        "repo": estate.repo,
        "database": estate.db,
        "task_repo": estate.task_repo,
        "metrics_repo": estate.metrics_repo,
        "agent_registry": estate.registry,
        "inventory_service": InventoryService(estate.repo),
    }


async def _get(app: FastAPI, path: str, **params: Any) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://parity") as client:
        resp = await client.get(path, params=params)
    assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text}"
    return resp.json()


class TestTheToolAndTheRouteDescribeTheSameEstate:
    """Not "the handler returned ok" - the two surfaces must AGREE."""

    async def test_list_agents(self, api, ctx, estate) -> None:
        route = await _get(api, "/agents/")
        tool = await _handle_tool("list_agents", {}, ctx)
        assert tool["agents"] == route
        assert tool["total"] == 1 and tool["connected"] == 1
        assert tool["agents"][0]["hostname"] == HOSTNAME

    async def test_get_agent_by_id_and_by_hostname(self, api, ctx) -> None:
        route = await _get(api, f"/agents/{AGENT_ID}")
        by_id = await _handle_tool("get_agent", {"agent_id": AGENT_ID}, ctx)
        by_host = await _handle_tool("get_agent", {"hostname": HOSTNAME}, ctx)
        assert by_id == route
        assert by_host == await _get(api, f"/agents/hostname/{HOSTNAME}")
        assert by_id["system_info"]["agent_version"] == "9.9.9"

    async def test_get_agent_audit(self, api, ctx) -> None:
        route = await _get(api, "/agents/audit")
        tool = await _handle_tool("get_agent_audit", {}, ctx)
        assert tool["entries"] == route
        assert tool["total"] == 1
        assert tool["entries"][0]["action"] == "register"

    async def test_get_enrolment_window(self, api, ctx) -> None:
        route = await _get(api, "/agents/enrolment-window")
        tool = await _handle_tool("get_enrolment_window", {}, ctx)
        assert tool == route
        assert tool["open"] is False
        # One agent is enrolled, so the "first host enrols anyway" case is off.
        assert tool["fleet_empty"] is False

    async def test_get_host(self, api, ctx, estate) -> None:
        route = await _get(api, f"/inventory/{estate.host_id}")
        tool = await _handle_tool("get_host", {"host_id": estate.host_id}, ctx)
        assert tool == route
        assert tool["hostname"] == HOSTNAME
        # The agent block is part of the host, and must reach MCP with it.
        assert tool["agent"]["agent_id"] == AGENT_ID
        assert tool["agent"]["version"] == "9.9.9"

    async def test_get_host_rejects_an_unknown_id(self, ctx) -> None:
        with pytest.raises(ValueError, match="Host not found"):
            await _handle_tool("get_host", {"host_id": "no-such-host"}, ctx)

    async def test_list_tasks(self, api, ctx, estate) -> None:
        route = await _get(api, "/tasks")
        tool = await _handle_tool("list_tasks", {}, ctx)
        assert tool == route
        assert tool["total"] == 1
        assert tool["items"][0]["id"] == estate.task_id

    async def test_get_audit_log(self, api, ctx) -> None:
        route = await _get(api, "/audit")
        tool = await _handle_tool("get_audit_log", {}, ctx)
        assert tool == route
        assert tool["total"] == 1
        assert tool["items"][0]["action"] == "artifact_applied"

    async def test_get_dashboard_summary(self, api, ctx) -> None:
        route = await _get(api, "/dashboard/summary")
        tool = await _handle_tool("get_dashboard_summary", {}, ctx)
        assert tool == route
        assert tool["inventory"]["total"] == 1
        assert tool["agents"] == {"known": 1, "connected": 1}
        assert [s["key"] for s in tool["onboarding"]["steps"]] == [
            "inventory",
            "adopt",
            "agent",
            "artifact",
        ]

    async def test_dashboard_summary_carries_what_dashboard_config_serves(self, api, ctx) -> None:
        """/dashboard/config maps to this tool; prove the field really is here."""
        config = await _get(api, "/dashboard/config")
        tool = await _handle_tool("get_dashboard_summary", {}, ctx)
        assert tool["metrics"]["retention_days"] == config["metrics_retention_days"]

    async def test_list_kb(self, api, ctx, estate) -> None:
        route = await _get(api, "/kb")
        tool = await _handle_tool("list_kb", {}, ctx)
        assert tool == route
        assert tool["total"] == 1
        assert tool["items"][0]["title"] == "Parity note"

    async def test_get_kb_doc(self, api, ctx, estate) -> None:
        route = await _get(api, f"/kb/{estate.doc_id}")
        tool = await _handle_tool("get_kb_doc", {"doc_id": estate.doc_id}, ctx)
        assert tool == route
        assert tool["target"] == HOSTNAME

    async def test_get_kb_doc_rejects_an_unknown_id(self, ctx) -> None:
        with pytest.raises(ValueError, match="KB entry not found"):
            await _handle_tool("get_kb_doc", {"doc_id": 987654}, ctx)

    # get_kb_embedding_status was removed: GET /kb/embedding-status is API
    # `admin`, and there is no admin MCP tier yet (see EXCLUDED_GET_ROUTES).

    async def test_list_alert_rules(self, api, ctx, estate) -> None:
        route = await _get(api, "/monitoring/rules")
        tool = await _handle_tool("list_alert_rules", {}, ctx)
        assert tool == route
        assert tool["total"] == 1
        assert tool["items"][0]["id"] == estate.rule_id

    async def test_get_monitoring_alerts(self, api, ctx) -> None:
        route = await _get(api, "/monitoring/alerts")
        tool = await _handle_tool("get_monitoring_alerts", {}, ctx)
        assert tool == route
        assert tool["total"] == 0

    async def test_get_host_metrics(self, api, ctx) -> None:
        route = await _get(api, f"/monitoring/hosts/{HOSTNAME}/latest")
        tool = await _handle_tool("get_host_metrics", {"hostname": HOSTNAME}, ctx)
        assert tool == route
        assert [m["metric"] for m in tool["metrics"]] == ["cpu.percent"]
        assert tool["metrics"][0]["value"] == 13.5

    async def test_get_host_metrics_series(self, api, ctx) -> None:
        route = await _get(
            api,
            f"/monitoring/hosts/{HOSTNAME}/series",
            metric="cpu.percent",
            hours=1,
        )
        tool = await _handle_tool(
            "get_host_metrics_series",
            {"hostname": HOSTNAME, "metric": "cpu.percent", "hours": 1},
            ctx,
        )
        assert tool["points"] == route["points"]
        assert [p["value"] for p in tool["points"]] == [12.5, 13.5]
        assert tool["truncated"] is False

    async def test_series_obeys_the_same_ceilings_as_the_route(self, ctx) -> None:
        """The route's Query bounds are product limits, not framework trivia:
        MCP must not be the way round them."""
        from homepilot.metrics.repository import MAX_SERIES_POINTS
        from homepilot.metrics.router import MAX_WINDOW_HOURS

        with pytest.raises(ValueError, match="hours"):
            await _handle_tool(
                "get_host_metrics_series",
                {"hostname": HOSTNAME, "metric": "cpu.percent", "hours": MAX_WINDOW_HOURS + 1},
                ctx,
            )
        with pytest.raises(ValueError, match="limit"):
            await _handle_tool(
                "get_host_metrics_series",
                {"hostname": HOSTNAME, "metric": "cpu.percent", "limit": MAX_SERIES_POINTS + 1},
                ctx,
            )


class TestFleetDriftIsTheStoredTable:
    async def test_reads_recorded_results_not_a_live_probe(self, ctx, estate) -> None:
        await estate.repo.upsert_drift_check(
            artifact_id="2026-08-01-parity",
            drifted=True,
            state="drifted",
            details_json=json.dumps({"reason": "package missing"}),
        )
        await estate.db.conn.commit()
        tool = await _handle_tool("get_fleet_drift", {}, ctx)
        assert tool["total"] == 1
        assert tool["items"][0]["artifact_id"] == "2026-08-01-parity"
        assert tool["items"][0]["state"] == "drifted"

    async def test_it_is_a_different_tool_from_the_live_single_check(self) -> None:
        """Both exist on purpose: one reads the record, one runs a probe."""
        assert "get_fleet_drift" in _TOOL_HANDLERS
        assert "check_artifact_drift" in _TOOL_HANDLERS
        assert _TOOL_HANDLERS["get_fleet_drift"] is not _TOOL_HANDLERS["check_artifact_drift"]

    async def test_it_cannot_start_a_refresh_cycle(self, ctx) -> None:
        """A refresh runs verification against real hosts. A read tool must not
        be able to ask for that, so the parameter does not exist."""
        from homepilot.mcp.server import _TOOL_DEFINITIONS

        spec = next(t for t in _TOOL_DEFINITIONS if t["name"] == "get_fleet_drift")
        assert "refresh" not in spec["inputSchema"]["properties"]


# ── The secret gate ──────────────────────────────────────────────────────────

_KNOWN_TOKEN = "root@pam!hp-parity=11111111-2222-3333-4444-555555555555"  # nosec B105


class _VaultWithAToken:
    async def get_secret(self, name: str) -> dict[str, Any]:
        if name == "pve-token":
            return {"token": _KNOWN_TOKEN}
        if name == "pve-write-token":
            return {"token": _KNOWN_TOKEN + "-write"}
        if name == "proxmox-config":
            return {"host": "pve.parity.local", "port": 8006, "verify_ssl": False}
        return {}


class TestProxmoxSettingsNeverLeakTheToken:
    async def test_a_configured_token_is_reported_but_never_returned(self, ctx) -> None:
        state = SimpleNamespace(
            settings=SimpleNamespace(proxmox_host="", proxmox_port=8006, proxmox_verify_ssl=True),
            vault=_VaultWithAToken(),
            proxmox=None,
        )
        out = await _handle_tool("get_proxmox_settings", {}, {**ctx, "app_state": state})

        assert out["token_configured"] is True
        assert out["write_token_configured"] is True
        assert out["token_source"] == "vault"
        assert out["host"] == "pve.parity.local"

        serialized = json.dumps(out)
        assert _KNOWN_TOKEN not in serialized, "the Proxmox API token leaked over MCP"
        assert (_KNOWN_TOKEN + "-write") not in serialized, "the write token leaked over MCP"
        # Not just the exact string: no field may carry a PVE token shape at all.
        assert "!" not in serialized.replace("\\", "")

    async def test_it_matches_what_the_admin_route_returns(self, ctx) -> None:
        from homepilot.admin.router import proxmox_settings_report

        state = SimpleNamespace(
            settings=SimpleNamespace(proxmox_host="", proxmox_port=8006, proxmox_verify_ssl=True),
            vault=_VaultWithAToken(),
            proxmox=None,
        )
        tool = await _handle_tool("get_proxmox_settings", {}, {**ctx, "app_state": state})
        assert tool == await proxmox_settings_report(state)


class TestSelfcheckReportsSubsystemsWithoutSecrets:
    async def test_it_returns_the_same_report_the_admin_route_serves(self, ctx) -> None:
        from homepilot.config import Settings
        from homepilot.selfcheck import selfcheck_report

        settings = Settings()
        settings.proxmox_host = ""
        settings.agent_hub_enabled = False
        settings.embedding_service_url = ""
        settings.embedding_fallback_url = ""
        settings.events_webhook_url = ""
        settings.artifacts_remote = ""
        state = SimpleNamespace(
            settings=settings,
            vault=None,
            proxmox=None,
            agent_hub=None,
            repo=ctx["repo"],
            mcp_app=None,
            agent_hub_disabled_reason="",
        )
        tool = await _handle_tool("get_selfcheck", {}, {**ctx, "app_state": state})
        expected = await selfcheck_report(state, settings)
        assert [s["name"] for s in tool["subsystems"]] == [
            s["name"] for s in expected["subsystems"]
        ]
        # Every subsystem states a consequence; that is the product here.
        assert all(entry["consequence"] for entry in tool["subsystems"])


# ── Scope ────────────────────────────────────────────────────────────────────


class TestReadOnlyScopeCanCallEveryReadTool:
    """A read_only MCP token must reach every tool in this wave. The gate drives
    the REAL scope check in _handle_tool, not the frozenset it consults."""

    @pytest.mark.parametrize(
        "name,arguments",
        [
            ("list_agents", {}),
            ("get_agent", {"agent_id": AGENT_ID}),
            ("get_agent_audit", {}),
            ("get_enrolment_window", {}),
            ("list_alert_rules", {}),
            ("get_monitoring_alerts", {}),
            ("get_host_metrics", {"hostname": HOSTNAME}),
            ("get_host_metrics_series", {"hostname": HOSTNAME, "metric": "cpu.percent"}),
            ("list_tasks", {}),
            ("get_audit_log", {}),
            ("get_dashboard_summary", {}),
            ("list_kb", {}),
            ("get_fleet_drift", {}),
        ],
    )
    async def test_read_only_token_is_not_refused(
        self, ctx, name: str, arguments: dict[str, Any]
    ) -> None:
        token = _mcp_token_scope_var.set("read_only")
        try:
            result = await _handle_tool(name, arguments, {**ctx, "_mcp_token_scope": "read_only"})
        finally:
            _mcp_token_scope_var.reset(token)
        assert isinstance(result, dict)

    async def test_the_scope_check_still_has_teeth(self, ctx) -> None:
        """Guard the guard: the same harness must REFUSE a mutating tool."""
        token = _mcp_token_scope_var.set("read_only")
        try:
            with pytest.raises(ValueError, match="requires write scope"):
                await _handle_tool(
                    "record_fact",
                    {"target": "x", "kind": "note", "content": "y"},
                    {**ctx, "_mcp_token_scope": "read_only"},
                )
        finally:
            _mcp_token_scope_var.reset(token)

    async def test_get_host_under_read_only(self, ctx, estate) -> None:
        token = _mcp_token_scope_var.set("read_only")
        try:
            result = await _handle_tool(
                "get_host", {"host_id": estate.host_id}, {**ctx, "_mcp_token_scope": "read_only"}
            )
        finally:
            _mcp_token_scope_var.reset(token)
        assert result["hostname"] == HOSTNAME


class TestBothTransportsCarryTheSameToolContext:
    """The stdio bootstrap and the HTTP mount build the MCP context separately.
    A key present in one and missing in the other is a tool that works over one
    transport and fails over the other - which is how query_guests came to
    report an empty invite list over HTTP."""

    async def test_the_http_mount_supplies_every_key_the_stdio_bootstrap_does(self) -> None:
        import inspect

        from homepilot import main as main_mod
        from homepilot.mcp import server as mcp_mod

        def _keys(source: str) -> set[str]:
            import re

            return set(re.findall(r'^\s+"([a-z_]+)":', source, re.MULTILINE))

        stdio_src = inspect.getsource(mcp_mod._bootstrap)
        stdio_keys = _keys(stdio_src.split("return {")[-1])

        http_src = inspect.getsource(main_mod.lifespan)
        http_block = http_src.split("await _server_context.async_update(")[-1].split("\n        )")[
            0
        ]
        http_keys = _keys(http_block)

        assert stdio_keys, "could not read the stdio context keys - the guard is vacuous"
        assert http_keys, "could not read the HTTP context keys - the guard is vacuous"
        missing = sorted(stdio_keys - http_keys - {"artifacts_dir", "settings"})
        assert not missing, (
            "the HTTP MCP mount does not supply context key(s) the stdio bootstrap "
            f"does, so these tools behave differently per transport: {missing}"
        )
