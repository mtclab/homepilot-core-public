"""An edit runs the same validation propose does (#652).

WHAT THIS FORBIDS: ``edit`` recomputed the hash and sent an approved artifact
back to ``proposed``, and checked nothing else. So every propose-time refusal
could be walked around by proposing something valid and editing it afterwards -
a composite step losing its ``artifact:`` reference, a spec block that no
longer parses, a ``skip_if`` the evaluator refuses, a literal credential in a
body bound for a git repository, a ``{{ name }}`` nothing defines - and the
result went on to approval.

The contract now: ``validate_artifact_content`` is the ONE content pipeline;
propose and both edit paths (``edit_content`` - the REST route and the CLI -
and ``edit``) run it, and a refused edit writes nothing. Any accepted edit
replaces the approval code, so a code a human read off the old content cannot
approve the new content.

One case per refusal CLASS. Teeth: delete the ``validate_artifact_content``
call from ``edit_content`` and every class below is accepted (200, file
changed); drop the code rotation and the stale code approves the new body.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import ApprovalRefusedError, LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.principal import Principal
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

_PRODUCED = {"session": "s1", "agent": "a1", "user": "u1"}
_VM = {"kind": "vm", "vmid": 100, "node": "pve1"}

ANSIBLE = {
    "id": "2026-09-26-edit-ansible-aaa111",
    "kind": "ansible-playbook",
    "intent": "Install nginx",
    "body": "---\n- name: x\n  hosts: all\n  tasks: []\n",
    "target": _VM,
    "idempotence": "via-precheck",
    "produced_by": _PRODUCED,
}
SHELL = {
    "id": "2026-09-26-edit-shell-bbb222",
    "kind": "shell-script",
    "intent": "Tune a service",
    "body": (
        "## Plan\nx\n## Idempotence preamble\nIdempotent: guarded.\n## Spec\n"
        "```bash shell-spec\n#!/bin/bash\necho tuned\n```\n"
    ),
    "target": {**_VM, "host": "web1"},
    "idempotence": "via-precheck",
    "produced_by": _PRODUCED,
}
COMPOSITE = {
    "id": "2026-09-26-edit-composite-ccc333",
    "kind": "composite",
    "intent": "Deploy in two steps",
    "body": (
        "## Plan\nTwo steps\n\n## Spec\n\n```yaml composite-spec\nsteps:\n"
        "  - id: first\n    artifact: 2026-09-26-sub-first-ddd444\n    on_error: halt\n"
        "  - id: second\n    artifact: 2026-09-26-sub-second-eee555\n    on_error: halt\n```\n"
    ),
    "target": _VM,
    "idempotence": "via-precheck",
    "produced_by": _PRODUCED,
}


def _sub(old: str, new: str) -> Callable[[str], str]:
    def edit(text: str) -> str:
        assert old in text, f"fixture drift: {old!r} is not in the artifact"
        return text.replace(old, new, 1)

    return edit


# (case id, the artifact proposed, the edit, what the refusal must say)
CLASSES: list[tuple[str, dict[str, Any], Callable[[str], str], str]] = [
    (
        "composite-step-loses-its-artifact",
        COMPOSITE,
        _sub("    artifact: 2026-09-26-sub-second-eee555\n", ""),
        "'artifact' reference",
    ),
    (
        "spec-block-stops-parsing",
        COMPOSITE,
        _sub("steps:\n", "steps: [unclosed\n"),
        "not valid YAML",
    ),
    (
        "skip_if-the-evaluator-refuses",
        ANSIBLE,
        _sub("intent: Install nginx\n", "intent: Install nginx\nskip_if: open('x')\n"),
        "skip_if",
    ),
    (
        "literal-credential-in-the-body",
        SHELL,
        _sub("echo tuned\n", "echo tuned\npassword=hunter2hunter2\n"),  # pragma: allowlist secret
        "literal credential",
    ),
    (
        "name-not-in-the-interpolation-context",
        ANSIBLE,
        _sub("tasks: []\n", "tasks: []\n# {{ no_such_name }}\n"),
        "undefined",
    ),
    (
        "rollback-claim-the-body-cannot-honour",
        ANSIBLE,
        _sub("rollback: false", "rollback: true"),
        "rollback: true",
    ),
    (
        "target-that-is-not-a-target",
        ANSIBLE,
        _sub("vmid: 100", "vmid: not-a-number"),
        "Invalid target",
    ),
]


async def _propose(lifecycle: ArtifactLifecycle, spec: dict[str, Any]) -> None:
    """Propose `spec`, and first the sub-artifacts a composite names - approving
    a composite cascades to its steps and refuses ones that do not exist."""
    if spec["kind"] == "composite":
        for sub in ("2026-09-26-sub-first-ddd444", "2026-09-26-sub-second-eee555"):
            await lifecycle.propose({**ANSIBLE, "id": sub, "intent": f"step {sub}"})
    await lifecycle.propose(dict(spec))


@pytest.fixture
async def estate(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store, repository=repo)
    yield store, lifecycle, repo
    await db.close()


def _client(lifecycle: ArtifactLifecycle) -> httpx.AsyncClient:
    from fastapi import FastAPI

    from homepilot.artifacts.edit_router import router as edit_router
    from homepilot.auth.deps import require_token

    app = FastAPI()
    app.include_router(edit_router, prefix="/artifacts")
    app.state.artifact_lifecycle = lifecycle
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "prefix": "hp_test"}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.parametrize(
    ("spec", "edit", "says"),
    [pytest.param(spec, edit, says, id=case) for case, spec, edit, says in CLASSES],
)
async def test_an_edit_into_a_shape_propose_refuses_is_refused(estate, spec, edit, says):
    store, lifecycle, _repo = estate
    await _propose(lifecycle, spec)
    path = store.resolve_path(spec["id"])
    before = path.read_text(encoding="utf-8")

    async with _client(lifecycle) as c:
        resp = await c.post(f"/artifacts/{spec['id']}/edit", json={"content": edit(before)})

    assert resp.status_code == 400, f"the edit was accepted: {resp.text}"
    assert says in resp.json()["detail"]
    assert path.read_text(encoding="utf-8") == before, "a refused edit changed the file"


@pytest.mark.parametrize(
    ("spec", "edit", "says"),
    [pytest.param(spec, edit, says, id=case) for case, spec, edit, says in CLASSES],
)
async def test_an_approved_artifact_edited_into_a_refused_shape_keeps_nothing(
    estate, spec, edit, says
):
    """The worse case: approval already given, then the content goes bad."""
    store, lifecycle, _repo = estate
    await _propose(lifecycle, spec)
    await lifecycle.approve(spec["id"], Principal.operator("operator"))
    path = store.resolve_path(spec["id"])
    before = path.read_text(encoding="utf-8")

    with pytest.raises(LifecycleError, match=says.replace("[", r"\[")):
        from homepilot.artifacts.edit_router import submit_edit

        await submit_edit(lifecycle, spec["id"], edit(before), "operator")

    assert path.read_text(encoding="utf-8") == before


async def test_the_record_an_edit_path_validates_too(estate):
    """`lifecycle.edit` records an edit already written to the file; it refuses
    to record one propose would refuse, so the stale hash keeps approval shut."""
    store, lifecycle, _repo = estate
    await lifecycle.propose(dict(SHELL))
    path = store.resolve_path(SHELL["id"])
    leaked = "token: abcdefgh12345678\n"  # pragma: allowlist secret
    path.write_text(
        path.read_text(encoding="utf-8").replace("echo tuned\n", leaked), encoding="utf-8"
    )

    with pytest.raises(LifecycleError, match="literal credential"):
        await lifecycle.edit(SHELL["id"])
    with pytest.raises(LifecycleError, match="Hash mismatch"):
        await lifecycle.approve(SHELL["id"], Principal.operator("operator"))


async def test_a_valid_edit_is_accepted_and_sends_an_approval_back(estate):
    store, lifecycle, _repo = estate
    await lifecycle.propose(dict(ANSIBLE))
    await lifecycle.approve(ANSIBLE["id"], Principal.operator("operator"))
    path = store.resolve_path(ANSIBLE["id"])
    text = path.read_text(encoding="utf-8").replace("tasks: []\n", "tasks: []\n# tuned\n")

    async with _client(lifecycle) as c:
        resp = await c.post(f"/artifacts/{ANSIBLE['id']}/edit", json={"content": text})

    assert resp.status_code == 200, resp.text
    fm, body = store.read(ANSIBLE["id"])
    assert "# tuned" in body
    assert fm["status"] == "proposed"
    assert "approved_by" not in fm


async def test_a_code_read_off_the_old_content_does_not_approve_the_new(estate):
    store, lifecycle, repo = estate
    await lifecycle.propose(dict(ANSIBLE))
    old_code = (await repo.get_approval_code_row(ANSIBLE["id"]))["code"]

    path = store.resolve_path(ANSIBLE["id"])
    text = path.read_text(encoding="utf-8").replace("tasks: []\n", "tasks: []\n# changed\n")
    from homepilot.artifacts.edit_router import submit_edit

    await submit_edit(lifecycle, ANSIBLE["id"], text, "assistant (hp_assist)")

    new_code = (await repo.get_approval_code_row(ANSIBLE["id"]))["code"]
    assert new_code != old_code
    assistant = Principal.assistant("assistant (hp_assist)")
    with pytest.raises(ApprovalRefusedError):
        await lifecycle.approve(ANSIBLE["id"], assistant, approval_code=old_code)
    await lifecycle.approve(ANSIBLE["id"], assistant, approval_code=new_code)
    assert store.read(ANSIBLE["id"])[0]["status"] == "approved"
