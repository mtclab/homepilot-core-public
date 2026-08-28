"""`hp token create` mints as an admin; the direct-DB write is bootstrap only.

THE DEFECT (owner, 2026-08-26): minting was an unauthenticated write straight to
the local database. Anyone who could run `hp` on the box could mint a fleet-root
credential, nothing recorded who did, and the vault passphrase - autocreated
anyway - gated none of it. Now the token comes from POST /auth/tokens with this
box's admin credential, exactly as `hp agent` does its work (#430), and the old
path survives only for the one case that cannot be authenticated: an instance
with no live token to authenticate WITH.

The gates below are journeys, not calls: each asserts what the operator ends up
holding (a working token, or none at all and a message naming the rule), and the
authenticated one round-trips its token against a REAL running API.

Teeth (verified by reverting): delete the live-token check in token_create and
``test_refuses_when_a_live_token_exists`` fails - the second mint succeeds and a
new row appears.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.auth.tokens import PREFIX_LENGTH, generate_api_token, validate_token
from homepilot.cli.main import app


@pytest.fixture(autouse=True)
def _reset_token_create_limiter():
    """These tests hit POST /auth/tokens repeatedly; the limiter is module
    state, so without a reset the budget they spend starves whichever
    /auth/tokens test runs after them (found as a 429 in
    test_gate_qa_pr253 during the full suite - the #518 class, in test form)."""
    from homepilot.auth import router as auth_router

    auth_router._token_create_attempts.clear()
    yield
    auth_router._token_create_attempts.clear()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """lru_cache on get_settings caches data_dir; clear between tests."""
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


runner = CliRunner()


def _offline_env(tmp_path) -> dict[str, str]:
    """A box with no backend to talk to and no admin credential of any kind.

    The port is pointed at a closed one on purpose: a HomePilot happening to run
    on this machine's :8000 would otherwise answer these tests, and a developer
    box's own ~/.hp/.env carries an HP_ADMIN_SECRET that would authenticate them.
    """
    return {
        "HP_DATA_DIR": str(tmp_path),
        "HP_PORT": "1",
        "HP_ADMIN_SECRET": "",
        "HP_ADMIN_TOKEN": "",
    }


def _tokens(tmp_path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(tmp_path / "homepilot.db"))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT prefix, hash, scope, label FROM api_tokens"))
    finally:
        conn.close()


class TestBootstrapMint:
    """Zero live tokens: the one case with no admin to mint through."""

    def test_bootstrap_mints_and_states_the_rule(self, tmp_path):
        with patch.dict("os.environ", _offline_env(tmp_path)):
            result = runner.invoke(app, ["token", "create"])
        assert result.exit_code == 0, result.output
        token = result.stdout.strip()
        assert token.startswith("hp_")
        assert len(token) > 20
        # The operator is told the rule they just used the exception to.
        assert "minted by admins" in result.stderr
        assert "Settings -> Tokens" in result.stderr

    def test_bootstrap_token_is_a_usable_credential(self, tmp_path):
        with patch.dict("os.environ", _offline_env(tmp_path)):
            result = runner.invoke(app, ["token", "create"])
        assert result.exit_code == 0, result.output
        token = result.stdout.strip()
        rows = _tokens(tmp_path)
        assert len(rows) == 1
        assert validate_token(token, rows[0]["hash"])
        assert rows[0]["prefix"] == token[:PREFIX_LENGTH]

    def test_json_output(self, tmp_path):
        with patch.dict("os.environ", _offline_env(tmp_path)):
            result = runner.invoke(app, ["token", "create", "--output", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["token"].startswith("hp_")
        assert data["scope"] == "read,write"
        assert data["bootstrap"] is True

    def test_custom_scope(self, tmp_path):
        with patch.dict("os.environ", _offline_env(tmp_path)):
            result = runner.invoke(app, ["token", "create", "--scope", "*", "--output", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["scope"] == "*"

    def test_full_scope_names_the_collision(self, tmp_path):
        """#579: 'full' is two words in one - API full (= '*', everything) vs
        the MCP full tier (= write). An operator typing --scope full is warned
        which one they are getting, BEFORE the token exists in their history.
        Teeth: remove the secho in token_create and this fails."""
        with patch.dict("os.environ", _offline_env(tmp_path)):
            result = runner.invoke(app, ["token", "create", "--scope", "full", "--output", "json"])
        assert result.exit_code == 0, result.output
        assert "EVERYTHING" in result.stderr
        assert "MCP" in result.stderr and "write" in result.stderr

    def test_ordinary_scopes_are_not_nagged(self, tmp_path):
        """The collision note is for 'full' only - read,write mints quietly."""
        with patch.dict("os.environ", _offline_env(tmp_path)):
            result = runner.invoke(app, ["token", "create", "--scope", "read,write"])
        assert result.exit_code == 0, result.output
        assert "EVERYTHING" not in result.stderr


class TestUnauthenticatedMintRefused:
    """THE gate. With a live token on the instance, an unauthenticated mint is
    refused - that is the whole point of the slice."""

    def test_refuses_when_a_live_token_exists(self, tmp_path):
        env = _offline_env(tmp_path)
        with patch.dict("os.environ", env):
            first = runner.invoke(app, ["token", "create"])
            assert first.exit_code == 0, first.output
            before = _tokens(tmp_path)
            second = runner.invoke(app, ["token", "create"])

        assert second.exit_code == 1, second.output
        assert "Refusing to mint" in second.stderr
        assert "minted by admins" in second.stderr
        assert "Settings -> Tokens" in second.stderr
        # The goal, not the exit code: NO credential was handed out.
        assert not second.stdout.strip()
        assert [dict(r) for r in _tokens(tmp_path)] == [dict(r) for r in before]

    def test_an_expired_token_does_not_block_the_bootstrap(self, tmp_path):
        """ "Live" means usable. An instance whose only token expired has no admin
        to mint through and must still be recoverable."""
        env = _offline_env(tmp_path)
        with patch.dict("os.environ", env):
            assert runner.invoke(app, ["token", "create"]).exit_code == 0
            conn = sqlite3.connect(str(tmp_path / "homepilot.db"))
            conn.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00Z'")
            conn.commit()
            conn.close()
            again = runner.invoke(app, ["token", "create"])
        assert again.exit_code == 0, again.output
        assert again.stdout.strip().startswith("hp_")


# ── The authenticated path, against a real running API ───────────────────────


@contextmanager
def _live_backend(tmp_path) -> Iterator[tuple[int, Any]]:
    """The real auth router on a real port over a real database.

    The CLI talks to 127.0.0.1:<daemon_port>, so the only honest way to test the
    authenticated path is to give it something real to talk to.
    """
    import uvicorn
    from fastapi import FastAPI

    from homepilot.auth.router import router as auth_router
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository

    loop = asyncio.new_event_loop()
    database = Database(str(tmp_path / "homepilot.db"))
    loop.run_until_complete(database.connect())
    loop.run_until_complete(run_migrations(database))
    repo = Repository(database)

    api = FastAPI()
    api.state.repo = repo
    api.include_router(auth_router, prefix="/auth")

    config = uvicorn.Config(api, host="127.0.0.1", port=0, log_level="error", loop="asyncio")
    server = uvicorn.Server(config)

    def _serve() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the test backend never started"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port, (loop, repo)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        with contextlib.suppress(Exception):
            loop.run_until_complete(database.close())
        loop.close()


def _seed_token(loop, repo, scope: str) -> str:
    async def _mint() -> str:
        token, prefix, token_hash = generate_api_token()
        user_id = await repo.create_user(display_name="admin", auth_source="api_token")
        await repo.create_api_token(
            user_id=str(user_id),
            token_type="personal",
            prefix=prefix,
            hash=token_hash,
            scope=scope,
            label="seed",
            expires_at=None,
        )
        return token

    return asyncio.run_coroutine_threadsafe(_mint(), loop).result(timeout=10)


class TestAuthenticatedMint:
    def test_admin_token_creates_through_the_api_and_the_token_works(self, tmp_path):
        import httpx

        with _live_backend(tmp_path) as (port, (loop, repo)):
            admin = _seed_token(loop, repo, "admin")
            env = {
                "HP_DATA_DIR": str(tmp_path),
                "HP_PORT": str(port),
                # A developer box's own ~/.hp/.env carries an HP_ADMIN_SECRET,
                # and both sides here would resolve the SAME one - which would
                # authenticate the mint without the token proving anything.
                "HP_ADMIN_SECRET": "",
                "HP_ADMIN_TOKEN": admin,
            }
            with patch.dict("os.environ", env):
                result = runner.invoke(app, ["token", "create", "--label", "assistant"])
            assert result.exit_code == 0, result.output + result.stderr
            minted = result.stdout.strip()
            assert minted.startswith("hp_")
            # It is a bootstrap-free mint...
            assert "Bootstrap" not in result.stderr
            # ...and it round-trips: the new token authenticates against the API.
            me = httpx.get(
                f"http://127.0.0.1:{port}/auth/me",
                headers={"Authorization": f"Bearer {minted}"},
                timeout=10,
            )
            assert me.status_code == 200, me.text
            assert me.json()["authenticated"] is True

    def test_a_read_token_cannot_mint(self, tmp_path):
        with _live_backend(tmp_path) as (port, (loop, repo)):
            reader = _seed_token(loop, repo, "read")
            env = {
                "HP_DATA_DIR": str(tmp_path),
                "HP_PORT": str(port),
                # A developer box's own ~/.hp/.env carries an HP_ADMIN_SECRET,
                # and both sides here would resolve the SAME one - which would
                # authenticate the mint without the token proving anything.
                "HP_ADMIN_SECRET": "",
                "HP_ADMIN_TOKEN": reader,
            }
            before = len(_tokens(tmp_path))
            with patch.dict("os.environ", env):
                result = runner.invoke(app, ["token", "create"])
            assert result.exit_code == 1, result.output
            assert not result.stdout.strip()
            assert "Refusing to mint" in result.stderr
            assert len(_tokens(tmp_path)) == before

    def test_the_stored_box_token_is_credential_enough(self, tmp_path):
        """The box's own autocreated credential (data dir `api-token`, written by
        `hp init` and by the claim) authenticates the CLI with nothing exported."""
        with _live_backend(tmp_path) as (port, (loop, repo)):
            admin = _seed_token(loop, repo, "admin")
            (tmp_path / "api-token").write_text(admin + "\n", encoding="utf-8")
            env = {
                "HP_DATA_DIR": str(tmp_path),
                "HP_PORT": str(port),
                "HP_ADMIN_SECRET": "",
            }
            with patch.dict("os.environ", env):
                result = runner.invoke(app, ["token", "create"])
            assert result.exit_code == 0, result.output + result.stderr
            assert result.stdout.strip().startswith("hp_")
            assert "Bootstrap" not in result.stderr
