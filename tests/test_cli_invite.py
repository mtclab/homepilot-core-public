"""Gates for `hp invite` (#442 stage 2).

The mint is the only moment the token exists in plaintext. These assert that it
is printed exactly once, that the database holds a hash and never the token, and
that a mint can never create caps provisioning would reject.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """lru_cache on get_settings caches data_dir; clear between tests."""
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(tmp_path) -> dict[str, str]:
    return {
        "HP_DATA_DIR": str(tmp_path),
        "HP_PORTAL_BASE_URL": "https://portal.example",
    }


def _mint(tmp_path, *extra: str):
    args = [
        "invite",
        "create",
        "--cn",
        "friend-a",
        "--template",
        "9000",
        "--node",
        "pve1",
        "--cores",
        "2",
        "--ram",
        "2048",
        "--disk",
        "20",
        *extra,
    ]
    with patch.dict("os.environ", _env(tmp_path)):
        return runner.invoke(app, args)


def _invite_rows(tmp_path) -> list[dict]:
    """Invites on disk. A refused mint must not even create the database."""
    db_path = tmp_path / "homepilot.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM invites").fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _token_from(output: str) -> str:
    marker = "/invite/"
    start = output.index(marker) + len(marker)
    # Rich panels wrap and pad; the token is the run of token characters.
    token = ""
    for ch in output[start:]:
        if ch.isalnum() or ch == "_":
            token += ch
        else:
            break
    return token


class TestInviteCreate:
    def test_prints_a_usable_url_once_and_stores_only_a_hash(self, tmp_path):
        result = _mint(tmp_path)
        assert result.exit_code == 0, result.output
        assert "https://portal.example/invite/" in result.output.replace("\n", "")

        token = _token_from(result.output)
        assert token.startswith("hpi_")

        rows = _invite_rows(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["bound_cn"] == "friend-a"
        assert row["cores"] == 2 and row["memory_mb"] == 2048 and row["disk_gb"] == 20
        assert row["template_vmid"] == 9000 and row["node"] == "pve1"
        # The token itself is nowhere in the database - only its prefix and hash.
        assert token not in str(row)
        assert row["token_prefix"] == token[:16]
        assert len(row["token_hash"]) == 64

    def test_refuses_caps_that_provisioning_would_reject(self, tmp_path):
        result = _mint(tmp_path, "--cores", "999")
        assert result.exit_code == 1
        assert "Invalid caps" in result.output
        assert _invite_rows(tmp_path) == []

    def test_refuses_an_unparseable_expiry(self, tmp_path):
        result = _mint(tmp_path, "--expires", "soon")
        assert result.exit_code == 1
        assert _invite_rows(tmp_path) == []


class TestInviteListAndRevoke:
    def test_list_shows_state_and_never_a_token(self, tmp_path):
        minted = _mint(tmp_path)
        token = _token_from(minted.output)

        with patch.dict("os.environ", _env(tmp_path)):
            listed = runner.invoke(app, ["invite", "list", "--output", "json"])
        assert listed.exit_code == 0, listed.output
        rows = json.loads(listed.output)
        assert len(rows) == 1
        assert rows[0]["state"] == "open"
        assert rows[0]["bound_cn"] == "friend-a"
        assert token not in listed.output
        assert "token_hash" not in listed.output

        with patch.dict("os.environ", _env(tmp_path)):
            table = runner.invoke(app, ["invite", "list"])
        assert table.exit_code == 0
        assert token not in table.output.replace("\n", "")

    def test_revoke_closes_the_invite_and_a_second_revoke_reports_nothing_to_do(self, tmp_path):
        minted = _mint(tmp_path)
        prefix = _token_from(minted.output)[:16]

        with patch.dict("os.environ", _env(tmp_path)):
            first = runner.invoke(app, ["invite", "revoke", prefix])
            second = runner.invoke(app, ["invite", "revoke", prefix])
            listed = runner.invoke(app, ["invite", "list", "--output", "json"])

        assert first.exit_code == 0
        assert second.exit_code == 1
        assert json.loads(listed.output)[0]["state"] == "revoked"
