"""#506's two answers, as journeys on the real routes, store, lifecycle and KB.

1. The library is the store plus a convention: ``tags: ["pattern"]`` on an
   artifact, and an exact-tag filter on ``GET /artifacts``, MCP
   ``query_artifacts`` and ``hp changes list --tag`` (the CLI journey is in
   tests/test_cli_operator_commands.py). Tags are short slugs, refused at
   propose otherwise.
2. A KB policy may carry a ``policy-rule`` block (artifacts/policy_rules.py). A
   change it matches is NEVER refused at propose; at APPROVE the approver must
   acknowledge it (``acknowledge_policy``), on every transport, and the
   acknowledgement is recorded on the approval and in the journal. Free-text
   policies stay free text and hold nothing up.

Teeth (each verified by reverting the code - evidence table in the PR):
  * drop the acknowledgement check in ``ArtifactLifecycle.approve`` -> the
    approve-is-held tests;
  * drop ``tag`` from ``ArtifactStore.list`` -> the tag-filter tests;
  * drop ``validate_tags`` from ``validate_artifact_content`` -> the slug test;
  * drop the composite walk in ``policy_rules._changes`` -> the composite test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts import policy_rules
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.kb.service import KBService

# Proxmox is not asked here (no run happens), so the run guard is off.
pytestmark = pytest.mark.usefixtures("no_target_guard")

RULE = """# Never destroy a guest by API

Destroying is done from the console, after a backup.

```policy-rule
forbid:
  - kind: proxmox-api-sequence
    verb: DELETE
    path: /nodes/*/qemu/*
```
"""

FREE_TEXT = "# Be careful with web1\n\nNo installs on Fridays. (Free text: never enforced.)\n"


def _destroy_spec(n: int, host: str = "web1", tags: list[str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": f"2026-11-18-destroy-{host}-{n:06x}",
        "kind": "proxmox-api-sequence",
        "intent": f"Destroy {host}",
        "body": (
            f"Destroy {host}.\n\n```yaml proxmox-api-spec\nsteps:\n  - id: destroy\n"
            "    method: DELETE\n"
            "    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}\n```\n"
        ),
        "target": {"kind": "vm", "vmid": 100 + n, "node": "pve1", "host": host},
        "idempotence": "declared-natural",
        "requires_snapshot": False,
    }
    if tags is not None:
        spec["tags"] = tags
    return spec


def _start_spec(n: int, host: str = "web1", tags: list[str] | None = None) -> dict[str, Any]:
    spec = _destroy_spec(n, host, tags)
    spec["id"] = f"2026-11-18-start-{host}-{n:06x}"
    spec["intent"] = f"Start {host}"
    spec["body"] = (
        spec["body"]
        .replace("method: DELETE", "method: POST")
        .replace("{{ target.vmid }}\n", "{{ target.vmid }}/status/start\n")
    )
    return spec


class _Env:
    client: AsyncClient
    repo: Repository
    store: ArtifactStore
    lifecycle: ArtifactLifecycle
    kb: KBService
    tokens: dict[str, str]

    def auth(self, who: str = "operator") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    async def policy(self, content: str, target: str | None = "web1") -> int:
        resp = await self.client.post(
            "/kb/notes",
            json={"kind": "policy", "target": target, "content": content},
            headers=self.auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["indexed"], resp.json()
        return int(resp.json()["doc_id"])

    async def propose(self, spec: dict[str, Any]) -> str:
        resp = await self.client.post("/artifacts", json=spec, headers=self.auth())
        assert resp.status_code == 200, resp.text
        return str(resp.json()["id"])


@pytest.fixture
async def env(tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.kb.router import router as kb_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    kb = KBService(repo=repo, store=store, lifecycle=lifecycle)
    lifecycle._kb_service = kb

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.include_router(kb_router, prefix="/kb")
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
    app.state.repo = repo
    app.state.kb_service = kb
    app.state.task_repo = AsyncMock()
    app.state.task_repo.get_active_task = AsyncMock(return_value=None)
    app.state.task_repo.list_tasks = AsyncMock(return_value=[])

    e = _Env()
    e.repo, e.store, e.lifecycle, e.kb, e.tokens = repo, store, lifecycle, kb, {}
    user_id = await repo.create_user(display_name="owner", auth_source="api_token")
    for who, scope in (("operator", "all"), ("assistant", "read,write")):
        full, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=str(user_id),
            token_type="personal",
            prefix=prefix,
            hash=token_hash,
            scope=scope,
            label=who,
        )
        e.tokens[who] = full
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        e.client = client
        yield e
    await kb.drain()
    await db.close()


# ── 1. The library: tags ─────────────────────────────────────────────────────


class TestTheLibraryIsATagAndAFilter:
    async def test_a_tagged_change_is_found_by_its_tag_and_only_it(self, env: _Env):
        pattern = await env.propose(_start_spec(1, tags=["pattern", "nginx"]))
        other = await env.propose(_start_spec(2, tags=["nginx"]))
        untagged = await env.propose(_start_spec(3))

        got = await env.client.get("/artifacts?tag=pattern", headers=env.auth())
        assert got.status_code == 200, got.text
        assert [a["id"] for a in got.json()["items"]] == [pattern]
        both = await env.client.get("/artifacts?tag=nginx", headers=env.auth())
        assert {a["id"] for a in both.json()["items"]} == {pattern, other}
        everything = await env.client.get("/artifacts", headers=env.auth())
        assert {pattern, other, untagged} <= {a["id"] for a in everything.json()["items"]}

        from homepilot.mcp.tools.artifact_tools import handle_query_artifacts

        mcp = await handle_query_artifacts(
            {"filter": json.dumps({"tag": "pattern"})}, {"store": env.store}
        )
        assert [a["id"] for a in mcp["items"]] == [pattern]
        assert mcp["summary"][0]["tags"] == ["pattern", "nginx"]

    @pytest.mark.parametrize(
        "tags",
        [
            ["Pattern"],
            ["has space"],
            ["x" * 33],
            "pattern",
            ["a", "a"],
            [f"t{i}" for i in range(11)],
            [7],
        ],
    )
    async def test_a_tag_that_is_not_a_short_slug_is_refused_at_propose(self, env: _Env, tags):
        resp = await env.client.post(
            "/artifacts", json=_start_spec(9, tags=tags), headers=env.auth()
        )
        assert resp.status_code == 400, resp.text
        assert "tag" in resp.json()["detail"]


# ── 2. Policy: a warning acknowledged at approve ─────────────────────────────


class TestAMatchingPolicyIsAcknowledgedAtApprove:
    async def test_propose_is_never_refused_and_approve_is_held_until_acknowledged(self, env: _Env):
        policy_id = await env.policy(RULE)
        aid = await env.propose(_destroy_spec(1))  # a draft: proposing is never refused

        shown = (await env.client.get(f"/artifacts/{aid}", headers=env.auth())).json()
        [warning] = shown["policy_warnings"]
        assert warning["policy_id"] == policy_id
        assert warning["matched"] == ["DELETE /nodes/pve1/qemu/101"]
        assert (
            warning["rule"] == "forbid kind proxmox-api-sequence, verb DELETE, path /nodes/*/qemu/*"
        )

        held = await env.client.post(f"/artifacts/{aid}/approve", json={}, headers=env.auth())
        assert held.status_code == 409, held.text
        detail = held.json()["detail"]
        assert detail["code"] == "policy_acknowledgement_required"
        assert "Never destroy a guest by API" in detail["message"]
        assert detail["policy_warnings"][0]["matched"] == ["DELETE /nodes/pve1/qemu/101"]
        fm, _ = env.store.read(aid)
        assert fm["status"] == "proposed", "an unacknowledged approval still approved"

        ok = await env.client.post(
            f"/artifacts/{aid}/approve", json={"acknowledge_policy": True}, headers=env.auth()
        )
        assert ok.status_code == 200, ok.text
        fm, _ = env.store.read(aid)
        assert fm["status"] == "approved"
        assert fm["approved_by"]["policy_acknowledged"] == [str(policy_id)]
        audit = await env.repo.query_audit_log(action="approve", artifact_id=aid)
        assert json.loads(audit[0]["details_json"])["policy_acknowledged"] == [str(policy_id)]
        # Answered: an approved change no longer carries the question.
        after = (await env.client.get(f"/artifacts/{aid}", headers=env.auth())).json()
        assert after["policy_warnings"] == []

    async def test_approve_and_apply_meets_the_same_acknowledgement(self, env: _Env):
        await env.policy(RULE)
        aid = await env.propose(_destroy_spec(2))
        held = await env.client.post(
            f"/artifacts/{aid}/approve-and-apply", json={}, headers=env.auth()
        )
        assert held.status_code == 409, held.text
        assert held.json()["detail"]["code"] == "policy_acknowledgement_required"
        fm, _ = env.store.read(aid)
        assert fm["status"] == "proposed"

    async def test_the_assistant_must_carry_the_acknowledgement_too(self, env: _Env):
        from homepilot.mcp.tools.artifact_tools import (
            handle_approve_artifact,
            handle_get_artifact,
        )

        await env.policy(RULE)
        aid = await env.propose(_destroy_spec(3))
        row = await env.repo.get_approval_code_row(aid)
        code = str(row["code"])
        ctx = {
            "lifecycle": env.lifecycle,
            "store": env.store,
            "repo": env.repo,
            "kb_service": env.kb,
        }

        seen = await handle_get_artifact({"artifact_id": aid}, ctx)
        assert seen["policy_warnings"][0]["matched"] == ["DELETE /nodes/pve1/qemu/103"]
        with pytest.raises(ValueError, match="acknowledge"):
            await handle_approve_artifact({"artifact_id": aid, "approval_code": code}, ctx)
        assert env.store.read(aid)[0]["status"] == "proposed"
        done = await handle_approve_artifact(
            {"artifact_id": aid, "approval_code": code, "acknowledge_policy": True}, ctx
        )
        assert done["status"] == "approved"

    async def test_a_change_the_rule_does_not_match_approves_as_before(self, env: _Env):
        await env.policy(RULE)
        await env.policy(FREE_TEXT)
        start = await env.propose(_start_spec(4))  # POST, not DELETE
        elsewhere = await env.propose(_destroy_spec(5, host="db1"))  # rule is about web1
        for aid in (start, elsewhere):
            shown = (await env.client.get(f"/artifacts/{aid}", headers=env.auth())).json()
            assert shown["policy_warnings"] == [], aid
            ok = await env.client.post(f"/artifacts/{aid}/approve", json={}, headers=env.auth())
            assert ok.status_code == 200, ok.text

    async def test_a_global_policy_binds_every_host(self, env: _Env):
        await env.policy(RULE, target=None)
        aid = await env.propose(_destroy_spec(6, host="db1"))
        held = await env.client.post(f"/artifacts/{aid}/approve", json={}, headers=env.auth())
        assert held.status_code == 409, held.text

    async def test_a_composite_is_held_for_a_step_the_rule_matches(self, env: _Env):
        await env.policy(RULE)
        step = await env.propose(_destroy_spec(7))
        composite = {
            "id": "2026-11-18-retire-web1-00000c",
            "kind": "composite",
            "intent": "Retire web1",
            "body": (
                "Retire.\n\n```yaml composite-spec\nsteps:\n"
                f"  - id: destroy\n    artifact: {step}\n```\n"
            ),
            "target": {"kind": "global"},
            "idempotence": "declared-natural",
        }
        cid = await env.propose(composite)
        held = await env.client.post(f"/artifacts/{cid}/approve", json={}, headers=env.auth())
        assert held.status_code == 409, held.text
        assert held.json()["detail"]["policy_warnings"][0]["artifact_id"] == step
        assert env.store.read(step)[0]["status"] == "proposed"

    async def test_a_rule_that_does_not_parse_is_said_not_swallowed(self, env: _Env):
        await env.policy("# Typo\n\n```policy-rule\nforbid:\n  - knd: shell-script\n```\n")
        aid = await env.propose(_destroy_spec(8))
        shown = (await env.client.get(f"/artifacts/{aid}", headers=env.auth())).json()
        [warning] = shown["policy_warnings"]
        assert warning["matched"] == []
        assert "unknown field" in warning["errors"][0]
        # Not a match, so nothing to acknowledge.
        ok = await env.client.post(f"/artifacts/{aid}/approve", json={}, headers=env.auth())
        assert ok.status_code == 200, ok.text


# ── The rule format itself ───────────────────────────────────────────────────


class TestTheRuleFormat:
    def test_free_text_has_no_rules(self):
        assert policy_rules.parse_rules(FREE_TEXT) == policy_rules.PolicyRules()

    def test_one_entry_or_a_list(self):
        one = policy_rules.parse_rules("```policy-rule\nforbid:\n  kind: shell-script\n```")
        assert one.rules == [policy_rules.Rule(kind="shell-script")]
        assert policy_rules.parse_rules(RULE).rules == [
            policy_rules.Rule(kind="proxmox-api-sequence", verb="DELETE", path="/nodes/*/qemu/*")
        ]

    @pytest.mark.parametrize(
        "block, error",
        [
            ("forbid: []\nallow: []", "exactly one key"),
            ("forbid:\n  - {}", "at least one"),
            ("forbid:\n  - kind: rm-rf", "not an artifact kind"),
            ("forbid:\n  - verb: 3", "non-empty string"),
            ("forbid: [", "not valid YAML"),
        ],
    )
    def test_a_bad_block_is_an_error_not_a_rule(self, block: str, error: str):
        parsed = policy_rules.parse_rules(f"```policy-rule\n{block}\n```")
        assert parsed.rules == []
        assert error in parsed.errors[0]

    def test_host_provision_operations_are_installs_writes_and_service_states(self):
        body = (
            "```yaml host-provision-spec\npackages: [nginx]\n"
            "services:\n  - name: nginx\n    state: started\n"
            "config_files:\n  - path: /etc/nginx/nginx.conf\n    content: x\n```\n"
        )
        ops = policy_rules.operations({"kind": "host-provision"}, body)
        assert ops == [
            policy_rules.Operation("install", "nginx"),
            policy_rules.Operation("write", "/etc/nginx/nginx.conf"),
            policy_rules.Operation("started", "nginx"),
        ]
        rule = policy_rules.Rule(verb="write", path="/etc/nginx/*")
        assert policy_rules.matches(rule, "host-provision", ops) == ["write /etc/nginx/nginx.conf"]
