"""The CLI must EXIT. Every command, on every path it can refuse or fail on.

THE DEFECT (#623, reproduced live on dev at 3.6.14). `hp token create` against a
running backend printed its refusal and then hung forever, creating no token; the
caller who gave up left an orphaned python process inside the container that only
a SIGKILL from inside it cleared. `docs/deployment.md` calls that command the way
to mint the first API token, and #623 filed the same symptom for
`hp invite create` on prod.

The cause was not the sqlite writer lock #623 suspected. It was this: the command
opened an aiosqlite connection, THEN discovered a backend held the data directory
and raised `typer.Exit` - leaving the connection open. aiosqlite runs the real
sqlite3 handle on a NON-DAEMON worker thread parked on a queue nobody will feed
again, and CPython joins every non-daemon thread at interpreter exit. Seven
commands did this (`token create`, `init`, `invite *`, `inventory list|show|
refresh`, `webhook *`); `webhook test <unknown id>` did it with no backend
running at all, on a plain not-found.

WHY THIS TEST IS A SUBPROCESS. Every other `hp` test drives the app in-process
with typer's `CliRunner`, which returns a result object and never asks the
interpreter to shut down - so a defect whose ENTIRE symptom is "the process does
not exit" is invisible to it by construction. The suite was green throughout.
Only a real process, given a deadline, can assert this.

TEETH (verified by reverting): move the `_refuse_if_server_running` call in
`_open_cli_db` back to after `Database.connect()`, or drop a `finally:
await db.close()`, and the matching case here fails with a TimeoutExpired
instead of an exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from homepilot.instance_lock import InstanceLock

# Generous next to the sub-second these commands need, short enough that a hang
# is a test failure rather than a stalled `make gate`.
DEADLINE_SECONDS = 45

# The commands that open the control-plane database and must refuse - by name,
# because the point is that the OPERATOR's command exits, not that a helper does.
# Each is run against a data directory whose instance lock is held, which is what
# a live install looks like to the CLI.
REFUSED_WHILE_BACKEND_RUNS = [
    pytest.param(["token", "create"], id="token-create"),
    pytest.param(["init", "--non-interactive"], id="init"),
    pytest.param(
        ["invite", "create", "--cn", "someone", "--template", "9000", "--node", "pve1"],
        id="invite-create",
    ),
    pytest.param(["invite", "list"], id="invite-list"),
    pytest.param(["inventory", "list"], id="inventory-list"),
    pytest.param(["inventory", "show", "somehost"], id="inventory-show"),
    pytest.param(["webhook", "list"], id="webhook-list"),
]


def _run(args: list[str], data_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the real `hp` entry point as its own process, with a deadline.

    `-c` rather than the installed console script so this works from a checkout
    whose venv was installed elsewhere, and so the child imports the SAME source
    tree this test does.
    """
    env = dict(os.environ)
    env["HP_DATA_DIR"] = str(data_dir)
    env["HP_ARTIFACTS_DIR"] = str(data_dir / "artifacts")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    # No credential on this box: the API path must not be the thing that saves
    # these commands, because the defect is on the fallback.
    env.pop("HP_ADMIN_TOKEN", None)
    env.pop("HP_ADMIN_SECRET", None)
    return subprocess.run(
        [sys.executable, "-c", "from homepilot.cli.main import app; app()", *args],
        capture_output=True,
        text=True,
        timeout=DEADLINE_SECONDS,
        env=env,
        cwd=str(data_dir),
    )


@pytest.fixture
def held_data_dir(tmp_path: Path):
    """A data directory a 'backend' holds, exactly as the real one does."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # A live install HAS a database. `hp inventory list|show` return early when
    # the file is absent, so without this they would never reach the code under
    # test and the case would pass vacuously.
    (data_dir / "homepilot.db").touch()
    lock = InstanceLock(data_dir)
    lock.acquire()
    try:
        yield data_dir
    finally:
        lock.release()


@pytest.mark.parametrize("args", REFUSED_WHILE_BACKEND_RUNS)
def test_refusing_a_live_install_still_exits(args: list[str], held_data_dir: Path) -> None:
    try:
        result = _run(args, held_data_dir)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"`hp {' '.join(args)}` did not exit within {DEADLINE_SECONDS}s. That is the "
            "#623 hang: the command holds an open aiosqlite connection whose non-daemon "
            f"worker thread CPython will never be able to join.\nstdout so far: {exc.stdout!r}"
        )

    assert result.returncode != 0, (
        f"`hp {' '.join(args)}` succeeded against a data directory a backend holds; "
        "it must refuse (#431)."
    )
    combined = result.stdout + result.stderr
    assert "backend is running" in combined.lower(), (
        f"`hp {' '.join(args)}` refused without saying a backend holds the data "
        f"directory. Output was:\n{combined}"
    )


def test_the_refusal_names_the_command_the_operator_ran(held_data_dir: Path) -> None:
    """The refusal used to name a DIFFERENT command: `_open_invite_repo` passed
    "hp token revoke" and the webhook helper passed "hp vault delete", so
    `hp invite create` refused with a message about revoking a token."""
    result = _run(["invite", "list"], held_data_dir)
    combined = result.stdout + result.stderr

    assert "hp invite" in combined, f"the refusal does not name `hp invite`:\n{combined}"
    assert "hp token revoke" not in combined, (
        f"`hp invite list` refused in the name of `hp token revoke`:\n{combined}"
    )


def test_webhook_test_exits_when_the_webhook_does_not_exist(tmp_path: Path) -> None:
    """No backend, no lock, nothing exotic - just a not-found. This path raised
    `typer.Exit` between opening the database and closing it, so the plainest
    error the command has hung the process."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    try:
        result = _run(["webhook", "test", "424242"], data_dir)
    except subprocess.TimeoutExpired:
        pytest.fail(
            "`hp webhook test <unknown id>` did not exit: the not-found path left the "
            "database open (#623's class, with no backend involved at all)."
        )
    assert result.returncode != 0
    assert "not found" in (result.stdout + result.stderr).lower()


def test_a_command_that_succeeds_also_exits(tmp_path: Path) -> None:
    """Guard the guard. If `_run` could not start `hp` at all, every test above
    would 'pass' on a non-zero exit that never touched the database."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = _run(["--help"], data_dir)
    assert result.returncode == 0, f"`hp --help` failed, so this file proves nothing:\n{result}"
    assert "HomePilot" in result.stdout
