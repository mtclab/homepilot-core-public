"""The CLI's per-command API tokens are short-lived and really deleted (#699).

WHAT THIS FORBIDS: every CLI command that talks to the backend leaving a live,
never-expiring API credential behind.

* ``_backend_api`` deleted its token by authenticating AS that token. For a
  ``write`` token (``hp artifacts apply|replay|revoke``) ``DELETE
  /auth/tokens/{prefix}`` answers 403 "requires 'admin'", and the failure was
  swallowed at debug level - so every apply left a permanent write credential.
* ``hp inventory refresh`` (write) and ``hp kb reindex`` (admin, commented
  "short-lived") never tried to delete theirs at all.
* ``POST /auth/tokens`` always stored ``expires_at=None``, so "short-lived"
  was a comment, not a property.

The test runs the REAL auth router over a real SQLite DB, holds an admin token
the way an operator does (``HP_ADMIN_TOKEN``), drives the commands, and then
reads the token table: nothing minted by the CLI may survive, and while it was
alive it had to carry a near expiry.

Teeth: revert the delete to "authenticate as the minted token" and the write-
scope journey leaves a row; drop ``expires_in_seconds`` from the mint and the
expiry assertion fails; drop the cleanup from inventory refresh / kb reindex and
their journeys leave a row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from typer.testing import CliRunner

from homepilot.auth.deps import require_scope
from homepilot.auth.tokens import generate_api_token
from homepilot.cli import main as cli
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


class Backend:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.seen_expiry: list[str | None] = []
        app = FastAPI()
        from homepilot.auth.router import router as auth_router

        app.include_router(auth_router, prefix="/auth")
        app.state.repo = repo

        async def _record(request: Request) -> None:
            bearer = request.headers.get("authorization", "")[len("Bearer ") :]
            row = await repo.get_token_by_prefix(bearer[:16])
            self.seen_expiry.append(row.get("expires_at") if row else "<missing>")

        for need in ("read", "write", "admin"):

            @app.get(f"/probe/{need}", dependencies=[Depends(require_scope(need))])
            async def probe(request: Request) -> dict[str, Any]:
                await _record(request)
                return {"ok": True}

        @app.post("/inventory/refresh", dependencies=[Depends(require_scope("write"))])
        async def refresh(request: Request) -> dict[str, Any]:
            await _record(request)
            return {"hosts": 0, "services": 0, "complete": True}

        @app.post("/kb/reindex", dependencies=[Depends(require_scope("admin"))])
        async def reindex(request: Request) -> dict[str, Any]:
            await _record(request)
            return {"removed": 0, "reindexed": 0, "errors": 0, "errors_detail": []}

        self.app = app

    async def cli_tokens(self) -> list[dict[str, Any]]:
        rows = await self.repo.db.fetchall(
            "SELECT prefix, label, scope, expires_at FROM api_tokens WHERE label LIKE 'cli-%'"
        )
        return [dict(r) for r in rows]


@pytest.fixture
async def backend(tmp_path: Path, monkeypatch) -> Any:
    from homepilot.config import Settings

    db = Database(str(tmp_path / "hp.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    uid = await repo.create_user("operator")
    admin, prefix, digest = generate_api_token()
    await repo.create_api_token(uid, "personal", prefix, digest, scope="*", label="operator")
    await db.conn.commit()

    fake = Backend(repo)
    # The mint route rate-limits per client IP; every test here mints from the
    # same in-process client, so start each one with a clean window.
    from homepilot.auth import router as auth_router

    auth_router._token_create_attempts.clear()
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=str(tmp_path / "data"),
        artifacts_dir=str(tmp_path / "data" / "artifacts"),
        admin_secret="",
    )
    monkeypatch.setenv("HP_ADMIN_TOKEN", admin)
    monkeypatch.setattr(cli, "_get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "_backend_http_client",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake.app), base_url="http://127.0.0.1"
        ),
    )
    yield fake
    await db.close()


def _assert_short_expiry(expiry: str | None) -> None:
    assert expiry not in (None, "<missing>"), "the CLI minted a token that never expires"
    when = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    remaining = (when - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= cli._CLI_TOKEN_TTL_SECONDS + 5, remaining


@pytest.mark.parametrize("scope", ["write", "admin", "read"])
async def test_backend_api_leaves_no_token_behind(backend, scope):
    await cli._backend_api("GET", f"/probe/{scope}", scope=scope)

    assert await backend.cli_tokens() == [], (
        f"a {scope}-scope CLI token survived the command it was minted for"
    )
    _assert_short_expiry(backend.seen_expiry[-1])


async def test_the_token_is_deleted_even_when_the_call_fails(backend):
    with pytest.raises(RuntimeError, match="404"):
        await cli._backend_api("GET", "/nope", scope="write")
    assert await backend.cli_tokens() == []


def test_inventory_refresh_leaves_no_token_behind(backend):
    import asyncio

    r = CliRunner().invoke(cli.app, ["inventory", "refresh"])

    assert r.exit_code == 0, r.output
    assert asyncio.run(backend.cli_tokens()) == []
    _assert_short_expiry(backend.seen_expiry[-1])


def test_kb_reindex_leaves_no_token_behind(backend):
    import asyncio

    r = CliRunner().invoke(cli.app, ["kb", "reindex", "--yes", "--no-embeddings"])

    assert r.exit_code == 0, r.output
    assert backend.seen_expiry, "kb reindex never reached the backend"
    assert asyncio.run(backend.cli_tokens()) == []
    _assert_short_expiry(backend.seen_expiry[-1])


async def test_the_api_stores_a_requested_expiry_and_enforces_it(backend):
    transport = httpx.ASGITransport(app=backend.app)
    admin = cli.os.environ["HP_ADMIN_TOKEN"]
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/auth/tokens",
            json={"label": "cli-x", "scope": "write", "expires_in_seconds": 120},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert r.status_code == 201, r.text
        token = r.json()["token"]
        rows = await backend.cli_tokens()
        assert len(rows) == 1
        _assert_short_expiry(rows[0]["expires_at"])

        # An expired token is refused (the enforcement the expiry relies on).
        await backend.repo.db.execute(
            "UPDATE api_tokens SET expires_at = ? WHERE prefix = ?",
            ("2000-01-01T00:00:00Z", token[:16]),
        )
        await backend.repo.db.conn.commit()
        probe = await c.get("/probe/read", headers={"Authorization": f"Bearer {token}"})
        assert probe.status_code == 401

        bad = await c.post(
            "/auth/tokens",
            json={"label": "cli-y", "scope": "write", "expires_in_seconds": 0},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert bad.status_code == 422
