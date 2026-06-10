from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app
from homepilot.config import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestArtifactsEdit:
    def test_edit_opens_editor_and_calls_lifecycle(self, tmp_path):
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        artifact_path = artifacts_dir / "2025" / "01"
        artifact_path.mkdir(parents=True)
        (artifact_path / "2025-01-01-test-edit-abc123.md").write_text(
            "---\nid: 2025-01-01-test-edit-abc123\nkind: ansible-playbook\n"
            "status: proposed\nmutating: true\nintent: Test\nhash: sha256:fake\n---\n\nbody"
        )

        mock_store = MagicMock()
        mock_store.resolve_path.return_value = artifact_path / "2025-01-01-test-edit-abc123.md"

        mock_lifecycle = MagicMock()
        mock_lifecycle.edit = MagicMock()

        env = {
            "HP_DATA_DIR": str(tmp_path),
            "HP_SECRET_KEY": "x" * 64,
            "HP_ARTIFACTS_DIR": str(artifacts_dir),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
            patch("homepilot.cli.main._get_lifecycle", return_value=mock_lifecycle),
            patch("homepilot.cli.main.subprocess.run") as mock_run,
        ):
            result = runner.invoke(app, ["artifacts", "edit", "2025-01-01-test-edit-abc123"])

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        editor_cmd = mock_run.call_args[0][0]
        assert editor_cmd[1] == str(artifact_path / "2025-01-01-test-edit-abc123.md")
        mock_lifecycle.edit.assert_called_once_with("2025-01-01-test-edit-abc123")

    def test_edit_uses_editor_env_variable(self, tmp_path):
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        artifact_path = artifacts_dir / "2025" / "01"
        artifact_path.mkdir(parents=True)
        (artifact_path / "2025-01-01-test-editor-env.md").write_text(
            "---\nid: 2025-01-01-test-editor-env\nkind: ansible-playbook\n"
            "status: proposed\nmutating: true\nintent: Test\nhash: sha256:fake\n---\n\nbody"
        )

        mock_store = MagicMock()
        mock_store.resolve_path.return_value = artifact_path / "2025-01-01-test-editor-env.md"
        mock_lifecycle = MagicMock()

        env = {
            "HP_DATA_DIR": str(tmp_path),
            "HP_SECRET_KEY": "x" * 64,
            "HP_ARTIFACTS_DIR": str(artifacts_dir),
            "EDITOR": "nano",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
            patch("homepilot.cli.main._get_lifecycle", return_value=mock_lifecycle),
            patch("homepilot.cli.main.subprocess.run") as mock_run,
        ):
            result = runner.invoke(app, ["artifacts", "edit", "2025-01-01-test-editor-env"])

        assert result.exit_code == 0
        editor_cmd = mock_run.call_args[0][0]
        assert editor_cmd[0] == "nano"

    def test_edit_invalid_id_exits_1(self, tmp_path):
        mock_store = MagicMock()
        mock_store.resolve_path.side_effect = ValueError("Invalid artifact ID: bad-id")

        env = {
            "HP_DATA_DIR": str(tmp_path),
            "HP_SECRET_KEY": "x" * 64,
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "edit", "bad-id"])

        assert result.exit_code == 1
        assert "Invalid artifact ID" in result.output

    def test_edit_missing_artifact_exits_1(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist.md"

        mock_store = MagicMock()
        mock_store.resolve_path.return_value = nonexistent

        env = {
            "HP_DATA_DIR": str(tmp_path),
            "HP_SECRET_KEY": "x" * 64,
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "edit", "2025-01-01-nonexistent-abc123"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_edit_lifecycle_error_prints_warning(self, tmp_path):
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        artifact_path = artifacts_dir / "2025" / "01"
        artifact_path.mkdir(parents=True)
        (artifact_path / "2025-01-01-test-lc-err-abc123.md").write_text(
            "---\nid: 2025-01-01-test-lc-err-abc123\nkind: ansible-playbook\n"
            "status: applied\nmutating: true\nintent: Test\nhash: sha256:fake\n---\n\nbody"
        )

        mock_store = MagicMock()
        mock_store.resolve_path.return_value = artifact_path / "2025-01-01-test-lc-err-abc123.md"

        mock_lifecycle = MagicMock()
        mock_lifecycle.edit.side_effect = RuntimeError("terminal state")

        env = {
            "HP_DATA_DIR": str(tmp_path),
            "HP_SECRET_KEY": "x" * 64,
            "HP_ARTIFACTS_DIR": str(artifacts_dir),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
            patch("homepilot.cli.main._get_lifecycle", return_value=mock_lifecycle),
            patch("homepilot.cli.main.subprocess.run"),
        ):
            result = runner.invoke(app, ["artifacts", "edit", "2025-01-01-test-lc-err-abc123"])

        assert "Edit sync" in result.output


class TestArtifactsShow:
    def test_show_displays_frontmatter_and_body(self, tmp_path):
        mock_store = MagicMock()
        mock_store.read.return_value = (
            {
                "id": "2025-01-01-test-show-abc123",
                "kind": "ansible-playbook",
                "status": "approved",
                "intent": "Install nginx",
                "mutating": True,
                "idempotence": "via-precheck",
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
            },
            "# Task\n\nInstall nginx on web server",
        )

        env = {
            "HP_DATA_DIR": str(tmp_path),
            "HP_SECRET_KEY": "x" * 64,
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "show", "2025-01-01-test-show-abc123"])

        assert result.exit_code == 0, result.output
        assert "2025-01-01-test-show-abc123" in result.output
        assert "kind=ansible-playbook" in result.output
        assert "Install nginx" in result.output

        mock_store.read.assert_called_once_with("2025-01-01-test-show-abc123")

    def test_show_includes_optional_fields(self, tmp_path):
        mock_store = MagicMock()
        mock_store.read.return_value = (
            {
                "id": "2025-01-01-test-fields-abc123",
                "kind": "shell-script",
                "status": "proposed",
                "intent": "Deploy app",
                "mutating": True,
                "idempotence": "replay-only",
                "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
                "tags": ["web", "deploy"],
                "rollback": True,
            },
            "# Deploy\n\nRun deployment script",
        )

        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "show", "2025-01-01-test-fields-abc123"])

        assert result.exit_code == 0, result.output
        assert "target" in result.output
        assert "rollback" in result.output
        assert "tags" in result.output

    def test_show_missing_artifact_exits_1(self, tmp_path):
        mock_store = MagicMock()
        mock_store.read.side_effect = FileNotFoundError("Artifact not found: nonexistent")

        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "show", "2025-01-01-nonexistent-abc123"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()
