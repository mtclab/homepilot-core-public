"""Malformed bodies on the artifact routes are a 4xx, never a 500 (#703).

The routes parsed ``await request.json()`` by hand with no handler behind it:
`POST /artifacts/{id}/approve` with no body raised JSONDecodeError, with ``[1]``
an AttributeError (``list.get``), and `/reject`, `DELETE /artifacts/{id}` and
`POST /artifacts` likewise - all answered 500. The bodies were also invisible to
the OpenAPI schema. Each route now declares a pydantic body, so FastAPI rejects
a bad one with 422 (and the app's handler strips the value, #628), and a good
one is documented.

The sweep below drives EVERY body-taking artifact route with every malformed
shape, so a route added later with a hand-parsed body fails here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

AID = "2026-09-24-bodies-a1b2c3"

# Every route under /artifacts that accepts a request body.
BODY_ROUTES: list[tuple[str, str]] = [
    ("POST", "/artifacts"),
    ("POST", f"/artifacts/{AID}/approve"),
    ("POST", f"/artifacts/{AID}/reject"),
    ("POST", f"/artifacts/{AID}/apply"),
    ("DELETE", f"/artifacts/{AID}"),
]

MALFORMED: dict[str, dict[str, Any]] = {
    "invalid-json": {"content": b"{not json", "headers": {"content-type": "application/json"}},
    "json-array": {"json": [1]},
    "json-string": {"json": "approve"},
    "wrong-field-type": {"json": {"reason": {"nested": True}, "approval_code": 12345}},
}


@pytest.fixture
async def client(tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.main import validation_error_handler

    db = Database(str(tmp_path / "hp.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    await lifecycle.propose(
        {
            "id": AID,
            "kind": "http-sequence",
            "intent": "bodies",
            "body": "```yaml http-spec\nsteps:\n  - method: GET\n    path: /health\n```\n",
            "target": {"kind": "service", "service": "demo", "vmid": 100, "node": "pve1"},
            "idempotence": "via-precheck",
            "produced_by": {"session": "s", "agent": "a", "user": "u"},
        }
    )
    from fastapi.exceptions import RequestValidationError

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
    app.state.repo = repo
    app.state.task_repo = AsyncMock()
    # The task layer is not under test: a started task answers like the real one.
    runner = AsyncMock()
    runner.start_apply.return_value = {"task_id": "t1", "artifact_id": AID, "action": "apply"}
    runner.start_revoke.return_value = {"task_id": "t2", "artifact_id": AID, "action": "revoke"}
    app.state.task_runner = runner
    app.dependency_overrides[require_token] = lambda: {
        "scope": "*",
        "label": "owner",
        "prefix": "hp_0123456789abc",
        "user_id": "u-1",
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.close()


@pytest.mark.parametrize("shape", list(MALFORMED))
@pytest.mark.parametrize(("method", "path"), BODY_ROUTES)
async def test_a_malformed_body_is_a_client_error(client, method, path, shape):
    if shape == "wrong-field-type" and path.endswith("/apply"):
        pytest.skip("apply declares no fields, so there is no field to have the wrong type")
    resp = await client.request(method, path, **MALFORMED[shape])
    assert 400 <= resp.status_code < 500, (
        f"{method} {path} with a {shape} body answered {resp.status_code}: {resp.text[:200]}"
    )


async def test_propose_with_no_body_is_a_client_error(client):
    resp = await client.post("/artifacts")
    assert 400 <= resp.status_code < 500, resp.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/artifacts/{AID}/approve"),
        ("POST", f"/artifacts/{AID}/reject"),
        ("DELETE", f"/artifacts/{AID}"),
    ],
)
async def test_the_body_is_optional_where_every_field_is(client, method, path):
    """approve/reject/revoke need nothing from the body: an empty one is a
    request, not an error."""
    resp = await client.request(method, path)
    assert resp.status_code < 500, resp.text
    assert resp.status_code != 422, resp.text


async def test_approve_with_no_body_actually_approves(client):
    """The goal, not the status: the artifact really is approved."""
    resp = await client.post(f"/artifacts/{AID}/approve")
    assert resp.status_code == 200, resp.text
    got = await client.get(f"/artifacts/{AID}")
    assert got.json()["frontmatter"]["status"] == "approved"


async def test_the_bodies_are_in_the_openapi_schema(client):
    schema = (await client.get("/openapi.json")).json()
    approve = schema["paths"]["/artifacts/{artifact_id}/approve"]["post"]
    assert "requestBody" in approve, "the approve body is undocumented"
    ref = approve["requestBody"]["content"]["application/json"]["schema"]
    text = str(schema["components"]["schemas"]) + str(ref)
    assert "approval_code" in text and "reason" in text
