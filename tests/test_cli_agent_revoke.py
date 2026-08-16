from __future__ import annotations

import asyncio
import sqlite3

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


runner = CliRunner()


def _seed_credential(data_dir, agent_id: str, hostname: str, credential_hash: str) -> None:
    async def _seed() -> None:
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(data_dir / "homepilot.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        await repo.set_agent_credential(agent_id, hostname, credential_hash)
        await db.close()

    asyncio.run(_seed())


class TestAgentRevokeCLI:
    def test_revoke_sets_revoked_at(self, tmp_path):
        """`hp agent revoke <id>` marks the credential revoked in the DB.

        Revert-check: delete the agent_revoke command and this fails (unknown
        command -> non-zero exit)."""
        from unittest.mock import patch

        _seed_credential(tmp_path, "agent-1", "host1", "deadbeef")

        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}
        with patch.dict("os.environ", env):
            result = runner.invoke(app, ["agent", "revoke", "agent-1"])
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(tmp_path / "homepilot.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT revoked_at FROM agents WHERE agent_id = ?", ("agent-1",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["revoked_at"] is not None

    def test_revoke_unknown_agent_errors(self, tmp_path):
        from unittest.mock import patch

        _seed_credential(tmp_path, "agent-1", "host1", "deadbeef")

        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}
        with patch.dict("os.environ", env):
            result = runner.invoke(app, ["agent", "revoke", "nope"])
        assert result.exit_code == 1, result.output
