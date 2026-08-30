"""The #388 batch's remaining defects.

Most of that batch is already fixed on main - shell-script ships its body with
`write_file` and runs an allowlisted `bash <path>`, the ansible ProxyCommand
remnants are gone, the http-sequence client cache closes in a `finally`, and the
Proxmox settings save has its `getattr` guard. These four were still real:

* taking a file LOCK created the artifact, so approving an unknown id poisoned
  that id permanently;
* a fact recorded over MCP was not searchable until the process restarted;
* `hp webhook list --output json` printed the HMAC signing key;
* `hp webhook test` reported success for an endpoint that never answered.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from homepilot.artifacts.file_store import _file_lock
from homepilot.artifacts.store import ArtifactStore
from homepilot.cli.main import app

runner = CliRunner()


def _fake_webhook_repo(repo: object):
    """Stand in for `_webhook_repo`, the async context manager the webhook
    commands open the database through.

    It became a context manager in review #648: three of the four commands raise
    `typer.Exit` on a not-found or an undelivered event, and a CLI that exits
    while holding an open aiosqlite connection never exits at all - its
    non-daemon worker thread is one CPython will join for ever.
    """

    @contextlib.asynccontextmanager
    async def _cm():
        yield AsyncMock(), repo

    return _cm


class TestTakingALockDoesNotCreateTheArtifact:
    def test_the_artifact_path_is_untouched(self, tmp_path: Path):
        """`os.open(path, O_CREAT)` for the lock wrote a zero-byte artifact, so
        `store.exists()` became true for an id nobody had ever proposed."""
        artifact = tmp_path / "2026" / "08" / "2026-08-21-never-proposed.md"

        with _file_lock(artifact):
            pass

        assert not artifact.exists(), (
            "merely locking created the artifact - that id is now poisoned"
        )

    def test_the_lock_still_excludes(self, tmp_path: Path):
        """A lock that stopped locking would be worse than the bug it fixes."""
        import fcntl
        import os

        artifact = tmp_path / "2026" / "08" / "2026-08-21-locked.md"
        with _file_lock(artifact):
            fd = os.open(f"{artifact}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_an_unknown_id_is_still_proposable_afterwards(self, tmp_path: Path):
        """The lasting damage of the bug: propose refuses an id that exists, so a
        poisoned id could never be used."""
        store = ArtifactStore(tmp_path / "artifacts")
        artifact_id = "2026-08-21-fresh-abc123"

        with _file_lock(store.resolve_path(artifact_id)):
            pass

        assert not store.exists(artifact_id)


@pytest.mark.asyncio
class TestARecordedFactIsSearchableNow:
    async def test_record_fact_indexes_before_it_returns(self):
        """It reported `{"status": "applied"}` and an immediate `search_kb`
        returned nothing until the process restarted."""
        from homepilot.mcp.tools.kb_tools import handle_record_fact

        lifecycle = AsyncMock()
        lifecycle.propose = AsyncMock(return_value="2026-08-21-fact-abc123")
        kb_service = AsyncMock()
        kb_service.index_note = AsyncMock(return_value={"indexed": True, "doc_id": 7})
        ctx = {"lifecycle": lifecycle, "kb_service": kb_service}

        result = await handle_record_fact(
            {"content": "web01 runs nginx 1.24", "kind": "fact", "target": "web01"}, ctx
        )

        assert result["status"] == "applied"
        # THIS note, indexed. It used to call `reindex_if_needed`, which rebuilt
        # the entire index with embeddings switched off and destroyed every
        # vector in it (#648 tranche 6).
        kb_service.index_note.assert_awaited_once_with("2026-08-21-fact-abc123")
        kb_service.reindex_if_needed.assert_not_awaited()
        assert result["indexed"] is True

    async def test_an_indexing_failure_does_not_lose_the_fact(self):
        from homepilot.mcp.tools.kb_tools import handle_record_fact

        lifecycle = AsyncMock()
        lifecycle.propose = AsyncMock(return_value="2026-08-21-fact-abc123")
        kb_service = AsyncMock()
        kb_service.index_note = AsyncMock(side_effect=RuntimeError("index down"))

        result = await handle_record_fact(
            {"content": "a fact", "kind": "fact", "target": "web01"},
            {"lifecycle": lifecycle, "kb_service": kb_service},
        )

        assert result["id"] == "2026-08-21-fact-abc123"
        # The fact survives, and the caller is TOLD it is not searchable rather
        # than left to assume it is.
        assert result["indexed"] is False


class TestTheWebhookCliDoesNotLeakOrLie:
    def test_list_json_redacts_the_signing_key(self):
        """`--output json` is what gets piped into CI logs; the table branch had
        always omitted the secret."""
        configs = [{"id": 1, "url": "https://example.test/hook", "secret": "super-secret-hmac-key"}]
        with patch(
            "homepilot.cli.main._webhook_repo",
            _fake_webhook_repo(AsyncMock(list_webhook_configs=AsyncMock(return_value=configs))),
        ):
            result = runner.invoke(app, ["webhook", "list", "--output", "json"])

        assert result.exit_code == 0, result.output
        assert "super-secret-hmac-key" not in result.output
        assert "***" in result.output
        assert "example.test" in result.output, "redaction ate the useful part too"

    def test_test_exits_non_zero_when_delivery_failed(self):
        """The one command whose entire job is to say whether delivery works."""
        config = {"id": 1, "url": "https://example.test/hook", "secret": None, "max_retries": 0}
        with (
            patch(
                "homepilot.cli.main._webhook_repo",
                _fake_webhook_repo(AsyncMock(get_webhook_config=AsyncMock(return_value=config))),
            ),
            patch("homepilot.events.deliver_with_retry", AsyncMock(return_value=False)),
        ):
            result = runner.invoke(app, ["webhook", "test", "1"])

        assert result.exit_code == 1, result.output
        assert "NOT accepted" in result.output

    def test_test_reports_success_when_it_worked(self):
        config = {"id": 1, "url": "https://example.test/hook", "secret": None, "max_retries": 0}
        with (
            patch(
                "homepilot.cli.main._webhook_repo",
                _fake_webhook_repo(AsyncMock(get_webhook_config=AsyncMock(return_value=config))),
            ),
            patch("homepilot.events.deliver_with_retry", AsyncMock(return_value=True)),
        ):
            result = runner.invoke(app, ["webhook", "test", "1"])

        assert result.exit_code == 0, result.output
        assert "delivered" in result.output


class TestKbReindexUsesACredentialTheRouteAccepts:
    def test_it_mints_a_bearer_token_rather_than_sending_an_admin_secret(self):
        """`/kb/reindex` requires the admin SCOPE. Sending only
        `X-HP-Admin-Secret` 401'd every time and fell silently through to the
        offline path - the one that wiped the index."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "homepilot" / "cli" / "main.py"
        ).read_text(encoding="utf-8")
        start = source.index("async def _kb_reindex_via_api")
        block = source[start : source.index("\n    async def _do(", start)]

        code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
        assert "X-HP-Admin-Secret" not in code, (
            "the reindex call still sends a credential the route does not accept"
        )
        assert "Authorization" in code and "_mint_token_via_api" in code
