from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_env(tmp_path: Path) -> dict[str, str]:
    return {"HP_DATA_DIR": str(tmp_path), "HP_SECRET_KEY": "x" * 64}


@pytest.fixture
async def seeded_db(tmp_path: Path):
    """DB with two hosts pre-created."""
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    _h1 = await repo.create_host(
        hostname="pve", host_type="node", role="node", ip_address="10.1.1.1"
    )
    h2 = await repo.create_host(hostname="web-vm", host_type="qemu", role="guest", node="pve")
    await repo.update_host(h2, status="running")
    await db.conn.commit()
    await db.close()
    return tmp_path


class TestInventoryList:
    def test_list_empty_db_shows_no_hosts(self, tmp_path, db_env):
        import asyncio

        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations

        async def init():
            db = Database(str(tmp_path / "homepilot.db"))
            await db.connect()
            await run_migrations(db)
            await db.close()

        asyncio.run(init())

        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "list"])
        assert result.exit_code == 0
        assert "No hosts" in result.output

    def test_list_missing_db_returns_empty(self, tmp_path, db_env):
        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "list"])
        assert result.exit_code == 0

    def test_list_shows_hosts_in_table(self, tmp_path, db_env, seeded_db):
        db_env["HP_DATA_DIR"] = str(seeded_db)
        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "list"])
        assert result.exit_code == 0, result.output
        assert "pve" in result.output
        assert "web-vm" in result.output

    def test_list_json_output(self, tmp_path, db_env, seeded_db):
        import json

        db_env["HP_DATA_DIR"] = str(seeded_db)
        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "list", "--output", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output.strip())
        assert isinstance(data, list)
        hostnames = [h["hostname"] for h in data]
        assert "pve" in hostnames

    def test_list_role_filter(self, tmp_path, db_env, seeded_db):
        db_env["HP_DATA_DIR"] = str(seeded_db)
        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "list", "--role", "node"])
        assert result.exit_code == 0
        assert "pve" in result.output
        assert "web-vm" not in result.output


class TestInventoryShow:
    def test_show_existing_host(self, tmp_path, db_env, seeded_db):
        db_env["HP_DATA_DIR"] = str(seeded_db)
        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "show", "pve"])
        assert result.exit_code == 0, result.output
        assert "pve" in result.output
        assert "10.1.1.1" in result.output

    def test_show_missing_host_exits_1(self, tmp_path, db_env, seeded_db):
        db_env["HP_DATA_DIR"] = str(seeded_db)
        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "show", "nonexistent"])
        assert result.exit_code == 1

    def test_show_json_output(self, tmp_path, db_env, seeded_db):
        import json

        db_env["HP_DATA_DIR"] = str(seeded_db)
        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "show", "pve", "--output", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output.strip())
        assert data["hostname"] == "pve"
        assert data["ip_address"] == "10.1.1.1"


class TestInventoryRefresh:
    def test_refresh_no_proxmox_host(self, tmp_path, db_env):
        db_env["HP_PROXMOX_HOST"] = ""
        import asyncio

        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations

        async def init():
            db = Database(str(tmp_path / "homepilot.db"))
            await db.connect()
            await run_migrations(db)
            await db.close()

        asyncio.run(init())

        with patch.dict("os.environ", db_env):
            result = runner.invoke(app, ["inventory", "refresh"])
        assert result.exit_code == 0
        assert "Sync complete: 0 hosts" in result.output

    def test_refresh_with_proxmox_syncs_node(self, tmp_path, db_env):
        db_env["HP_PROXMOX_HOST"] = "10.1.1.1"
        db_env["PVE_API_TOKEN"] = "test-user@pve!test-token=test-secret"
        import asyncio

        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations

        async def init():
            db = Database(str(tmp_path / "homepilot.db"))
            await db.connect()
            await run_migrations(db)
            await db.close()

        asyncio.run(init())

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(
            side_effect=lambda path: (
                {"data": [{"node": "pve", "ip": "10.1.1.1"}]} if path == "/nodes" else {"data": []}
            )
        )

        with (
            patch.dict("os.environ", db_env),
            patch("homepilot.adapters.proxmox.ProxmoxClient", return_value=mock_proxmox),
        ):
            result = runner.invoke(app, ["inventory", "refresh"])
        assert result.exit_code == 0, result.output
        assert "1 hosts" in result.output
