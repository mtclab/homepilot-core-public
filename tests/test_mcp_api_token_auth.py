"""MCP speaks API tokens (owner decision, 2026-08-26).

An operator mints an assistant a token in Settings -> Tokens like any other
client - auditable and revocable - and the MCP transport authenticates THAT.
``HP_MCP_TOKEN`` stays as the legacy static fallback.

These are journey gates, not call gates: each one opens a real MCP session over
the real transport (the same middleware stack the mounted /mcp app carries) and
CALLS A TOOL, because "the token authenticated" is not evidence that the client
reached - or was kept out of - the tool it wanted.

Teeth (each verified by reverting the fix and watching the named test fail):
  * map read/write/admin all to one tier -> the tier journeys fail;
  * let an unverified hp_ token fall through to the legacy compare -> the
    revoked-token journey fails;
  * drop the API-token branch entirely -> every journey here fails while the
    legacy gate below still passes.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


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


pytestmark = pytest.mark.asyncio

_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class _Lifespan:
    """Drive an ASGI app's lifespan on the current loop (the session manager
    only starts inside it)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._up = asyncio.Event()
        self._down = asyncio.Event()
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def _send(self, message: dict) -> None:
        if message["type"] in ("lifespan.startup.complete", "lifespan.startup.failed"):
            self._up.set()
        elif message["type"] == "lifespan.shutdown.complete":
            self._down.set()

    async def _receive(self) -> dict:
        return await self._queue.get()

    async def __aenter__(self) -> _Lifespan:
        self._task = asyncio.create_task(self.app({"type": "lifespan"}, self._receive, self._send))
        await self._queue.put({"type": "lifespan.startup"})
        await self._up.wait()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._queue.put({"type": "lifespan.shutdown"})
        await self._down.wait()
        if self._task is not None:
            await self._task


def _parse(resp: httpx.Response) -> dict[str, Any]:
    """The transport answers either JSON or a single SSE event."""
    body = resp.text
    if body.lstrip().startswith("{"):
        return dict(json.loads(body))
    for line in body.splitlines():
        if line.startswith("data:"):
            return dict(json.loads(line[5:].strip()))
    raise AssertionError(f"no JSON-RPC payload in response: {body[:200]}")


class _Session:
    """One authenticated MCP client: initialize, then call tools."""

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._headers = {**_MCP_HEADERS, "Authorization": f"Bearer {token}"}
        self._id = 0

    async def open(self) -> httpx.Response:
        resp = await self._client.post(
            "/",
            headers=self._headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "hp-test", "version": "1.0"},
                },
            },
        )
        if resp.status_code == 200:
            session_id = resp.headers.get("mcp-session-id")
            if session_id:
                self._headers["mcp-session-id"] = session_id
            await self._client.post(
                "/",
                headers=self._headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        return resp

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        resp = await self._client.post(
            "/",
            headers=self._headers,
            json={
                "jsonrpc": "2.0",
                "id": self._id + 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        )
        assert resp.status_code == 200, resp.text[:300]
        payload = _parse(resp)
        assert "result" in payload, payload
        return dict(payload["result"])


@asynccontextmanager
async def _transport(
    tmp_path: Any, env: dict[str, str]
) -> AsyncIterator[tuple[Repository, httpx.AsyncClient]]:
    """The real MCP HTTP app over a real database, with the real server context."""
    from homepilot.mcp import server as mcp_mod

    database = Database(str(tmp_path / "mcp.db"))
    await database.connect()
    await run_migrations(database)
    repo = Repository(database)

    mcp_mod._server_context.clear()
    await mcp_mod._server_context.async_update({"repo": repo, "database": database})
    try:
        with patch.dict(os.environ, env, clear=False):
            app = mcp_mod.create_http_app(mcp_mod.create_server())
        async with _Lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
                yield repo, client
    finally:
        mcp_mod._server_context.clear()
        await database.close()


async def _mint(repo: Repository, scope: str, *, expires_at: str | None = None) -> str:
    token, prefix, token_hash = generate_api_token()
    user_id = await repo.create_user(display_name=f"mcp-{scope}", auth_source="api_token")
    await repo.create_api_token(
        user_id=str(user_id),
        token_type="personal",
        prefix=prefix,
        hash=token_hash,
        scope=scope,
        label=f"mcp-{scope}",
        expires_at=expires_at,
    )
    return token


# The tools each journey drives: a read one and an admin one, both of which need
# nothing from the context but the repository.
_READ_TOOL = "query_inventory"
_ADMIN_TOOL = "delete_auth_token"


class TestApiTokenJourneys:
    async def test_admin_token_reaches_an_admin_tool(self, tmp_path):
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, client):
            admin = await _mint(repo, "admin")
            victim = await _mint(repo, "read")
            victim_prefix = victim[:16]

            session = _Session(client, admin)
            assert (await session.open()).status_code == 200

            read = await session.call_tool(_READ_TOOL)
            assert not read.get("isError"), read

            # The admin tool actually does its work: the token is gone afterwards.
            result = await session.call_tool(_ADMIN_TOOL, {"prefix": victim_prefix})
            assert not result.get("isError"), result
            assert await repo.get_token_by_prefix(victim_prefix) is None

    async def test_read_token_is_refused_the_admin_tool_but_reads(self, tmp_path):
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, client):
            reader = await _mint(repo, "read")
            other = await _mint(repo, "read")

            session = _Session(client, reader)
            assert (await session.open()).status_code == 200

            allowed = await session.call_tool(_READ_TOOL)
            assert not allowed.get("isError"), allowed

            refused = await session.call_tool(_ADMIN_TOOL, {"prefix": other[:16]})
            assert refused.get("isError") is True, refused
            assert "admin tier" in json.dumps(refused)
            # Refused means REFUSED: the token it targeted is still there.
            assert await repo.get_token_by_prefix(other[:16]) is not None

    async def test_write_token_gets_the_full_tier(self, tmp_path):
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, client):
            writer = await _mint(repo, "read,write")
            other = await _mint(repo, "read")

            session = _Session(client, writer)
            assert (await session.open()).status_code == 200
            refused = await session.call_tool(_ADMIN_TOOL, {"prefix": other[:16]})
            assert refused.get("isError") is True, refused
            assert await repo.get_token_by_prefix(other[:16]) is not None

    async def test_revocation_is_live(self, tmp_path):
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, client):
            admin = await _mint(repo, "admin")
            session = _Session(client, admin)
            assert (await session.open()).status_code == 200
            assert not (await session.call_tool(_READ_TOOL)).get("isError")

            row = await repo.get_token_by_prefix(admin[:16])
            await repo.delete_token(row["id"])

            # No restart, no cache: the next session is refused outright.
            assert (await _Session(client, admin).open()).status_code == 401

    async def test_expired_token_is_refused(self, tmp_path):
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, client):
            past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            expired = await _mint(repo, "admin", expires_at=past)
            assert (await _Session(client, expired).open()).status_code == 401

    async def test_last_used_is_stamped(self, tmp_path):
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, client):
            admin = await _mint(repo, "admin")
            assert (await repo.get_token_by_prefix(admin[:16]))["last_used_at"] is None
            assert (await _Session(client, admin).open()).status_code == 200
            assert (await repo.get_token_by_prefix(admin[:16]))["last_used_at"] is not None

    async def test_the_audit_sees_which_token_called(self, tmp_path):
        """Revocable is only half of auditable: the tool context must name the
        credential, not a generic "mcp-http" every client shares.

        The tool table is stood in for so the context a REAL request delivers can
        be read; everything above it - verifier, auth middleware, contextvars -
        is the shipped path."""
        from homepilot.mcp import server as mcp_mod

        seen: dict[str, Any] = {}

        async def _probe(_args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            seen.update({"caller": ctx.get("_mcp_caller_id"), "tier": ctx.get("_mcp_token_scope")})
            return {"ok": True}

        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, client):
            admin = await _mint(repo, "admin")
            session = _Session(client, admin)
            assert (await session.open()).status_code == 200
            with patch.dict(mcp_mod._TOOL_HANDLERS, {_READ_TOOL: _probe}):
                await session.call_tool(_READ_TOOL)

        assert seen["tier"] == "admin"
        assert seen["caller"] == f"mcp-api:{admin[:16]}"

    async def test_garbage_is_refused(self, tmp_path):
        async with _transport(tmp_path, {"HP_MCP_TOKEN": "legacy-secret"}) as (_repo, client):
            for junk in ("not-a-token", "hp_" + "0" * 64, "Bearer"):
                assert (await _Session(client, junk).open()).status_code == 401, junk

    async def test_unauthenticated_is_refused_with_no_token_configured(self, tmp_path):
        """The mount is unconditional now; "no credential configured" must mean
        "refuses everything", never "lets everyone in"."""
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (_repo, client):
            resp = await client.post("/", headers=_MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1})
            assert resp.status_code == 401


class TestLegacyEnvToken:
    """HP_MCP_TOKEN keeps working, at HP_MCP_TOKEN_SCOPE (regression gate)."""

    async def test_env_token_authenticates_at_its_env_scope(self, tmp_path):
        env = {"HP_MCP_TOKEN": "legacy-secret", "HP_MCP_TOKEN_SCOPE": "read_only"}
        async with _transport(tmp_path, env) as (repo, client):
            other = await _mint(repo, "read")
            session = _Session(client, "legacy-secret")
            assert (await session.open()).status_code == 200
            assert not (await session.call_tool(_READ_TOOL)).get("isError")
            refused = await session.call_tool(_ADMIN_TOOL, {"prefix": other[:16]})
            assert refused.get("isError") is True, refused

    async def test_env_token_at_admin_scope_reaches_admin_tools(self, tmp_path):
        env = {"HP_MCP_TOKEN": "legacy-secret", "HP_MCP_TOKEN_SCOPE": "admin"}
        async with _transport(tmp_path, env) as (repo, client):
            victim = await _mint(repo, "read")
            session = _Session(client, "legacy-secret")
            assert (await session.open()).status_code == 200
            result = await session.call_tool(_ADMIN_TOOL, {"prefix": victim[:16]})
            assert not result.get("isError"), result

    async def test_a_revoked_api_token_never_falls_back_to_the_env_secret(self, tmp_path):
        """An hp_-prefixed token that does not verify is refused outright - it is
        never handed to the legacy compare, so a static secret set to an old
        token value cannot resurrect it."""
        async with _transport(tmp_path, {"HP_MCP_TOKEN": ""}) as (repo, _client):
            token = await _mint(repo, "admin")
            row = await repo.get_token_by_prefix(token[:16])
            await repo.delete_token(row["id"])
            with patch.dict(os.environ, {"HP_MCP_TOKEN": token}, clear=False):
                # Rebuild the app so the legacy value IS the revoked token.
                from homepilot.mcp import server as mcp_mod

                app = mcp_mod.create_http_app(mcp_mod.create_server())
            async with _Lifespan(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://mcp.test"
                ) as legacy_client:
                    assert (await _Session(legacy_client, token).open()).status_code == 401


class TestScopeLadderMapping:
    """The tier map is the SAME constant the tier<->scope parity gate reads, so
    the two cannot drift apart."""

    async def test_mapping_comes_from_the_shared_constant(self):
        from homepilot.auth.scopes import API_SCOPE_TO_MCP_TIER
        from homepilot.auth.tokens import mcp_tier_for_token

        assert API_SCOPE_TO_MCP_TIER == {
            "read": "read_only",
            "write": "full",
            "admin": "admin",
        }
        assert mcp_tier_for_token("read") == API_SCOPE_TO_MCP_TIER["read"]
        assert mcp_tier_for_token("read,write") == API_SCOPE_TO_MCP_TIER["write"]
        assert mcp_tier_for_token("admin") == API_SCOPE_TO_MCP_TIER["admin"]
        # "full"/"*" is the API's own way of writing "everything".
        assert mcp_tier_for_token("full") == API_SCOPE_TO_MCP_TIER["admin"]
        assert mcp_tier_for_token("*") == API_SCOPE_TO_MCP_TIER["admin"]
        # A role grants its scopes even when the scope column is empty.
        assert mcp_tier_for_token(None, "operator") == API_SCOPE_TO_MCP_TIER["write"]
        assert mcp_tier_for_token(None, None) is None

    async def test_every_tier_the_map_produces_is_one_the_enforcement_knows(self):
        """A tier the enforcement has never heard of would sail past every check.

        Each tier the map can produce is put through the REAL enforcement
        (_handle_tool) against an admin tool and a read tool, and the whole
        accept/refuse matrix has to come out right."""
        from homepilot.auth.scopes import API_SCOPE_TO_MCP_TIER
        from homepilot.mcp.server import _handle_tool

        expected = {
            "read_only": {"read": True, "admin": False},
            "full": {"read": True, "admin": False},
            "admin": {"read": True, "admin": True},
        }
        assert set(API_SCOPE_TO_MCP_TIER.values()) == set(expected)

        for tier, outcomes in expected.items():
            for kind, allowed in outcomes.items():
                tool = _ADMIN_TOOL if kind == "admin" else _READ_TOOL
                ctx: dict[str, Any] = {"_mcp_token_scope": tier, "repo": None}
                try:
                    await _handle_tool(tool, {"prefix": "hp_none"}, ctx)
                except ValueError as exc:
                    refused_by_scope = "scope" in str(exc)
                except Exception:
                    refused_by_scope = False  # reached the handler, which then failed
                else:
                    refused_by_scope = False
                assert refused_by_scope is not allowed, (
                    f"tier {tier} on a {kind} tool: expected {'allowed' if allowed else 'refused'}"
                )
