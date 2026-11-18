"""Phone alerts from the product (#822), proven against a REAL ntfy server.

The issue's acceptance: on a fresh install, enable phone alerts in the UI, add a
phone, receive the test push - no shell, no hand-edited file, no token ever
shown. This drives that path through the REAL routes, the REAL vault and the
REAL ntfy release the compose files pin, started by the compose file's OWN
service script (extracted from docker-compose.yml, ``$$`` unescaped the way
Compose does). Only container paths are swapped for scratch ones, via ntfy's
environment variables, which take precedence over its config file.

The journey: enable -> HomePilot writes server.yml + tokens -> the service
script starts ntfy with deny-all -> add a phone -> that login subscribes and
RECEIVES HomePilot's test push -> an unauthenticated subscriber is refused ->
no token appears in any API response, log line, audit row or database row ->
removing the phone locks it out -> disabling stops ntfy -> enabling again
brings the server back and the phone still hears it.

Needs the ntfy binary: set HP_NTFY_BIN to the ``ntfy`` from the v2.37.2 release
tarball (checksum-verified, see docs/spikes/ntfy-bundle.md). Without it every
test here SKIPS, saying so.

Teeth (each reverted by hand and seen red; recorded in the PR):
* ``auth-default-access`` rendered as read-write -> the anonymous subscriber is
  served and ``test_the_journey`` fails;
* ``add_phone`` without the read grant -> the phone's subscription is refused;
* ``status()`` carrying the admin token -> the no-token sweep fails;
* the service script without its config-checksum watch -> disabling leaves ntfy
  serving and the stop step fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.notifications import Notifier
from homepilot.notifications.bundled import ADMIN_USER, TOPIC, VAULT_REF, PhoneAlerts
from homepilot.vault.manager import VaultManager

REPO = Path(__file__).resolve().parents[1]
NTFY_BIN = os.environ.get("HP_NTFY_BIN", "")
SKIP_REASON = (
    "HP_NTFY_BIN is not set: the phone-alert journey needs the real ntfy binary "
    "(v2.37.2 release tarball, checksum-verified - docs/spikes/ntfy-bundle.md)"
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not (NTFY_BIN and os.access(NTFY_BIN, os.X_OK)), reason=SKIP_REASON),
]


def service_script(compose: Path = REPO / "docker-compose.yml") -> str:
    """The ntfy service's shell script exactly as Compose would hand it to sh."""
    service = yaml.safe_load(compose.read_text())["services"]["ntfy"]
    assert service["entrypoint"] == ["sh", "-c"]
    return str(service["command"][0]).replace("$$", "$")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class NtfyService:
    """The compose service's script, run as a local process group."""

    def __init__(self, data_dir: Path, port: int, bin_dir: Path) -> None:
        self.data_dir = data_dir
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.bin_dir = bin_dir
        self.log = data_dir.parent / "ntfy-service.log"
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        ntfy_dir = self.data_dir / "ntfy"
        env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "NTFY_HP_CONFIG": str(ntfy_dir / "server.yml"),
            # Container paths -> scratch paths. Env beats the config file.
            "NTFY_LISTEN_HTTP": f"127.0.0.1:{self.port}",
            "NTFY_AUTH_FILE": str(ntfy_dir / "user.db"),
            "NTFY_CACHE_FILE": str(ntfy_dir / "cache.db"),
        }
        with self.log.open("ab") as log:
            self.proc = subprocess.Popen(
                ["/bin/sh", "-c", service_script()],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=15)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None


@pytest.fixture
def ntfy_bin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    (d / "ntfy").symlink_to(NTFY_BIN)
    return d


@pytest.fixture
def service(tmp_path: Path, ntfy_bin_dir: Path) -> Iterator[NtfyService]:
    data = tmp_path / "hp"
    data.mkdir()
    svc = NtfyService(data, _free_port(), ntfy_bin_dir)
    yield svc
    svc.stop()


class Recorder(httpx.AsyncClient):
    """The API client; keeps every response so the sweep can read them all."""

    seen: list[str]

    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:  # type: ignore[override]
        resp = await super().request(*args, **kwargs)
        self.seen.append(json.dumps(dict(resp.headers)) + "\n" + resp.text)
        return resp


@pytest.fixture
async def hp(tmp_path: Path, service: NtfyService) -> AsyncIterator[dict[str, Any]]:
    from homepilot.notifications.router import router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    vault = VaultManager(service.data_dir, "a-test-vault-passphrase-long-enough")
    await vault.ensure_master_identity()
    notifier = Notifier(db, vault, retry_delays=())
    app = FastAPI()
    app.include_router(router)
    app.state.notifier = notifier
    app.state.repo = repo
    app.state.phone_alerts = PhoneAlerts(
        notifier=notifier, repo=repo, data_dir=service.data_dir, internal_url=service.url
    )
    app.dependency_overrides[require_token] = lambda: {
        "user_id": "1",
        "token_id": "1",
        "scope": "admin",
        "role": "admin",
        "display_name": "admin",
    }
    client = Recorder(transport=httpx.ASGITransport(app=app), base_url="http://hp")
    client.seen = []
    try:
        yield {"client": client, "repo": repo, "vault": vault, "db": db, "notifier": notifier}
    finally:
        await notifier.drain()
        await client.aclose()
        await db.close()


async def _until(predicate: Any, timeout: float = 25.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.5)


async def _served_with_our_config(client: httpx.AsyncClient) -> bool:
    body = (await client.get("/admin/notifications/phone")).json()
    return bool(body["bundled"] and body["bundled"]["server"]["configured"])


async def _down(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            await c.get(f"{url}/v1/health")
    except httpx.HTTPError:
        return True
    return False


async def _receive_one(url: str, auth: tuple[str, str], trigger: Any) -> dict[str, Any]:
    """Subscribe like the phone app (JSON stream), fire ``trigger`` once the
    stream is open, and return the first message event."""
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as c,
        c.stream("GET", f"{url}/{TOPIC}/json", auth=auth) as stream,
    ):
        assert stream.status_code == 200, f"subscription refused: HTTP {stream.status_code}"
        lines = stream.aiter_lines()
        opened = json.loads(await anext(lines))
        assert opened["event"] == "open"
        await trigger()
        async for line in lines:
            event = json.loads(line)
            if event["event"] == "message":
                return dict(event)
    raise AssertionError("the stream ended without a message")


async def test_the_journey(hp, service: NtfyService, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client: Recorder = hp["client"]

    # 1. Nothing is set up on a fresh install.
    fresh = (await client.get("/admin/notifications/phone")).json()
    assert fresh["bundled"] is None
    assert fresh["compose_command"] == "docker compose --profile ntfy up -d"

    # 2. Enable: config + tokens written, channel created - before ntfy runs.
    enabled = await client.post("/admin/notifications/phone/bundled")
    assert enabled.status_code == 200, enabled.text
    bundled = enabled.json()["bundled"]
    assert bundled["server"]["running"] is False, "nothing serves before the service starts"
    config_path = service.data_dir / "ntfy" / "server.yml"
    assert config_path.stat().st_mode & 0o777 == 0o600
    config = yaml.safe_load(config_path.read_text())
    assert config["auth-default-access"] == "deny-all"
    assert config["enable-signup"] is False
    assert "attachment-cache-dir" not in config
    stored = await hp["vault"].get_secret(VAULT_REF)
    admin_token, publisher_token = stored["admin_token"], stored["publisher_token"]
    # Positive control for the sweep below: these are the strings to hunt for.
    assert admin_token in config_path.read_text()
    assert publisher_token in config_path.read_text()

    # 3. The compose service script starts ntfy from that file.
    service.start()
    await _until(lambda: _served_with_our_config(client), what="ntfy serving HomePilot's config")

    # 4. The address a phone uses answers as THIS server (deny-all).
    checked = await client.put(
        "/admin/notifications/phone/bundled/public-url", json={"url": service.url}
    )
    assert checked.json()["ok"] is True, checked.text

    # 5. Add a phone: name + password shown once, never cached.
    added = await client.post("/admin/notifications/phone/phones", json={"name": "pixel"})
    assert added.status_code == 201, added.text
    assert added.headers["cache-control"] == "no-store"
    phone = added.json()
    login = (phone["username"], phone["password"])
    assert phone["subscribe_link"].startswith("ntfy://127.0.0.1:")
    assert phone["subscribe_link"].endswith("&secure=false")
    listed = (await client.get("/admin/notifications/phone/phones")).json()
    assert listed["items"] == [{"name": "pixel", "can_read": True}]

    # 6. That login subscribes and RECEIVES HomePilot's test push.
    channel_id = bundled["channel_id"]

    async def send_test() -> None:
        tested = await client.post(f"/admin/notifications/channels/{channel_id}/test")
        assert tested.json()["delivered"] is True, tested.text

    message = await _receive_one(service.url, login, send_test)
    assert message["title"] == "HomePilot test notification"
    assert message["topic"] == TOPIC

    # 7. Everyone else is refused: anonymous, a wrong password, and the phone
    #    itself may not publish (read-only).
    async with httpx.AsyncClient(timeout=5) as c:
        anon = await c.get(f"{service.url}/{TOPIC}/json", params={"poll": "1"})
        assert anon.status_code == 403, "an unauthenticated subscriber was served"
        wrong = await c.get(
            f"{service.url}/{TOPIC}/json", params={"poll": "1"}, auth=("pixel", "not-it")
        )
        assert wrong.status_code == 401
        publish = await c.post(f"{service.url}/{TOPIC}", content=b"spoof", auth=login)
        assert publish.status_code == 403, "a phone login could publish"
        guessed = await c.get(f"{service.url}/v1/users", auth=(ADMIN_USER, "password"))
        assert guessed.status_code == 401, "the admin account accepted a password"

    # 8. No token in any API response, log line, audit row or database row.
    audit = await hp["db"].fetchall("SELECT * FROM audit_log")
    settings_rows = await hp["db"].fetchall("SELECT * FROM settings")
    channels = await hp["db"].fetchall("SELECT * FROM notification_channels")
    haystacks = {
        "api responses": "\n".join(client.seen),
        "logs": caplog.text,
        "audit": json.dumps([dict(r) for r in audit]),
        "settings": json.dumps([dict(r) for r in settings_rows]),
        "channels": json.dumps([dict(r) for r in channels]),
    }
    assert audit, "the journey wrote no audit rows - the sweep would be vacuous"
    for where, text in haystacks.items():
        assert admin_token not in text, f"the admin token appears in {where}"
        assert publisher_token not in text, f"the publisher token appears in {where}"
        assert phone["password"] not in text or where == "api responses", (
            f"the phone's password appears in {where}"
        )
    # The password is in exactly ONE response: the one that created it.
    assert sum(phone["password"] in s for s in client.seen) == 1

    # 9. Removing the phone locks it out.
    removed = await client.delete("/admin/notifications/phone/phones/pixel")
    assert removed.status_code == 200, removed.text
    async with httpx.AsyncClient(timeout=5) as c:
        gone = await c.get(f"{service.url}/{TOPIC}/json", params={"poll": "1"}, auth=login)
        assert gone.status_code == 401

    # 10. Disabling removes config, channel and tokens; the service stops ntfy.
    disabled = await client.delete("/admin/notifications/phone/bundled")
    assert disabled.json()["bundled"] is None
    assert not config_path.exists()
    assert await hp["notifier"].store.get(channel_id) is None
    with pytest.raises(Exception):  # noqa: B017 - any vault refusal: the entry is gone
        await hp["vault"].get_secret(VAULT_REF)
    await _until(lambda: _down(service.url), what="ntfy to stop after its config was removed")


async def test_enabling_again_keeps_the_phones(hp, service: NtfyService) -> None:
    """ntfy's own database (phone logins) stays in the data dir across a
    disable: re-enabling mints NEW tokens, the service starts ntfy again on its
    own when the config reappears, and the phone still hears HomePilot."""
    client: Recorder = hp["client"]
    service.start()
    await client.post("/admin/notifications/phone/bundled")
    await _until(lambda: _served_with_our_config(client), what="first start")
    first = await hp["vault"].get_secret(VAULT_REF)
    phone = (await client.post("/admin/notifications/phone/phones", json={"name": "tab"})).json()

    await client.delete("/admin/notifications/phone/bundled")
    await _until(lambda: _down(service.url), what="the stop")

    again = await client.post("/admin/notifications/phone/bundled")
    assert again.status_code == 200, again.text
    await _until(lambda: _served_with_our_config(client), what="the restart on the new config")
    second = await hp["vault"].get_secret(VAULT_REF)
    assert second["admin_token"] != first["admin_token"], "a disable must retire the old tokens"

    async with httpx.AsyncClient(timeout=5) as c:
        old = await c.get(
            f"{service.url}/v1/version",
            headers={"Authorization": f"Bearer {first['admin_token']}"},
        )
        assert old.status_code == 401, "a retired admin token still works"

    channel_id = again.json()["bundled"]["channel_id"]

    async def send_test() -> None:
        tested = await client.post(f"/admin/notifications/channels/{channel_id}/test")
        assert tested.json()["delivered"] is True, tested.text

    message = await _receive_one(service.url, (phone["username"], phone["password"]), send_test)
    assert message["title"] == "HomePilot test notification"
