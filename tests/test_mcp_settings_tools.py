"""Operator settings reach MCP, at the admin tier and no lower (#553 C4).

THE DEFECTS THIS FORBIDS:

* a setting written over MCP that the running process never reads - the tool
  says "ok" and the reconciler keeps the boot value forever. The journey gate
  drives a REAL scheduler, exactly as the C2 gate drives it through the API;
* a secret becoming reachable because a second surface was built on the
  registry and forgot the rule - the walk below tries every FORBIDDEN key and a
  sample of the secret ``Settings`` fields through all four tools;
* an escalation: a read_only or full MCP token doing what the API reserves for
  admin. Every route behind these tools is ``require_scope("admin")``;
* a refusal that MCP softens - a value the cluster refutes, or a key the
  environment decides, must be refused in the SAME words the API uses, with
  nothing stored.

The route<->tool coverage and the tier<->scope invariant live in
``tests/test_mcp_read_parity.py``; this file is the behavioural half.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot import app_settings
from homepilot.admin.router import _require_admin_dep
from homepilot.admin.router import router as admin_router
from homepilot.app_settings import (
    DB_KEY_PREFIX,
    FORBIDDEN_KEYS,
    REGISTRY,
    SettingsResolver,
)
from homepilot.config import Settings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import (
    _ADMIN_TOOLS,
    _MUTATING_TOOLS,
    _READ_ONLY_TOOLS,
    _handle_tool,
    _mcp_token_scope_var,
)

# The SAME fake cluster the C3 gates drive, imported rather than re-declared so
# the two files cannot drift into disagreeing about what a PVE reply looks like.
from .test_provisioning_defaults import FakeCluster

pytestmark = pytest.mark.asyncio

C4_TOOLS = (
    "query_settings_overrides",
    "set_setting_override",
    "clear_setting_override",
    "probe_setting_override",
)


def _settings(**overrides: Any) -> Settings:
    return Settings(
        data_dir="/tmp/hp-c4-test", artifacts_dir="/tmp/hp-c4-test/artifacts", **overrides
    )


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "c4.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield Repository(db)
    finally:
        await db.close()


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()


@pytest.fixture
def ctx(repo, cluster) -> dict[str, Any]:
    """The MCP tool context, with an admin token - the same keys both transports
    put in it."""
    return {
        "repo": repo,
        "settings": _settings(),
        "proxmox": cluster,
        "_mcp_token_scope": "admin",
        "_mcp_caller_id": "c4-tester",
    }


async def _stored(repo: Repository, key: str) -> str | None:
    row = await repo.get_setting(DB_KEY_PREFIX + key)
    return None if row is None else str(row["value"])


async def _call(tool: str, arguments: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """Through the REAL dispatcher, so every call pays the scope check."""
    return await _handle_tool(tool, arguments, ctx)


# ── The design's gate: the journey, over MCP ─────────────────────────────────


class TestTheJourney:
    """Set the archive push interval over admin MCP; the RUNNING scheduler uses
    the new one for its next cycle.

    The C2 gate proves this through the HTTP surface. This is the same journey
    through the other surface: nothing is faked but the poll slice, and the
    assertion is the SHAPE a working reschedule produces - a loop idle on an
    hourly interval starts cycling once the interval is cut to a second.
    """

    async def test_an_interval_set_over_mcp_reschedules_the_running_loop(
        self, ctx, repo, monkeypatch
    ) -> None:
        from homepilot.reconciler import base as base_mod
        from homepilot.reconciler.base import Reconciler, ReconcilerResult, ReconcilerScheduler

        monkeypatch.setattr(base_mod, "INTERVAL_POLL_SECONDS", 0.02)

        resolver = SettingsResolver(repo, ctx["settings"])
        await resolver.set("artifacts_push_interval_seconds", 3600)

        runs = 0

        class Counting(Reconciler):
            async def run(self) -> ReconcilerResult:
                nonlocal runs
                runs += 1
                return ReconcilerResult(name="counting", success=True)

        scheduler = ReconcilerScheduler()
        scheduler.register(
            Counting(),
            interval=lambda: app_settings.resolve_interval(
                resolver, "artifacts_push_interval_seconds", 3600.0
            ),
        )
        await scheduler.start()
        try:
            await asyncio.sleep(0.2)
            assert runs == 1, (
                f"an hourly loop cycled {runs} times - it is not honouring its interval"
            )

            out = await _call(
                "set_setting_override",
                {"key": "artifacts_push_interval_seconds", "value": 1},
                ctx,
            )
            assert out["status"] == "ok" and out["value"] == 1 and out["source"] == "db"

            deadline = time.monotonic() + 6.0
            while runs < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            assert runs >= 2, (
                "the scheduler never ran again after the interval was cut from an hour "
                "to a second over MCP - the tool reported success but nothing reached "
                "the running loop"
            )
        finally:
            await scheduler.stop()

    async def test_clearing_it_over_mcp_puts_the_default_back(self, ctx, repo) -> None:
        await _call(
            "set_setting_override", {"key": "artifacts_push_interval_seconds", "value": 5}, ctx
        )
        assert await _stored(repo, "artifacts_push_interval_seconds") == "5"

        out = await _call("clear_setting_override", {"key": "artifacts_push_interval_seconds"}, ctx)

        assert out["source"] == "default"
        assert out["value"] == ctx["settings"].artifacts_push_interval_seconds
        assert await _stored(repo, "artifacts_push_interval_seconds") is None
        # And the resolver a live consumer would ask agrees.
        resolver = SettingsResolver(repo, ctx["settings"])
        assert (await resolver.resolve("artifacts_push_interval_seconds")).source == "default"


# ── Both surfaces describe the same wiring ───────────────────────────────────


class TestTheSameAnswerAsTheApi:
    async def test_the_tool_report_equals_the_routes_report(self, ctx, repo, cluster) -> None:
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        app.state.repo = repo
        app.state.settings = ctx["settings"]
        app.state.settings_resolver = SettingsResolver(repo, ctx["settings"])
        app.state.proxmox = cluster
        app.dependency_overrides[_require_admin_dep.dependency] = lambda: {
            "user_id": 1,
            "scope": "*",
            "role": "admin",
        }

        await _call("set_setting_override", {"key": "retention_days", "value": 17}, ctx)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            route_report = (await client.get("/admin/settings/overrides")).json()["settings"]

        tool_report = (await _call("query_settings_overrides", {}, ctx))["settings"]

        assert tool_report == route_report
        by_key = {entry["key"]: entry for entry in tool_report}
        assert set(by_key) == set(REGISTRY)
        assert by_key["retention_days"]["value"] == 17
        assert by_key["retention_days"]["source"] == "db"


# ── Secrets are not reachable here ───────────────────────────────────────────


class TestSecretsAreNotReachable:
    """The registry holds no secret, so every one of these is refused as an
    unknown setting rather than by a filter someone has to remember."""

    # Secret-bearing Settings fields; every one is a real attribute of Settings
    # (asserted below, so a rename cannot quietly empty this sample).
    _SECRET_FIELDS = (
        "admin_secret",
        "events_webhook_secret",
        "vault_passphrase",
        "agent_hub_auth_token",
        "n8n_api_key",
    )

    def _keys(self) -> list[str]:
        return sorted(set(FORBIDDEN_KEYS) | set(self._SECRET_FIELDS))

    async def test_the_sample_names_real_settings_fields(self) -> None:
        """Guard the guard: a walk over names that are not settings at all would
        prove nothing."""
        fields = set(Settings.model_fields)
        missing = [name for name in self._SECRET_FIELDS if name not in fields]
        assert not missing, f"the secret sample names non-existent Settings fields: {missing}"

    async def test_no_secret_can_be_set_over_mcp(self, ctx, repo) -> None:
        for key in self._keys():
            with pytest.raises(ValueError, match="unknown setting"):
                await _call("set_setting_override", {"key": key, "value": "nope"}, ctx)
            assert await _stored(repo, key) is None, f"{key} was written"

    async def test_no_secret_can_be_cleared_or_probed_over_mcp(self, ctx) -> None:
        for key in self._keys():
            with pytest.raises(ValueError, match="unknown setting"):
                await _call("clear_setting_override", {"key": key}, ctx)
            with pytest.raises(ValueError, match="unknown setting"):
                await _call("probe_setting_override", {"key": key, "value": "nope"}, ctx)

    async def test_no_secret_appears_in_the_report(self, ctx) -> None:
        report = (await _call("query_settings_overrides", {}, ctx))["settings"]
        listed = {entry["key"] for entry in report}
        assert not (listed & set(self._keys()))

    async def test_a_configured_secret_value_is_nowhere_in_the_report(self, repo, cluster) -> None:
        """The other direction: a secret that IS configured must not leak into the
        report through some field that happens to carry it."""
        secret = "s3cr3t-c4-signing-key"
        settings = _settings(events_webhook_secret=secret, admin_secret=secret)
        ctx = {
            "repo": repo,
            "settings": settings,
            "proxmox": cluster,
            "_mcp_token_scope": "admin",
        }
        report = await _call("query_settings_overrides", {}, ctx)
        assert secret not in repr(report)


# ── The scope ladder ─────────────────────────────────────────────────────────


class TestScope:
    async def test_all_four_tools_sit_at_the_admin_tier(self) -> None:
        assert set(C4_TOOLS) <= _ADMIN_TOOLS
        assert not (set(C4_TOOLS) & _MUTATING_TOOLS)
        assert not (set(C4_TOOLS) & _READ_ONLY_TOOLS)

    async def test_the_tier_gate_needed_no_new_exemption(self) -> None:
        """C4 must not buy its way past the spine gate: the only tier exemption
        in the estate is still the deliberately read-classified refresh."""
        from .test_mcp_read_parity import _TIER_EXEMPTIONS

        assert set(_TIER_EXEMPTIONS) == {"refresh_inventory"}

    @pytest.mark.parametrize("scope", ["read_only", "full"])
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("query_settings_overrides", {}),
            ("set_setting_override", {"key": "retention_days", "value": 7}),
            ("clear_setting_override", {"key": "retention_days"}),
            ("probe_setting_override", {"key": "provision_default_node", "value": "pve1"}),
        ],
    )
    async def test_a_lesser_token_is_refused_and_nothing_happens(
        self, ctx, repo, cluster, scope, tool, arguments
    ) -> None:
        token = _mcp_token_scope_var.set(scope)
        try:
            with pytest.raises(ValueError, match="needs the admin tier"):
                await _call(tool, arguments, {**ctx, "_mcp_token_scope": scope})
        finally:
            _mcp_token_scope_var.reset(token)
        # The refusal fires BEFORE the handler: nothing stored, cluster never asked.
        assert await _stored(repo, "retention_days") is None
        assert cluster.reads == []

    async def test_an_admin_token_passes_the_same_call(self, ctx) -> None:
        """Guard the guard: the refusals above must be about the tier, not about
        a call that fails for everyone."""
        out = await _call("query_settings_overrides", {}, {**ctx, "_mcp_token_scope": "admin"})
        assert isinstance(out, dict) and out["settings"]


# ── The refusals, in the API's words ─────────────────────────────────────────


class TestTheRefusalsAreTheApis:
    async def test_a_cluster_refused_bridge_stores_nothing_and_repeats_the_cluster(
        self, ctx, repo
    ) -> None:
        await _call("set_setting_override", {"key": "provision_default_node", "value": "pve1"}, ctx)

        with pytest.raises(ValueError) as exc:
            await _call(
                "set_setting_override", {"key": "provision_default_bridge", "value": "vmbr7"}, ctx
            )

        assert str(exc.value) == "no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1"
        assert await _stored(repo, "provision_default_bridge") is None

    async def test_a_good_bridge_is_saved_with_what_the_cluster_said(self, ctx, repo) -> None:
        await _call("set_setting_override", {"key": "provision_default_node", "value": "pve1"}, ctx)

        out = await _call(
            "set_setting_override", {"key": "provision_default_bridge", "value": "vmbr0"}, ctx
        )

        assert out["probe"] == {"ok": True, "detail": "Bridge vmbr0 is on node pve1."}
        assert await _stored(repo, "provision_default_bridge") == "vmbr0"

    async def test_an_unreachable_cluster_saves_nothing(self, ctx, repo, cluster) -> None:
        cluster.down = True

        with pytest.raises(ValueError, match="could not be asked"):
            await _call(
                "set_setting_override", {"key": "provision_default_node", "value": "pve1"}, ctx
            )

        assert await _stored(repo, "provision_default_node") is None

    async def test_an_env_locked_key_is_refused_in_the_apis_words(
        self, repo, cluster, monkeypatch
    ) -> None:
        monkeypatch.setenv("HP_ARTIFACTS_REMOTE", "git@env.example:me/archive.git")
        ctx = {
            "repo": repo,
            "settings": _settings(),
            "proxmox": cluster,
            "_mcp_token_scope": "admin",
        }

        with pytest.raises(ValueError) as exc:
            await _call(
                "set_setting_override", {"key": "artifacts_remote", "value": "git@ui:x"}, ctx
            )

        detail = str(exc.value)
        assert "HP_ARTIFACTS_REMOTE" in detail and "records nothing" in detail
        assert await _stored(repo, "artifacts_remote") is None

        # Clearing is the same write in the other direction, refused the same way.
        with pytest.raises(ValueError, match="records nothing"):
            await _call("clear_setting_override", {"key": "artifacts_remote"}, ctx)

        entry = next(
            e
            for e in (await _call("query_settings_overrides", {}, ctx))["settings"]
            if e["key"] == "artifacts_remote"
        )
        assert entry["source"] == "env" and entry["editable"] is False

    async def test_a_value_of_the_wrong_shape_is_refused_before_any_probe(
        self, ctx, repo, cluster
    ) -> None:
        with pytest.raises(ValueError, match="not a PVE ipconfig0"):
            await _call(
                "set_setting_override",
                {"key": "provision_default_ipconfig", "value": "dhcp please"},
                ctx,
            )
        assert await _stored(repo, "provision_default_ipconfig") is None
        assert cluster.reads == []


# ── The probe asks and never saves ───────────────────────────────────────────


class TestTheProbeIsReadOnly:
    async def test_it_returns_the_refusal_without_storing_anything(self, ctx, repo) -> None:
        await _call("set_setting_override", {"key": "provision_default_node", "value": "pve1"}, ctx)

        out = await _call(
            "probe_setting_override", {"key": "provision_default_bridge", "value": "vmbr7"}, ctx
        )

        assert out == {
            "key": "provision_default_bridge",
            "ok": False,
            "reachable": True,
            "detail": "no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1",
        }
        assert await _stored(repo, "provision_default_bridge") is None

    async def test_an_accepted_value_is_still_not_saved(self, ctx, repo) -> None:
        await _call("set_setting_override", {"key": "provision_default_node", "value": "pve1"}, ctx)

        out = await _call(
            "probe_setting_override", {"key": "provision_default_bridge", "value": "vmbr0"}, ctx
        )

        assert out["ok"] is True
        assert await _stored(repo, "provision_default_bridge") is None, (
            "the probe saved the value it was only asked about"
        )

    async def test_a_setting_with_no_probe_says_so(self, ctx, cluster) -> None:
        out = await _call("probe_setting_override", {"key": "retention_days", "value": 30}, ctx)
        assert out["ok"] is True and "nothing to check it against" in out["detail"]
        assert cluster.reads == [], "a setting with no probe still went to the cluster"
