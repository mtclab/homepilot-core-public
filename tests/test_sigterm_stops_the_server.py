"""SIGTERM stops the server, gracefully - the way `docker stop` asks it to.

The image runs `uvicorn homepilot.main:app` (Dockerfile ENTRYPOINT), and
`docker stop` sends SIGTERM. The lifespan used to install its own SIGTERM
handler with `loop.add_signal_handler`, which REPLACES uvicorn's; it logged
"initiating graceful shutdown" and set an Event that nothing awaited. The
server kept serving until Docker's SIGKILL, so the lifespan's shutdown - stop
the reconcilers, close the hub, checkpoint and close the database - never ran.
Found by `make gate-e2e`, whose server would not stop.

This runs that entry point for real (a child process, an isolated HOME and data
dir, free localhost ports), waits until it serves, sends SIGTERM and holds the
outcome: the process exits cleanly, in bounded time, having run its shutdown.

Teeth: restore the handler in main.lifespan and the process is still alive
when the wait runs out.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Boot is a few seconds; shutdown is bounded by the lifespan's own budgets
# (the hub's stop, the reconcilers'), well inside this.
_BOOT_DEADLINE_S = 60.0
_STOP_DEADLINE_S = 30.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_sigterm_runs_the_graceful_shutdown_and_exits(tmp_path: Path) -> None:
    port, hub_port = _free_port(), _free_port()
    home = tmp_path / "home"
    home.mkdir()
    # The data dir lives under $HOME, not tmp_path: create_app_state refuses an
    # artifacts dir under /tmp, and pytest's tmp_path is under /tmp unless
    # TMPDIR says otherwise - so with tmp_path here the server never booted on
    # a plain checkout or a CI runner (3.10.0 integration; the lane's gate had
    # TMPDIR under /home). Same pattern as tests/hermetic.py documents.
    data = Path(tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-sigterm-"))
    # An EMPTY environment plus what the server needs: no operator config
    # (HP_*, PVE_*, ~/.hp/.env) can reach it, so it cannot touch anything real.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HP_DATA_DIR": str(data),
        "HP_ARTIFACTS_DIR": str(data / "artifacts"),
        "HP_SECRET_KEY": "sigterm-test-secret",
        "HP_VAULT_PASSPHRASE": "sigterm-test-pass",
        "HP_PORT": str(port),
        "HP_AGENT_HUB_HOST": "127.0.0.1",
        "HP_AGENT_HUB_PORT": str(hub_port),
        "HP_UPDATE_CHECK_ENABLED": "0",
    }
    log = tmp_path / "server.log"
    with log.open("wb") as out:
        proc = subprocess.Popen(
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
            cwd=tmp_path,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _BOOT_DEADLINE_S
        while True:
            assert proc.poll() is None, "the server exited during boot:\n" + log.read_text()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            assert time.monotonic() < deadline, "the server never served:\n" + log.read_text()
            time.sleep(0.2)

        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=_STOP_DEADLINE_S)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"SIGTERM did not stop the server within {_STOP_DEADLINE_S:.0f} s "
                "(a handler replaced uvicorn's?):\n" + log.read_text()[-3000:]
            ) from None
        output = log.read_text()
        # uvicorn re-raises the signal it handled once its shutdown is done, so
        # a clean SIGTERM exit reads as -SIGTERM here (143 to Docker); 0 is
        # clean too. Anything else is a crash on the way down.
        assert code in (0, -signal.SIGTERM), f"exit status {code}:\n{output[-3000:]}"
        # The lifespan's shutdown ran AND finished - not merely "the process
        # went away".
        assert "HomePilot v2 shutting down" in output, output[-3000:]
        assert "Application shutdown complete." in output, output[-3000:]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(data, ignore_errors=True)
