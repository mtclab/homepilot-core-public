"""The three re-join surfaces, and the one thing they must agree about (#628).

A join that failed used to be terminal: the status page told the redeemer to go
and run `tailscale up` themselves on a machine they had just been handed. The
commonest cause - a key that has expired or has already been used - can only be
fixed by a FRESH key, and the original provision cannot be given a second one.

Owner-approved surfaces, and only these:

* the **guest portal** (primary - the redeemer is the person holding the key),
* `POST /guests/{vmid}/tailnet-join` (admin),
* the `rejoin_tailnet` MCP tool (admin).

Deliberately NO CLI. An `--auth-key tskey-...` flag puts the key in an argv and
in the operator's shell history, which is the one property this whole code path
exists to protect - `TestThereIsNoCliSurface` holds that line.

The gates:

* ``TestBothAdminSurfacesResolveTheSameGuest`` - the route and the tool are
  driven with the same arguments against the same repo and must reach the same
  (node, hostname). Teeth: give either surface its own copy of the fallback
  chain and change one, and the pair fails.
* ``TestTheKeyIsNeverEchoed`` - a rejected key must not come back in the error.
  Teeth: put the value into the message and it fails.
* ``TestOnlyAdminMayRejoin`` - drop the admin dep / the _ADMIN_TOOLS entry and
  it fails.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import _ADMIN_TOOLS, _TOOL_DEFINITIONS, _handle_tool
from homepilot.mcp.tools.guest_tools import handle_rejoin_tailnet
from homepilot.provision.router import router as provision_router
from homepilot.provision.service import TailnetJoinConflictError

KEY = "tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk"


def _admin_token() -> dict:
    return {"user_id": "u1", "token_id": "t1", "scope": "admin", "role": "admin"}


def _read_token() -> dict:
    return {"user_id": "u2", "token_id": "t2", "scope": "read", "role": "viewer"}


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def repo(db: Database) -> Repository:
    repository = Repository(db)
    await repository.create_host(
        hostname="web-01",
        host_type="qemu",
        proxmox_id=105,
        node="pve1",
        source="hp_created",
    )
    return repository


@pytest.fixture
def service(repo: Repository) -> MagicMock:
    svc = MagicMock()
    svc.proxmox = MagicMock()
    svc.repo = repo
    svc.defaults_source = None
    svc.start_tailnet_join = AsyncMock(return_value="task-join-1")
    return svc


def _app(service: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(provision_router, prefix="/guests", dependencies=[Depends(require_token)])
    app.state.repo = MagicMock()
    app.state.provision_service = service
    return app


@pytest.fixture
def client(service: MagicMock):
    app = _app(service)
    app.dependency_overrides[require_token] = _admin_token
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestTheRouteStartsAJoin:
    def test_a_fresh_key_is_accepted_and_the_task_handed_back(
        self, client: TestClient, service: MagicMock
    ):
        resp = client.post("/guests/105/tailnet-join", json={"auth_key": KEY})

        assert resp.status_code == 202, resp.text
        assert resp.json() == {
            "task_id": "task-join-1",
            "status": "pending",
            "vmid": 105,
            "node": "pve1",
        }
        kwargs = service.start_tailnet_join.await_args.kwargs
        assert kwargs["vmid"] == 105
        assert kwargs["node"] == "pve1"
        assert kwargs["hostname"] == "web-01"
        assert kwargs["key"] == KEY
        assert kwargs["actor"] == "u1"

    def test_a_second_join_on_the_same_guest_is_409_not_202(
        self, client: TestClient, service: MagicMock
    ):
        service.start_tailnet_join = AsyncMock(
            side_effect=TailnetJoinConflictError("A tailnet join is already running for vmid 105")
        )
        resp = client.post("/guests/105/tailnet-join", json={"auth_key": KEY})
        assert resp.status_code == 409, resp.text

    def test_a_guest_nobody_has_heard_of_is_422_naming_the_missing_field(
        self, client: TestClient, service: MagicMock
    ):
        resp = client.post("/guests/999/tailnet-join", json={"auth_key": KEY})
        assert resp.status_code == 422
        assert "node" in resp.json()["detail"]
        service.start_tailnet_join.assert_not_awaited()

    def test_no_proxmox_is_503(self, service: MagicMock):
        service.proxmox = None
        app = _app(service)
        app.dependency_overrides[require_token] = _admin_token
        with TestClient(app) as c:
            assert c.post("/guests/105/tailnet-join", json={"auth_key": KEY}).status_code == 503


class TestOnlyAdminMayRejoin:
    """It runs a command inside somebody's machine, exactly like provision does."""

    def test_a_read_token_is_403(self, service: MagicMock):
        app = _app(service)
        app.dependency_overrides[require_token] = _read_token
        with TestClient(app) as c:
            assert c.post("/guests/105/tailnet-join", json={"auth_key": KEY}).status_code == 403
        service.start_tailnet_join.assert_not_awaited()

    def test_no_credentials_is_401(self, service: MagicMock):
        with TestClient(_app(service)) as c:
            assert c.post("/guests/105/tailnet-join", json={"auth_key": KEY}).status_code == 401

    def test_the_mcp_tool_sits_at_the_admin_tier(self) -> None:
        assert "rejoin_tailnet" in _ADMIN_TOOLS

    async def test_a_full_scope_mcp_token_is_refused(self, service: MagicMock) -> None:
        with pytest.raises(ValueError, match="admin tier"):
            await _handle_tool(
                "rejoin_tailnet",
                {"vmid": 105, "auth_key": KEY},
                {"provision_service": service, "_mcp_token_scope": "full"},
            )


class TestTheKeyIsNeverEchoed:
    """A validation error that quoted the key back would put it in the transcript."""

    def test_the_route_rejects_a_bad_key_without_repeating_it(self, service: MagicMock):
        """Driven through the REAL app, because the fix lives on the real app.

        FastAPI's default 422 handler echoes the rejected value back under
        `input`, so a mistyped - or a correctly typed but wrong-shaped - auth key
        came straight back out of the API it was posted to. A bare test app would
        pass this without the handler and prove nothing.
        """
        from homepilot.main import app as real_app

        bad = "definitely-not-a-tailscale-key"
        real_app.state.provision_service = service
        real_app.dependency_overrides[require_token] = _admin_token
        try:
            # No `with`: entering the client would run the real app's lifespan,
            # which migrates whatever database this machine's settings point at.
            resp = TestClient(real_app).post("/guests/105/tailnet-join", json={"auth_key": bad})
        finally:
            real_app.dependency_overrides.clear()
            del real_app.state.provision_service

        assert resp.status_code == 422, resp.text
        assert bad not in resp.text, "the API handed the rejected auth key back to its caller"
        assert "auth_key" in resp.text, "a 422 that does not name the field is not usable"

    async def test_the_tool_rejects_a_bad_key_without_repeating_it(
        self, service: MagicMock
    ) -> None:
        bad = "definitely-not-a-tailscale-key"
        with pytest.raises(ValueError) as excinfo:
            await handle_rejoin_tailnet(
                {"vmid": 105, "auth_key": bad}, {"provision_service": service}
            )
        assert bad not in str(excinfo.value)


class TestBothAdminSurfacesResolveTheSameGuest:
    """One fallback chain, not two.

    The route reaches the provisioning defaults through the FastAPI app state
    and the tool through the service, so the temptation is a copy each - and a
    copy that drifts means the same call answers differently depending on which
    transport it arrived on. Both call `resolve_join_target`; this drives them
    side by side and compares.
    """

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({}, ("pve1", "web-01")),
            ({"node": "pve2"}, ("pve2", "web-01")),
            ({"tailnet_hostname": "renamed"}, ("pve1", "renamed")),
            ({"node": "pve2", "tailnet_hostname": "renamed"}, ("pve2", "renamed")),
        ],
    )
    async def test_the_route_and_the_tool_reach_the_same_place(
        self, service: MagicMock, body: dict[str, Any], expected: tuple[str, str]
    ) -> None:
        app = _app(service)
        app.dependency_overrides[require_token] = _admin_token
        with TestClient(app) as c:
            resp = c.post("/guests/105/tailnet-join", json={"auth_key": KEY, **body})
        assert resp.status_code == 202, resp.text
        via_route = (
            service.start_tailnet_join.await_args.kwargs["node"],
            service.start_tailnet_join.await_args.kwargs["hostname"],
        )

        await handle_rejoin_tailnet(
            {"vmid": 105, "auth_key": KEY, **body}, {"provision_service": service}
        )
        via_tool = (
            service.start_tailnet_join.await_args.kwargs["node"],
            service.start_tailnet_join.await_args.kwargs["hostname"],
        )

        assert via_route == via_tool == expected


class TestTheToolDescribesWhatItReturns:
    def test_it_points_at_get_task_result_and_names_all_three_outcomes(self) -> None:
        """An async tool whose outcome cannot be read is not usable.

        `get_task_result` is the only way to learn what a re-join did, and its
        answer has three shapes; a description that named two of them would send
        an assistant hunting for a fresh key on a guest that never answered.
        """
        tool = next(t for t in _TOOL_DEFINITIONS if t["name"] == "rejoin_tailnet")
        description = tool["description"]
        assert "get_task_result" in description
        for word in ("joined", "failed", "unknown", "tailnet_detail"):
            assert word in description


class TestThereIsNoCliSurface:
    """Owner decision, and the reason is the point of the whole path.

    WHAT THIS FORBIDS: a `--auth-key tskey-...` flag. An argv is readable by
    every process on the box and lands in the operator's shell history, which is
    exactly what staging the key in a tmpfs file exists to avoid. If a re-join
    ever needs a CLI, it must read the key from a prompt or a file - and this
    gate should be revisited deliberately, not deleted by accident.
    """

    def test_no_cli_command_takes_a_tailscale_auth_key(self) -> None:
        from pathlib import Path

        cli_dir = Path(__file__).resolve().parents[1] / "src" / "homepilot" / "cli"
        modules = list(cli_dir.rglob("*.py"))
        sources = {path.name: path.read_text() for path in modules}
        # Guard the guard: a walk that found nothing proves nothing, and this one
        # would go quietly empty if the CLI package were ever moved or renamed.
        assert "invite" in sources.get("main.py", ""), (
            f"the CLI walk did not find the invite commands; it saw {sorted(sources)}"
        )
        offenders = [
            name for name, text in sources.items() if "auth_key" in text or "auth-key" in text
        ]
        assert offenders == [], (
            f"{offenders} put a tailscale auth key on a CLI surface; an argv is "
            "readable by every process on the box and lands in shell history"
        )
