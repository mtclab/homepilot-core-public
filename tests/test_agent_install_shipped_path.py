"""The Hosts panel's install one-liner, run against the SHIPPED backend (3.7.0-rc2).

The rc2 dev walk ran the exact command ``POST /agents/bootstrap`` returned, as
root, on a real host. It printed ``curl: (22) ... 401``, exited 0, and left the
old agent in place. Two defects, both invisible to the gates that existed:

* ``main.py`` mounts the agents router behind ``require_token`` (an API bearer
  token), so ``/agents/dist/*`` refused the enrolment credential the installer
  and its binary fetch present (``x-hp-agent-token``: the hub token or a
  bootstrap token) before the route's own ``_enrolment_credential`` ever ran.
  Every earlier test built the router ALONE with ``require_token`` overridden,
  which is exactly the layer that failed. Zero-touch installs fetch the same
  routes the same way, so they were refused too.
* ``curl ... | bash -s`` reports bash's status: curl refused, bash ran nothing,
  and the operator was told it worked.

So this runs ``uvicorn homepilot.main:app`` - the image's entry point - as a
process with auth on, claims it the way the README says, mints a bootstrap
token through the real route, and runs the returned command under bash with
curl. The installer it serves is the SHIPPED ``scripts/install-agent.sh``, cut
after its download block (everything past it creates users and systemd units),
so the real argument parsing, real binary fetch and real sha256 verification run,
and ``install`` lands the binary in ``INSTALL_DIR``.

Teeth (each proven by reverting the fix):
  * mount ``dist_router`` behind ``require_token`` again ->
    ``test_the_panel_one_liner_installs_the_agent`` fails (curl 22, no binary);
  * build the command as ``curl ... | bash -s -- ...`` again ->
    ``test_a_refused_download_fails_the_command`` fails (exit status 0).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from homepilot.agent_hub.dist import INSTALL_FAILED_MESSAGE

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install-agent.sh"
END_OF_DOWNLOAD = "# <<< end download block"
# A real ELF header: the installer checks the download with `file`.
AGENT_BYTES = b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"hp-agent payload for the shipped-path gate"

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _payload(root: Path) -> None:
    """What the Dockerfile bakes into /opt/agent-dist, with the shipped installer
    cut after its download block."""
    root.mkdir()
    text = INSTALLER.read_text()
    assert END_OF_DOWNLOAD in text, "the installer's download block marker moved"
    head = text[: text.index(END_OF_DOWNLOAD) + len(END_OF_DOWNLOAD)]
    (root / "install-agent.sh").write_text(head + "\nexit 0\n")
    for arch in ("amd64", "arm64"):
        (root / f"hp-agent-linux-{arch}").write_bytes(AGENT_BYTES)


@pytest.fixture(scope="module")
def backend() -> Iterator[tuple[str, Path]]:
    """``uvicorn homepilot.main:app`` with a fresh data dir, auth on, hub on."""
    # Under $HOME, not /tmp: the artifacts-dir guard refuses /tmp.
    data = Path(tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix=".hp-test-install-"))
    payload = data / "agent-dist"
    _payload(payload)
    port, hub_port = _free_port(), _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HP_")}
    env.update(
        {
            "HP_DATA_DIR": str(data / "hp"),
            "HP_ARTIFACTS_DIR": str(data / "hp" / "artifacts"),
            "HP_PORT": str(port),
            "HP_AGENT_HUB_HOST": "127.0.0.1",
            "HP_AGENT_HUB_PORT": str(hub_port),
            "HP_AGENT_DIST_DIR": str(payload),
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
    )
    log = (data / "backend.log").open("w")
    proc = subprocess.Popen(  # nosec B603 - the shipped entry point, fixed argv
        [
            sys.executable,
            "-m",
            "uvicorn",
            "homepilot.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(data),
    )
    api = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            if proc.poll() is not None:
                pytest.fail(f"the backend exited: {(data / 'backend.log').read_text()[-3000:]}")
            try:
                if httpx.get(f"{api}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"no /health: {(data / 'backend.log').read_text()[-3000:]}")
            time.sleep(0.25)
        yield api, data
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log.close()
        shutil.rmtree(data, ignore_errors=True)


def _admin_token(api: str, data: Path) -> str:
    """Claim the instance as the README says: the claim code from the data dir."""
    code = (data / "hp" / ".claim_code").read_text().strip()
    resp = httpx.post(f"{api}/claim", json={"code": code}, timeout=10)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


@pytest.fixture(scope="module")
def minted(backend: tuple[str, Path]) -> dict:
    api, data = backend
    admin = _admin_token(api, data)
    resp = httpx.post(
        f"{api}/agents/bootstrap", headers={"Authorization": f"Bearer {admin}"}, timeout=10
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["install_source"] == "control_plane", body
    return body


def _run(command: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    """The command as an operator pastes it, with root's `install` reachable
    unprivileged: a `sudo` that just runs its arguments, and INSTALL_DIR."""
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    sudo = stub / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    proc = subprocess.run(  # nosec B603 B607 - a test runs the command under test
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": f"{stub}:/usr/bin:/bin",
            "INSTALL_DIR": str(install_dir),
            "TMPDIR": str(tmp_path),
        },
    )
    return proc, install_dir / "hp-agent"


def test_the_panel_one_liner_installs_the_agent(minted: dict, tmp_path: Path):
    proc, landed = _run(minted["install_command"], tmp_path)

    assert proc.returncode == 0, (
        f"the one-liner the panel shows failed against the shipped backend:\n"
        f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
    )
    assert landed.is_file(), f"no agent binary landed: {proc.stdout[-1500:]}"
    assert landed.read_bytes() == AGENT_BYTES, "the binary is not the one this backend serves"
    assert "Verified sha256" in proc.stdout


def test_a_refused_download_fails_the_command(minted: dict, tmp_path: Path):
    """A wrong token: the control plane refuses the installer. The command must
    end non-zero, say so, and install nothing."""
    token = minted["bootstrap_token"]
    wrong = "hpbat_" + "0" * 40
    command = minted["install_command"].replace(token, wrong)
    assert wrong in command

    proc, landed = _run(command, tmp_path)

    assert proc.returncode != 0, "a refused download reported success"
    assert "401" in proc.stderr, proc.stderr[-1500:]
    assert INSTALL_FAILED_MESSAGE in proc.stderr
    assert not landed.exists()
