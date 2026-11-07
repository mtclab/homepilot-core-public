"""#623: `hp invite` against a RUNNING backend goes through its API.

`hp invite create` opened the control-plane SQLite file itself - a second
writer next to the live backend, the class `hp token create` was cured of
(#430). On prod it hung; on this branch it refused ("the backend is running")
and minted nothing, while the docs name it as the way to mint an invite.

The journey here is the operator's: a data directory a backend holds (the real
instance lock), the real `hp` command, and the real admin route answering over
HTTP - only the hop from the CLI's HTTP client to the app is in-process. The
outcome asserted is the invite in the database, minted by the route (so its
audit row and its caps come from the backend), and the URL on the terminal.

Teeth: drop the `_server_is_running` branch from `invite_create` and the mint
goes back to `_open_invite_repo`, which refuses - the first test fails.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from homepilot.cli.main import app as cli_app
from homepilot.config import get_settings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.guest.admin_router import router as guest_admin_router
from homepilot.instance_lock import InstanceLock
from homepilot.portal.repository import InviteRepository

runner = CliRunner()


@pytest.fixture
def live_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A data dir a backend holds, and that backend's admin API."""
    import asyncio

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HP_ARTIFACTS_DIR", str(data_dir / "artifacts"))
    get_settings.cache_clear()

    db_path = str(data_dir / "homepilot.db")

    async def _migrate() -> None:
        db = Database(db_path)
        await db.connect()
        await run_migrations(db)
        await db.close()

    asyncio.run(_migrate())

    api = FastAPI()
    api.include_router(guest_admin_router)
    from homepilot.auth.deps import require_token

    api.dependency_overrides[require_token] = lambda: {
        "scope": "*",
        "role": "admin",
        "prefix": "hp_cliadmin",
    }
    calls: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def _app_client(timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        # The backend's own connection, opened per request like a server
        # worker would hold it - never the CLI's.
        db = Database(db_path)
        await db.connect()
        api.state.repo = Repository(db)
        api.state.invite_repo = InviteRepository(db)
        transport = httpx.ASGITransport(app=api)

        async def _record(request: httpx.Request) -> None:
            calls.append((request.method, request.url.path))

        try:
            async with httpx.AsyncClient(
                transport=transport, timeout=timeout, event_hooks={"request": [_record]}
            ) as client:
                yield client
        finally:
            await db.close()

    lock = InstanceLock(data_dir)
    lock.acquire()
    try:
        with (
            patch("homepilot.cli.main._backend_http_client", _app_client),
            patch(
                "homepilot.cli.main._mint_token_via_api",
                return_value=("hp_ephemeral_test_token", None),
            ),
        ):
            yield {"db_path": db_path, "calls": calls}
    finally:
        lock.release()
        get_settings.cache_clear()


def _invite_rows(db_path: str) -> list[dict[str, Any]]:
    import sqlite3

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM invites")]
    finally:
        con.close()


ARGS = [
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
    "--expires",
    "48h",
    "--base-url",
    "https://portal.example.com",
]


class TestMintingAgainstALiveBackend:
    def test_the_invite_is_minted_by_the_backend_and_the_url_printed(
        self, live_backend: dict[str, Any]
    ) -> None:
        result = runner.invoke(cli_app, ARGS)

        assert result.exit_code == 0, result.output
        assert ("POST", "/admin/guests/invites") in live_backend["calls"]
        rows = _invite_rows(live_backend["db_path"])
        assert len(rows) == 1, "the CLI said it minted an invite the database does not hold"
        row = rows[0]
        assert row["bound_cn"] == "friend-a"
        assert (row["template_vmid"], row["node"], row["cores"]) == (9000, "pve1", 2)
        assert (row["memory_mb"], row["disk_gb"]) == (2048, 20)
        # The route's attribution, not the shell user's name - in the one
        # spelling every surface uses (`label (prefix)`, auth.principal.audit_actor).
        assert row["created_by"] == "api-token (hp_cliadmin)"
        url = re.search(r"https://portal\.example\.com/invite/(\S+)", result.output)
        assert url, f"no invite URL on the terminal:\n{result.output}"
        assert url.group(1).startswith(row["token_prefix"])

    def test_the_validity_window_survives_the_api(self, live_backend: dict[str, Any]) -> None:
        from datetime import datetime

        result = runner.invoke(cli_app, ARGS)
        assert result.exit_code == 0, result.output
        row = _invite_rows(live_backend["db_path"])[0]
        created = datetime.fromisoformat(row["created_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        hours = (expires - created).total_seconds() / 3600
        assert 47.9 < hours < 48.1, f"--expires 48h became {hours:.2f}h"

    def test_a_missing_size_is_refused_by_name_not_invented(
        self, live_backend: dict[str, Any]
    ) -> None:
        args = [a for a in ARGS if a not in ("--ram", "2048")]
        result = runner.invoke(cli_app, args)

        assert result.exit_code == 1
        assert "--ram" in result.output
        assert _invite_rows(live_backend["db_path"]) == []

    def test_list_and_revoke_go_through_the_api_too(self, live_backend: dict[str, Any]) -> None:
        assert runner.invoke(cli_app, ARGS).exit_code == 0
        prefix = _invite_rows(live_backend["db_path"])[0]["token_prefix"]

        listed = runner.invoke(cli_app, ["invite", "list", "--output", "json"])
        assert listed.exit_code == 0, listed.output
        assert prefix in listed.output and "open" in listed.output

        revoked = runner.invoke(cli_app, ["invite", "revoke", prefix])
        assert revoked.exit_code == 0, revoked.output
        assert _invite_rows(live_backend["db_path"])[0]["revoked_at"]
        assert ("POST", f"/admin/guests/invites/{prefix}/revoke") in live_backend["calls"]

    def test_an_api_refusal_exits_non_zero_and_says_why(self, live_backend: dict[str, Any]) -> None:
        result = runner.invoke(cli_app, ["invite", "revoke", "hpi_nosuchinvite"])

        assert result.exit_code == 1
        out = re.sub(r"\s+", " ", result.output)
        assert "backend is running" in out
        assert "No open invite" in out
