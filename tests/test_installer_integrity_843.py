"""The zero-touch install and Upgrade agent check the installer before running it (#843).

The wrapper ``enroll._install_script`` runs inside the guest as root: it sources
the env file HomePilot staged, fetches ``install-agent.sh`` from the control
plane (``$HP_API``) and runs it. The env file now carries the installer's
sha256 (``INSTALLER_SHA256``, from ``dist.installer_sha256``) and the wrapper
refuses a download that does not match - before bash runs it.

This runs the REAL wrapper under bash, with curl against a local HTTP server
standing in for the control plane's ``/agents/dist/install-agent.sh``. Only the
two guest paths it writes are pointed into a temp dir.

Teeth: drop the check from ``_install_script`` and
``test_a_tampered_installer_is_refused_and_nothing_runs`` fails (the marker is
written); drop ``INSTALLER_SHA256`` from ``_env_file`` and it fails too (no
digest, no check).
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from homepilot.agent_hub import enroll
from homepilot.agent_hub.dist import INSTALLER_MISMATCH_MESSAGE

TOKEN = "hpbat_" + "a" * 40  # pragma: allowlist secret - test fixture


class _Served:
    body = b""


@pytest.fixture
def control_plane() -> Iterator[tuple[str, type[_Served]]]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/agents/dist/install-agent.sh":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(_Served.body)))
            self.end_headers()
            self.wfile.write(_Served.body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _Served
    finally:
        server.shutdown()
        server.server_close()


def _run(
    tmp_path: Path, api: str, served: bytes, vouched: bytes, monkeypatch
) -> tuple[subprocess.CompletedProcess[str], Path]:
    env_path, installer_path = tmp_path / "enroll.env", tmp_path / "install.sh"
    monkeypatch.setattr(enroll, "_ENV_PATH", str(env_path))
    monkeypatch.setattr(enroll, "_INSTALLER_PATH", str(installer_path))
    env_path.write_text(
        enroll._env_file(
            "127.0.0.1",
            8443,
            TOKEN,
            False,
            "",
            api,
            installer_sha256=hashlib.sha256(vouched).hexdigest(),
        )
    )
    _Served.body = served
    proc = subprocess.run(  # nosec B603 B607 - a test runs the wrapper under test
        ["bash", "-c", enroll._install_script()],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )
    return proc, installer_path


def _installer(marker: Path) -> bytes:
    return f'#!/bin/sh\ntouch "{marker}"\n'.encode()


def test_the_genuine_installer_runs(tmp_path: Path, control_plane, monkeypatch) -> None:
    api, _ = control_plane
    ran = tmp_path / "installer-ran"
    genuine = _installer(ran)
    proc, _ = _run(tmp_path, api, genuine, genuine, monkeypatch)
    assert ran.exists(), f"the genuine installer did not run: {proc.stderr}"
    assert INSTALLER_MISMATCH_MESSAGE not in proc.stderr


def test_a_tampered_installer_is_refused_and_nothing_runs(
    tmp_path: Path, control_plane, monkeypatch
) -> None:
    api, _ = control_plane
    ran = tmp_path / "installer-ran"
    genuine = _installer(tmp_path / "never")
    tampered = _installer(ran)
    proc, installer_path = _run(tmp_path, api, tampered, genuine, monkeypatch)
    assert proc.returncode != 0
    assert INSTALLER_MISMATCH_MESSAGE in proc.stderr, proc.stderr
    assert not ran.exists(), "the tampered installer RAN"
    assert not installer_path.exists(), "the refused installer was left on the guest"


def test_the_env_file_carries_the_digest_and_rejects_anything_else() -> None:
    digest = "ab" * 32
    body = enroll._env_file("h", 1, TOKEN, False, "", "http://h:8000", installer_sha256=digest)
    assert f"INSTALLER_SHA256={digest}\n" in body
    with pytest.raises(ValueError):
        enroll._env_file("h", 1, TOKEN, False, "", "", installer_sha256="ab; rm -rf /")
