from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.artifacts.router import router as artifacts_router


def _make_fm(
    artifact_id: str = "2025-01-01-test-artifact-abc123",
    kind: str = "ansible-playbook",
    status: str = "approved",
    intent: str = "Install nginx",
) -> dict:
    return {
        "id": artifact_id,
        "kind": kind,
        "status": status,
        "intent": intent,
    }


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.root = Path("/tmp/fake-artifacts")
    return store


@pytest.fixture
def api_client(mock_store):
    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.artifact_store = mock_store
    app.state.artifact_lifecycle = MagicMock()
    app.state.task_repo = MagicMock()
    app.state.task_runner = MagicMock()

    from homepilot.auth import deps as auth_deps

    app.dependency_overrides[auth_deps.require_token] = lambda: {
        "scope": "*",
        "token_id": "test-token",
    }
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


class TestPreviewArtifact:
    def test_returns_404_for_nonexistent_artifact(self, api_client, mock_store):
        mock_store.read.side_effect = FileNotFoundError("not found")
        resp = api_client.post("/artifacts/2025-01-01-nonexistent-xyz000/preview")
        assert resp.status_code == 404

    def test_returns_correct_structure(self, api_client, mock_store):
        fm = _make_fm()
        body = "---\n- name: Install nginx\n  hosts: all\n"
        mock_store.read.return_value = (fm, body)
        mock_store.resolve_path.return_value = Path(
            "/tmp/fake-artifacts/2025/01/2025-01-01-test-artifact-abc123.md"
        )

        with patch("homepilot.artifacts.router.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="diff output here")
            resp = api_client.post("/artifacts/2025-01-01-test-artifact-abc123/preview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "2025-01-01-test-artifact-abc123"
        assert data["status"] == "approved"
        assert data["kind"] == "ansible-playbook"
        assert data["intent"] == "Install nginx"
        assert data["diff"] == "diff output here"
        assert data["body"] == body
        assert data["frontmatter"] == fm

    def test_returns_empty_diff_when_git_fails(self, api_client, mock_store):
        fm = _make_fm()
        body = "some body"
        mock_store.read.return_value = (fm, body)
        mock_store.resolve_path.return_value = Path(
            "/tmp/fake-artifacts/2025/01/2025-01-01-test-artifact-abc123.md"
        )

        with patch("homepilot.artifacts.router.subprocess.run") as mock_run:
            mock_run.side_effect = Exception("git not available")
            resp = api_client.post("/artifacts/2025-01-01-test-artifact-abc123/preview")

        assert resp.status_code == 200
        assert resp.json()["diff"] == ""
