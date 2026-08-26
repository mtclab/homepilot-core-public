"""The guest network's operator surfaces: the settings, the API read, the MCP tool (#553).

Three things are gated here, and each is a place the product could quietly stop
telling the truth:

* **The settings.** The eight `guest_network_*` keys resolve through the same
  env > db > default precedence as everything else, refuse a shape that could
  never work (before it is stored, naming the field), and refuse a write when
  the environment already decides them. The isolate list - which IS the fence -
  is parsed rather than stored as typed, so a typo cannot become a network a
  guest can still reach.

* **The API read.** `GET /admin/guest-network` answers with survey + desired +
  plan, and answers HONESTLY in the two states a fresh install is actually in:
  nothing configured, and no Proxmox wired up. Neither is an error, and a 503
  would tell an operator nothing about what to do next.

* **The MCP tool.** The same report, through the same function, at the admin
  tier - and NO mutating twin, because the change ships as an artifact.

Teeth (each proven by planting the defect and watching the NAMED test fail):
  - accept any string as the isolate list -> `test_a_typo_in_the_fence_is_refused` fails;
  - make the route 503 when Proxmox is missing ->
    `test_no_proxmox_is_reported_not_raised` fails;
  - drop query_guest_network from _ADMIN_TOOLS ->
    `test_the_tool_is_admin_tier` fails (and the parity tier gate fails too);
  - let the tool answer differently from the route ->
    `test_the_tool_and_the_route_answer_the_same` fails.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.admin.router import _require_admin_dep
from homepilot.admin.router import router as admin_router
from homepilot.app_settings import (
    REGISTRY,
    EnvOverrideError,
    SettingError,
    SettingsResolver,
    checked_set,
    run_probe,
)
from homepilot.config import Settings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import _ADMIN_TOOLS, _TOOL_DEFINITIONS, _handle_tool
from homepilot.provision.guest_network import KEYS

pytestmark = pytest.mark.asyncio


def _settings(**overrides: Any) -> Settings:
    return Settings(
        data_dir="/tmp/hp-guest-net", artifacts_dir="/tmp/hp-guest-net/artifacts", **overrides
    )


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "gn.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield Repository(db)
    finally:
        await db.close()


@pytest.fixture
async def state(repo):
    settings = _settings()
    return SimpleNamespace(
        repo=repo,
        settings=settings,
        settings_resolver=SettingsResolver(repo, settings),
        proxmox=None,
        provision_service=None,
    )


@pytest.fixture
async def api(state):
    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    app.state.repo = state.repo
    app.state.settings = state.settings
    app.state.settings_resolver = state.settings_resolver
    app.state.proxmox = None
    app.dependency_overrides[_require_admin_dep.dependency] = lambda: {
        "user_id": 1,
        "scope": "*",
        "role": "admin",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app


class TestTheSettings:
    async def test_every_key_is_in_the_registry_and_resolves(self, state) -> None:
        for key in KEYS:
            assert key in REGISTRY, f"{key} is consumed but not editable"
            resolved = await state.settings_resolver.resolve(key)
            assert resolved.source == "default"

    async def test_the_fence_default_is_empty(self, state) -> None:
        """The code default must be EMPTY: a shipped default cannot name any
        operator's LAN, and the public-mirror scrub proved it by silently
        rewriting a LAN-bearing default into a nonsense value (found staging
        the 3.6.0 export). Fail-closed lives in the provision path instead:
        an empty fence list REFUSES to provision onto the guest vnet."""
        value = await state.settings_resolver.value("guest_network_isolate_cidrs")
        assert value == ""

    async def test_a_typo_in_the_fence_is_refused(self, state) -> None:
        with pytest.raises(SettingError):
            await state.settings_resolver.set("guest_network_isolate_cidrs", "10.0.0.1/24, nope")

    async def test_a_host_address_where_a_cidr_belongs_is_refused(self, state) -> None:
        with pytest.raises(SettingError):
            await state.settings_resolver.set("guest_network_isolate_cidrs", "10.0.0.1/24")

    async def test_the_fence_list_is_stored_in_one_canonical_spelling(self, state) -> None:
        resolved = await state.settings_resolver.set(
            "guest_network_isolate_cidrs", " 10.0.0.1/24 ,192.168.1.0/24 "
        )
        assert resolved.value == "10.0.0.1/24,192.168.1.0/24"

    async def test_the_switches_take_only_0_or_1(self, state) -> None:
        assert (await state.settings_resolver.set("guest_network_snat", "0")).value == 0
        assert (await state.settings_resolver.set("guest_network_dhcp", True)).value == 1
        with pytest.raises(SettingError):
            await state.settings_resolver.set("guest_network_snat", "2")

    async def test_an_env_locked_key_refuses_the_write(self, repo, monkeypatch) -> None:
        monkeypatch.setenv("HP_GUEST_NETWORK_SUBNET", "10.96.17.0/24")
        settings = _settings()
        resolver = SettingsResolver(repo, settings)
        with pytest.raises(EnvOverrideError):
            await resolver.set("guest_network_subnet", "10.0.0.0/24")
        assert (await resolver.resolve("guest_network_subnet")).source == "env"

    async def test_the_api_refuses_an_env_locked_key_with_409(self, repo, monkeypatch) -> None:
        monkeypatch.setenv("HP_GUEST_NETWORK_GATEWAY", "10.96.17.1")
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        settings = _settings()
        app.state.repo = repo
        app.state.settings = settings
        app.state.settings_resolver = SettingsResolver(repo, settings)
        app.dependency_overrides[_require_admin_dep.dependency] = lambda: {
            "user_id": 1,
            "scope": "*",
            "role": "admin",
        }
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                "/admin/settings/overrides/guest_network_gateway", json={"value": "10.0.0.1"}
            )
        assert response.status_code == 409
        assert "HP_GUEST_NETWORK_GATEWAY" in response.json()["detail"]


class TestTheProbes:
    async def test_a_gateway_outside_the_stored_subnet_is_refused_before_saving(
        self, state
    ) -> None:
        await state.settings_resolver.set("guest_network_subnet", "10.96.17.0/24")
        result = await run_probe(state, "guest_network_gateway", "10.0.0.1")
        assert result is not None
        assert result.ok is False
        assert result.reachable is True, "a local shape check always 'reached' something"
        assert "not inside the guest subnet" in result.detail

    async def test_a_gateway_inside_the_subnet_is_confirmed(self, state) -> None:
        await state.settings_resolver.set("guest_network_subnet", "10.96.17.0/24")
        result = await run_probe(state, "guest_network_gateway", "10.96.17.1")
        assert result is not None and result.ok is True
        assert "No cluster call" in result.detail

    async def test_a_dhcp_range_outside_the_subnet_is_refused(self, state) -> None:
        await state.settings_resolver.set("guest_network_subnet", "10.96.17.0/24")
        result = await run_probe(state, "guest_network_dhcp_range", "10.96.18.100-10.96.18.199")
        assert result is not None and result.ok is False

    async def test_a_bad_value_is_never_stored(self, state) -> None:
        from homepilot.app_settings import ProbeRefusedError

        await state.settings_resolver.set("guest_network_subnet", "10.96.17.0/24")
        with pytest.raises(ProbeRefusedError):
            await checked_set(state, state.settings_resolver, "guest_network_gateway", "10.0.0.1")
        assert (await state.settings_resolver.resolve("guest_network_gateway")).source == "default"

    async def test_an_empty_isolate_list_says_what_that_costs(self, state) -> None:
        result = await run_probe(state, "guest_network_isolate_cidrs", "")
        assert result is not None and result.ok is True
        assert "NOT fenced" in result.detail


class TestTheApiRead:
    async def test_nothing_configured_is_reported_not_raised(self, api) -> None:
        client, _app = api
        response = await client.get("/admin/guest-network")
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is False
        assert body["survey"] is None and body["plan"] is None
        assert "guest_network_subnet" in body["detail"]

    async def test_no_proxmox_is_reported_not_raised(self, api, state) -> None:
        client, app = api
        resolver: SettingsResolver = app.state.settings_resolver
        await resolver.set("guest_network_subnet", "10.96.17.0/24")
        await resolver.set("guest_network_gateway", "10.96.17.1")
        await resolver.set("guest_network_dhcp_range", "10.96.17.100-10.96.17.199")

        response = await client.get("/admin/guest-network")
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["desired"]["subnet_cidr"] == "10.96.17.0/24"
        assert body["survey"] is None
        assert "Proxmox is not configured" in body["detail"]

    async def test_unusable_settings_say_which_field_is_wrong(self, api, state) -> None:
        """A stored combination that cannot work must not read as 'no guest
        network' - the operator would go looking for a setting that is there."""
        client, app = api
        resolver: SettingsResolver = app.state.settings_resolver
        await resolver.set("guest_network_subnet", "10.96.17.0/24")
        # Stored directly, bypassing checked_set, the way a hand-edited row or an
        # older build's value arrives.
        await resolver.set("guest_network_gateway", "10.96.17.1")
        await resolver.set("guest_network_dhcp_range", "")
        response = await client.get("/admin/guest-network")
        body = response.json()
        assert body["configured"] is False
        assert "nothing to hand out" in body["detail"]

    async def test_the_route_needs_admin(self, repo) -> None:
        app = FastAPI()
        app.include_router(admin_router, prefix="/admin")
        settings = _settings()
        app.state.repo = repo
        app.state.settings = settings
        app.state.settings_resolver = SettingsResolver(repo, settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/admin/guest-network")
        assert response.status_code in (401, 403)


class TestTheMcpTool:
    def _ctx(self, state: Any) -> dict[str, Any]:
        return {
            "repo": state.repo,
            "settings": state.settings,
            "proxmox": None,
            "app_state": state,
            "_mcp_caller_id": "mcp-test",
            "_mcp_token_scope": "admin",
        }

    async def test_the_tool_is_admin_tier(self, state) -> None:
        assert "query_guest_network" in _ADMIN_TOOLS
        ctx = self._ctx(state)
        ctx["_mcp_token_scope"] = "full"
        with pytest.raises(ValueError) as exc:
            await _handle_tool("query_guest_network", {}, ctx)
        assert "admin scope" in str(exc.value)

    async def test_the_tool_and_the_route_answer_the_same(self, api, state) -> None:
        client, app = api
        resolver: SettingsResolver = app.state.settings_resolver
        await resolver.set("guest_network_subnet", "10.96.17.0/24")
        await resolver.set("guest_network_gateway", "10.96.17.1")
        await resolver.set("guest_network_dhcp_range", "10.96.17.100-10.96.17.199")

        over_http = (await client.get("/admin/guest-network")).json()
        over_mcp = await _handle_tool("query_guest_network", {}, self._ctx(state))
        assert over_mcp == over_http

    async def test_there_is_no_mutating_twin(self) -> None:
        """The change ships as an artifact. A `set_guest_network` tool would be a
        second way to change the estate that leaves no record of intent."""
        names = {t["name"] for t in _TOOL_DEFINITIONS}
        offenders = {n for n in names if "guest_network" in n and n != "query_guest_network"}
        assert offenders == set()

    async def test_the_description_points_at_the_artifact_path(self) -> None:
        tool = next(t for t in _TOOL_DEFINITIONS if t["name"] == "query_guest_network")
        assert "propose_artifact" in tool["description"]
        assert "guest-network" in tool["description"]
        # And it states the enforcement caveat, because a model reading only this
        # tool would otherwise believe the vnet rules are what fences a guest.
        assert "legacy" in tool["description"].lower()

    async def test_propose_advertises_the_new_kind(self) -> None:
        tool = next(t for t in _TOOL_DEFINITIONS if t["name"] == "propose_artifact")
        assert "guest-network" in tool["inputSchema"]["properties"]["spec"]["description"]
