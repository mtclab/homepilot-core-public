from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """lru_cache on get_settings caches data_dir; clear between tests."""
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


runner = CliRunner()


class TestTokenCreate:
    def test_creates_token_plain_output(self, tmp_path):
        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}
        with patch.dict("os.environ", env):
            result = runner.invoke(app, ["token", "create"])
        assert result.exit_code == 0, result.output
        token = result.output.strip()
        assert token.startswith("hp_")
        assert len(token) > 20

    def test_creates_token_json_output(self, tmp_path):
        import json

        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}
        with patch.dict("os.environ", env):
            result = runner.invoke(app, ["token", "create", "--output", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output.strip())
        assert data["token"].startswith("hp_")
        assert data["scope"] == "read,write"

    def test_custom_scope(self, tmp_path):
        import json

        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}
        with patch.dict("os.environ", env):
            result = runner.invoke(app, ["token", "create", "--scope", "*", "--output", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output.strip())
        assert data["scope"] == "*"

    def test_second_create_reuses_existing_user(self, tmp_path):
        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}
        with patch.dict("os.environ", env):
            r1 = runner.invoke(app, ["token", "create"])
            r2 = runner.invoke(app, ["token", "create"])
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        t1 = r1.output.strip()
        t2 = r2.output.strip()
        assert t1 != t2  # distinct tokens
        assert t1.startswith("hp_")
        assert t2.startswith("hp_")

    def test_token_validates_against_api(self, tmp_path):
        """Token written to DB must validate via the auth layer (sync sqlite3 check)."""
        import sqlite3

        from homepilot.auth.tokens import validate_token

        env = {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}
        with patch.dict("os.environ", env):
            result = runner.invoke(app, ["token", "create"])
        assert result.exit_code == 0, result.output
        token = result.output.strip()
        from homepilot.auth.tokens import PREFIX_LENGTH

        prefix = token[:PREFIX_LENGTH]

        db_path = tmp_path / "homepilot.db"
        assert db_path.exists(), f"DB not found at {db_path}"

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert "api_tokens" in tables, f"Missing api_tokens, got: {tables}"
        row = conn.execute("SELECT hash FROM api_tokens WHERE prefix = ?", (prefix,)).fetchone()
        conn.close()
        assert row is not None
        assert validate_token(token, row["hash"])
