"""The real `hp` entry point against the real app, for the CLI's journey tests.

``fleet_server()`` starts ``tests/e2e_fleet_server.py`` - ``homepilot.main:app``
with its lifespan, task runner and every route, over the suite's fake Proxmox
(placeholder fleet, 192.0.2.0/24; gone machines old-web/old-db/old-cache; a
friend's machine alice-web) - in its own process on a free loopback port, and
mints an admin token over HTTP with the admin secret. Nothing leaves the box.

``run_hp()`` runs the INSTALLED console script (``<venv>/bin/hp``, the
``[project.scripts]`` entry point) as a subprocess with an isolated HOME and XDG
directories and this tree's ``src`` first on the path, the way an operator's
shell runs it. Assertions are made on what that process prints and exits with.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HP = Path(sys.executable).with_name("hp")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Fleet:
    base: str
    token: str
    admin_secret: str
    data: Path

    def log(self) -> str:
        path = self.data / "server.log"
        return path.read_text(encoding="utf-8", errors="replace")[-4000:]


@contextmanager
def fleet_server() -> Iterator[Fleet]:
    port = free_port()
    # Under $HOME: create_app_state refuses an artifacts dir in /tmp.
    data = Path(tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix=".hp-cli-fleet-"))
    home = data / "home"
    home.mkdir()
    admin_secret = secrets.token_hex(16)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(REPO / "src"),
        "HP_SECRET_KEY": "cli-fleet-secret",
        "HP_VAULT_PASSPHRASE": "cli-fleet-pass",
        "HP_ADMIN_SECRET": admin_secret,
        "PROXMOX_SECRETS": "/nonexistent",
    }
    log = (data / "server.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "e2e_fleet_server.py"), str(data / "hp"), str(port)],
        cwd=data,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    fleet = Fleet(f"http://127.0.0.1:{port}", "", admin_secret, data)
    try:
        deadline = time.monotonic() + 120
        while "E2E_FLEET_READY" not in fleet.log():
            if proc.poll() is not None:
                raise RuntimeError(f"the fleet server exited:\n{fleet.log()}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"the fleet server never got ready:\n{fleet.log()}")
            time.sleep(0.2)
        req = urllib.request.Request(
            f"{fleet.base}/auth/tokens",
            data=json.dumps({"label": "cli-journey", "scope": "*"}).encode(),
            headers={"Content-Type": "application/json", "x-hp-admin-secret": admin_secret},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            fleet.token = str(json.loads(resp.read())["token"])
        yield fleet
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        import shutil

        shutil.rmtree(data, ignore_errors=True)


def client_env(home: Path, **extra: str) -> dict[str, str]:
    """An operator workstation: its own HOME/XDG dirs, no server data dir."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HP_DATA_DIR": str(home / "no-server-here"),
        "LANG": "C.UTF-8",
        "COLUMNS": "200",
        "PYTHONPATH": str(REPO / "src"),
        "PROXMOX_SECRETS": "/nonexistent",
    }
    env.update(extra)
    return env


def run_hp(
    *args: str,
    home: Path,
    stdin: str | None = None,
    timeout: float = 120,
    **env: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HP), *args],
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=client_env(home, **env),
        cwd=str(home),
    )
