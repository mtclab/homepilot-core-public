"""No test reads the developer's client profile or leaves this machine (#840 review).

Found running the 3.13 gate on a workstation: CLI tests run under
``patch.dict(os.environ, ..., clear=True)``, which also drops the conftest's
``XDG_CONFIG_HOME``. ``hp`` then fell back to ``Path.home()`` - with HOME
cleared, the account's real home from the password database - read the real
``~/.config/homepilot/client.toml`` and ran in REMOTE mode against production.
Thirty-six tests stopped at a local-only refusal (exit 2) before any request;
nothing guaranteed the next one would.

Two fixes, proven here against a real-looking profile (``default_profile``
pointing at 192.0.2.1, RFC 5737) in a stand-in home that ``Path.home()`` returns:

* the conftest patches ``remote.config_dir`` / ``remote.cache_dir`` themselves,
  which no environment change can undo - the profile is never read;
* tests/network_guard.py fails any test that connects off this machine.

Teeth: drop the ``monkeypatch.setattr(remote, ...)`` lines from the conftest and
the probe and the 36-test session fail (remote mode, 192.0.2.1, the guard
naming it); drop ``network_guard.install()`` and the guard session passes a test
that left the machine.
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import network_guard

REPO_ROOT = Path(__file__).resolve().parents[1]
OFF_BOX_URL = "http://192.0.2.1:8000"

# The tests that read the real profile on the box that found this (36 of them).
_LEAKY_MODULES = [
    "tests/test_cli_init.py",
    "tests/test_cli_db_restore.py",
    "tests/test_cli_backup_restore.py",
    "tests/test_cli_backup_compose_shape.py",
    "tests/test_export_hub_identity_722.py",
]


def _real_looking_home(home: Path) -> Path:
    """A home holding a client profile the way `hp login` writes one."""
    config = home / ".config" / "homepilot"
    (config / "tokens").mkdir(parents=True)
    token = config / "tokens" / "prod.token"
    token.write_text("hp_" + "0" * 13 + "\n")
    token.chmod(0o600)
    config.chmod(0o700)
    (config / "tokens").chmod(0o700)
    (config / "client.toml").write_text(
        'default_profile = "prod"\n\n[profiles.prod]\n'
        f'url = "{OFF_BOX_URL}"\ntoken_file = "{token}"\nallow_http = true\n'
    )
    return home


@pytest.mark.parametrize(
    ("family", "address", "leaves"),
    [
        (socket.AF_INET, ("127.0.0.1", 80), False),
        (socket.AF_INET, ("127.8.9.10", 80), False),
        (socket.AF_INET, ("0.0.0.0", 80), False),
        (socket.AF_INET6, ("::1", 80, 0, 0), False),
        (socket.AF_INET6, ("::ffff:127.0.0.1", 80, 0, 0), False),
        (socket.AF_INET, ("localhost", 80), False),
        (socket.AF_INET, ("192.0.2.1", 8000), True),
        (socket.AF_INET, ("10.0.0.5", 443), True),
        (socket.AF_INET6, ("2001:db8::1", 443, 0, 0), True),
        (socket.AF_INET, ("homepilot.example.com", 443), True),
        (socket.AF_UNIX, "/run/some.sock", False),
    ],
)
def test_what_counts_as_leaving_the_machine(family, address, leaves: bool) -> None:
    assert (network_guard.off_box(family, address) is not None) is leaves


def test_a_cleared_environment_cannot_reach_the_real_client_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process: the stand-in home holds a profile, the environment is cleared
    the way the leaky tests clear it, and the CLI still runs locally."""
    from typer.testing import CliRunner

    from homepilot.cli import remote
    from homepilot.cli.main import app

    home = _real_looking_home(tmp_path / "home")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    with patch.dict(os.environ, {"HP_DATA_DIR": str(tmp_path / "data")}, clear=True):
        # A remote-capable command first: in remote mode it calls 192.0.2.1,
        # and the network guard fails this test naming the address.
        result = CliRunner().invoke(app, ["hosts", "list"])
        assert not remote.config_path().is_relative_to(home), remote.config_path()
        assert remote.resolve_target(None, None) is None
    assert "192.0.2.1" not in result.output, result.output


def _child(args: list[str], cwd: Path, home: Path | None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    for name in ("HP_GATE", "HP_URL", "HP_PROFILE", "HP_TOKEN"):
        env.pop(name, None)
    plugin_args: list[str] = []
    if home is not None:
        # The stand-in for "the developer's home": every Path.home() in the
        # child session - including the password-database fallback a cleared
        # environment hits - answers this directory.
        plugin_dir = cwd / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "fakehome.py").write_text(
            "import pathlib\n"
            f"pathlib.Path.home = classmethod(lambda cls: pathlib.Path({str(home)!r}))\n"
        )
        env["PYTHONPATH"] += os.pathsep + str(plugin_dir)
        plugin_args = ["-p", "fakehome"]
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *plugin_args, *args],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=cwd,
        env=env,
    )


def test_the_tests_that_leaked_run_locally_beside_a_real_looking_profile(tmp_path: Path) -> None:
    home = _real_looking_home(tmp_path / "home")
    proc = _child(
        ["--rootdir", str(REPO_ROOT), *[str(REPO_ROOT / m) for m in _LEAKY_MODULES]],
        tmp_path,
        home,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-6000:]
    assert "36 passed" in out, out[-3000:]
    assert "192.0.2.1" not in out and "Network guard" not in out, out[-3000:]


_GUARD_CONFTEST = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo!r})
    from tests import network_guard

    def pytest_configure(config):
        network_guard.install()
        config.pluginmanager.register(network_guard, "hp-network-guard")
    """
)

_GUARD_TESTS = textwrap.dedent(
    """
    import socket
    import httpx

    def test_an_http_client_that_swallows_the_error():
        try:
            httpx.get("http://192.0.2.1:8000/health", timeout=5)
        except httpx.HTTPError:
            pass  # a client carrying on regardless: the guard still fails it

    def test_a_raw_socket():
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=5)
        except OSError:
            pass

    def test_loopback_is_fine():
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen()
            socket.create_connection(srv.getsockname(), timeout=5).close()

    def test_a_route_probe_sends_nothing_and_is_fine():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
    """
)


def test_the_guard_fails_a_test_that_leaves_the_machine(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(_GUARD_CONFTEST.format(repo=str(REPO_ROOT)))
    (tmp_path / "test_child.py").write_text(_GUARD_TESTS)
    started = time.monotonic()
    proc = _child(["-rf", str(tmp_path / "test_child.py")], tmp_path, None)
    took = time.monotonic() - started
    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    assert "FAILED test_child.py::test_an_http_client_that_swallows_the_error" in out, out
    assert "FAILED test_child.py::test_a_raw_socket" in out, out
    assert "2 failed, 2 passed" in out, out
    assert "('192.0.2.1', 8000)" in out and "('192.0.2.1', 443)" in out, out
    # Refused at once, never left to a connect timeout.
    assert took < 60, took
