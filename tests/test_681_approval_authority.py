"""Approval authority is enforced in the lifecycle, for every transport (#681).

The human-relay approval code (#385) was only ever checked by the MCP
``approve_artifact`` handler. The SAME ``read,write`` token an assistant is
minted for MCP could, over REST on the same app:

  1. ``POST /artifacts``            - propose a shell script (root on a host);
  2. ``GET /artifacts/{id}``        - read the approval code back;
  3. ``POST /artifacts/{id}/approve`` with NO code - approved, as any ``user``
     it liked to name in the body;
  4. ``POST /artifacts/{id}/apply`` - run it.

So "a valid code is proof a human decided" was not true, and a write token was
root on every agent host while ``exec_on_host`` is admin-only.

The rule these tests hold, whatever the transport:

* approval needs HUMAN authority: either an operator credential (a token that
  holds the admin capability, or the on-box CLI) or the per-artifact code a
  human relayed. An assistant credential without a valid code is refused.
* the code is only ever shown to an operator credential - not to a write/read
  token over REST, not on the read-scope SSE stream, not in the stored webhook
  delivery rows.
* resetting the brute-force lock is operator-only (an assistant must not undo
  its own lock).
* who approved / rejected / applied / revoked comes from the AUTHENTICATED
  credential, never from the request body.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor, ExecutionResult
from homepilot.reconciler.apply import ApplyReconciler
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner

# Proxmox is an AsyncMock here: it cannot say who is at a VMID, so the run
# guard (#813) is off for this module; tests/test_target_identity.py proves it.
pytestmark = pytest.mark.usefixtures("no_target_guard")

SHELL_BODY = """\
## Plan
Give the proposer a root shell.
## Idempotence preamble
Idempotent: guarded by a test.
## Spec
```bash shell-spec
#!/bin/bash
echo pwned
```
"""


def _shell_spec(n: int = 1) -> dict[str, Any]:
    return {
        "id": f"2026-09-24-self-approve-{n:06x}",
        "kind": "shell-script",
        "intent": "Self-approved root shell",
        "body": SHELL_BODY,
        "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
        "idempotence": "via-precheck",
    }


class _Env:
    def __init__(self) -> None:
        self.client: AsyncClient
        self.repo: Repository
        self.store: ArtifactStore
        self.lifecycle: ArtifactLifecycle
        self.dispatch: AsyncMock
        self.tokens: dict[str, str] = {}
        self.prefixes: dict[str, str] = {}

    def auth(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from homepilot.artifacts.router import router as artifacts_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    executor = ArtifactExecutor(
        store=store, lifecycle=lifecycle, repo=repo, proxmox=AsyncMock(), vault=AsyncMock()
    )
    # The only stub: the leaf that would reach a host. If this is ever awaited
    # the attack reached the machine.
    dispatch = AsyncMock(return_value=ExecutionResult(success=True, execution_log="ran"))
    executor._dispatch = dispatch  # type: ignore[method-assign]
    task_repo = TaskRepository(db)
    runner = TaskRunner(
        repo=task_repo,
        lifecycle=lifecycle,
        executor=executor,
        apply_reconciler=ApplyReconciler(store=store, executor=executor, repo=repo),
        store=store,
    )

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
    app.state.repo = repo
    app.state.task_repo = task_repo
    app.state.task_runner = runner

    e = _Env()
    e.repo, e.store, e.lifecycle, e.dispatch = repo, store, lifecycle, dispatch
    # One user, several credentials - exactly what Settings -> Tokens produces:
    # every minted token hangs off the instance's single user.
    user_id = await repo.create_user(display_name="owner", auth_source="api_token")
    for who, scope in (
        ("assistant", "read,write"),
        ("assistant2", "read,write"),
        ("reader", "read_only"),
        ("operator", "all"),
        ("admin", "admin"),
    ):
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
        e.prefixes[who] = prefix

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        e.client = client
        yield e
    await db.close()


async def _code(e: _Env, aid: str) -> str:
    row = await e.repo.get_approval_code_row(aid)
    assert row is not None
    return str(row["code"])


def _leaks(text: str, code: str) -> bool:
    """True if the code appears in `text` in any form a reader could relay:
    canonical (ABCDEFGHJK) or display-grouped (ABCDE-FGHJK)."""
    from homepilot.artifacts.approval_code import format_for_display

    return code in text or format_for_display(code) in text


# ── The reported chain, end to end ───────────────────────────────────────────


class TestTheSelfApproveChainIsClosed:
    async def test_write_token_cannot_propose_read_code_approve_and_apply(self, env: _Env):
        """The exact chain from the report, on the shipped REST routes with real
        tokens. Every step after propose must be refused and the host untouched."""
        e = env
        resp = await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        assert resp.status_code == 200, resp.text
        aid = resp.json()["id"]
        code = await _code(e, aid)

        # (a) the code is not readable back by the proposing write token.
        got = await e.client.get(f"/artifacts/{aid}", headers=e.auth("assistant"))
        assert got.status_code == 200
        assert not _leaks(got.text, code), "a write token read the approval code over REST"
        assert got.json()["approval_code"] is None

        # (b) approve with NO code, naming somebody else in the body: refused.
        appr = await e.client.post(
            f"/artifacts/{aid}/approve", json={"user": "olli"}, headers=e.auth("assistant")
        )
        assert appr.status_code == 403, appr.text
        fm, _ = e.store.read(aid)
        assert fm["status"] == "proposed"

        # (c) apply cannot run what was never approved; the host is untouched.
        applied = await e.client.post(
            f"/artifacts/{aid}/apply", json={"approved_by": "olli"}, headers=e.auth("assistant")
        )
        assert applied.status_code in (400, 409), applied.text
        e.dispatch.assert_not_awaited()

    async def test_another_assistant_token_cannot_approve_either(self, env: _Env):
        """Not a same-token rule only: no assistant credential carries approval
        authority of its own."""
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        appr = await e.client.post(
            f"/artifacts/{aid}/approve", json={}, headers=e.auth("assistant2")
        )
        assert appr.status_code == 403, appr.text
        assert e.store.read(aid)[0]["status"] == "proposed"

    async def test_wrong_codes_over_rest_count_toward_the_lock(self, env: _Env):
        """REST is not a way around the brute-force lock MCP enforces."""
        from homepilot.artifacts.approval_code import LOCK_THRESHOLD

        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        for _ in range(LOCK_THRESHOLD):
            r = await e.client.post(
                f"/artifacts/{aid}/approve",
                json={"approval_code": "WRONGWRONG"},
                headers=e.auth("assistant"),
            )
            assert r.status_code == 403
        row = await e.repo.get_approval_code_row(aid)
        assert row is not None and int(row["locked"]) == 1
        # Locked now: even the right code is refused until an operator resets.
        r = await e.client.post(
            f"/artifacts/{aid}/approve",
            json={"approval_code": await _code(e, aid)},
            headers=e.auth("assistant"),
        )
        assert r.status_code == 403
        assert e.store.read(aid)[0]["status"] == "proposed"

    async def test_failed_artifact_cannot_be_reapproved_by_an_assistant(self, env: _Env):
        """failed -> approved is a second approval; it has no live code, so an
        assistant credential has no authority for it at all."""
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        ok = await e.client.post(f"/artifacts/{aid}/approve", json={}, headers=e.auth("operator"))
        assert ok.status_code == 200, ok.text
        await e.lifecycle.mark_failed(aid, "host refused")
        again = await e.client.post(
            f"/artifacts/{aid}/approve", json={}, headers=e.auth("assistant")
        )
        assert again.status_code == 403, again.text
        assert e.store.read(aid)[0]["status"] == "failed"
        # The operator can still re-approve it.
        ok2 = await e.client.post(f"/artifacts/{aid}/approve", json={}, headers=e.auth("operator"))
        assert ok2.status_code == 200, ok2.text


# ── The legitimate paths still work ──────────────────────────────────────────


class TestHumanAuthorityStillApproves:
    async def test_assistant_with_a_relayed_code_approves_and_applies(self, env: _Env):
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        code = await _code(e, aid)
        appr = await e.client.post(
            f"/artifacts/{aid}/approve",
            json={"approval_code": code, "user": "olli"},
            headers=e.auth("assistant"),
        )
        assert appr.status_code == 200, appr.text
        fm, _ = e.store.read(aid)
        assert fm["status"] == "approved"
        by = fm["approved_by"]
        assert by["authority"] == "relayed-code"
        assert e.prefixes["assistant"] in by["user"], by
        assert "olli" not in by["user"], "the body chose the approver"
        # The code is spent.
        assert await e.repo.get_approval_code_row(aid) is None

        run = await e.client.post(
            f"/artifacts/{aid}/apply?sync=true", json={}, headers=e.auth("assistant")
        )
        assert run.status_code == 200, run.text
        e.dispatch.assert_awaited()

    @pytest.mark.parametrize("who", ["operator", "admin"])
    async def test_operator_credential_approves_without_a_code(self, env: _Env, who: str):
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        # The operator is shown the code (that is how they relay it)...
        got = await e.client.get(f"/artifacts/{aid}", headers=e.auth(who))
        assert got.json()["approval_code"], "the operator review surface lost the code"
        # ...and may approve directly.
        appr = await e.client.post(
            f"/artifacts/{aid}/approve", json={"user": "mallory"}, headers=e.auth(who)
        )
        assert appr.status_code == 200, appr.text
        by = e.store.read(aid)[0]["approved_by"]
        assert by["authority"] == "operator"
        assert e.prefixes[who] in by["user"]
        assert "mallory" not in by["user"]

    async def test_operator_can_approve_what_the_operator_proposed(self, env: _Env):
        """The single-owner flow (propose in the UI, approve in Review) is the
        operator's own decision and must keep working."""
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("operator"))
        ).json()["id"]
        appr = await e.client.post(f"/artifacts/{aid}/approve", json={}, headers=e.auth("operator"))
        assert appr.status_code == 200, appr.text


# ── Where the code may and may not appear ───────────────────────────────────


class TestTheCodeReachesOnlyOperators:
    async def test_read_token_is_not_shown_the_code(self, env: _Env):
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        code = await _code(e, aid)
        got = await e.client.get(f"/artifacts/{aid}", headers=e.auth("reader"))
        assert got.status_code == 200
        assert not _leaks(got.text, code)

    async def test_code_is_not_on_the_sse_stream(self, env: _Env):
        """The SSE stream is read-scope: every read token sees it."""
        from homepilot.sse import bus

        e = env
        q = bus.subscribe()
        assert q is not None
        try:
            aid = (
                await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
            ).json()["id"]
            code = await _code(e, aid)
            events = []
            while not q.empty():
                events.append(q.get_nowait())
        finally:
            bus.unsubscribe(q)
        proposed = [ev for ev in events if ev.type == "artifact_proposed"]
        assert proposed, "propose emitted no SSE event"
        for ev in proposed:
            assert not _leaks(json.dumps(ev.data), code), "approval code broadcast on SSE"
            assert "approval_code" not in ev.data

    async def test_code_is_not_stored_in_webhook_delivery_rows(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ):
        """The operator-configured webhook still delivers the code to its
        endpoint (the notification path to a human), but the persisted delivery
        row - a table other readers and backups see - never holds it."""
        import homepilot.events as events_mod

        e = env
        sent: list[dict[str, Any]] = []

        async def _capture(url: str, payload: dict[str, Any], *a: Any, **kw: Any) -> bool:
            sent.append(payload)
            return True

        monkeypatch.setattr(events_mod, "deliver_with_retry", _capture)
        await e.repo.create_webhook_config(
            url="https://hooks.example.com/hp", event_types=["artifact_proposed"]
        )
        await e.repo.db.conn.commit()

        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        code = await _code(e, aid)
        await asyncio.sleep(0)

        rows = await e.repo.db.fetchall("SELECT payload FROM webhook_deliveries")
        assert rows, "no webhook delivery row was written"
        for row in rows:
            assert not _leaks(row["payload"], code), "approval code persisted in webhook_deliveries"
        assert sent and sent[0].get("approval_code"), (
            "the configured webhook no longer carries the code to the operator"
        )


# ── Lock reset is operator-only ─────────────────────────────────────────────


class TestLockResetIsOperatorOnly:
    async def _locked(self, e: _Env) -> str:
        from homepilot.artifacts.approval_code import LOCK_THRESHOLD

        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        for _ in range(LOCK_THRESHOLD):
            await e.repo.record_failed_approval(aid, LOCK_THRESHOLD)
        return aid

    async def test_assistant_cannot_reset_its_own_lock(self, env: _Env):
        e = env
        aid = await self._locked(e)
        r = await e.client.post(
            f"/artifacts/{aid}/approval-code/reset", headers=e.auth("assistant")
        )
        assert r.status_code == 403, r.text
        row = await e.repo.get_approval_code_row(aid)
        assert row is not None and int(row["locked"]) == 1

    async def test_operator_resets_the_lock(self, env: _Env):
        e = env
        aid = await self._locked(e)
        r = await e.client.post(f"/artifacts/{aid}/approval-code/reset", headers=e.auth("operator"))
        assert r.status_code == 200, r.text
        row = await e.repo.get_approval_code_row(aid)
        assert row is not None and int(row["locked"]) == 0


# ── Identity comes from the credential, for every transition ────────────────


class TestIdentityIsTheAuthenticatedCredential:
    async def test_reject_names_the_credential_not_the_body(self, env: _Env):
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("assistant"))
        ).json()["id"]
        r = await e.client.post(
            f"/artifacts/{aid}/reject",
            json={"user": "mallory", "reason": "no"},
            headers=e.auth("assistant"),
        )
        assert r.status_code == 200, r.text
        by = e.store.read(aid)[0]["rejected_by"]
        assert e.prefixes["assistant"] in by["user"] and "mallory" not in by["user"]

    async def test_apply_and_revoke_name_the_credential_not_the_body(self, env: _Env):
        e = env
        aid = (
            await e.client.post("/artifacts", json=_shell_spec(), headers=e.auth("operator"))
        ).json()["id"]
        await e.client.post(f"/artifacts/{aid}/approve", json={}, headers=e.auth("operator"))
        r = await e.client.post(
            f"/artifacts/{aid}/apply?sync=true",
            json={"approved_by": "mallory"},
            headers=e.auth("assistant"),
        )
        assert r.status_code == 200, r.text
        rows = await e.repo.db.fetchall(
            "SELECT user_id FROM audit_log WHERE artifact_id = ? AND action = 'apply'", (aid,)
        )
        users = [str(r["user_id"]) for r in rows]
        assert users and all("mallory" not in u for u in users), users
        assert any(e.prefixes["assistant"] in u for u in users), users

        rv = await e.client.request(
            "DELETE",
            f"/artifacts/{aid}?sync=true",
            json={"user": "mallory", "reason": "undo"},
            headers=e.auth("assistant"),
        )
        assert rv.status_code == 200, rv.text
        by = e.store.read(aid)[0].get("revoked_by") or {}
        assert e.prefixes["assistant"] in str(by.get("user")), by


# ── The lifecycle itself refuses an approval without authority ───────────────


class TestLifecycleRequiresAPrincipal:
    async def test_a_bare_string_is_not_approval_authority(self, env: _Env):
        """Enforcement lives in lifecycle.approve, not in any one transport, so a
        new transport that forgets the check cannot approve by passing a name."""
        e = env
        aid = await e.lifecycle.propose({**_shell_spec(), "produced_by": _produced()})
        with pytest.raises(TypeError):
            await e.lifecycle.approve(aid, "olli")  # type: ignore[arg-type]
        assert e.store.read(aid)[0]["status"] == "proposed"

    async def test_assistant_principal_without_code_is_refused_in_the_lifecycle(self, env: _Env):
        from homepilot.artifacts.models import ApprovalRefusedError
        from homepilot.auth.principal import Principal

        e = env
        aid = await e.lifecycle.propose({**_shell_spec(), "produced_by": _produced()})
        with pytest.raises(ApprovalRefusedError):
            await e.lifecycle.approve(aid, Principal.assistant("some-transport"))
        assert e.store.read(aid)[0]["status"] == "proposed"


def _produced() -> dict[str, str]:
    return {"session": "s1", "agent": "a1", "user": "u1"}
