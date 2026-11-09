"""`make gate` runs tests/test_e2e.py against the real built artifact.

3.9.0 shipped with two e2e failures that only the public mirror's CI found,
after release: tests/test_e2e.py skips itself without a live server and token,
so gate-py "ran" it as a skip. `gate-e2e` (scripts/gate-e2e.sh) runs the
mirror workflow's steps locally. These hold the wiring, so the gap cannot
quietly reopen: drop gate-e2e from `gate`, or let the script pass on a run
that skipped its tests, and this fails.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
SCRIPT = (REPO_ROOT / "scripts" / "gate-e2e.sh").read_text(encoding="utf-8")


def test_gate_depends_on_gate_e2e() -> None:
    match = re.search(r"^gate:(.*)$", MAKEFILE, re.MULTILINE)
    assert match, "no `gate:` target in the Makefile"
    assert "gate-e2e" in match.group(1).split()


def test_gate_e2e_runs_the_script_with_the_venv() -> None:
    recipe = MAKEFILE.split("\ngate-e2e:\n", 1)[1].split("\n\n", 1)[0]
    assert 'bash scripts/gate-e2e.sh "$(VENV)"' in recipe


def test_the_script_runs_the_e2e_file_on_the_built_artifact() -> None:
    # The shipped path: the UI built from the lockfile, the image's entry point.
    assert "npm ci" in SCRIPT and "npm run build" in SCRIPT
    assert '"$VENV_BIN/uvicorn" homepilot.main:app' in SCRIPT
    assert "-m pytest tests/test_e2e.py" in SCRIPT
    # Isolated from operator config: empty environment, throwaway HOME.
    assert "env -i" in SCRIPT and 'HOME="$WORK/home"' in SCRIPT
    # The data dir must be one create_app_state accepts whatever TMPDIR is:
    # under /tmp (mktemp -t's default) the server refuses to boot.
    assert 'WORK="$(mktemp -d "$HOME/' in SCRIPT
    assert "mktemp -d -t" not in SCRIPT


def test_the_script_fails_a_run_that_skipped() -> None:
    assert "--junitxml=" in SCRIPT
    assert 'c.find("skipped") is not None' in SCRIPT
    assert 'fail "tests were skipped' in SCRIPT


def test_the_script_holds_the_sigterm_stop() -> None:
    assert "kill -TERM" in SCRIPT
    assert 'stop_server || fail "the server ignored SIGTERM' in SCRIPT
