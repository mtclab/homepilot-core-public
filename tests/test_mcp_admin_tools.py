"""MCP admin-tier tools <-> management API parity (wave 3).

Wave 1 gave the assistant the READ surface, wave 2 the standard `full` mutators;
this wave adds the ADMIN tier and every admin-scoped operation. These tests
assert the property that matters - not "the handler returned ok" but that the
effect ACTUALLY HAPPENED (the rule row appeared, the window opened, the agent's
credential was revoked and its channel closed, the TLS migration refused an
offline fleet) - and that the scope ladder read_only < full < admin has teeth:
an admin tool refuses a `full` token and admits an `admin` one.

The route<->tool coverage map and the tier<->scope invariant live in
``tests/test_mcp_read_parity.py`` (``TestMutationParityGate`` and
``TestMcpTierMatchesApiScope``). This file is the behavioural half.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import _ADMIN_TOOLS, _handle_tool, _mcp_token_scope_var
from homepilot.metrics.repository import MetricsRepository

pytestmark = pytest.mark.asyncio

HOSTNAME = "admin-host"
AGENT_ID = "agent-admin-1"


class _FakeRegistry:
    """A live hub registry that records disconnect/unregister and can report a
    connected set the test dials in."""

    def __init__(self, connected: list[dict[str, Any]] | None = None) -> None:
        self._connected = connected or []
        self.audit_log = MagicMock()
        self.disconnect = MagicMock(return_value=True)
        self.unregister = MagicMock()
        self.hub_server = None

    def list_connected(self) -> list[dict[str, Any]]:
        return list(self._connected)


@pytest.fixture
async def estate(tmp_path: Path):
    db = Database(str(tmp_path / "admin.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    metrics_repo = MetricsRepository(db)
    yield SimpleNamespace(db=db, repo=repo, metrics_repo=metrics_repo)
    await db.close()


@pytest.fixture
def ctx(estate):
    return {
        "repo": estate.repo,
        "database": estate.db,
        "metrics_repo": estate.metrics_repo,
        "_mcp_token_scope": "admin",
        "_mcp_caller_id": "admin-tester",
    }


# ── KB admin ─────────────────────────────────────────────────────────────────


class TestKbAdmin:
    async def test_delete_kb_doc_removes_the_row(self, ctx, estate) -> None:
        doc_id = await estate.repo.create_doc_metadata(
            source="kb:a", title="t", content="c", kind="note", target=HOSTNAME
        )
        out = await _handle_tool("delete_kb_doc", {"doc_id": doc_id}, ctx)
        assert out == {"id": doc_id, "deleted": True}
        assert await estate.repo.get_doc_metadata(doc_id) is None

    async def test_delete_kb_doc_unknown_id(self, ctx) -> None:
        with pytest.raises(ValueError, match="KB entry not found"):
            await _handle_tool("delete_kb_doc", {"doc_id": 987654}, ctx)

    async def test_ingest_reindex_and_status_reach_the_service(self, ctx) -> None:
        svc = MagicMock()
        svc.ingest = AsyncMock(return_value={"created": 2, "skipped": 0, "errors": 0})
        svc.reindex = AsyncMock(return_value={"status": "ok", "reindexed": 3})
        svc.embedding_status = AsyncMock(return_value={"primary_ok": True, "pending": 0})
        c = {**ctx, "kb_service": svc}

        ingest = await _handle_tool("ingest_kb", {"sources": [{"path": "/tmp/x.md"}]}, c)
        assert ingest["created"] == 2
        svc.ingest.assert_awaited_once_with(sources=[{"path": "/tmp/x.md"}])

        reindex = await _handle_tool("reindex_kb", {"no_embeddings": True}, c)
        assert reindex["status"] == "ok"
        svc.reindex.assert_awaited_once_with(
            no_embeddings=True, reason="manual", force_embeddings=False
        )

        status = await _handle_tool("get_kb_embedding_status", {}, c)
        assert status["primary_ok"] is True


# ── Monitoring admin ─────────────────────────────────────────────────────────


class TestAlertRuleAdmin:
    async def test_create_rule_writes_and_returns_it(self, ctx, estate) -> None:
        out = await _handle_tool(
            "create_alert_rule",
            {"name": "cpu", "metric": "cpu.percent", "comparison": "gt", "threshold": 90},
            ctx,
        )
        assert out["metric"] == "cpu.percent"
        stored = await estate.metrics_repo.get_rule(out["id"])
        assert stored is not None and stored["threshold"] == 90.0

    async def test_create_rule_rejects_a_bad_comparison(self, ctx) -> None:
        with pytest.raises(ValueError, match="comparison"):
            await _handle_tool(
                "create_alert_rule",
                {"name": "x", "metric": "m", "comparison": "between", "threshold": 1},
                ctx,
            )

    async def test_update_rule_silences_it(self, ctx, estate) -> None:
        rule = await estate.metrics_repo.create_rule(
            name="r", metric="cpu.percent", comparison="gt", threshold=1.0
        )
        out = await _handle_tool(
            "update_alert_rule", {"rule_id": rule["id"], "enabled": False}, ctx
        )
        assert out["enabled"] in (0, False)
        assert (await estate.metrics_repo.get_rule(rule["id"]))["enabled"] in (0, False)

    async def test_update_rule_retunes_threshold(self, ctx, estate) -> None:
        # The #593 bug: update_alert_rule only toggled enabled, so an operator
        # could not retune a threshold - they had to delete and recreate. Assert
        # the new threshold is actually stored and read back.
        rule = await estate.metrics_repo.create_rule(
            name="r", metric="cpu.percent", comparison="gt", threshold=1.0
        )
        out = await _handle_tool(
            "update_alert_rule",
            {"rule_id": rule["id"], "threshold": 88.0, "comparison": "gte", "for_seconds": 120},
            ctx,
        )
        assert out["threshold"] == 88.0
        assert out["comparison"] == "gte"
        stored = await estate.metrics_repo.get_rule(rule["id"])
        assert stored["threshold"] == 88.0
        assert stored["for_seconds"] == 120
        # A field not passed is left as it was, not nulled.
        assert stored["metric"] == "cpu.percent"
        assert stored["enabled"] in (1, True)

    async def test_update_rule_without_enabled_does_not_crash(self, ctx, estate) -> None:
        # #593: `bool(arguments["enabled"])` raised a raw KeyError when enabled
        # was omitted. Omitting it (and every other field) must be a no-op that
        # returns the unchanged rule, not a crash.
        rule = await estate.metrics_repo.create_rule(
            name="r", metric="cpu.percent", comparison="gt", threshold=5.0
        )
        out = await _handle_tool("update_alert_rule", {"rule_id": rule["id"]}, ctx)
        assert out["threshold"] == 5.0
        assert out["enabled"] in (1, True)

    async def test_update_rule_unknown_id(self, ctx) -> None:
        with pytest.raises(ValueError, match="Alert rule not found"):
            await _handle_tool("update_alert_rule", {"rule_id": "nope", "enabled": True}, ctx)

    async def test_delete_rule_removes_it(self, ctx, estate) -> None:
        rule = await estate.metrics_repo.create_rule(
            name="r", metric="cpu.percent", comparison="gt", threshold=1.0
        )
        out = await _handle_tool("delete_alert_rule", {"rule_id": rule["id"]}, ctx)
        assert out == {"id": rule["id"], "deleted": True}
        assert await estate.metrics_repo.get_rule(rule["id"]) is None

    async def test_delete_rule_unknown_id(self, ctx) -> None:
        with pytest.raises(ValueError, match="Alert rule not found"):
            await _handle_tool("delete_alert_rule", {"rule_id": "nope"}, ctx)


# ── Enrolment window (security-relaxing: the real row must flip) ──────────────


class TestEnrolmentWindowAdmin:
    async def test_open_sets_the_row_and_close_clears_it(self, ctx, estate) -> None:
        from homepilot.agent_hub import enrolment_window

        registry = _FakeRegistry()
        c = {**ctx, "agent_registry": registry}

        opened = await _handle_tool("open_enrolment_window", {"minutes": 30}, c)
        assert opened["open"] is True
        assert await enrolment_window.is_open(estate.repo) is True

        closed = await _handle_tool("close_enrolment_window", {}, c)
        assert closed["open"] is False
        assert await enrolment_window.is_open(estate.repo) is False

    async def test_open_rejects_a_window_longer_than_a_day(self, ctx, estate) -> None:
        with pytest.raises(ValueError, match="minutes"):
            await _handle_tool(
                "open_enrolment_window",
                {"minutes": 60 * 25},
                {**ctx, "agent_registry": _FakeRegistry()},
            )

    async def test_open_is_audited_with_the_caller(self, ctx, estate) -> None:
        registry = _FakeRegistry()
        await _handle_tool(
            "open_enrolment_window", {"minutes": 5}, {**ctx, "agent_registry": registry}
        )
        assert registry.audit_log.log.called
        kwargs = registry.audit_log.log.call_args.kwargs
        assert kwargs["action"] == "enrolment_window_opened"
        assert kwargs["caller"] == "admin-tester"


# ── Agent revoke / forget (security: credential dead + channel closed) ────────


async def _seed_credentialed_agent(repo: Repository) -> None:
    await repo.db.execute(
        "INSERT INTO agents (agent_id, hostname, connected, credential_hash, credential_set_at) "
        "VALUES (?, ?, 0, 'deadbeef', ?)",
        (AGENT_ID, HOSTNAME, "2026-08-01T00:00:00Z"),
    )
    await repo.db.conn.commit()


class TestAgentRevokeForget:
    async def test_revoke_kills_the_credential_and_closes_the_channel(self, ctx, estate) -> None:
        await _seed_credentialed_agent(estate.repo)
        registry = _FakeRegistry()
        out = await _handle_tool(
            "revoke_agent", {"agent_id": AGENT_ID}, {**ctx, "agent_registry": registry}
        )
        assert out["revoked"] is True
        assert out["channel_closed"] is True
        # The live channel was closed (the #430 semantics), and closed AFTER the
        # credential died - the registry disconnect ran.
        registry.disconnect.assert_called_once()
        # The credential is really revoked: a second revoke finds nothing active.
        assert await estate.repo.revoke_agent_credential(AGENT_ID) is False

    async def test_revoke_unknown_agent(self, ctx, estate) -> None:
        with pytest.raises(ValueError, match="No active credential"):
            await _handle_tool(
                "revoke_agent", {"agent_id": "ghost"}, {**ctx, "agent_registry": _FakeRegistry()}
            )

    async def test_forget_deletes_the_row(self, ctx, estate) -> None:
        await _seed_credentialed_agent(estate.repo)
        registry = _FakeRegistry(connected=[])  # not connected
        out = await _handle_tool(
            "forget_agent", {"agent_id": AGENT_ID}, {**ctx, "agent_registry": registry}
        )
        assert out == {"agent_id": AGENT_ID, "forgotten": True}
        rows = await estate.repo.list_agents()
        assert all(r["agent_id"] != AGENT_ID for r in rows)

    async def test_forget_refuses_a_connected_agent(self, ctx, estate) -> None:
        await _seed_credentialed_agent(estate.repo)
        registry = _FakeRegistry(connected=[{"agent_id": AGENT_ID}])
        with pytest.raises(ValueError, match="connected right now"):
            await _handle_tool(
                "forget_agent", {"agent_id": AGENT_ID}, {**ctx, "agent_registry": registry}
            )
        # Refused BEFORE touching the row.
        assert any(r["agent_id"] == AGENT_ID for r in await estate.repo.list_agents())

    async def test_forget_unknown_agent(self, ctx, estate) -> None:
        with pytest.raises(ValueError, match="not found"):
            await _handle_tool(
                "forget_agent", {"agent_id": "ghost"}, {**ctx, "agent_registry": _FakeRegistry()}
            )


# ── TLS migration (mirror the API's 409-on-offline) ──────────────────────────


class TestMigrateTls:
    async def test_refuses_an_offline_fleet_without_force(self, ctx, estate) -> None:
        await _seed_credentialed_agent(estate.repo)  # enrolled, not connected
        registry = _FakeRegistry(connected=[])
        registry.hub_server = SimpleNamespace(tls_enabled=False)
        # The offline refusal fires before any certificate is generated, so
        # data_dir is never touched here.
        settings = SimpleNamespace(data_dir="/tmp", agent_hub_advertise_host="", agent_hub_host="")
        with pytest.raises(ValueError, match="not connected"):
            await _handle_tool(
                "migrate_agents_tls",
                {"force": False},
                {**ctx, "agent_registry": registry, "settings": settings},
            )

    async def test_refuses_when_the_hub_already_serves_tls(self, ctx, estate) -> None:
        registry = _FakeRegistry(connected=[])
        registry.hub_server = SimpleNamespace(tls_enabled=True)
        settings = SimpleNamespace(data_dir="/tmp", agent_hub_advertise_host="", agent_hub_host="")
        with pytest.raises(ValueError, match="already serving TLS"):
            await _handle_tool(
                "migrate_agents_tls",
                {},
                {**ctx, "agent_registry": registry, "settings": settings},
            )


# ── Host exec / write-file: go through the agent path, guard intact ──────────


class _FakeHub:
    def __init__(self) -> None:
        self.registry = MagicMock()
        agent = SimpleNamespace(agent_id=AGENT_ID, hostname=HOSTNAME)
        self.registry.get_by_hostname = MagicMock(
            side_effect=lambda h: agent if h == HOSTNAME else None
        )
        self.send_command = AsyncMock(return_value={"exit_code": 0, "stdout": "ok", "stderr": ""})
        self.send_write_file = AsyncMock(return_value={"written": True})
        self.send_read_file = AsyncMock(return_value={"content": ""})


class TestHostExecAndWrite:
    def _adapter(self):
        from homepilot.adapters.agent import AgentAdapter

        return AgentAdapter(hub_server=_FakeHub(), pve_nodes=["pve1"])

    async def test_exec_on_host_runs_through_the_agent_command_path(self, ctx) -> None:
        adapter = self._adapter()
        out = await _handle_tool(
            "exec_on_host",
            {"host": HOSTNAME, "command": "systemctl restart nginx"},
            {**ctx, "agent_adapter": adapter},
        )
        assert out == {"exit_code": 0, "stdout": "ok", "stderr": ""}
        adapter._hub.send_command.assert_awaited_once()

    async def test_exec_on_host_keeps_the_pve_node_guard(self, ctx) -> None:
        """The guard is NOT bypassed: a PVE hypervisor node is still refused."""
        from homepilot.adapters.agent import GuestHostError

        adapter = self._adapter()
        with pytest.raises(GuestHostError):
            await _handle_tool(
                "exec_on_host",
                {"host": "pve1", "command": "ls"},
                {**ctx, "agent_adapter": adapter},
            )
        adapter._hub.send_command.assert_not_awaited()

    async def test_write_file_on_host_goes_through_send_write_file(self, ctx) -> None:
        adapter = self._adapter()
        out = await _handle_tool(
            "write_file_on_host",
            {"host": HOSTNAME, "path": "/etc/homepilot/agent.env", "content": "X=1"},
            {**ctx, "agent_adapter": adapter},
        )
        assert out["changed"] is True
        adapter._hub.send_write_file.assert_awaited_once()

    async def test_write_file_on_host_keeps_the_pve_node_guard(self, ctx) -> None:
        from homepilot.adapters.agent import GuestHostError

        adapter = self._adapter()
        with pytest.raises(GuestHostError):
            await _handle_tool(
                "write_file_on_host",
                {"host": "pve1", "path": "/x", "content": "y"},
                {**ctx, "agent_adapter": adapter},
            )
        adapter._hub.send_write_file.assert_not_awaited()


# ── Admin settings / credentials ─────────────────────────────────────────────


class TestProxmoxAndTokenAdmin:
    async def test_test_proxmox_connection_reports_not_configured(self, ctx) -> None:
        state = SimpleNamespace(
            settings=SimpleNamespace(proxmox_host="", proxmox_port=8006, proxmox_verify_ssl=True),
            vault=None,
            proxmox=None,
        )
        out = await _handle_tool("test_proxmox_connection", {}, {**ctx, "app_state": state})
        assert out["status"] == "error"
        assert "host" in out["message"].lower()

    async def test_delete_auth_token_revokes_it(self, ctx, estate) -> None:
        from homepilot.auth.tokens import generate_api_token

        user_id = await estate.repo.create_user("admin", "a@example.com")
        _tok, prefix, token_hash = generate_api_token()
        await estate.repo.create_api_token(
            user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
        )
        out = await _handle_tool("delete_auth_token", {"prefix": prefix}, ctx)
        assert out == {"prefix": prefix, "deleted": True}
        assert await estate.repo.get_token_by_prefix(prefix) is None

    async def test_delete_auth_token_unknown_prefix(self, ctx) -> None:
        with pytest.raises(ValueError, match="Token not found"):
            await _handle_tool("delete_auth_token", {"prefix": "hp_nope"}, ctx)


# ── Guest provisioning (owner decision 2026-08-25): admin tier ───────────────


class TestProvisionGuest:
    def _service(self):
        """A ProvisionService stand-in with the PVE boundary mocked: proxmox is
        present (so the route's 503 guard passes) and start returns a task id."""
        service = MagicMock()
        service.proxmox = MagicMock()
        service.start = AsyncMock(return_value="task-prov-123")
        # An explicit None, not a MagicMock attribute: a mock here reads back as
        # a mock RESOLVER, and every provisioning default then resolves to the
        # repr of a MagicMock. That already put a nonsense pool on the request
        # this test asserts about (it went unnoticed because pool has no shape
        # rule); the storage field's validator (#618) is what surfaced it. None
        # means "this process has no defaults", which is what a bare service is.
        service.defaults_source = None
        return service

    async def test_starts_a_provision_task_via_the_service(self, ctx) -> None:
        from homepilot.provision.models import ProvisionRequest

        service = self._service()
        out = await _handle_tool(
            "provision_guest",
            {"name": "web01", "node": "pve1", "template_vmid": 9000, "cores": 2},
            {**ctx, "provision_service": service, "_mcp_caller_id": "prov-tester"},
        )
        assert out == {"task_id": "task-prov-123", "status": "pending"}
        # The SAME service the route calls was invoked with a validated request.
        service.start.assert_awaited_once()
        sent = service.start.await_args.args[0]
        assert isinstance(sent, ProvisionRequest)
        assert sent.name == "web01" and sent.node == "pve1" and sent.template_vmid == 9000
        assert service.start.await_args.kwargs["actor"] == "prov-tester"

    async def test_refuses_when_proxmox_is_not_configured(self, ctx) -> None:
        service = MagicMock()
        service.proxmox = None
        with pytest.raises(ValueError, match="Proxmox not configured"):
            await _handle_tool(
                "provision_guest",
                {"name": "web01", "node": "pve1", "template_vmid": 9000},
                {**ctx, "provision_service": service},
            )

    async def test_invalid_request_is_refused_by_the_model(self, ctx) -> None:
        """Validation is the ProvisionRequest model's, not re-implemented: an
        illegal name is refused before the service is ever called."""
        service = self._service()
        with pytest.raises(ValueError, match="Invalid provision request"):
            await _handle_tool(
                "provision_guest",
                {"name": "BAD NAME!", "node": "pve1", "template_vmid": 9000},
                {**ctx, "provision_service": service},
            )
        service.start.assert_not_awaited()

    async def test_an_in_flight_name_conflict_is_surfaced(self, ctx) -> None:
        from homepilot.provision.service import ProvisionConflictError

        service = self._service()
        service.start = AsyncMock(side_effect=ProvisionConflictError("already in flight"))
        with pytest.raises(ValueError, match="already in flight"):
            await _handle_tool(
                "provision_guest",
                {"name": "web01", "node": "pve1", "template_vmid": 9000},
                {**ctx, "provision_service": service},
            )

    async def test_provision_guest_is_admin_not_full(self) -> None:
        from homepilot.mcp.server import _ADMIN_TOOLS, _MUTATING_TOOLS

        assert "provision_guest" in _ADMIN_TOOLS
        assert "provision_guest" not in _MUTATING_TOOLS

    async def test_a_full_token_is_denied_provision(self, ctx) -> None:
        """Teeth: a `full` token cannot provision - admin is required - and the
        scope check fires BEFORE the service, so start is never reached."""
        service = self._service()
        token = _mcp_token_scope_var.set("full")
        try:
            with pytest.raises(ValueError, match="needs the admin tier"):
                await _handle_tool(
                    "provision_guest",
                    {"name": "web01", "node": "pve1", "template_vmid": 9000},
                    {**ctx, "_mcp_token_scope": "full", "provision_service": service},
                )
        finally:
            _mcp_token_scope_var.reset(token)
        service.start.assert_not_awaited()


# ── Artifacts: revoke is `full`, not admin ───────────────────────────────────


class TestRevokeArtifactIsFull:
    async def test_revoke_of_a_proposed_artifact_is_refused(self, ctx, estate) -> None:
        from homepilot.tasks.repository import TaskRepository
        from homepilot.tasks.runner import TaskRunner

        store = MagicMock()
        store.read = MagicMock(
            return_value=({"id": "a1", "kind": "shell-script", "status": "proposed"}, "body")
        )
        task_repo = TaskRepository(estate.db)
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=store,
        )
        with pytest.raises(ValueError, match="Invalid transition"):
            await _handle_tool(
                "revoke_artifact",
                {"artifact_id": "a1"},
                {**ctx, "_mcp_token_scope": "full", "task_runner": runner},
            )
        assert await task_repo.list_tasks("a1", limit=10) == []

    async def test_revoke_of_an_applied_artifact_starts_a_task(self, ctx, estate) -> None:
        from homepilot.tasks.repository import TaskRepository
        from homepilot.tasks.runner import TaskRunner

        store = MagicMock()
        store.read = MagicMock(
            return_value=({"id": "a1", "kind": "shell-script", "status": "applied"}, "body")
        )
        task_repo = TaskRepository(estate.db)
        lifecycle = MagicMock()
        lifecycle.mark_applied = AsyncMock()
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=store,
        )
        out = await _handle_tool(
            "revoke_artifact",
            {"artifact_id": "a1"},
            {**ctx, "_mcp_token_scope": "full", "task_runner": runner},
        )
        assert out["action"] == "revoke"
        tasks = await task_repo.list_tasks("a1", limit=10)
        assert len(tasks) == 1 and tasks[0]["action"] == "revoke"

    async def test_revoke_artifact_is_full_not_admin(self) -> None:
        from homepilot.mcp.server import _ADMIN_TOOLS, _MUTATING_TOOLS

        assert "revoke_artifact" in _MUTATING_TOOLS
        assert "revoke_artifact" not in _ADMIN_TOOLS


# ── The scope ladder: read_only < full < admin, teeth in both directions ─────


# Representative admin tools spanning KB, monitoring, agents and settings, each
# with arguments that reach the scope check (which runs before the handler).
_ADMIN_TOOL_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("delete_kb_doc", {"doc_id": 1}),
    ("reindex_kb", {}),
    ("create_alert_rule", {"name": "n", "metric": "m", "comparison": "gt", "threshold": 1}),
    ("delete_alert_rule", {"rule_id": "x"}),
    ("open_enrolment_window", {"minutes": 5}),
    ("close_enrolment_window", {}),
    ("revoke_agent", {"agent_id": "x"}),
    ("forget_agent", {"agent_id": "x"}),
    ("migrate_agents_tls", {}),
    ("exec_on_host", {"host": "h", "command": "ls"}),
    ("write_file_on_host", {"host": "h", "path": "/p", "content": "c"}),
    ("test_proxmox_connection", {}),
    ("delete_auth_token", {"prefix": "p"}),
    ("query_guests", {}),
    ("set_guest_quota", {"cn": "x"}),
    ("delete_guest_quota", {"cn": "x"}),
    ("revoke_guest_invite", {"prefix": "p"}),
    ("provision_guest", {"name": "g1", "node": "pve1", "template_vmid": 9000}),
    (
        "create_guest_template",
        {"node": "pve1", "template_vmid": 9000, "source_volid": "local:import/u.qcow2"},
    ),
    ("get_kb_embedding_status", {}),
]


class TestScopeLadder:
    async def test_every_admin_call_targets_a_real_admin_tool(self) -> None:
        """Guard the guard: a typo below would make its denial vacuous."""
        for name, _ in _ADMIN_TOOL_CALLS:
            assert name in _ADMIN_TOOLS, f"{name} is not an admin tool"

    @pytest.mark.parametrize("name,arguments", _ADMIN_TOOL_CALLS)
    async def test_a_full_token_is_denied_every_admin_tool(
        self, ctx, name: str, arguments: dict[str, Any]
    ) -> None:
        token = _mcp_token_scope_var.set("full")
        try:
            with pytest.raises(ValueError, match="needs the admin tier"):
                await _handle_tool(name, arguments, {**ctx, "_mcp_token_scope": "full"})
        finally:
            _mcp_token_scope_var.reset(token)

    @pytest.mark.parametrize("name,arguments", _ADMIN_TOOL_CALLS)
    async def test_a_read_only_token_is_denied_every_admin_tool(
        self, ctx, name: str, arguments: dict[str, Any]
    ) -> None:
        token = _mcp_token_scope_var.set("read_only")
        try:
            with pytest.raises(ValueError, match="needs the admin tier"):
                await _handle_tool(name, arguments, {**ctx, "_mcp_token_scope": "read_only"})
        finally:
            _mcp_token_scope_var.reset(token)

    async def test_an_admin_token_passes_the_scope_check(self, ctx, estate) -> None:
        """The teeth for the deny above: the SAME harness must NOT refuse an admin
        token on scope. Use delete_alert_rule - it gets past the scope gate and
        fails only on the missing rule, which proves the scope check let it
        through rather than a blanket refusal."""
        token = _mcp_token_scope_var.set("admin")
        try:
            with pytest.raises(ValueError, match="Alert rule not found"):
                await _handle_tool(
                    "delete_alert_rule",
                    {"rule_id": "no-such-rule"},
                    {**ctx, "_mcp_token_scope": "admin"},
                )
        finally:
            _mcp_token_scope_var.reset(token)

    async def test_a_full_token_still_reaches_full_mutators(self, ctx, estate) -> None:
        """The admin gate must not have broken the `full` tier: a full mutator
        (revoke_artifact) still passes the scope check for a full token."""
        from homepilot.tasks.repository import TaskRepository
        from homepilot.tasks.runner import TaskRunner

        store = MagicMock()
        store.read = MagicMock(
            return_value=({"id": "a1", "kind": "shell-script", "status": "proposed"}, "body")
        )
        runner = TaskRunner(
            repo=TaskRepository(estate.db),
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=store,
        )
        token = _mcp_token_scope_var.set("full")
        try:
            # Past the scope gate: it fails on the transition, not on scope.
            with pytest.raises(ValueError, match="Invalid transition"):
                await _handle_tool(
                    "revoke_artifact",
                    {"artifact_id": "a1"},
                    {**ctx, "_mcp_token_scope": "full", "task_runner": runner},
                )
        finally:
            _mcp_token_scope_var.reset(token)
