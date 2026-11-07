"""Webhook configs are reachable on a running install, and their URLs stay secret (#700).

WHAT THIS FORBIDS:
* webhook configs had no REST, UI or MCP surface - only `hp webhook`, which
  opened the database the running backend holds and so refused on every live
  install;
* an `event_types` typo was stored and then never matched an event, so the
  endpoint silently heard nothing;
* endpoint URLs (which carry their own credential in the path or query) in
  delivery logs and in every listing.

The contract: `/admin/webhooks` (admin) lists, creates, deletes and tests; the
listing shows scheme://host:port only, the full URL is echoed once to the
creating admin; event names are checked against what the platform emits; MCP
can list and test but never create or delete (an endpoint receives approval
codes, #681).

Teeth: drop `validate_event_types` from `WebhookIn` and the typo case is
accepted; list `row["url"]` instead of `redact_endpoint` and the listing and
audit cases find the path; put `url` back in the delivery log lines and the
log case finds it; register a create tool and the MCP case fails.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.webhooks.catalog import EVENT_TYPES

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"
SECRET_PATH = "/hooks/T0K3N-in-the-path"
URL = f"https://hooks.example.com{SECRET_PATH}?sig=also-secret"


@pytest.fixture
async def estate(tmp_path: Path):
    db = Database(str(tmp_path / "w.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    uid = await repo.create_user("operator")
    admin, admin_prefix, digest = generate_api_token()
    await repo.create_api_token(uid, "personal", admin_prefix, digest, scope="admin", label="ops")
    writer, _wp, wdigest = generate_api_token()
    await repo.create_api_token(uid, "personal", _wp, wdigest, scope="read,write", label="bot")
    await db.conn.commit()
    yield repo, admin, admin_prefix, writer
    await db.close()


def _client(repo: Repository) -> httpx.AsyncClient:
    from fastapi import Depends
    from fastapi.exceptions import RequestValidationError

    from homepilot.auth.deps import require_token
    from homepilot.main import validation_error_handler
    from homepilot.webhooks.router import router

    app = FastAPI()
    app.include_router(router, dependencies=[Depends(require_token)])
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.state.repo = repo
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# ── the catalogue is the source ──────────────────────────────────────────────


def test_the_event_catalogue_is_exactly_what_the_platform_emits():
    emitted: set[str] = set()
    constants = dict(
        re.findall(
            r'^(EVENT_[A-Z_]+)\s*=\s*"([a-z_]+)"', (SRC / "metrics" / "alerts.py").read_text(), re.M
        )
    )
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        emitted.update(re.findall(r'emit_event\(\s*"([a-z_]+)"', text))
        for name in re.findall(r"emit_event\(\s*(EVENT_[A-Z_]+)", text):
            emitted.add(constants[name])
    assert emitted == set(EVENT_TYPES), (
        f"emitted but not subscribable: {sorted(emitted - EVENT_TYPES)}; "
        f"subscribable but never emitted: {sorted(EVENT_TYPES - emitted)}"
    )


# ── the API journey ──────────────────────────────────────────────────────────


async def test_admin_creates_lists_tests_and_deletes_a_webhook(estate):
    repo, admin, admin_prefix, _writer = estate
    auth = {"Authorization": f"Bearer {admin}"}
    async with _client(repo) as c:
        made = await c.post(
            "/admin/webhooks",
            json={"url": URL, "event_types": ["artifact_proposed"], "secret": "k" * 16},
            headers=auth,
        )
        assert made.status_code == 201, made.text
        body = made.json()
        # The one echo of the full URL, to the admin who typed it.
        assert body["url"] == URL
        assert body["endpoint"] == "https://hooks.example.com"
        assert body["has_secret"] is True
        assert "secret" not in body

        listed = await c.get("/admin/webhooks", headers=auth)
        assert listed.status_code == 200
        text = listed.text
        assert SECRET_PATH not in text and "also-secret" not in text and "k" * 16 not in text
        listing = listed.json()
        assert listing["items"][0]["event_types"] == ["artifact_proposed"]
        assert set(listing["event_types"]) == set(EVENT_TYPES)

        hook_id = body["id"]
        with patch("homepilot.events.deliver_with_retry", AsyncMock(return_value=False)) as send:
            tested = await c.post(f"/admin/webhooks/{hook_id}/test", headers=auth)
        assert tested.status_code == 200
        assert tested.json()["delivered"] is False
        assert send.await_args.kwargs["max_retries"] == 0

        gone = await c.delete(f"/admin/webhooks/{hook_id}", headers=auth)
        assert gone.status_code == 200
        again = await c.delete(f"/admin/webhooks/{hook_id}", headers=auth)
        assert again.status_code == 404
        assert (await c.post(f"/admin/webhooks/{hook_id}/test", headers=auth)).status_code == 404

    rows = await repo.db.fetchall(
        "SELECT action, user_id, details_json FROM audit_log WHERE action LIKE 'webhook_%'"
    )
    assert [r["action"] for r in rows] == ["webhook_created", "webhook_tested", "webhook_deleted"]
    for r in rows:
        assert admin_prefix in r["user_id"]
        assert SECRET_PATH not in (r["details_json"] or "")


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"url": URL, "event_types": ["artifact_aplied"]}, "artifact_aplied"),
        ({"url": "ftp://hooks.example.com/x", "event_types": ["*"]}, "http"),
        ({"url": "https:///no-host", "event_types": ["*"]}, "host"),
        ({"url": URL, "event_types": []}, "event_types"),
    ],
)
async def test_a_webhook_that_could_never_work_is_refused(estate, body, says):
    repo, admin, _p, _w = estate
    async with _client(repo) as c:
        r = await c.post("/admin/webhooks", json=body, headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 422, r.text
    assert says in r.text
    assert SECRET_PATH not in r.text, "a refusal echoed the URL"
    assert await repo.list_webhook_configs() == []


async def test_a_write_token_cannot_see_or_add_webhooks(estate):
    repo, _admin, _p, writer = estate
    auth = {"Authorization": f"Bearer {writer}"}
    async with _client(repo) as c:
        assert (await c.get("/admin/webhooks", headers=auth)).status_code == 403
        r = await c.post("/admin/webhooks", json={"url": URL, "event_types": ["*"]}, headers=auth)
        assert r.status_code == 403
    assert await repo.list_webhook_configs() == []


async def test_a_wildcard_subscription_is_stored_as_the_wildcard(estate):
    repo, admin, _p, _w = estate
    async with _client(repo) as c:
        r = await c.post(
            "/admin/webhooks",
            json={"url": URL, "event_types": ["*", "artifact_applied"]},
            headers={"Authorization": f"Bearer {admin}"},
        )
    assert r.status_code == 201
    assert r.json()["event_types"] == ["*"]
    assert await repo.get_webhook_configs_for_event("alert_firing")


# ── logs ─────────────────────────────────────────────────────────────────────


async def test_a_failed_delivery_logs_the_host_not_the_url(caplog):
    from homepilot.events import deliver_with_retry

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    real_client = httpx.AsyncClient

    def client(*_a: Any, **_k: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(refuse))

    caplog.set_level(logging.DEBUG)
    with patch("homepilot.events.httpx.AsyncClient", client):
        delivered = await deliver_with_retry(URL, {"event": "x"}, None, max_retries=0)

    assert delivered is False
    assert "hooks.example.com" in caplog.text
    assert SECRET_PATH not in caplog.text
    assert "also-secret" not in caplog.text


# ── MCP: read and test, never create or delete ───────────────────────────────


async def test_mcp_lists_and_tests_at_the_admin_tier_only(estate):
    from homepilot.mcp.server import _ADMIN_TOOLS, _TOOL_HANDLERS, _handle_tool
    from homepilot.webhooks import service

    repo, _admin, _p, _w = estate
    await service.create_webhook(
        repo, url=URL, event_types=["*"], secret=None, max_retries=1, actor="t", source="test"
    )
    ctx: dict[str, Any] = {"repo": repo, "_mcp_token_scope": "admin", "_mcp_caller_id": "x (hp_x)"}

    listing = await _handle_tool("list_webhooks", {}, ctx)
    assert listing["total"] == 1
    assert SECRET_PATH not in json.dumps(listing)

    with pytest.raises(ValueError, match="admin tier"):
        await _handle_tool("list_webhooks", {}, {**ctx, "_mcp_token_scope": "full"})

    assert {"list_webhooks", "test_webhook"} <= _ADMIN_TOOLS
    assert not {n for n in _TOOL_HANDLERS if "webhook" in n} - {"list_webhooks", "test_webhook"}, (
        "an MCP tool can now add or remove where approval codes are sent"
    )
