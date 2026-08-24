"""Gates for `hp claim-code` (#458 S1).

The escape hatch for an instance that is reached from outside its own network.
It has to print the code that ACTUALLY claims the instance - a command that
prints something plausible but stale is worse than no command at all - and it
has to refuse to print anything once the instance is claimed.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.auth.tokens import generate_api_token
from homepilot.claim.repository import ClaimRepository
from homepilot.claim.startup import ensure_claim_code
from homepilot.cli.main import app
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

runner = CliRunner()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(tmp_path: Path) -> dict[str, str]:
    return {"HP_DATA_DIR": str(tmp_path)}


def _run(tmp_path: Path):
    with patch.dict("os.environ", _env(tmp_path)):
        return runner.invoke(app, ["claim-code"])


async def _boot(tmp_path: Path) -> str | None:
    """First boot against this data dir, exactly as the backend runs it."""
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    try:
        await run_migrations(database)
        return await ensure_claim_code(ClaimRepository(database), tmp_path)
    finally:
        await database.close()


async def _mint_admin_token(tmp_path: Path) -> None:
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    try:
        repo = Repository(database)
        _token, prefix, token_hash = generate_api_token()
        user_id = await repo.create_user(display_name="admin", auth_source="api_token")
        await repo.create_api_token(
            user_id=user_id,
            token_type="personal",
            prefix=prefix,
            hash=token_hash,
            scope="full",
            label="admin",
        )
    finally:
        await database.close()


def _flat(output: str) -> str:
    """Rich hard-wraps to the terminal width; compare against unwrapped text."""
    return re.sub(r"\s+", " ", output.replace("\n", ""))


# These tests are deliberately SYNCHRONOUS: the command calls asyncio.run(), which
# raises inside a running loop, so an async test would exercise a path the real
# CLI never takes.
def test_prints_the_code_that_actually_claims_the_instance(tmp_path: Path):
    code = asyncio.run(_boot(tmp_path))

    result = _run(tmp_path)

    assert result.exit_code == 0, result.output
    # THE GOAL: what it printed IS the credential, not a lookalike.
    assert code is not None
    assert code in _flat(result.output)


def test_says_nothing_secret_once_the_instance_is_claimed(tmp_path: Path):
    code = asyncio.run(_boot(tmp_path))
    asyncio.run(_mint_admin_token(tmp_path))

    result = _run(tmp_path)

    assert result.exit_code == 0, result.output
    assert "already claimed" in _flat(result.output)
    assert code is not None
    assert code not in _flat(result.output)


def test_refuses_before_the_backend_has_ever_run(tmp_path: Path):
    result = _run(tmp_path)

    assert result.exit_code == 1
    assert "Start the backend" in _flat(result.output)
