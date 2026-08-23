"""A person can propose an artifact without hand-crafting bookkeeping (#445 A2).

Until now artifacts could only arrive over MCP or the CLI, so the web UI could
review and approve work but never originate it. The blocker was not the endpoint
- `POST /artifacts` existed - but what it demanded: an `id` matching a dated-slug
regex and a `produced_by` session/agent/user triple. An MCP client has those to
hand; a person filling in a form does not, and asking someone to invent an id
that satisfies a pattern is how a create screen becomes unusable.

The server now fills those gaps. These assert the OUTCOME: a spec shaped the way
a human would supply it becomes a real proposed artifact, the identity recorded
is the AUTHENTICATED one rather than whatever the client claimed, and anything
the caller did supply is left alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

# What a form actually collects. No id, no produced_by - that is the point.
HUMAN_SPEC = {
    "kind": "host-provision",
    "intent": "Install nginx on web01",
    "idempotence": "via-precheck",
    "target": {"kind": "service", "host": "web01", "service": "nginx"},
    "body": ("Install the web server.\n\n```yaml host-provision-spec\npackages:\n  - nginx\n```\n"),
}


@pytest.fixture
async def api(tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.artifact_store = store
    app.state.artifact_lifecycle = ArtifactLifecycle(store=store, repository=repo)
    app.state.repo = repo
    app.dependency_overrides[require_token] = lambda: {
        "scope": "*",
        "role": "admin",
        "user_id": "u-1",
        "display_name": "olli",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store
    await db.close()


class TestAFormShapedSpecIsEnough:
    async def test_a_human_spec_becomes_a_proposed_artifact(self, api):
        """The goal: what a form collects is sufficient, end to end."""
        client, store = api

        resp = await client.post("/artifacts", json=HUMAN_SPEC)

        assert resp.status_code == 200, resp.text
        artifact_id = resp.json()["id"]
        # The OUTCOME, not the response: it is really on disk and readable.
        fm, body = store.read(artifact_id)
        assert fm["status"] == "proposed"
        assert fm["kind"] == "host-provision"
        assert fm["intent"] == "Install nginx on web01"
        assert "nginx" in body

    async def test_the_generated_id_is_dated_and_derived_from_the_intent(self, api):
        """An id a person can recognise later, not an opaque uuid."""
        client, _store = api
        artifact_id = (await client.post("/artifacts", json=HUMAN_SPEC)).json()["id"]

        assert re.match(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+-[a-f0-9]{6}$", artifact_id), artifact_id
        assert "install-nginx-on-web01" in artifact_id

    async def test_two_proposals_with_the_same_intent_do_not_collide(self, api):
        """The id carries a random suffix precisely so a second attempt at the
        same thing is not rejected as a duplicate."""
        client, _store = api
        first = (await client.post("/artifacts", json=HUMAN_SPEC)).json()["id"]
        second = await client.post("/artifacts", json=HUMAN_SPEC)

        assert second.status_code == 200, second.text
        assert second.json()["id"] != first

    async def test_a_kb_note_needs_no_target_or_idempotence(self, api):
        """The non-mutating kind is the one a person is most likely to write, and
        it must not demand fields that do not apply to it."""
        client, store = api
        resp = await client.post(
            "/artifacts",
            json={"kind": "kb-note", "intent": "How the hub picks its cert", "body": "Notes."},
        )

        assert resp.status_code == 200, resp.text
        fm, _ = store.read(resp.json()["id"])
        assert fm["kind"] == "kb-note"


class TestTheRecordedIdentityIsTheRealOne:
    async def test_the_authenticated_user_is_recorded(self, api):
        client, store = api
        artifact_id = (await client.post("/artifacts", json=HUMAN_SPEC)).json()["id"]

        fm, _ = store.read(artifact_id)
        assert fm["produced_by"]["user"] == "olli"
        assert fm["produced_by"]["agent"] == "web"

    async def test_a_client_cannot_claim_to_be_someone_else(self, api):
        """The audit trail's whole value is naming who really did this, so a
        caller-supplied user must not win over the authenticated one."""
        client, store = api
        resp = await client.post(
            "/artifacts",
            json={
                **HUMAN_SPEC,
                "produced_by": {"session": "s", "agent": "a", "user": "somebody-else"},
            },
        )

        fm, _ = store.read(resp.json()["id"])
        assert fm["produced_by"]["user"] == "olli", (
            "a client's claimed identity overwrote the authenticated one"
        )


class TestItDoesNotOverwriteWhatWasSupplied:
    async def test_an_explicit_id_is_respected(self, api):
        """This fills gaps; it does not take decisions away from a caller that
        made them - the CLI and MCP still supply their own ids."""
        client, store = api
        resp = await client.post(
            "/artifacts", json={**HUMAN_SPEC, "id": "2026-08-21-explicit-id-abc123"}
        )

        assert resp.json()["id"] == "2026-08-21-explicit-id-abc123"
        fm, _ = store.read("2026-08-21-explicit-id-abc123")
        assert fm["id"] == "2026-08-21-explicit-id-abc123"

    async def test_an_invalid_spec_still_fails_with_the_reason(self, api):
        """Filling defaults must not paper over a genuinely bad proposal."""
        client, _store = api
        resp = await client.post("/artifacts", json={**HUMAN_SPEC, "body": ""})

        assert resp.status_code == 400
        assert "Body" in resp.json()["detail"]

    async def test_a_non_object_spec_is_refused_clearly(self, api):
        client, _store = api
        resp = await client.post("/artifacts", json=["not", "a", "spec"])
        assert resp.status_code == 400
