"""Anonymous /health is cheap, bounded and says little (#687).

WHAT THIS FORBIDS, all measured on the shipped backend:
* every anonymous GET /health ran live Proxmox reads with BOTH tokens
  (``test_connection`` + ``check_tokens``) - a free amplifier toward the
  hypervisor, with no rate limit (the middleware skipped /health);
* with a wrong/locked vault passphrase, ``vault_unlocked`` ran the 600k-round
  PBKDF2 ON the event loop and cached nothing on failure: 5 calls = 1.17 s with
  zero event-loop ticks, and the agent hub shares that loop;
* the per-subsystem map went to anyone who asked.

The contract now: the verdict is computed at most once per cache window and
shared; an anonymous caller gets the overall status and version only (the
checks map needs a credential); /health is rate-limited except for the
container's own loopback healthcheck; the unwrap runs in a thread, and a failed
unwrap is remembered until the protected identity file changes.

Teeth: drop the cache and the Proxmox call count grows with the requests; put
the vault unwrap back on the loop and the ticker starves; drop the failure
memo and PBKDF2 runs on every call; return checks to anonymous callers and the
disclosure test fails; exempt /health from the limiter again and the flood
test is never refused.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from homepilot import main as main_mod


@pytest.fixture
def client(monkeypatch):
    from homepilot.main import app

    main_mod._health_cache_clear()
    for leaked in ("agent_registry", "agent_hub", "mcp_app", "proxmox_host", "vault", "repo"):
        if hasattr(app.state, leaked):
            delattr(app.state, leaked)
    db = MagicMock()
    db.fetchall = AsyncMock(return_value=[{"key": "schema_version", "value": "1"}])
    app.state.db = db
    app.state.settings = MagicMock(
        proxmox_host="pve", agent_hub_enabled=False, cors_origins="http://localhost:5173"
    )
    proxmox = MagicMock()
    proxmox.test_connection = AsyncMock(return_value=True)
    proxmox.check_tokens = AsyncMock(return_value={"write": {"ok": True}})
    app.state.proxmox = proxmox
    main_mod._RATE_WINDOW.clear()
    main_mod._RATE_WINDOW_AUTH.clear()
    yield TestClient(app), proxmox
    main_mod._health_cache_clear()
    main_mod._RATE_WINDOW.clear()
    del app.state.proxmox


def test_the_verdict_is_shared_not_recomputed_per_request(client):
    c, proxmox = client
    for _ in range(20):
        assert c.get("/health").status_code == 200
    assert proxmox.test_connection.await_count == 1, (
        f"{proxmox.test_connection.await_count} live Proxmox reads for 20 anonymous requests"
    )
    assert proxmox.check_tokens.await_count == 1


def test_the_verdict_expires(client, monkeypatch):
    c, proxmox = client
    c.get("/health")
    monkeypatch.setattr(main_mod, "_HEALTH_CACHE_SECONDS", 0.0)
    c.get("/health")
    assert proxmox.test_connection.await_count == 2


def test_anonymous_callers_get_the_status_only(client):
    c, _ = client
    body = c.get("/health").json()
    assert set(body) == {"status", "version"}, body


def test_an_authenticated_caller_gets_the_checks(client, monkeypatch):
    c, _ = client

    async def yes(_request):
        return True

    monkeypatch.setattr(main_mod, "_is_authenticated", yes)
    body = c.get("/health", headers={"Authorization": "Bearer hp_x"}).json()
    assert body["checks"]["proxmox"] == "ok"
    assert body["checks"]["database"] == "ok"


def test_an_anonymous_flood_is_rate_limited(client):
    c, _ = client
    codes = [c.get("/health").status_code for _ in range(main_mod._RATE_LIMIT + 5)]
    assert 429 in codes, "anonymous /health is not rate-limited"


def test_the_container_healthcheck_is_never_rate_limited(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(main_mod, "_is_loopback_probe", lambda _r: True)
    codes = [c.get("/health").status_code for _ in range(main_mod._RATE_LIMIT + 5)]
    assert 429 not in codes


def _flood(app, peer: str, headers: dict[str, str]) -> list[int]:
    local = TestClient(app, client=(peer, 50000))
    return [
        local.get("/health", headers=headers).status_code for _ in range(main_mod._RATE_LIMIT + 5)
    ]


def test_the_real_loopback_healthcheck_is_exempt(client):
    """The probe itself, unmocked: a direct loopback peer with no headers."""
    from homepilot.main import app

    assert 429 not in _flood(app, "127.0.0.1", {})


def test_a_same_host_proxy_does_not_exempt_the_clients_it_forwards(client, monkeypatch):
    """A reverse proxy on the same host connects from loopback for a REMOTE
    client. Keying the exemption on the raw peer (#705) let that client flood
    /health unlimited; the resolved client is what is asked."""
    import ipaddress

    from homepilot.main import app

    monkeypatch.setattr(main_mod, "_trusted_proxy_networks", [ipaddress.ip_network("127.0.0.0/8")])
    codes = _flood(app, "127.0.0.1", {"x-forwarded-for": "203.0.113.9"})
    assert 429 in codes, "a remote client behind a loopback proxy bypassed the /health limit"


def test_a_spoofed_loopback_header_from_an_untrusted_peer_is_not_exempt(client):
    from homepilot.main import app

    assert 429 in _flood(app, "203.0.113.9", {"x-forwarded-for": "127.0.0.1"})


# ── The vault unwrap ────────────────────────────────────────────────────────


@pytest.fixture
def locked_vault(tmp_path: Path):
    from homepilot.vault.manager import VaultManager

    good = VaultManager(tmp_path, "the-right-passphrase")
    asyncio.run(good.ensure_master_identity())
    return VaultManager(tmp_path, "a-wrong-passphrase")


async def test_a_failed_unwrap_does_not_block_the_event_loop(locked_vault):
    from homepilot.selfcheck import vault_unlocked

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    started = time.monotonic()
    assert await vault_unlocked(locked_vault) is False
    elapsed = time.monotonic() - started
    stop = True
    await task
    assert ticks >= max(3, int(elapsed / 0.005) // 4), (
        f"the event loop ticked {ticks} times during a {elapsed:.2f}s unwrap - it was blocked"
    )


async def test_a_failed_unwrap_is_remembered(locked_vault, monkeypatch):
    from homepilot.selfcheck import vault_unlocked

    calls = 0
    real = locked_vault._derive_wrapping_key

    def counting(salt: bytes) -> bytes:
        nonlocal calls
        calls += 1
        return real(salt)

    monkeypatch.setattr(locked_vault, "_derive_wrapping_key", counting)
    for _ in range(5):
        assert await vault_unlocked(locked_vault) is False
    assert calls == 1, f"PBKDF2 ran {calls} times for one wrong passphrase"

    # A changed identity file (a restore) is tried again.
    protected = locked_vault._identities_dir / "master.protected"
    st = protected.stat()
    os.utime(protected, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert await vault_unlocked(locked_vault) is False
    assert calls == 2
