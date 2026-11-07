"""With the backend running, the CLI's database commands go through its API (#700).

WHAT THIS FORBIDS: every command built on `_open_cli_db` refused while the
backend held the data directory - reads included - and on the shipped Docker
deploy the backend always runs, so `hp inventory list|show`, `hp invite *`,
`hp quota *`, `hp webhook *`, `hp agent revoke|remove`, the `hp kb reindex`
fallback and `hp policy init` were dead. `hp agent revoke` also skipped the
route's closing of the agent's live channel.

The contract: with a backend holding the data dir, each of these is a request
to the route the console uses, and the database is never opened. (With it
stopped they keep their direct path; their own tests cover that.)

The backend is a fake that records what it was asked; `_open_cli_db` is made
to explode, so a command that still reaches for the database fails loudly.

Teeth: remove a command's `_server_is_running` branch and its case here hits
the exploding `_open_cli_db` (or never sends its request).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from homepilot.cli import main as cli


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.answers: dict[tuple[str, str], httpx.Response] = {}

    def answer(self, method: str, path: str, status: int, body: Any) -> None:
        self.answers[(method, path)] = httpx.Response(status, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path.startswith("/auth/tokens/"):
            return httpx.Response(204)
        found = self.answers.get((request.method, request.url.path))
        return found if found is not None else httpx.Response(404, json={"detail": "not found"})

    def sent(self, method: str, path: str) -> Any:
        for m, p, b in self.calls:
            if (m, p) == (method, path):
                return b if b is not None else {}
        raise AssertionError(f"{method} {path} was never sent; calls: {self.calls}")


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()

    async def mint(*_a: Any, **_k: Any) -> Any:
        return "hp_" + "c" * 64, None

    async def no_db(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the CLI opened the database a running backend holds")

    monkeypatch.setattr(cli, "_server_is_running", lambda _s: True)
    monkeypatch.setattr(cli, "_mint_token_via_api", mint)
    monkeypatch.setattr(cli, "_open_cli_db", no_db)
    monkeypatch.setattr(
        cli,
        "_backend_http_client",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler), base_url="http://127.0.0.1"
        ),
    )
    return fake


def _run(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(cli.app, list(args), input=stdin)


# ── inventory ────────────────────────────────────────────────────────────────


def test_inventory_list_and_show_read_the_api(backend):
    host = {"hostname": "nas", "ip_address": "192.0.2.10", "role": "storage", "status": "online"}
    other = {"hostname": "nas-backup", "role": "storage"}
    backend.answer("GET", "/inventory", 200, {"items": [other, host], "total": 2})

    listed = _run("inventory", "list", "--output", "json", "--role", "storage")
    shown = _run("inventory", "show", "nas", "--output", "json")

    assert listed.exit_code == 0, listed.output
    assert "nas-backup" in listed.output
    assert shown.exit_code == 0, shown.output
    # `q` is free text; show picks the EXACT hostname out of what came back.
    assert json.loads(shown.output)["hostname"] == "nas"


# ── invites and quotas ───────────────────────────────────────────────────────


def test_invite_create_mints_through_the_console_route(backend):
    backend.answer("POST", "/admin/guests/invites", 201, {"id": 1, "token": "inv_" + "z" * 40})

    r = _run(
        "invite", "create", "--cn", "friend", "--template", "9000", "--node", "pve1",
        "--cores", "2", "--ram", "2048", "--disk", "20", "--expires", "3d", "--pool", "guests",
        "--base-url", "https://portal.example.com",
    )  # fmt: skip

    assert r.exit_code == 0, r.output
    assert backend.sent("POST", "/admin/guests/invites") == {
        "cn": "friend",
        "template_vmid": 9000,
        "node": "pve1",
        "cores": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "disk": "scsi0",
        "ttl_minutes": 3 * 24 * 60,
        "pool": "guests",
    }
    assert "https://portal.example.com/invite/" in r.output


def test_invite_create_refuses_an_unsized_machine_by_name(backend):
    # The route takes sub-day windows (#623's ttl_minutes), so `30m` is fine;
    # a size the route requires is refused by name before anything is sent.
    r = _run(
        "invite", "create", "--cn", "friend", "--template", "9000", "--node", "pve1",
        "--expires", "30m", "--ram", "512",
    )  # fmt: skip

    assert r.exit_code == 1
    out = " ".join(r.output.split())
    assert "--cores" in out and "--disk" in out and "--ram" not in out
    assert not [c for c in backend.calls if c[1] == "/admin/guests/invites"]


def test_invite_list_and_revoke_use_the_api(backend):
    backend.answer(
        "GET",
        "/admin/guests",
        200,
        {
            "guests": [],
            "invites": [
                {
                    "prefix": "inv_aaaaaaaaaaaa",
                    "cn": "friend",
                    "state": "open",
                    "caps": {"template_vmid": 9000, "node": "pve1", "cores": 1},
                    "expires_at": "2026-10-01T00:00:00Z",
                }
            ],
        },
    )
    backend.answer("POST", "/admin/guests/invites/inv_aaaaaaaaaaaa/revoke", 200, {"revoked": True})

    listed = _run("invite", "list", "--output", "json")
    revoked = _run("invite", "revoke", "inv_aaaaaaaaaaaa")
    missing = _run("invite", "revoke", "inv_nothere00000")

    assert listed.exit_code == 0, listed.output
    assert "inv_aaaaaaaaaaaa" in listed.output and "open" in listed.output
    assert revoked.exit_code == 0, revoked.output
    assert missing.exit_code == 1
    assert "No open invite" in missing.output


def test_quota_set_and_list_use_the_api(backend):
    backend.answer(
        "POST",
        "/admin/guests/quota",
        200,
        {"cn": "friend", "limits": {"vms": 2}, "usage": {"vms": 1, "cores": 2}},
    )
    backend.answer(
        "GET",
        "/admin/guests",
        200,
        {
            "guests": [
                {"cn": "friend", "limits": {"vms": 2}, "usage": {"vms": 1}},
                {"cn": "nobudget", "limits": None, "usage": {"vms": 0}},
            ],
            "invites": [],
        },
    )

    s = _run("quota", "set", "--cn", "friend", "--max-vms", "2")
    listed = _run("quota", "list")

    assert s.exit_code == 0, s.output
    assert backend.sent("POST", "/admin/guests/quota")["max_vms"] == 2
    assert listed.exit_code == 0, listed.output
    assert "friend" in listed.output and "nobudget" not in listed.output


# ── agents ───────────────────────────────────────────────────────────────────


def test_agent_revoke_and_remove_use_the_routes(backend):
    backend.answer("POST", "/agents/ag-1/revoke", 200, {"revoked": True, "channel_closed": True})
    backend.answer("DELETE", "/agents/ag-2", 409, {"detail": "Agent ag-2 is connected right now."})

    revoked = _run("agent", "revoke", "ag-1")
    refused = _run("agent", "remove", "ag-2", "--yes")
    unknown = _run("agent", "revoke", "ag-none")

    assert revoked.exit_code == 0, revoked.output
    assert "live connection was closed" in revoked.output
    # The route's refusal of a CONNECTED agent reaches the operator.
    assert refused.exit_code == 1
    assert "connected right now" in " ".join(refused.output.split())
    assert unknown.exit_code == 1
    assert "No active credential" in unknown.output


# ── webhooks ─────────────────────────────────────────────────────────────────


def test_webhook_commands_use_the_api(backend):
    backend.answer(
        "POST",
        "/admin/webhooks",
        201,
        {"id": 5, "endpoint": "https://hooks.example.com", "event_types": ["artifact_applied"]},
    )
    backend.answer(
        "GET",
        "/admin/webhooks",
        200,
        {"items": [{"id": 5, "endpoint": "https://hooks.example.com"}], "total": 1},
    )
    backend.answer("POST", "/admin/webhooks/5/test", 200, {"delivered": False, "endpoint": "x"})
    backend.answer("DELETE", "/admin/webhooks/5", 200, {"deleted": True})

    added = _run(
        "webhook", "add", "--url", "https://hooks.example.com/p", "--events", "artifact_applied"
    )
    listed = _run("webhook", "list", "--output", "json")
    tested = _run("webhook", "test", "5")
    deleted = _run("webhook", "delete", "5")

    assert added.exit_code == 0, added.output
    assert backend.sent("POST", "/admin/webhooks")["event_types"] == ["artifact_applied"]
    assert listed.exit_code == 0 and "hooks.example.com" in listed.output
    assert tested.exit_code == 1, "a test delivery the endpoint refused must not exit 0"
    assert deleted.exit_code == 0, deleted.output


def test_webhook_add_refuses_an_event_that_does_not_exist_before_asking(backend):
    r = _run(
        "webhook", "add", "--url", "https://hooks.example.com/p", "--events", "artifact_aplied"
    )

    assert r.exit_code == 1
    assert "artifact_aplied" in r.output
    assert not [c for c in backend.calls if c[1] == "/admin/webhooks"]


# ── policy init and kb reindex ───────────────────────────────────────────────


def test_policy_init_proposes_through_the_api(backend):
    backend.answer("POST", "/artifacts", 200, {"id": "x"})
    # Two answers, the rest left blank.
    answers = "debian-13\n\nnvme-pool\n" + "\n" * 16

    r = _run("policy", "init", stdin=answers)

    assert r.exit_code == 0, r.output
    proposed = [b for m, p, b in backend.calls if (m, p) == ("POST", "/artifacts")]
    assert [b["intent"] for b in proposed] == ["policy: template", "policy: storage_pool"]
    assert all(b["kind"] == "kb-note" for b in proposed)


def test_kb_reindex_does_not_fall_back_to_the_database_under_a_live_backend(backend):
    backend.answer("POST", "/kb/reindex", 500, {"detail": "boom"})

    r = _run("kb", "reindex", "--yes", "--no-embeddings")

    assert r.exit_code == 1
    assert "Nothing was reindexed" in " ".join(r.output.split())
