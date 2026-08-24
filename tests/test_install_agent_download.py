"""install-agent.sh must never install an unverified root binary, and never via
a predictable path.

Two defects in the shipped installer (#381 remainder):

1. The GitHub fallback ran a bare ``curl -fSL -o /tmp/hp-agent "$URL"`` with NO
   digest check at all. The control-plane path has verified against the
   ``x-hp-sha256`` header since #464 and fails closed; the fallback - the path
   taken by exactly the hosts that have no control plane to trust yet - did not.
   Anyone able to answer for github.com (a proxy, a poisoned resolver, a
   compromised release asset) got root on the box.
2. Both paths downloaded to the fixed path ``/tmp/hp-agent``, chmod'd it and
   moved it into place. A local user can pre-create that path as a symlink and
   have root write through it, and between the ``sha256sum`` check and the ``mv``
   there was a window in which the verified bytes could be swapped for others.

The gates below execute the SHIPPED script's download block (sliced verbatim
between its markers) against a local HTTP server standing in for both the
control plane and GitHub, so they assert the real script's behaviour.

Teeth (each checked by reverting the corresponding hunk):

* Restore ``curl -fSL -o /tmp/hp-agent "$URL"`` with no manifest lookup and
  ``test_fallback_without_a_manifest_refuses`` fails - the install succeeds and
  the binary lands unverified (`assert proc.returncode == 1` -> got 0).
* Restore ``chmod +x /tmp/hp-agent`` + ``mv`` and
  ``test_the_block_never_uses_a_predictable_download_path`` fails on the
  "/tmp/hp-agent" assertion.
"""

from __future__ import annotations

import hashlib
import http.server
import platform
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-agent.sh"
START = ">>> download block"
END = "<<< end download block"

REPO = "mtclab/homepilot-core-public"
VERSION = "v9.9.9"

_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), "amd64")
ASSET = f"hp-agent-linux-{_ARCH}"

# A real ELF, because the block sanity-checks the download with `file`.
PAYLOAD = Path("/bin/true").read_bytes()
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
WRONG_SHA = "0" * 64


def _download_block() -> str:
    text = SCRIPT.read_text()
    start = text.index(START)
    end = text.index(END)
    block = text[text.index("\n", start) + 1 : text.rindex("\n", start, end)]
    assert "mktemp -d" in block, "download block markers no longer wrap the download logic"
    return block


def _download_code() -> str:
    """The block with comment lines stripped - what bash actually executes."""
    return "\n".join(
        line for line in _download_block().splitlines() if not line.lstrip().startswith("#")
    )


class _Server(http.server.BaseHTTPRequestHandler):
    """Stands in for both the control plane and GitHub release downloads."""

    # Set per test.
    hub_digest: str | None = PAYLOAD_SHA
    manifest: bytes | None = None
    payload: bytes = PAYLOAD

    def log_message(self, *_args: object) -> None:  # keep pytest output clean
        pass

    def _send(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        cls = type(self)
        path = self.path
        if path == f"/agents/dist/{ASSET}":
            headers = {}
            if cls.hub_digest is not None:
                headers["x-hp-sha256"] = cls.hub_digest
            self._send(200, cls.payload, headers)
            return
        if path == f"/{REPO}/releases/download/{VERSION}/{ASSET}":
            self._send(200, cls.payload)
            return
        if path == f"/{REPO}/releases/download/{VERSION}/SHA256SUMS":
            if cls.manifest is None:
                self._send(404, b"not found")
            else:
                self._send(200, cls.manifest)
            return
        self._send(404, b"not found")


@pytest.fixture
def server() -> Iterator[str]:
    _Server.hub_digest = PAYLOAD_SHA
    _Server.manifest = None
    _Server.payload = PAYLOAD
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _run(
    base: str,
    install_dir: Path,
    *,
    hp_api: str = "",
    allow_unverified: str = "false",
) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        [
            "set -euo pipefail",
            "SUDO=''",
            f'INSTALL_DIR="{install_dir}"',
            f'HP_API="{hp_api}"',
            'TOKEN="enrollment-token"',
            f'REPO="{REPO}"',
            f'VERSION="{VERSION}"',
            f'GOARCH="{_ARCH}"',
            f'GH_API_BASE="{base}"',
            f'GH_DL_BASE="{base}"',
            'SUMS_NAME="SHA256SUMS"',
            f'ALLOW_UNVERIFIED="{allow_unverified}"',
            _download_block(),
        ]
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


class TestTheControlPlanePathStillVerifies:
    def test_a_matching_x_hp_sha256_installs(self, server: str, tmp_path: Path) -> None:
        proc = _run(server, tmp_path, hp_api=server)
        assert proc.returncode == 0, proc.stderr
        installed = tmp_path / "hp-agent"
        assert installed.read_bytes() == PAYLOAD
        assert oct(installed.stat().st_mode)[-3:] == "755"

    def test_a_missing_header_still_refuses(self, server: str, tmp_path: Path) -> None:
        _Server.hub_digest = None
        proc = _run(server, tmp_path, hp_api=server)
        assert proc.returncode == 1, proc.stdout
        assert "Refusing to install an unverified binary" in proc.stderr
        assert not (tmp_path / "hp-agent").exists()

    def test_allow_unverified_does_not_loosen_the_control_plane_path(
        self, server: str, tmp_path: Path
    ) -> None:
        """The override exists for a release with no manifest, not as a global
        "skip verification" switch: the hub always serves its digest."""
        _Server.hub_digest = None
        proc = _run(server, tmp_path, hp_api=server, allow_unverified="true")
        assert proc.returncode == 1, proc.stdout
        assert not (tmp_path / "hp-agent").exists()

    def test_a_wrong_header_digest_refuses(self, server: str, tmp_path: Path) -> None:
        _Server.hub_digest = WRONG_SHA
        proc = _run(server, tmp_path, hp_api=server)
        assert proc.returncode == 1, proc.stdout
        assert "Checksum mismatch" in proc.stderr
        assert not (tmp_path / "hp-agent").exists()


class TestTheGitHubFallbackVerifies:
    def test_a_manifest_that_matches_installs(self, server: str, tmp_path: Path) -> None:
        _Server.manifest = f"{PAYLOAD_SHA}  {ASSET}\n".encode()
        proc = _run(server, tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert (tmp_path / "hp-agent").read_bytes() == PAYLOAD
        assert f"Verified sha256 {PAYLOAD_SHA}" in proc.stdout

    def test_a_manifest_that_does_not_match_refuses(self, server: str, tmp_path: Path) -> None:
        """The whole point: a tampered release asset is caught."""
        _Server.manifest = f"{WRONG_SHA}  {ASSET}\n".encode()
        proc = _run(server, tmp_path)
        assert proc.returncode == 1, proc.stdout
        assert "Checksum mismatch" in proc.stderr
        assert not (tmp_path / "hp-agent").exists()

    def test_fallback_without_a_manifest_refuses(self, server: str, tmp_path: Path) -> None:
        _Server.manifest = None  # the release publishes no SHA256SUMS
        proc = _run(server, tmp_path)
        assert proc.returncode == 1, proc.stdout
        assert not (tmp_path / "hp-agent").exists(), (
            "an unverified root binary was installed from the GitHub fallback"
        )
        # The refusal must name the manifest it looked for, or the operator
        # cannot tell what to publish.
        assert "SHA256SUMS" in proc.stderr
        assert f"{REPO}/releases/download/{VERSION}/SHA256SUMS" in proc.stderr
        assert "--allow-unverified" in proc.stderr

    def test_a_manifest_without_a_line_for_this_asset_refuses(
        self, server: str, tmp_path: Path
    ) -> None:
        _Server.manifest = f"{PAYLOAD_SHA}  some-other-artifact\n".encode()
        proc = _run(server, tmp_path)
        assert proc.returncode == 1, proc.stdout
        assert ASSET in proc.stderr
        assert not (tmp_path / "hp-agent").exists()

    def test_allow_unverified_is_the_only_override(self, server: str, tmp_path: Path) -> None:
        _Server.manifest = None
        proc = _run(server, tmp_path, allow_unverified="true")
        assert proc.returncode == 0, proc.stderr
        assert (tmp_path / "hp-agent").read_bytes() == PAYLOAD
        assert "UNVERIFIED" in proc.stderr, "the override must say what it just did"


class TestNoPredictablePath:
    def test_the_block_never_uses_a_predictable_download_path(self) -> None:
        block = _download_code()
        assert "/tmp/hp-agent" not in block, (
            "the download block writes a predictable /tmp path again - a local "
            "user can pre-create it as a symlink for root to write through"
        )
        assert "mktemp -d" in block
        assert "trap " in block and 'rm -rf "$TMP_DIR"' in block, (
            "the private temp dir is not cleaned up on exit"
        )

    def test_the_verified_file_is_the_installed_file(self) -> None:
        """No TOCTOU window: verify $BIN, then publish $BIN in one `install`."""
        block = _download_code()
        assert 'sha256sum "$BIN"' in block
        assert 'install -m 0755 "$BIN" "$INSTALL_DIR/hp-agent"' in block
        assert "chmod +x" not in block, (
            "chmod-then-mv is back: that is two operations on a path the checker already released"
        )
