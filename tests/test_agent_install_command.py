"""The by-hand install one-liner installs THIS control plane's agent (lane A3, 05/1).

The bug: the Hosts page rendered
``curl -fsSL https://github.com/.../releases/latest/download/install-agent.sh | bash
-s -- --hub http://HOST:8443 --token ...``. The installer and (inside it) the
binary came from GitHub's LATEST release, not from the hub doing the enrolling -
``install-agent.sh`` had a ``--hp-api`` flag for exactly this, and the UI never
passed it. A hand-installed agent matched its hub only when the two releases
happened to line up, a host with no internet could not be enrolled by hand at
all, and ``http://`` taught operators the hub speaks HTTP (it is raw TCP).

Found while building this: pointing ``--hp-api`` at the control plane could not
have worked with the token the panel hands out anyway. The payload routes
accepted the SHARED hub token or an admin API token - not a bootstrap token - so
a bootstrap one-liner 401'd before a byte was fetched. The zero-touch install
stages a bootstrap token and fetches the same routes, so it hit the same wall.

What these gates hold:
  * the one-liner is built by the server (``dist.install_command``), fetches the
    installer from this control plane with the token it carries, and passes
    ``--hp-api`` so the binary comes from here too;
  * a live bootstrap token fetches the payload; a used or expired one does not;
  * when this control plane cannot serve the payload (a source checkout with no
    built binaries), the documented fallback is GitHub PINNED to this control
    plane's version - never ``latest`` - with the reason stated;
  * the GOAL, end to end, lives in ``test_agent_install_shipped_path.py``: the
    exact command, run by bash with curl against ``uvicorn homepilot.main:app``.
    The router-alone version that used to sit here overrode ``require_token``
    and wrapped the command in ``set -o pipefail``, so it could see neither of
    the two defects the 3.7.0-rc2 dev walk hit.

Teeth (each proven by reverting the fix):
  * drop the bootstrap ``token_store.verify`` branch from
    ``router._enrolment_credential`` ->
    ``test_a_live_bootstrap_token_fetches_the_payload`` fails;
  * make ``_install_fields`` return the old GitHub ``latest`` string -> every
    test in ``TestTheOneLiner`` fails.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot import __version__
from homepilot.agent_hub import dist
from homepilot.agent_hub.tokens import BootstrapTokenStore
from homepilot.auth.deps import require_token

SHARED = "hub-enrolment-token-for-the-one-liner"  # pragma: allowlist secret - test fixture
PIN = "ab" * 32


@pytest.fixture
def payload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """What the Dockerfile bakes into the image. The 'installer' prints the
    arguments it was started with, so a test can see what the one-liner passed."""
    root = tmp_path / "agent-dist"
    root.mkdir()
    (root / "hp-agent-linux-amd64").write_bytes(b"\x7fELF" + b"amd64 agent payload")
    (root / "hp-agent-linux-arm64").write_bytes(b"\x7fELF" + b"arm64 agent payload")
    (root / "install-agent.sh").write_text(
        '#!/usr/bin/env bash\nfor a in "$@"; do printf \'ARG:%s\\n\' "$a"; done\n'
    )
    monkeypatch.setenv("HP_AGENT_DIST_DIR", str(root))
    return root


class _Hub:
    def __init__(self, tls: bool = False) -> None:
        self.host = "127.0.0.1"
        self.port = 8443
        self.auth_token = SHARED
        self.tls_enabled = tls
        self.cert_fingerprint = PIN if tls else ""
        self._token_store = BootstrapTokenStore()


def _app(hub: _Hub) -> FastAPI:
    from homepilot.agent_hub.router import dist_router as agents_dist_router
    from homepilot.agent_hub.router import router as agents_router

    class _Registry:
        hub_server = hub

    app = FastAPI()
    app.include_router(agents_router)
    # Mounted as main.py mounts it: without require_token (3.7.0-rc2).
    app.include_router(agents_dist_router)
    app.state.agent_registry = _Registry()
    app.state.repo = None
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    return app


@pytest.fixture
def client(payload_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import homepilot.app_state as app_state

    hub = _Hub()
    app = _app(hub)
    monkeypatch.setattr(app_state, "_agent_registry", app.state.agent_registry, raising=False)
    return TestClient(app)


def _argv(command: str) -> tuple[list[str], list[str]]:
    """The curl argv and the installer's arguments, out of
    ``( f=... && trap ... && curl ... -o "$f" && bash "$f" ARGS ) || {...}``."""
    inner = command[command.index("( ") + 2 : command.rindex(" ) || ")]
    steps = inner.split(" && ")
    fetch, run = shlex.split(steps[2]), shlex.split(steps[3])
    assert fetch[-2:] == ["-o", "$f"] and run[:2] == ["bash", "$f"], command
    return fetch[:-2], run[2:]


class TestTheOneLiner:
    def test_bootstrap_one_liner_installs_from_this_control_plane(self, client: TestClient):
        body = client.post("/agents/bootstrap").json()
        token = body["bootstrap_token"]
        assert body["install_source"] == "control_plane"
        assert body["install_version"] == __version__
        assert body["install_fallback_reason"] is None

        fetch, run = _argv(body["install_command"])
        api = dist.api_base_for("127.0.0.1")
        assert fetch[-1] == f"{api}/agents/dist/install-agent.sh"
        assert f"x-hp-agent-token: {token}" in fetch
        assert run[run.index("--hp-api") + 1] == api, "the binary would not come from here"
        assert run[run.index("--hub") + 1] == "127.0.0.1:8443"
        assert run[run.index("--token") + 1] == token
        assert "github.com" not in body["install_command"]
        # The hub is raw TCP; the one-liner must not dress it up as a URL.
        assert "http://127.0.0.1:8443" not in body["install_command"]

    def test_the_shared_token_one_liner_is_built_the_same_way(self, client: TestClient):
        body = client.get("/agents/token").json()
        fetch, run = _argv(body["install_command"])
        assert body["install_source"] == "control_plane"
        assert f"x-hp-agent-token: {SHARED}" in fetch
        assert "--hp-api" in run

    def test_a_tls_hub_pins_its_certificate(self, payload_dir: Path):
        out = dist.install_command(
            hub_host="hub.example.com",
            hub_port=8443,
            token="t",
            tls=True,
            pin=PIN,
            control_version=__version__,
        )
        _fetch, run = _argv(out["install_command"])
        assert run[run.index("--tls-pin") + 1] == f"sha256:{PIN}"

    def test_a_hostile_token_is_quoted_not_executed(self, payload_dir: Path):
        """The shared token is operator-settable and this string goes into a
        root shell."""
        nasty = "tok'en; rm -rf / #$(id)"
        out = dist.install_command(
            hub_host="hub.example.com",
            hub_port=8443,
            token=nasty,
            tls=False,
            pin="",
            control_version=__version__,
        )
        _fetch, run = _argv(out["install_command"])
        assert run[run.index("--token") + 1] == nasty

    def test_an_unvalidated_hub_host_gets_no_command(self, client: TestClient, monkeypatch):
        from homepilot.agent_hub import router

        # A host that is not a hostname or IP never reaches a root command.
        monkeypatch.setattr(router, "_advertised_hub", lambda *_a: ("bad host;id", 8443))
        body = client.post("/agents/bootstrap").json()
        assert body["install_command"] is None


class TestTheFallback:
    def test_no_payload_in_this_image_falls_back_pinned_to_this_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HP_AGENT_DIST_DIR", str(tmp_path / "empty"))
        out = dist.install_command(
            hub_host="hub.example.com",
            hub_port=8443,
            token="t",
            tls=False,
            pin="",
            control_version="3.9.1",
        )
        assert out["install_source"] == "github"
        assert out["install_fallback_reason"], "the fallback must say why"
        fetch, run = _argv(out["install_command"])
        assert fetch[-1].endswith("/releases/download/v3.9.1/install-agent.sh")
        assert run[run.index("--version") + 1] == "v3.9.1"
        assert "latest" not in out["install_command"], "a fallback to latest is version roulette"
        assert "--hp-api" not in run


class TestBootstrapTokensFetchThePayload:
    def test_a_live_bootstrap_token_fetches_the_payload(self, client: TestClient):
        token = client.post("/agents/bootstrap").json()["bootstrap_token"]
        headers = {"x-hp-agent-token": token}
        assert client.get("/agents/dist/install-agent.sh", headers=headers).status_code == 200
        binary = client.get("/agents/dist/hp-agent-linux-amd64", headers=headers)
        assert binary.status_code == 200 and binary.headers["x-hp-sha256"]

    async def test_fetching_does_not_spend_it_but_registering_does(self, client: TestClient):
        hub = client.app.state.agent_registry.hub_server
        token = client.post("/agents/bootstrap").json()["bootstrap_token"]
        headers = {"x-hp-agent-token": token}
        client.get("/agents/dist/install-agent.sh", headers=headers)
        client.get("/agents/dist/hp-agent-linux-amd64", headers=headers)
        assert hub._token_store.verify(token), "downloading the payload consumed the token"

        assert await hub._token_store.consume(token)  # what register does
        resp = client.get("/agents/dist/install-agent.sh", headers=headers)
        assert resp.status_code == 401, "a spent bootstrap token still downloads"

    def test_an_expired_bootstrap_token_is_refused(self, client: TestClient):
        hub = client.app.state.agent_registry.hub_server
        token = client.post("/agents/bootstrap").json()["bootstrap_token"]
        for key, (h, _exp) in list(hub._token_store._tokens.items()):
            hub._token_store._tokens[key] = (h, time.time() - 1)
        resp = client.get("/agents/dist/install-agent.sh", headers={"x-hp-agent-token": token})
        assert resp.status_code == 401
