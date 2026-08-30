"""#648 tranche-1 follow-up: does the fleet run the agent this build shipped?

`agent_hub/dist.py` names this gap in its own docstring. Enrolment serves the
agent from the image, so a NEW agent matches the hub that enrolled it - and then
nothing upgrades it and nothing compared the versions. Dev ran a v3.6.6 agent
against a 3.6.15 hub for weeks with every surface green, which means a fix that
lived in the Go binary was written, gated, released and deployed and changed
nothing at all on any managed host.

`agent_hub: ok` is not this claim. It says hosts CAN connect; it says nothing
about what connected being current. Collapsing the two is the belief this whole
review keeps finding.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from homepilot.agent_hub.version_skew import is_behind, parse, summarise
from homepilot.selfcheck import (
    STATE_OK,
    STATE_UNKNOWN,
    STATE_UNREACHABLE,
    selfcheck_report,
)


class TestTheComparison:
    def test_reads_the_shapes_agents_actually_report(self):
        assert parse("v3.6.15") == (3, 6, 15)
        assert parse("3.6.15") == (3, 6, 15)
        assert parse("3.6.15-dirty") == (3, 6, 15)

    def test_an_older_agent_is_behind(self):
        assert is_behind("v3.6.15", "3.6.18") is True

    def test_a_matching_agent_is_not_behind(self):
        assert is_behind("3.6.18", "3.6.18") is False

    @pytest.mark.parametrize("version", ["", None, "unknown", "dev-build"])
    def test_a_version_it_cannot_read_is_unknown_not_fine(self, version):
        """The fail-safe direction. Answering "up to date" for a version nobody
        could parse is exactly the rounding-up this module exists to refuse."""
        assert is_behind(version, "3.6.18") is None

    def test_an_agent_ahead_of_the_control_plane_is_not_called_current(self):
        """A downgraded control plane is a different problem, but calling that
        fleet up-to-date would be the same mistake wearing a different hat."""
        assert is_behind("3.7.0", "3.6.18") is None

    def test_a_disconnected_agent_is_not_judged(self):
        """It is not running anything, so 'outdated' would send an operator to
        chase a machine whose actual problem is that it is offline."""
        summary = summarise(
            [
                {
                    "hostname": "gone",
                    "connected": False,
                    "system_info": {"agent_version": "v1.0.0"},
                }
            ],
            "3.6.18",
        )
        assert summary["connected"] == 0
        assert summary["behind"] == []


def _settings(**over):
    base = {
        "proxmox_host": "",
        "agent_hub_enabled": True,
        "agent_hub_host": "0.0.0.0",
        "agent_hub_port": 8443,
        "vault_enabled": False,
        "embeddings_url": "",
        "events_webhook_url": "",
        "artifacts_remote": "",
        "artifacts_push_interval_seconds": 3600,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _state(agents):
    return SimpleNamespace(
        proxmox=None,
        vault=None,
        agent_hub=None,
        mcp_app=None,
        agent_registry=SimpleNamespace(list_connected=lambda: agents),
    )


def _agent(hostname: str, version: str | None):
    return {
        "agent_id": f"id-{hostname}",
        "hostname": hostname,
        "system_info": {"agent_version": version} if version else {},
    }


@pytest.mark.asyncio
class TestTheSelfcheckSaysIt:
    """Teeth: drop `_agent_versions_subsystem` from `build_subsystems` and every
    test here fails on the missing entry; return plain True from its probe and
    the stale-fleet case fails on the state."""

    async def _entry(self, agents, control):
        from homepilot import agent_hub

        original = agent_hub.version_skew.control_plane_version
        agent_hub.version_skew.control_plane_version = lambda: control
        try:
            report = await selfcheck_report(_state(agents), _settings())
        finally:
            agent_hub.version_skew.control_plane_version = original
        return {e["name"]: e for e in report["subsystems"]}["agent_versions"]

    async def test_a_stale_agent_is_reported_broken_and_named(self):
        entry = await self._entry([_agent("hp-test-server", "v3.6.15")], "3.6.18")

        assert entry["state"] == STATE_UNREACHABLE
        assert "hp-test-server" in entry["consequence"]
        assert "3.6.15" in entry["consequence"]
        # The consequence has to say what it COSTS, not just that they differ.
        assert "security fix" in entry["consequence"]

    async def test_a_current_fleet_is_ok(self):
        """The honest green has to stay reachable, or the report is noise."""
        entry = await self._entry([_agent("hp-test-server", "v3.6.18")], "3.6.18")
        assert entry["state"] == STATE_OK

    async def test_an_unreadable_version_is_unknown_not_ok(self):
        entry = await self._entry([_agent("odd-box", "nightly")], "3.6.18")
        assert entry["state"] == STATE_UNKNOWN
        assert "odd-box" in entry["consequence"]

    async def test_no_agents_connected_cannot_claim_a_current_fleet(self):
        entry = await self._entry([], "3.6.18")
        assert entry["state"] == STATE_UNKNOWN


class TestTheWriteTokenIsExercised:
    """#624: `connection_status: ok` was established with the READ token only.

    On prod, 2026-08-28: `write_token_configured: true`, connection ok, and
    every clone 401ing - discovered by a friend, in their face, on the first
    real invite redemption. "Configured" and "authenticates" are different
    claims and only one of them was ever checked.

    Teeth: make `check_tokens` probe `self._client` for both and the separate-
    credential test fails; drop the `/health` write check and its test fails.
    """

    @staticmethod
    def _client(read_ok: bool, write_ok: bool):
        import httpx

        from homepilot.adapters.proxmox import ProxmoxClient

        def handler(request: httpx.Request) -> httpx.Response:
            token = request.headers.get("authorization", "")
            ok = read_ok if "readtok" in token else write_ok
            if not ok:
                return httpx.Response(401, json={"data": None})
            return httpx.Response(200, json={"data": {"version": "8.2"}})

        client = ProxmoxClient(
            base_url="https://pve.example.com:8006",
            token="user@pve!read=readtok",
            write_token="user@pve!write=writetok",
            verify_ssl=False,
        )
        transport = httpx.MockTransport(handler)
        client._client._transport = transport
        client._write_client._transport = transport
        return client

    async def test_a_dead_write_token_is_reported_even_when_reads_work(self):
        client = self._client(read_ok=True, write_ok=False)
        result = await client.check_tokens()

        assert result["read"]["ok"] is True
        assert result["write"]["ok"] is False
        assert "401" in result["write"]["detail"]
        # And the old check still says everything is fine, which is the point.
        assert await client.test_connection() is True

    async def test_both_working_tokens_report_ok(self):
        client = self._client(read_ok=True, write_ok=True)
        result = await client.check_tokens()
        assert result["read"]["ok"] is True and result["write"]["ok"] is True

    async def test_one_shared_token_does_not_invent_a_second_verdict(self):
        import httpx

        from homepilot.adapters.proxmox import ProxmoxClient

        client = ProxmoxClient(base_url="https://h:8006", token="user@pve!only=t", verify_ssl=False)
        client._client._transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {}})
        )
        result = await client.check_tokens()
        assert result["write"]["ok"] is True
        assert "used for writes too" in result["write"]["detail"]


class TestBothStateObjectsStayInStep:
    """A reload must not leave the AppState holding the client it just closed.

    Found by driving 3.6.19 on dev, not by the suite: after changing the Proxmox
    write token, `/health` and `/admin/settings/proxmox` (which hold
    `app.state`) reported correctly, while the MCP `get_proxmox_settings` (which
    holds the `AppState`) answered `connection_status: error` with an EMPTY
    token verdict - because `_do_reload` swapped the client on one object and
    closed the old one the other was still using. One rule, two objects, two
    answers: #631's shape, and the third time this session.

    Teeth: drop the `hp_state.proxmox = new_proxmox` line and the first test
    fails; drop `app.state.hp_state = state` from the lifespan and the second.
    """

    def test_the_reload_updates_the_appstate_client_too(self):
        import importlib
        import inspect
        import sys

        # `from homepilot.admin import router` hands back the APIRouter the
        # package exports, not the module; go through sys.modules for the file.
        importlib.import_module("homepilot.admin.router")
        src = inspect.getsource(sys.modules["homepilot.admin.router"]._do_reload)
        assert "hp_state.proxmox = new_proxmox" in src, (
            "the AppState keeps the closed client; every MCP-side report goes wrong"
        )

    def test_the_lifespan_exposes_the_appstate_by_name(self):
        import inspect

        from homepilot import main as main_mod

        assert "app.state.hp_state = state" in inspect.getsource(main_mod.lifespan)

    def test_the_appstate_declares_the_fields_the_selfcheck_reads(self):
        """Both attributes the report reads off `state` are on the dataclass, so
        a caller holding an AppState cannot silently miss one."""
        from homepilot.app_state import AppState

        for field in ("proxmox", "mcp_app", "reconciler_scheduler"):
            assert field in AppState.__dataclass_fields__, field
