"""#624: the connection test exercises the WRITE token, without mutating.

`test_proxmox_connection` / POST /admin/settings/proxmox/test read `/version`
with the READ token and answered "Connection successful" while the write
token - the one every clone uses - was refused by the cluster. On prod the
first to find out was a friend redeeming an invite.

The probe now asks PVE, AS the write token, what it may do
(`GET /access/permissions`, a read). These gates drive the real probe with the
cluster stubbed at the library boundary (`proxmox_mcp`'s MultiClient) and
assert what the operator is told:

- a refused write token is reported as refused, in the message and in its own
  field, even though the read connection is fine;
- a write token missing a provisioning privilege names the privilege;
- the question is asked with the WRITE credential, on the elevated client;
- a cluster that cannot be asked yields `unknown`, never a guessed `ok`.

Teeth: drop the `probe_write_privileges` call from `probe_proxmox_connection`
and every TestTheConnectionTest case fails.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from proxmoxer.core import ResourceException

from homepilot.adapters.pve_access import PROVISION_PRIVILEGES, probe_write_privileges
from homepilot.adapters.pve_sdn import PveCredentials

READ = "reader@pve!ro=00000000-0000-0000-0000-000000000001"  # pragma: allowlist secret
WRITE = "writer@pve!rw=00000000-0000-0000-0000-000000000002"  # pragma: allowlist secret


def _all_privs() -> dict[str, dict[str, int]]:
    return {"/": {p: 1 for p in PROVISION_PRIVILEGES}, "/storage": {"Datastore.Audit": 1}}


class _FakeMultiClient:
    """proxmox_mcp's MultiClient surface: get_client(elevated) + safe_api_call."""

    def __init__(self, credentials: Any, answer: Any) -> None:
        self.credentials = credentials
        self.answer = answer
        self.asked: list[tuple[bool, bool]] = []

    def get_client(self, elevated: bool = False, endpoint: str | None = None) -> Any:
        get = SimpleNamespace(elevated=elevated)
        return SimpleNamespace(access=SimpleNamespace(permissions=SimpleNamespace(get=get)))

    async def safe_api_call(self, func: Any, *a: Any, elevated: bool = False, **kw: Any) -> Any:
        self.asked.append((func.elevated, elevated))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _factory(answer: Any, made: list[_FakeMultiClient]) -> Any:
    def make(credentials: Any) -> _FakeMultiClient:
        client = _FakeMultiClient(credentials, answer)
        made.append(client)
        return client

    return make


def _creds(write: str = WRITE) -> PveCredentials:
    return PveCredentials(
        base_url="https://pve.example.com:8006", token=READ, write_token=write, verify_ssl=True
    )


class TestTheProbe:
    async def test_full_privileges_are_ok(self) -> None:
        made: list[_FakeMultiClient] = []
        out = await probe_write_privileges(_creds(), client_factory=_factory(_all_privs(), made))
        assert out["status"] == "ok" and out["missing"] == [] and out["separate"] is True

    async def test_it_asks_as_the_write_token_on_the_elevated_client(self) -> None:
        made: list[_FakeMultiClient] = []
        await probe_write_privileges(_creds(), client_factory=_factory(_all_privs(), made))
        assert made[0].credentials.write_token == WRITE
        assert made[0].asked == [(True, True)], "the probe did not use the write client"

    async def test_a_missing_privilege_is_named(self) -> None:
        perms = _all_privs()
        del perms["/"]["VM.Clone"]
        perms["/"]["VM.Allocate"] = 0
        made: list[_FakeMultiClient] = []
        out = await probe_write_privileges(_creds(), client_factory=_factory(perms, made))
        assert out["status"] == "missing_privileges"
        assert out["missing"] == ["VM.Allocate", "VM.Clone"]
        assert "VM.Clone" in out["detail"]

    async def test_a_refused_token_says_so(self) -> None:
        made: list[_FakeMultiClient] = []
        refused = ResourceException(401, "authentication failure", "")
        out = await probe_write_privileges(_creds(), client_factory=_factory(refused, made))
        assert out["status"] == "refused"
        assert "refused the write token (401)" in out["detail"]

    async def test_an_unreachable_cluster_claims_nothing(self) -> None:
        made: list[_FakeMultiClient] = []
        out = await probe_write_privileges(
            _creds(), client_factory=_factory(ConnectionError("down"), made)
        )
        assert out["status"] == "unknown"

    async def test_no_token_value_is_in_the_answer(self) -> None:
        made: list[_FakeMultiClient] = []
        refused = ResourceException(401, "authentication failure", "")
        out = await probe_write_privileges(_creds(), client_factory=_factory(refused, made))
        assert WRITE.split("=")[1] not in repr(out)


class _FakePve:
    """ProxmoxClient whose READ token is fine."""

    def __init__(self, base_url: str, token: str, verify_ssl: bool, write_token: str) -> None:
        self._creds = PveCredentials(
            base_url=base_url, token=token, write_token=write_token, verify_ssl=verify_ssl
        )
        self.read = AsyncMock(return_value={"version": "8.2.2"})
        self.close = AsyncMock()

    def credentials(self) -> PveCredentials:
        return self._creds


class TestTheConnectionTest:
    @pytest.fixture
    def cluster(self) -> dict[str, Any]:
        holder: dict[str, Any] = {"answer": _all_privs(), "made": []}

        def make(credentials: Any) -> _FakeMultiClient:
            client = _FakeMultiClient(credentials, holder["answer"])
            holder["made"].append(client)
            return client

        with (
            patch("homepilot.adapters.proxmox.ProxmoxClient", _FakePve),
            patch("homepilot.adapters.pve_sdn.multi_client_from", make),
        ):
            yield holder

    async def _probe(self) -> dict[str, Any]:
        from homepilot.admin.router import ProxmoxConfigIn, probe_proxmox_connection

        state = SimpleNamespace(settings=None, vault=None)
        config = ProxmoxConfigIn(host="pve.example.com", port=8006, token=READ, write_token=WRITE)
        return await probe_proxmox_connection(state, config)

    async def test_a_refused_write_token_is_not_called_successful(
        self, cluster: dict[str, Any]
    ) -> None:
        cluster["answer"] = ResourceException(401, "authentication failure", "")
        out = await self._probe()
        assert out["status"] == "ok", "the read connection itself is fine"
        assert out["write_token"]["status"] == "refused"
        assert out["message"] != "Connection successful"
        assert "write token" in out["message"]

    async def test_a_working_write_token_is_reported_ok(self, cluster: dict[str, Any]) -> None:
        out = await self._probe()
        assert out["write_token"]["status"] == "ok"
        assert out["message"] == "Connection successful"
        assert cluster["made"][0].credentials.write_token == WRITE

    async def test_the_mcp_tool_gives_the_same_verdict(self, cluster: dict[str, Any]) -> None:
        from homepilot.admin.router import ProxmoxConfigIn, probe_proxmox_connection
        from homepilot.mcp.tools import ops_tools

        cluster["answer"] = ResourceException(401, "authentication failure", "")
        seen: list[Any] = []

        async def _probe(state: Any, config: ProxmoxConfigIn) -> dict[str, Any]:
            # The tool passes no token (MCP never accepts one); stand in the
            # stored ones and run the REAL probe.
            seen.append(config)
            stored = ProxmoxConfigIn(
                host="pve.example.com", port=8006, token=READ, write_token=WRITE
            )
            return await probe_proxmox_connection(
                SimpleNamespace(settings=None, vault=None), stored
            )

        with patch("homepilot.admin.router.probe_proxmox_connection", _probe):
            out = await ops_tools.handle_test_proxmox_connection({}, {"app_state": object()})
        assert seen and out["write_token"]["status"] == "refused"
