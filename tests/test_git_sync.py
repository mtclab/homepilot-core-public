from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from homepilot.artifacts.store import ArtifactStore, GitOperationError, GitOperationErrorCategory
from homepilot.cli.main import app
from homepilot.config import get_settings

runner = pytest.importorskip("typer.testing").CliRunner()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_store(tmp_path, remote: str = "", ssh_key: str = "") -> ArtifactStore:
    artifacts_dir = tmp_path / "artifacts"
    return ArtifactStore(artifacts_dir, remote=remote, ssh_key=ssh_key)


class TestPushCallsGitPush:
    def test_push_calls_git_push(self, tmp_path):
        store = _make_store(tmp_path, remote="https://example.com/repo.git")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"Everything up-to-date\n"
        mock_result.stderr = b""

        with (
            patch.object(store, "_check_remote_exists", return_value=True),
            patch.object(store, "_current_branch", return_value="master"),
            patch.object(store, "_run_git", return_value=mock_result) as mock_run,
        ):
            output = store.push(remote="origin")

        assert "Everything up-to-date" in output
        args = mock_run.call_args
        called_args = args[0][0] if args[0] else args[1].get("args", [])
        assert called_args[0] == "push"
        assert "origin" in called_args

    def test_push_uses_current_branch(self, tmp_path):
        store = _make_store(tmp_path, remote="https://example.com/repo.git")

        mock_push_result = MagicMock()
        mock_push_result.returncode = 0
        mock_push_result.stdout = b"Pushed\n"
        mock_push_result.stderr = b""

        with (
            patch.object(store, "_check_remote_exists", return_value=True),
            patch.object(store, "_current_branch", return_value="main"),
            patch.object(store, "_run_git", return_value=mock_push_result) as mock_run,
        ):
            store.push(remote="origin")

        called_args = mock_run.call_args[0][0]
        assert called_args == ["push", "origin", "main"]


class TestPullCallsGitPullFfOnly:
    def test_pull_calls_git_pull_ff_only(self, tmp_path):
        store = _make_store(tmp_path, remote="https://example.com/repo.git")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"Already up to date.\n"
        mock_result.stderr = b""

        with (
            patch.object(store, "_check_remote_exists", return_value=True),
            patch.object(store, "_current_branch", return_value="master"),
            patch.object(store, "_run_git", return_value=mock_result) as mock_run,
        ):
            output = store.pull(remote="origin")

        assert "Already up to date" in output
        args = mock_run.call_args
        called_args = args[0][0] if args[0] else args[1].get("args", [])
        assert "--ff-only" in called_args
        assert called_args[0] == "pull"


class TestPushNoRemote:
    def test_push_no_remote_shows_message(self, tmp_path):
        store = _make_store(tmp_path)

        with (
            patch.object(store, "_check_remote_exists", return_value=False),
            pytest.raises(GitOperationError) as exc_info,
        ):
            store.push(remote="origin")

        assert "No git remote" in str(exc_info.value)
        assert "origin" in str(exc_info.value)

    def test_pull_no_remote_shows_message(self, tmp_path):
        store = _make_store(tmp_path)

        with (
            patch.object(store, "_check_remote_exists", return_value=False),
            pytest.raises(GitOperationError) as exc_info,
        ):
            store.pull(remote="origin")

        assert "No git remote" in str(exc_info.value)
        assert "origin" in str(exc_info.value)


class TestPullConflictFails:
    def test_pull_conflict_fails(self, tmp_path):
        store = _make_store(tmp_path, remote="https://example.com/repo.git")

        with (
            patch.object(store, "_check_remote_exists", return_value=True),
            patch.object(store, "_current_branch", return_value="master"),
            patch.object(
                store,
                "_run_git",
                side_effect=GitOperationError(
                    GitOperationErrorCategory.UNKNOWN,
                    "pull",
                    "Not possible to fast-forward, aborting.",
                ),
            ),
            pytest.raises(GitOperationError) as exc_info,
        ):
            store.pull(remote="origin")

        assert (
            "fast-forward" in str(exc_info.value).lower()
            or "aborting" in str(exc_info.value).lower()
        )


class TestSyncStatus:
    def test_sync_status_returns_status_and_log(self, tmp_path):
        store = _make_store(tmp_path)

        status_result = MagicMock()
        status_result.returncode = 0
        status_result.stdout = b" M 2025/01/test.md\n"

        log_result = MagicMock()
        log_result.returncode = 0
        log_result.stdout = b"abc1234 init: add README\ndef5678 init: add .gitattributes\n"

        call_count = 0

        def _mock_run_git(args, operation, **kwargs):
            nonlocal call_count
            call_count += 1
            if args[0] == "status":
                return status_result
            if args[0] == "log":
                return log_result
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch.object(store, "_run_git", side_effect=_mock_run_git):
            info = store.sync_status()

        assert "test.md" in info["status"]
        assert "abc1234" in info["log"]


class TestCLIPush:
    def test_cli_push_success(self, tmp_path):
        mock_store = MagicMock()
        mock_store.push.return_value = "Pushed 2 commits\n"

        env = {
            "HP_DATA_DIR": str(tmp_path),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "push"])

        assert result.exit_code == 0, result.output
        assert "Pushed to origin" in result.output
        mock_store.push.assert_called_once_with(remote="origin")

    def test_cli_push_with_remote(self, tmp_path):
        mock_store = MagicMock()
        mock_store.push.return_value = "Pushed\n"

        env = {
            "HP_DATA_DIR": str(tmp_path),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "push", "--remote", "upstream"])

        assert result.exit_code == 0, result.output
        mock_store.push.assert_called_once_with(remote="upstream")

    def test_cli_push_no_remote_exits_1(self, tmp_path):
        mock_store = MagicMock()
        mock_store.push.side_effect = GitOperationError(
            GitOperationErrorCategory.UNKNOWN,
            "push",
            "No git remote 'origin' configured.",
        )

        env = {
            "HP_DATA_DIR": str(tmp_path),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "push"])

        assert result.exit_code == 1
        assert "Push failed" in result.output


class TestCLIPull:
    def test_cli_pull_success(self, tmp_path):
        mock_store = MagicMock()
        mock_store.pull.return_value = "Already up to date.\n"

        env = {
            "HP_DATA_DIR": str(tmp_path),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "pull"])

        assert result.exit_code == 0, result.output
        assert "Pulled from origin" in result.output
        mock_store.pull.assert_called_once_with(remote="origin")

    def test_cli_pull_conflict_exits_1(self, tmp_path):
        mock_store = MagicMock()
        mock_store.pull.side_effect = GitOperationError(
            GitOperationErrorCategory.UNKNOWN,
            "pull",
            "Not possible to fast-forward, aborting.",
        )

        env = {
            "HP_DATA_DIR": str(tmp_path),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "pull"])

        assert result.exit_code == 1
        assert "Pull failed" in result.output


class TestCLISyncStatus:
    def test_cli_sync_status_clean(self, tmp_path):
        mock_store = MagicMock()
        mock_store.sync_status.return_value = {
            "status": "",
            "log": "abc1234 init\n",
        }

        env = {
            "HP_DATA_DIR": str(tmp_path),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "sync-status"])

        assert result.exit_code == 0, result.output
        assert "Working tree clean" in result.output

    def test_cli_sync_status_dirty(self, tmp_path):
        mock_store = MagicMock()
        mock_store.sync_status.return_value = {
            "status": " M 2025/01/test.md\n",
            "log": "abc1234 commit\n",
        }

        env = {
            "HP_DATA_DIR": str(tmp_path),
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("homepilot.cli.main._get_artifact_store", return_value=mock_store),
        ):
            result = runner.invoke(app, ["artifacts", "sync-status"])

        assert result.exit_code == 0, result.output
        assert "Uncommitted changes" in result.output
