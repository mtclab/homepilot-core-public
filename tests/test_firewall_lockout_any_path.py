"""THE GATE for #707: no path enables a PVE firewall into a lockout.

#600's guard sat in ONE caller - the guest-network path. A
``proxmox-api-sequence`` step ``PUT /cluster/firewall/options {enable: 1}`` went
straight to the wire, PVE defaulted the input policy to DROP, and the artifact
reported success: the #600 node lockout through another door.

The guard now runs on the wire itself - ``ProxmoxClient.call`` for every write,
and the SDN gateway's own enable - so these tests drive the REAL shipped paths
(the proxmox-api-sequence executor over a real ``ProxmoxClient``, the real
``PveSdnGateway``) against a fake cluster at the HTTP / library boundary, and
assert what reached the cluster.

Owner rule: HomePilot never rewrites rules or policies on an operator's
firewall to make a write pass - it refuses. So no test here expects a "fix".

Teeth (each proven by reverting and watching the NAMED test fail):
  - delete the ``_guard_firewall_options_write`` call from ``ProxmoxClient.call``
    -> ``test_a_bare_enable_artifact_never_reaches_the_wire`` fails;
  - drop REJECT from ``LOCKOUT_POLICIES`` -> ``test_reject_is_a_lockout_too``;
  - stop merging the path's query string -> ``test_the_query_string_is_no_way_round``;
  - make ``_may_block`` return False -> ``test_an_allow_shadowed_by_a_drop_is_not_verified``;
  - restore ``{enable:1, policy_in:ACCEPT}`` for a staged DROP ->
    ``test_a_staged_drop_is_kept_not_overwritten``;
  - drop the guard call from ``ensure_datacenter_firewall_enabled`` ->
    ``test_the_gateway_refuses_a_staged_drop_with_no_allow``;
  - drop ``_check_pve_firewall_writes`` from ``validate_propose_spec`` /
    ``_pve_firewall_write_refusal`` from the http executor -> the two
    http-sequence tests.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from homepilot.adapters import pve_firewall_guard
from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.adapters.pve_firewall_guard import PveFirewallLockoutError
from homepilot.adapters.pve_sdn import PveSdnGateway, safe_datacenter_firewall_enable
from homepilot.artifacts.models import LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.artifacts.validator import validate_propose_spec
from homepilot.executor.http_sequence import execute as http_execute
from homepilot.executor.proxmox_api import execute as pve_execute

pytestmark = pytest.mark.asyncio

# HomePilot's own address towards the API, as the guard computes it. An RFC 5737
# documentation address: the gate never resolves or reaches anything real.
MGMT = ipaddress.ip_address("192.0.2.10")

# An allow the guard can PROVE: enabled, in, ACCEPT, tcp, both ports, any source.
ALLOW_ANY = {
    "pos": 0,
    "type": "in",
    "action": "ACCEPT",
    "proto": "tcp",
    "dport": "22,8006",
    "enable": 1,
    "comment": "management",
}


@pytest.fixture(autouse=True)
def _known_management_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pve_firewall_guard, "_local_address_towards", lambda host, port: MGMT)


class FakePVE:
    """The firewall endpoints of a one-node cluster, over HTTP."""

    def __init__(
        self,
        cluster_options: dict[str, Any] | None = None,
        cluster_rules: list[dict[str, Any]] | None = None,
        node_rules: list[dict[str, Any]] | None = None,
        node_options: dict[str, Any] | None = None,
    ) -> None:
        self.cluster_options = dict(cluster_options or {})
        self.cluster_rules = list(cluster_rules or [])
        self.node_rules = list(node_rules or [])
        self.node_options = dict(node_options or {})
        self.writes: list[tuple[str, str, dict[str, Any]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        if request.method == "GET":
            answers: dict[str, Any] = {
                "/nodes": [{"node": "pve1", "status": "online"}],
                "/cluster/firewall/options": self.cluster_options,
                "/cluster/firewall/rules": self.cluster_rules,
                "/nodes/pve1/firewall/rules": self.node_rules,
                "/nodes/pve1/firewall/options": self.node_options,
            }
            if path in answers:
                return httpx.Response(200, json={"data": answers[path]})
            return httpx.Response(404, json={"data": None})
        params = dict(parse_qsl(request.url.query.decode()))
        if request.content:
            params.update(json.loads(request.content))
        self.writes.append((request.method, path, params))
        if path == "/cluster/firewall/options":
            self.cluster_options.update(params)
        return httpx.Response(200, json={"data": None})


def _client(pve: FakePVE) -> ProxmoxClient:
    client = ProxmoxClient(base_url="https://pve.example.com:8006", token="root@pam!t=uuid")
    fake = httpx.AsyncClient(
        base_url="https://pve.example.com:8006/api2/json",
        transport=httpx.MockTransport(pve.handle),
    )
    client._client = fake
    client._write_client = fake
    return client


def _body(path: str, body: dict[str, Any]) -> str:
    lines = "".join(f"      {k}: {v!r}\n" for k, v in body.items())
    return (
        "# fw\n\n```yaml proxmox-api-spec\nsteps:\n"
        f"  - id: fw\n    method: PUT\n    path: {path}\n    body:\n{lines}```\n"
    )


FM = {"id": "2026-11-02-fw-probe", "intent": "probe"}
CLUSTER = {"kind": "cluster"}


def _options_writes(pve: FakePVE) -> list[tuple[str, str, dict[str, Any]]]:
    return [w for w in pve.writes if w[1].endswith("/firewall/options")]


class TestTheArtifactPathIsGuarded:
    async def test_a_bare_enable_artifact_never_reaches_the_wire(self) -> None:
        """The #707 repro: enable=1, no policy_in (PVE: DROP), no rules."""
        pve = FakePVE()
        result = await pve_execute(
            FM, _body("/cluster/firewall/options", {"enable": 1}), CLUSTER, _client(pve)
        )
        assert _options_writes(pve) == [], "a lockout-shaped enable reached the cluster"
        assert result["success"] is False
        assert "REFUSED" in result["execution_log"]
        assert pve.cluster_options == {}

    async def test_an_explicit_drop_is_refused_the_same(self) -> None:
        pve = FakePVE()
        result = await pve_execute(
            FM,
            _body("/cluster/firewall/options", {"enable": 1, "policy_in": "DROP"}),
            CLUSTER,
            _client(pve),
        )
        assert result["success"] is False
        assert _options_writes(pve) == []

    async def test_reject_is_a_lockout_too(self) -> None:
        pve = FakePVE()
        client = _client(pve)
        with pytest.raises(ProxmoxError) as exc:
            await client.call(
                "PUT", "cluster/firewall/options", body={"enable": 1, "policy_in": "REJECT"}
            )
        assert "REFUSED" in exc.value.body
        assert _options_writes(pve) == []

    async def test_the_query_string_is_no_way_round(self) -> None:
        pve = FakePVE()
        client = _client(pve)
        with pytest.raises(ProxmoxError):
            await client.call("PUT", "/cluster/firewall/options?enable=1")
        with pytest.raises(ProxmoxError):
            await client.call("PUT", "cluster/firewall/options", query={"enable": "1"})
        assert _options_writes(pve) == []

    async def test_a_dot_segment_spelling_is_refused(self) -> None:
        pve = FakePVE()
        with pytest.raises(ProxmoxError):
            await _client(pve).call(
                "PUT", "nodes/pve1/../../cluster/firewall/options", body={"enable": 1}
            )
        assert pve.writes == []

    async def test_enabling_a_node_firewall_under_a_dropping_datacenter_is_guarded(self) -> None:
        pve = FakePVE(
            cluster_options={"enable": 1, "policy_in": "DROP"}, node_options={"enable": 0}
        )
        with pytest.raises(ProxmoxError):
            await _client(pve).call("PUT", "nodes/pve1/firewall/options", body={"enable": 1})
        assert _options_writes(pve) == []

    async def test_a_verified_allow_lets_the_operators_enable_through(self) -> None:
        pve = FakePVE(cluster_rules=[ALLOW_ANY])
        result = await pve_execute(
            FM, _body("/cluster/firewall/options", {"enable": 1}), CLUSTER, _client(pve)
        )
        assert result["success"] is True, result
        assert _options_writes(pve) == [("PUT", "/cluster/firewall/options", {"enable": 1})]

    async def test_an_allow_from_the_management_subnet_counts(self) -> None:
        rule = dict(ALLOW_ANY, source="192.0.2.0/24")
        pve = FakePVE(cluster_rules=[rule])
        await _client(pve).call("PUT", "cluster/firewall/options", body={"enable": 1})
        assert len(_options_writes(pve)) == 1

    async def test_an_allow_from_another_subnet_does_not(self) -> None:
        rule = dict(ALLOW_ANY, source="198.51.100.0/24")
        pve = FakePVE(cluster_rules=[rule])
        with pytest.raises(ProxmoxError):
            await _client(pve).call("PUT", "cluster/firewall/options", body={"enable": 1})
        assert _options_writes(pve) == []

    async def test_an_allow_shadowed_by_a_drop_is_not_verified(self) -> None:
        drop = {"pos": 0, "type": "in", "action": "DROP", "enable": 1, "comment": "deny all"}
        pve = FakePVE(cluster_rules=[drop, dict(ALLOW_ANY, pos=1)])
        with pytest.raises(ProxmoxError) as exc:
            await _client(pve).call("PUT", "cluster/firewall/options", body={"enable": 1})
        assert "may drop" in exc.value.body
        assert _options_writes(pve) == []

    async def test_a_node_rule_that_drops_management_is_not_verified(self) -> None:
        drop = {"pos": 0, "type": "in", "action": "DROP", "dport": "22", "enable": 1}
        pve = FakePVE(cluster_rules=[ALLOW_ANY], node_rules=[drop])
        with pytest.raises(ProxmoxError):
            await _client(pve).call("PUT", "cluster/firewall/options", body={"enable": 1})
        assert _options_writes(pve) == []

    async def test_an_allow_with_only_one_port_is_not_enough(self) -> None:
        pve = FakePVE(cluster_rules=[dict(ALLOW_ANY, dport="8006")])
        with pytest.raises(ProxmoxError) as exc:
            await _client(pve).call("PUT", "cluster/firewall/options", body={"enable": 1})
        assert "tcp/22" in exc.value.body

    async def test_safe_writes_are_never_held_up(self) -> None:
        """An ACCEPT enable, a disable and a log-level change need no allow at all."""
        pve = FakePVE(cluster_options={"enable": 1, "policy_in": "DROP"})
        client = _client(pve)
        await client.call(
            "PUT", "cluster/firewall/options", body={"enable": 1, "policy_in": "ACCEPT"}
        )
        await client.call("PUT", "cluster/firewall/options", body={"enable": 0})
        await client.call("PUT", "cluster/firewall/options", body={"log_ratelimit": "enable=1"})
        assert len(_options_writes(pve)) == 3

    async def test_an_unreadable_ruleset_is_a_refusal(self) -> None:
        class Broken(FakePVE):
            def handle(self, request: httpx.Request) -> httpx.Response:
                if request.method == "GET" and request.url.path.endswith("/firewall/rules"):
                    return httpx.Response(500, json={"data": None})
                return super().handle(request)

        pve = Broken()
        with pytest.raises(ProxmoxError) as exc:
            await _client(pve).call("PUT", "cluster/firewall/options", body={"enable": 1})
        assert "could not be read" in exc.value.body
        assert _options_writes(pve) == []


# ── The guest-network path's own enable (proxmox_mcp boundary) ───────────────


class _Opts:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    async def get(self, **_kw: Any) -> dict[str, Any]:
        return dict(self._store["options"])

    async def put(self, **kw: Any) -> None:
        self._store["puts"].append(dict(kw))
        self._store["options"].update(kw)


class _Rules:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def get(self, **_kw: Any) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeLibrary:
    """Enough of proxmox_mcp's MultiClient for the datacenter-firewall enable."""

    def __init__(self, options: dict[str, Any], rules: list[dict[str, Any]]) -> None:
        self.store: dict[str, Any] = {"options": dict(options), "puts": []}
        self._rules = rules

    def get_client(self, elevated: bool = False, endpoint: str | None = None) -> Any:
        node_api = SimpleNamespace(firewall=SimpleNamespace(rules=_Rules([])))

        class _Nodes:
            # `nodes` is both a collection (`.get`) and callable (`nodes(name)`).
            get = staticmethod(_Rules([{"node": "pve1", "status": "online"}]).get)

            def __call__(self, name: str) -> Any:
                return node_api

        return SimpleNamespace(
            cluster=SimpleNamespace(
                firewall=SimpleNamespace(options=_Opts(self.store), rules=_Rules(self._rules))
            ),
            nodes=_Nodes(),
        )

    async def safe_api_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("elevated", None)
        return await fn(*args, **kwargs)


class TestTheGatewayEnableIsGuarded:
    async def test_a_staged_drop_is_kept_not_overwritten(self) -> None:
        assert safe_datacenter_firewall_enable({"enable": 0, "policy_in": "DROP"}) == {"enable": 1}
        assert safe_datacenter_firewall_enable({"policy_in": "reject"}) == {"enable": 1}
        assert safe_datacenter_firewall_enable({"enable": 0, "policy_in": "ACCEPT"}) == {
            "enable": 1,
            "policy_in": "ACCEPT",
        }

    async def test_the_gateway_refuses_a_staged_drop_with_no_allow(self) -> None:
        lib = _FakeLibrary({"enable": 0, "policy_in": "DROP"}, rules=[])
        gw = PveSdnGateway(lib, base_url="https://pve.example.com:8006")
        with pytest.raises(PveFirewallLockoutError):
            await gw.ensure_datacenter_firewall_enabled()
        assert lib.store["puts"] == [], "a staged DROP was enabled with no verified allow"
        assert lib.store["options"] == {"enable": 0, "policy_in": "DROP"}

    async def test_the_gateway_enables_a_staged_drop_over_a_verified_allow(self) -> None:
        lib = _FakeLibrary({"enable": 0, "policy_in": "DROP"}, rules=[ALLOW_ANY])
        gw = PveSdnGateway(lib, base_url="https://pve.example.com:8006")
        said = await gw.ensure_datacenter_firewall_enabled()
        # The operator's policy is KEPT: only the switch is written.
        assert lib.store["puts"] == [{"enable": 1}]
        assert lib.store["options"]["policy_in"] == "DROP"
        assert "management-allow verified" in said


# ── http-sequence is a generic client: it may not write firewall options ─────


HTTP_FW_BODY = (
    "# fw\n\n```yaml http-spec\nsteps:\n"
    "  - id: fw\n    name: pve\n    method: PUT\n    path: /cluster/firewall/options\n"
    "    body:\n      enable: 1\n```\n"
)


class TestHttpSequenceCannotGoRound:
    async def test_it_is_refused_at_propose(self, tmp_path: Path) -> None:
        spec = {
            "id": "2026-11-02-fw-http-a1b2c3",
            "kind": "http-sequence",
            "intent": "enable the datacenter firewall over raw HTTP",
            "body": HTTP_FW_BODY,
            "target": {"kind": "cluster"},
            "idempotence": "via-precheck",
            "produced_by": {"session": "s1", "agent": "a1", "user": "operator"},
        }
        with pytest.raises(LifecycleError) as exc:
            validate_propose_spec(spec, ArtifactStore(tmp_path / "artifacts"))
        assert "proxmox-api-sequence" in str(exc.value)

    async def test_it_is_refused_at_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json={"data": None})

        class Vault:
            async def get_secret(self, name: str) -> dict[str, Any]:
                return {"base_url": "https://pve.example.com:8006/api2/json", "headers": {}}

        import homepilot.executor.http_sequence as hs

        real_client = httpx.AsyncClient

        def patched(**kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(**kwargs)

        monkeypatch.setattr(hs.httpx, "AsyncClient", patched)
        result = await http_execute(FM, HTTP_FW_BODY, CLUSTER, Vault())
        assert result["success"] is False
        assert "REFUSED" in result["execution_log"], result
        assert not any(s.startswith("PUT") for s in seen), "the firewall write was sent"
