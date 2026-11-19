"""What happened never names its actor by an internal id (3.14 walk).

On the 3.14.0 candidate `hp status` printed

    <uuid> upgraded the agent on web-01 to v3.13.0

The agent-install, provision and tailnet-join routes recorded the caller's
internal user id (a uuid every minted token shares) as the journal actor, and
each surface (the Overview, `hp status`, MCP `get_audit_log`) said the actor its
own way - by printing what was recorded.

The contract now: the server's words carry ``actor`` (dashboard/wording.py).
A recorded internal id is said as the label of the token (or the name of the
user) it belongs to, or as "A token that no longer exists"; and the routes
record the credential's name (`audit_actor`), never the id
(tests/test_one_actor_name.py holds the source).

Teeth: drop the id lookup in ``actor_words`` (or have /audit or MCP call
``with_journal_words`` without names) and the uuid comes back in the words;
have ``hp status`` print ``row["user_id"]`` again and its line test fails.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from homepilot.auth.deps import require_token
from homepilot.auth.tokens import generate_api_token
from homepilot.dashboard import wording
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
GONE_ID = "5f0c2a1e-7b3d-4c9e-9a41-0d2e6b8f3c17"  # invented: matches no token or user


@pytest.fixture
async def journal(tmp_path) -> Any:
    db = Database(str(tmp_path / "j.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    uid = await repo.create_user("operator")
    _token, prefix, digest = generate_api_token()
    token_id = await repo.create_api_token(
        uid, "personal", prefix, digest, scope="read,write", label="claude"
    )
    upgrade = '{"upgrade": true, "agent_version": "v3.13.0"}'
    # Oldest first: the journal reads newest first.
    await repo.log_audit(user_id=GONE_ID, source="api", action="host_added", target_host="nas")
    await repo.log_audit(user_id=uid, source="api", action="provision", target_host="web-02")
    await repo.log_audit(
        user_id=token_id,
        source="api",
        action="install_agent",
        target_host="web-01",
        details_json=upgrade,
    )
    await repo.log_audit(
        user_id=f"claude ({prefix})", source="mcp", action="approve", artifact_id="c-1"
    )
    await db.conn.commit()
    try:
        yield repo
    finally:
        await db.close()


def _app(repo: Repository) -> FastAPI:
    from homepilot.audit.router import router as audit_router

    app = FastAPI()
    app.include_router(audit_router, prefix="/audit")
    app.state.repo = repo
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "label": "owner"}
    return app


async def _audit(repo: Repository) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(repo)), base_url="http://t"
    ) as c:
        r = await c.get("/audit", params={"actor": "people"})
        assert r.status_code == 200, r.text
        return list(r.json()["items"])


def _no_uuid(words: dict[str, Any]) -> None:
    for key, value in words.items():
        assert not UUID.search(str(value or "")), f"an internal id in words[{key!r}]: {value}"


EXPECTED = {
    "approve": "claude approved c-1",
    "install_agent": "claude upgraded the agent on web-01 to v3.13.0",
    "provision": "operator created the guest web-02",
    "host_added": "A token that no longer exists added the host nas",
}


def _sentences(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {r["action"]: f"{r['words']['actor']} {r['words']['text']}" for r in rows}


async def test_the_console_route_names_every_actor_in_words(journal: Repository) -> None:
    rows = await _audit(journal)
    for row in rows:
        _no_uuid(row["words"])
    assert _sentences(rows) == EXPECTED


async def test_mcp_get_audit_log_says_the_same(journal: Repository) -> None:
    from homepilot.mcp.tools.ops_tools import handle_get_audit_log

    out = await handle_get_audit_log({"actor": "people"}, {"repo": journal})
    for row in out["items"]:
        _no_uuid(row["words"])
    assert _sentences(out["items"]) == EXPECTED


async def test_hp_status_prints_the_label_not_the_id(journal: Repository) -> None:
    from homepilot.cli.ops import _what_happened

    lines = _what_happened(await _audit(journal))
    assert "claude upgraded the agent on web-01 to v3.13.0" in lines
    assert "A token that no longer exists added the host nas" in lines
    for line in lines:
        assert not UUID.search(line), line


def test_an_older_servers_row_without_actor_words_is_still_no_id() -> None:
    """`hp status` against a server that sends no ``actor``: never the id."""
    from homepilot.cli.ops import _what_happened

    lines = _what_happened(
        [{"action": "provision", "user_id": GONE_ID, "words": {"text": "created the guest x"}}]
    )
    assert lines == ["A token that no longer exists created the guest x"]


def test_actor_words_cover_every_spelling() -> None:
    names = {"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee": "claude"}
    assert wording.actor_words("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", names) == "claude"
    assert wording.actor_words(GONE_ID, names) == wording.GONE_CREDENTIAL
    assert wording.actor_words("claude (hp_0123456789abc)") == "claude"
    assert wording.actor_words("system") == "HomePilot"
    assert wording.actor_words(None) == "HomePilot"
    assert wording.actor_words("admin-secret") == "admin-secret"
