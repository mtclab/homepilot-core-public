"""Approval shows what will actually change on the host (#445 A1).

The hole this closes: `POST /artifacts/{id}/preview` returns a **git diff of the
artifact file** - how the spec TEXT changed - and nothing in the UI even called
it. Meanwhile `executor/host_provision.check_drift` was a genuine plan engine
that no interface could reach. So an operator approving a change was told what
the document said, never what the machine would do.

These assert the OUTCOME an approver needs: the per-item before and after, a
truthful count, and - the one that matters most - that the plan and the drift
verdict can never disagree, because they read the same probe. Two engines that
answer the same question separately is exactly how #423 happened.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.artifacts.models import parse_host_provision_spec
from homepilot.auth.deps import require_token
from homepilot.executor.host_provision import check_drift, probe

SPEC_BODY = """
Install the web stack.

```yaml host-provision-spec
packages:
  - nginx
  - curl
services:
  - name: nginx
    state: started
config_files:
  - path: /etc/nginx/nginx.conf
    content: "worker_processes 1;\\n"
    mode: "0644"
```
"""

FRONTMATTER: dict[str, Any] = {
    "id": "art-plan-1",
    "kind": "host-provision",
    "status": "proposed",
    "target": {"host": "web01"},
}


def _agent(*, nginx_installed: bool, curl_installed: bool, active: bool, config_matches: bool):
    """A host in a known state, answering the same probes a real agent would."""
    agent = MagicMock()

    # dpkg's REAL answer for a package that is not installed. The fake used to
    # say "not found", which no dpkg has ever printed - and since the probe
    # tested only `rc == 0`, any rc≠1 read as "absent" too. That conflation is
    # #642 A6, and a harness that does not speak the host's words cannot catch
    # it (review #648).
    _dpkg_absent = "dpkg-query: package 'x' is not installed and no information is available"

    async def exec_readonly(host: str, command: str):
        if command == "dpkg -s nginx":
            return (0, "install ok installed", "") if nginx_installed else (1, "", _dpkg_absent)
        if command == "dpkg -s curl":
            return (0, "install ok installed", "") if curl_installed else (1, "", _dpkg_absent)
        if command.startswith("systemctl is-active"):
            return (0, "active\n" if active else "inactive\n", "")
        return (1, "", "unexpected probe")

    async def read_file(host: str, path: str):
        if config_matches:
            return "worker_processes 1;\n"
        return "worker_processes 4;\n"

    agent.exec_readonly = AsyncMock(side_effect=exec_readonly)
    agent.read_file = AsyncMock(side_effect=read_file)
    return agent


@pytest.fixture
def client():
    from homepilot.artifacts.router import router as artifacts_router

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")

    store = MagicMock()
    store.read = MagicMock(return_value=(FRONTMATTER, SPEC_BODY))
    app.state.artifact_store = store

    executor = MagicMock()
    executor.agent = _agent(
        nginx_installed=False, curl_installed=True, active=False, config_matches=False
    )
    app.state.artifact_executor = executor
    app.state.task_repo = MagicMock(get_active_task=AsyncMock(return_value=None))
    # The plan carries the operator's policies for the host (#429). Selected by
    # TARGET, not by text similarity - see `_policies_for` (#648 tranche 6).
    kb = MagicMock()
    kb.policies_for_target = AsyncMock(
        return_value=[
            {
                "id": 1,
                "title": "nginx on web01",
                "content": "reload, never restart - it terminates in-flight uploads",
                "target": "web01",
                "applies_via": "target",
            }
        ]
    )
    kb.search = AsyncMock(return_value=[])
    app.state.kb_service = kb
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    return TestClient(app)


class TestThePlanCarriesThePoliciesForTheHost:
    """Approving is meant to be an informed decision, and the plan alone cannot
    supply the rules the operator wrote about the machine (#429)."""

    def test_the_policies_are_returned_with_the_plan(self, client):
        body = client.post("/artifacts/2026-08-20-nginx-abc123/plan").json()

        titles = [p["title"] for p in body["policies"]]
        assert "nginx on web01" in titles
        assert "never restart" in body["policies"][0]["content"]

    def test_they_are_looked_up_for_the_target_host(self, client):
        client.post("/artifacts/2026-08-20-nginx-abc123/plan")

        client.app.state.kb_service.policies_for_target.assert_awaited_once()
        call = client.app.state.kb_service.policies_for_target.await_args
        assert (call.args[0] if call.args else call.kwargs.get("host")) == "web01"
        # Never free-text search: `search("web01", kind="policy")` surfaced a
        # policy whose own text said it was about a DIFFERENT host, and hid every
        # global policy - reproduced on dev 3.6.17 (#648 tranche 6).
        client.app.state.kb_service.search.assert_not_awaited()

    def test_a_kb_failure_does_not_block_the_plan(self, client):
        """The plan is what an approver came for; a KB that is down must not take
        the approval screen with it."""
        client.app.state.kb_service.policies_for_target = AsyncMock(
            side_effect=RuntimeError("kb down")
        )

        resp = client.post("/artifacts/2026-08-20-nginx-abc123/plan")

        assert resp.status_code == 200
        assert resp.json()["policies"] == []
        assert resp.json()["items"], "the plan itself vanished with the KB"


class TestThePlanDescribesTheHostNotTheFile:
    def test_it_names_each_item_with_before_and_after(self, client):
        """The thing an approver is deciding on: per item, what is there now and
        what it becomes."""
        plan = client.post("/artifacts/art-plan-1/plan").json()

        assert plan["host"] == "web01"
        by_id = {item["id"]: item for item in plan["items"]}

        assert by_id["package:nginx"]["observed"] == "absent"
        assert by_id["package:nginx"]["desired"] == "installed"
        assert by_id["package:nginx"]["changes"] is True

        # Already installed: present in the plan, but NOT as a change.
        assert by_id["package:curl"]["observed"] == "installed"
        assert by_id["package:curl"]["changes"] is False

        assert by_id["service:nginx"]["observed"] == "inactive"
        assert by_id["service:nginx"]["desired"] == "started"
        assert by_id["service:nginx"]["changes"] is True

        assert by_id["config:/etc/nginx/nginx.conf"]["observed"] == "differs"
        assert by_id["config:/etc/nginx/nginx.conf"]["changes"] is True

    def test_the_count_matches_the_items_that_change(self, client):
        plan = client.post("/artifacts/art-plan-1/plan").json()
        assert plan["change_count"] == sum(1 for i in plan["items"] if i["changes"])
        assert plan["change_count"] == 3
        assert plan["in_spec"] is False
        assert "3 of 4" in plan["summary"]

    def test_a_host_already_in_spec_says_applying_changes_nothing(self, client):
        """The other half of an honest plan: approving a no-op should say so,
        not present four reassuring green rows and a silent apply."""
        client.app.state.artifact_executor.agent = _agent(
            nginx_installed=True, curl_installed=True, active=True, config_matches=True
        )
        plan = client.post("/artifacts/art-plan-1/plan").json()

        assert plan["change_count"] == 0
        assert plan["in_spec"] is True
        assert "would change nothing" in plan["summary"]
        assert len(plan["items"]) == 4, "an in-spec host still lists what was checked"


class TestThePlanCannotContradictDrift:
    """One probe, two views. Drift saying 'in spec' while the plan promises
    changes - or the reverse - would be #423 wearing a different hat."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state",
        [
            {
                "nginx_installed": False,
                "curl_installed": True,
                "active": False,
                "config_matches": False,
            },
            {
                "nginx_installed": True,
                "curl_installed": True,
                "active": True,
                "config_matches": True,
            },
            {
                "nginx_installed": True,
                "curl_installed": False,
                "active": True,
                "config_matches": False,
            },
        ],
    )
    async def test_they_agree_on_every_host_state(self, state):
        spec = parse_host_provision_spec(SPEC_BODY)

        items = await probe(_agent(**state), "web01", spec)
        drift = await check_drift(_agent(**state), "web01", spec)

        planned = sorted(item["id"] for item in items if item["changes"])
        assert planned == sorted(drift["drifted_items"])
        assert bool(planned) == drift["drifted"]
        assert drift["unknown_items"] == [], "this host answered every probe"

    @pytest.mark.asyncio
    async def test_an_item_the_host_never_answered_is_not_drift(self):
        """The third view the pair needs (review #648): applying WOULD change an
        unreadable file, so the plan says so - but nobody established that it is
        out of spec, so drift must answer `unknown`, not red. Reporting it as
        drift is what sends an operator to re-apply, and the apply overwrites a
        file whose prior bytes were never read."""
        spec = parse_host_provision_spec(SPEC_BODY)
        agent = _agent(nginx_installed=True, curl_installed=True, active=True, config_matches=True)
        agent.read_file = AsyncMock(side_effect=Exception("permission denied"))

        items = await probe(agent, "web01", spec)
        drift = await check_drift(agent, "web01", spec)

        config = next(i for i in items if i["id"] == "config:/etc/nginx/nginx.conf")
        assert config["changes"] is True, "the plan must still say a write would happen"
        assert config["established"] is False
        assert drift["drifted"] is False
        assert drift["unknown_items"] == ["config:/etc/nginx/nginx.conf"]


class TestItRefusesToGuess:
    def test_an_unsupported_kind_is_refused_not_answered_with_an_empty_plan(self, client):
        """An empty plan reads as 'nothing will change'. For a kind with no plan
        engine that is a lie, so it must refuse instead."""
        client.app.state.artifact_store.read = MagicMock(
            return_value=({**FRONTMATTER, "kind": "shell-script"}, "echo hi")
        )
        resp = client.post("/artifacts/art-plan-1/plan")
        assert resp.status_code == 422
        assert "shell-script" in resp.json()["detail"]

    def test_no_agent_transport_is_refused_rather_than_guessed(self, client):
        client.app.state.artifact_executor = None
        resp = client.post("/artifacts/art-plan-1/plan")
        assert resp.status_code == 503
        assert "guess" in resp.json()["detail"]

    def test_a_failing_probe_does_not_read_as_nothing_to_do(self, client):
        """The dangerous failure: a host that cannot be reached returning an
        empty plan, which an approver reads as 'safe, no changes'."""
        agent = MagicMock()
        agent.exec_readonly = AsyncMock(side_effect=RuntimeError("host unreachable"))
        agent.read_file = AsyncMock(side_effect=RuntimeError("host unreachable"))
        client.app.state.artifact_executor.agent = agent

        resp = client.post("/artifacts/art-plan-1/plan")
        assert resp.status_code == 502
        assert "web01" in resp.json()["detail"]

    def test_an_artifact_with_no_target_is_refused(self, client):
        client.app.state.artifact_store.read = MagicMock(
            return_value=({**FRONTMATTER, "target": {}}, SPEC_BODY)
        )
        resp = client.post("/artifacts/art-plan-1/plan")
        assert resp.status_code == 400


class TestThePlanIsReadOnly:
    """It runs when an approval screen opens, so it must not be able to change
    anything - #419 is what a 'preview' that mutates costs."""

    @pytest.mark.asyncio
    async def test_it_only_ever_reads(self):
        agent = _agent(
            nginx_installed=False, curl_installed=False, active=False, config_matches=False
        )
        await probe(agent, "web01", parse_host_provision_spec(SPEC_BODY))

        for forbidden in (
            "install_package",
            "manage_service",
            "write_config",
            "exec",
            "write_file",
        ):
            called = getattr(agent, forbidden).called if hasattr(agent, forbidden) else False
            assert not called, f"the plan called {forbidden} - a preview must mutate nothing"

        for call in agent.exec_readonly.await_args_list:
            command = call.args[1]
            assert command.startswith(("dpkg -s", "systemctl is-active", "systemctl is-enabled")), (
                f"the plan ran an unexpected command: {command!r}"
            )
