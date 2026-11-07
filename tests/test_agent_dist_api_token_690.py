"""The agent payload download holds API tokens to the enrolment terms (#690).

`/agents/dist/*` accepts the hub's enrolment token (what a guest holds) or an
API token (the operator running the one-liner by hand). The API-token path
checked only prefix + hash: an EXPIRED token, and a READ-only one, downloaded
the installer and the agent binary - where every other enrolment surface
(bootstrap, install, the enrolment window) demands an admin token, and every
other route refuses an expired one.

Teeth: drop the expiry or the scope check in `_enrolment_credential` and the
expired / read / write cases below get 200.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.tokens import generate_api_token

ENROLMENT_TOKEN = "hub-enrolment-token-for-690"  # pragma: allowlist secret - test fixture
BINARY = "/agents/dist/hp-agent-linux-amd64"


class _Repo:
    """Stands in for Repository.get_token_by_prefix over real token hashes."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def mint(
        self, scope: str | None, *, role: str | None = None, expires_at: str | None = None
    ) -> str:
        full, prefix, token_hash = generate_api_token()
        self.rows[prefix] = {
            "id": len(self.rows) + 1,
            "hash": token_hash,
            "scope": scope,
            "role": role,
            "expires_at": expires_at,
        }
        return full

    async def get_token_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        return self.rows.get(prefix)


@pytest.fixture
def repo() -> _Repo:
    return _Repo()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: _Repo) -> TestClient:
    root = tmp_path / "agent-dist"
    root.mkdir()
    (root / "hp-agent-linux-amd64").write_bytes(b"\x7fELF" + b"amd64 agent payload")
    (root / "install-agent.sh").write_text("#!/usr/bin/env bash\necho installer\n")
    monkeypatch.setenv("HP_AGENT_DIST_DIR", str(root))

    from homepilot.agent_hub.router import router as agents_router

    app = FastAPI()
    app.include_router(agents_router)

    class _Hub:
        auth_token = ENROLMENT_TOKEN

    class _Registry:
        hub_server = _Hub()

    app.state.agent_registry = _Registry()
    app.state.repo = repo
    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _future() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).isoformat()


def _past() -> str:
    return (datetime.now(UTC) - timedelta(minutes=1)).isoformat()


def test_an_unexpired_admin_token_downloads(client: TestClient, repo: _Repo) -> None:
    for token in (
        repo.mint("admin", expires_at=_future()),
        repo.mint("all"),
        repo.mint(None, role="admin"),
    ):
        resp = client.get(BINARY, headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        assert resp.content.startswith(b"\x7fELF")


def test_an_expired_admin_token_is_refused(client: TestClient, repo: _Repo) -> None:
    token = repo.mint("admin", expires_at=_past())
    for path in (BINARY, "/agents/dist/install-agent.sh"):
        resp = client.get(path, headers=_bearer(token))
        assert resp.status_code == 401, (path, resp.status_code)


@pytest.mark.parametrize("scope", ["read", "read_only", "write", "read,write"])
def test_a_non_admin_token_is_refused(client: TestClient, repo: _Repo, scope: str) -> None:
    token = repo.mint(scope, expires_at=_future())
    resp = client.get(BINARY, headers=_bearer(token))
    assert resp.status_code == 403, resp.status_code
    assert b"ELF" not in resp.content


def test_the_guests_enrolment_token_still_works(client: TestClient) -> None:
    resp = client.get(BINARY, headers={"x-hp-agent-token": ENROLMENT_TOKEN})
    assert resp.status_code == 200
