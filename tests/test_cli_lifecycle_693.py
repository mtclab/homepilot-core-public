"""`hp artifacts approve|reject|edit` go through the lifecycle that owns the DB (#693).

WHAT THIS FORBIDS, both reproduced on the shipped CLI:

* After ``hp artifacts approve`` the FILE said approved while the DB row still
  said proposed and the audit log held only ``propose``: the CLI built
  ``ArtifactLifecycle(store)`` with no repository, so the DB sync, the audit
  entry and the per-config webhooks were all no-ops. Dashboard counts, "N
  changes awaiting review" and the host Changes tab stayed stale - and with the
  backend running, a second process was writing its data behind its back.
* ``hp artifacts edit`` on an APPLIED artifact opened $EDITOR, let the edit be
  saved into the applied artifact's file, printed a dim line and exited 0,
  leaving a tampered file with a stale hash.

The contract now: with the backend RUNNING, approve/reject/edit are requests to
its API (the same routes the UI uses); with it STOPPED, the CLI runs the
lifecycle with the real repository. An edit is refused before the editor opens
for a terminal artifact, the editor works on a COPY, and a refused edit leaves
the artifact's file exactly as it was and exits non-zero. An edit may not
rewrite the fields the lifecycle owns (status, hash, approvals), and any change
to an approved artifact sends it back for review.

Teeth: restore ``_get_lifecycle()`` without a repository and the DB/audit
assertions fail; open $EDITOR on the store file again and the applied-artifact
test finds the injected line; drop the running-backend branch and the API
tests see a direct write.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from homepilot.cli import main as cli

SPEC = {
    "id": "2026-09-26-cli-lifecycle-abc123",
    "kind": "ansible-playbook",
    "intent": "Install nginx",
    "body": "---\n- name: x\n  hosts: all\n  tasks: []",
    "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
    "idempotence": "via-precheck",
    "produced_by": {"session": "s1", "agent": "a1", "user": "u"},
}
AID = SPEC["id"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "artifacts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HP_DATA_DIR", str(data))
    monkeypatch.setenv("HP_ARTIFACTS_DIR", str(data / "artifacts"))
    from homepilot.config import get_settings

    get_settings.cache_clear()
    asyncio.run(_propose(data))
    yield data
    get_settings.cache_clear()


async def _open(data: Path) -> Any:
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(data / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    return db


async def _propose(data: Path) -> None:
    from homepilot.artifacts.lifecycle import ArtifactLifecycle
    from homepilot.artifacts.store import ArtifactStore
    from homepilot.db.repository import Repository

    db = await _open(data)
    try:
        lc = ArtifactLifecycle(ArtifactStore(data / "artifacts"), repository=Repository(db))
        await lc.propose(dict(SPEC))
    finally:
        await db.close()


async def _db_state(data: Path) -> tuple[str, list[str]]:
    db = await _open(data)
    try:
        row = await db.fetchone("SELECT status FROM artifacts WHERE id=?", (AID,))
        audits = await db.fetchall(
            "SELECT action FROM audit_log WHERE artifact_id=? ORDER BY id", (AID,)
        )
        return dict(row)["status"], [dict(a)["action"] for a in audits]
    finally:
        await db.close()


def _file(data: Path) -> Path:
    from homepilot.artifacts.store import ArtifactStore

    return ArtifactStore(data / "artifacts").resolve_path(AID)


def _set_status(data: Path, status: str) -> None:
    p = _file(data)
    p.write_text(p.read_text().replace("status: proposed", f"status: {status}"))


def _editor(data: Path, monkeypatch, script: str) -> Path:
    marker = data / "editor-ran"
    editor = data / "ed.sh"
    editor.write_text(f"#!/bin/sh\ntouch '{marker}'\n{script}\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    return marker


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli.app, ["artifacts", *args])


# ── Backend stopped: the lifecycle runs here, WITH the repository ─────────────


def test_approve_updates_the_db_and_the_audit_log(home):
    r = _run("approve", AID)

    assert r.exit_code == 0, r.output
    status, audits = asyncio.run(_db_state(home))
    assert status == "approved", "the file says approved but the DB row does not"
    assert "approve" in audits, f"no approve entry in the audit log: {audits}"


def test_reject_updates_the_db_and_the_audit_log(home):
    r = _run("reject", AID, "--reason", "not now")

    assert r.exit_code == 0, r.output
    status, audits = asyncio.run(_db_state(home))
    assert status == "rejected"
    assert "reject" in audits


def test_edit_of_an_applied_artifact_is_refused_before_the_editor_opens(home, monkeypatch):
    _set_status(home, "applied")
    before = _file(home).read_text()
    marker = _editor(home, monkeypatch, "echo 'rm -rf / # injected' >> \"$1\"")

    r = _run("edit", AID)

    assert r.exit_code != 0, r.output
    assert not marker.exists(), "the editor was opened on a terminal artifact"
    assert _file(home).read_text() == before, "the applied artifact's file was changed"


def test_an_edit_is_recorded_in_the_db_and_the_audit_log(home, monkeypatch):
    _editor(home, monkeypatch, "echo '# tuned' >> \"$1\"")

    r = _run("edit", AID)

    assert r.exit_code == 0, r.output
    assert "# tuned" in _file(home).read_text()
    _status, audits = asyncio.run(_db_state(home))
    assert "edit" in audits


def test_an_edit_may_not_rewrite_the_lifecycle_fields(home, monkeypatch):
    before = _file(home).read_text()
    _editor(home, monkeypatch, "sed -i 's/^status: proposed/status: approved/' \"$1\"")

    r = _run("edit", AID)

    assert r.exit_code != 0, r.output
    assert "status" in r.output
    assert _file(home).read_text() == before, "a refused edit left the file changed"


def test_any_change_to_an_approved_artifact_sends_it_back_for_review(home, monkeypatch):
    assert _run("approve", AID).exit_code == 0
    # Frontmatter only - the body hash does not move.
    _editor(home, monkeypatch, "sed -i 's/^intent: Install nginx/intent: Install apache/' \"$1\"")

    r = _run("edit", AID)

    assert r.exit_code == 0, r.output
    text = _file(home).read_text()
    assert "Install apache" in text
    assert "status: proposed" in text, "an approved artifact changed and kept its approval"
    status, _audits = asyncio.run(_db_state(home))
    assert status == "proposed"


# ── Backend running: the CLI asks the API and writes nothing itself ──────────


class FakeApi:
    def __init__(self, answer: int = 200) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, bytes]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, request.content))
        if request.url.path.startswith("/auth/tokens/"):
            return httpx.Response(204)
        if self.answer >= 400:
            return httpx.Response(self.answer, json={"detail": "refused by the backend"})
        return httpx.Response(200, json={"id": AID, "status": "ok", "changed": True})


@pytest.fixture
def running(home, monkeypatch):
    from homepilot.instance_lock import InstanceLock

    api = FakeApi()

    async def mint(*_a: Any, **_k: Any) -> tuple[str, None]:
        return "hp_" + "a" * 64, None

    monkeypatch.setattr(cli, "_mint_token_via_api", mint)
    monkeypatch.setattr(
        cli,
        "_backend_http_client",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(api.handler), base_url="http://127.0.0.1"
        ),
    )
    with InstanceLock(home):
        yield api


def _api_paths(api: FakeApi) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _b in api.calls if not p.startswith("/auth/")]


def test_approve_with_the_backend_running_goes_through_the_api(home, running):
    before = _file(home).read_text()

    r = _run("approve", AID)

    assert r.exit_code == 0, r.output
    assert ("POST", f"/artifacts/{AID}/approve") in _api_paths(running)
    assert _file(home).read_text() == before, "the CLI wrote the artifact behind the backend"


def test_approve_through_the_api_carries_operator_authority(home, running, monkeypatch):
    """#693 x #681: the API decides approval authority from the CREDENTIAL. A
    write-tier token is an assistant there and is refused without a relayed
    code, so the on-box CLI must approve with the operator (admin) capability -
    and it must not claim an identity in the body, which the API ignores."""
    scopes: list[str] = []

    async def mint(_settings: Any, _label: str, scope: str, **_k: Any) -> tuple[str, None]:
        scopes.append(scope)
        return "hp_" + "a" * 64, None

    monkeypatch.setattr(cli, "_mint_token_via_api", mint)

    assert _run("approve", AID).exit_code == 0
    assert scopes == ["admin"], f"approve minted {scopes}, not the operator capability"
    body = next(b for m, p, b in running.calls if p == f"/artifacts/{AID}/approve")
    assert b'"user"' not in body, "the CLI still names itself in the request body"

    scopes.clear()
    assert _run("reject", AID).exit_code == 0
    assert scopes == ["write"], "reject needs no more than write"


def test_reject_with_the_backend_running_goes_through_the_api(home, running):
    r = _run("reject", AID)

    assert r.exit_code == 0, r.output
    assert ("POST", f"/artifacts/{AID}/reject") in _api_paths(running)


def test_a_refused_api_approve_exits_non_zero(home, running):
    running.answer = 409

    r = _run("approve", AID)

    assert r.exit_code != 0
    assert "refused by the backend" in r.output


def test_edit_with_the_backend_running_submits_the_edited_copy(home, running, monkeypatch):
    before = _file(home).read_text()
    _editor(home, monkeypatch, "echo '# tuned' >> \"$1\"")

    r = _run("edit", AID)

    assert r.exit_code == 0, r.output
    posts = [(m, p, b) for m, p, b in running.calls if p == f"/artifacts/{AID}/edit"]
    assert posts, "the edit never reached the backend"
    assert b"# tuned" in posts[0][2]
    assert _file(home).read_text() == before, "the CLI wrote the artifact behind the backend"


def test_a_refused_api_edit_exits_non_zero_and_changes_nothing(home, running, monkeypatch):
    running.answer = 409
    before = _file(home).read_text()
    _editor(home, monkeypatch, "echo '# tuned' >> \"$1\"")

    r = _run("edit", AID)

    assert r.exit_code != 0
    assert _file(home).read_text() == before


# ── The route the running backend answers the edit on ────────────────────────


async def test_the_edit_route_records_refuses_and_protects(tmp_path):
    from fastapi import FastAPI

    from homepilot.artifacts.edit_router import router as edit_router
    from homepilot.artifacts.lifecycle import ArtifactLifecycle
    from homepilot.artifacts.store import ArtifactStore
    from homepilot.auth.deps import require_token
    from homepilot.db.repository import Repository

    db = await _open(tmp_path)
    try:
        store = ArtifactStore(tmp_path / "artifacts")
        lifecycle = ArtifactLifecycle(store, repository=Repository(db))
        await lifecycle.propose(dict(SPEC))
        app = FastAPI()
        app.include_router(edit_router, prefix="/artifacts")
        app.state.artifact_lifecycle = lifecycle
        app.dependency_overrides[require_token] = lambda: {"scope": "*", "prefix": "hp_test"}
        original = store.resolve_path(AID).read_text()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            ok = await c.post(f"/artifacts/{AID}/edit", json={"content": original + "# more\n"})
            assert ok.status_code == 200, ok.text
            assert "# more" in store.resolve_path(AID).read_text()

            owned = await c.post(
                f"/artifacts/{AID}/edit",
                json={"content": original.replace("status: proposed", "status: approved")},
            )
            assert owned.status_code == 400
            assert "status" in owned.json()["detail"]

            malformed = await c.post(f"/artifacts/{AID}/edit", json={"content": "no frontmatter"})
            assert malformed.status_code == 400

            text = store.resolve_path(AID).read_text()
            store.resolve_path(AID).write_text(text.replace("status: proposed", "status: applied"))
            terminal = await c.post(f"/artifacts/{AID}/edit", json={"content": original})
            assert terminal.status_code == 409

            missing = await c.post(
                "/artifacts/2026-09-26-nope-000000/edit", json={"content": original}
            )
            assert missing.status_code == 404
    finally:
        await db.close()
