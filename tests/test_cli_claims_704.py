"""CLI commands say only what they established (#704).

WHAT THIS FORBIDS, each found on the shipped CLI:

* ``hp drift`` printed the applied/approved artifacts under the title "Drift:
  Artifact Pile vs Inventory" and compared nothing; the drift checks live in
  the backend, behind ``/artifacts/drift``.
* ``hp status`` promised "daemon info, PVE connectivity" and showed neither.
* ``hp vault delete`` printed "deleted" for a name that was never stored.
* ``hp vault set`` took the secret as ``--value`` (process list, shell
  history) or at a prompt that echoed it.

Teeth: restore the old ``drift`` body and the drift tests find the old title
and no states; drop the ``delete_secret`` return check and the delete test
sees "deleted"; put ``--value`` back and the argv test exits 0.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from homepilot.cli import main as cli

PASSPHRASE = "a-test-passphrase-that-is-long-enough"  # pragma: allowlist secret


def _run(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(cli.app, list(args), input=stdin)


# ── hp drift: the stored verdicts, through the backend ───────────────────────

ARTIFACTS = {
    "applied": [
        {"id": "a-drifted", "kind": "shell-script", "target": {"host": "web1"}},
        {"id": "a-in-spec", "kind": "ansible-playbook", "target": {"node": "pve1"}},
        {"id": "a-unknown", "kind": "http-sequence", "target": {"service": "demo"}},
    ],
    "approved": [{"id": "a-never", "kind": "shell-script", "target": {}}],
}
CHECKS = [
    {"artifact_id": "a-drifted", "state": "drifted", "checked_at": "2026-09-24T10:00:00Z"},
    {"artifact_id": "a-in-spec", "state": "in_spec", "checked_at": "2026-09-24T10:00:00Z"},
    {
        "artifact_id": "a-unknown",
        "state": "unknown",
        "checked_at": "2026-09-24T10:00:00Z",
        "details_json": json.dumps({"reason": "agent_unreachable"}),
    },
]


@pytest.fixture
def backend(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path.startswith("/auth/tokens/"):
            return httpx.Response(204)
        if path == "/artifacts/drift":
            return httpx.Response(200, json={"items": CHECKS, "total": len(CHECKS)})
        if path == "/artifacts":
            items = ARTIFACTS.get(request.url.params.get("status", ""), [])
            return httpx.Response(200, json={"items": items, "total": len(items)})
        return httpx.Response(404, json={"detail": "not found"})

    async def mint(*_a: Any, **_k: Any) -> Any:
        return "hp_" + "b" * 64, None

    monkeypatch.setattr(cli, "_mint_token_via_api", mint)
    monkeypatch.setattr(
        cli,
        "_backend_http_client",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"
        ),
    )
    return seen


def test_drift_states_are_the_stored_verdicts():
    artifacts = ARTIFACTS["applied"] + ARTIFACTS["approved"]
    states = {row[0]: row[3] for row in cli._drift_rows(artifacts, CHECKS)}

    assert states == {
        "a-drifted": "drifted",
        "a-in-spec": "in spec",
        "a-unknown": "unknown (agent unreachable)",
        "a-never": "never checked",
    }


def test_drift_reads_the_backend_and_exits_non_zero_on_drift(backend):
    r = _run("drift")

    assert "Artifact Pile vs Inventory" not in r.output
    assert "1 drifted, 1 never checked, of 4" in r.output
    assert any(u.endswith("/artifacts/drift?limit=1000") for u in backend)
    # Drift found is a non-zero exit a script can act on.
    assert r.exit_code == 2, r.output


def test_drift_refresh_asks_the_backend_to_check_first(backend):
    _run("drift", "--refresh")

    assert any("/artifacts/drift" in u and "refresh=true" in u for u in backend)


def test_drift_without_a_backend_says_so_and_fails(monkeypatch):
    async def no_mint(*_a: Any, **_k: Any) -> Any:
        return None, None

    monkeypatch.setattr(cli, "_mint_token_via_api", no_mint)

    r = _run("drift")

    assert r.exit_code == 1
    assert "Could not read drift from the backend" in r.output


# ── hp status: no connectivity claim it did not make ─────────────────────────


def test_status_says_the_pve_address_is_configured_not_probed(monkeypatch):
    monkeypatch.setattr(cli, "_pve_host", lambda _s: "pve.example.com")
    monkeypatch.setattr(cli, "_vault_state", lambda _s: "locked")
    monkeypatch.setattr(cli, "_server_is_running", lambda _s: False)
    monkeypatch.setattr(
        cli, "_get_artifact_store", lambda: type("S", (), {"list": lambda self: []})()
    )

    r = _run("status")

    assert r.exit_code == 0, r.output
    assert "not probed" in r.output
    assert "not running" in r.output
    assert "connectivity" not in (cli.status.__doc__ or "").split("\n")[0].lower()


# ── hp vault: delete and set ─────────────────────────────────────────────────


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HP_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HP_VAULT_PASSPHRASE", PASSPHRASE)
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_deleting_a_name_that_is_not_there_does_not_say_deleted(vault_home):
    r = _run("vault", "delete", "never-stored", "--yes")

    assert r.exit_code == 1
    assert "nothing was deleted" in r.output
    assert "Secret 'never-stored' deleted" not in r.output


def test_deleting_a_stored_secret_says_deleted(vault_home):
    from homepilot.vault import VaultManager

    vault = VaultManager(vault_home, PASSPHRASE)

    async def _seed() -> None:
        await vault.ensure_master_identity()
        await vault.store_secret("stored", {"k": "v"})

    asyncio.run(_seed())

    r = _run("vault", "delete", "stored", "--yes")

    assert r.exit_code == 0, r.output
    assert "Secret 'stored' deleted" in r.output
    assert not (vault_home / "vault" / "secrets" / "stored.age").exists()


def test_vault_set_takes_no_secret_on_the_command_line(vault_home):
    r = _run("vault", "set", "argv-secret", "--value", '{"k": "v"}')

    assert r.exit_code != 0
    assert not (vault_home / "vault" / "secrets" / "argv-secret.age").exists()


def test_vault_set_reads_the_secret_from_stdin(vault_home):
    r = _run("vault", "set", "piped", stdin='{"token": "from-stdin-value"}')

    assert r.exit_code == 0, r.output
    assert "from-stdin-value" not in r.output
    from homepilot.vault import VaultManager

    stored = asyncio.run(VaultManager(vault_home, PASSPHRASE).get_secret("piped"))
    assert stored == {"token": "from-stdin-value"}


def test_vault_set_refuses_bad_json_without_echoing_it(vault_home):
    r = _run("vault", "set", "bad", stdin="not-json-but-secret-looking")

    assert r.exit_code == 1
    assert "Invalid JSON" in r.output
    assert "not-json-but-secret-looking" not in r.output
