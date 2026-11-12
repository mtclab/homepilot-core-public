"""`make gate` refuses a venv that does not match uv.lock (3.10.0).

The 3.9.0 release gate passed in a venv that had drifted from the lock to newer
fastapi / pydantic / starlette. On the locked versions - the ones the image
installs - the first request after every start blocked the event loop ~300 ms
(tests/test_first_request_does_not_block.py), and no step of the gate could see
it. `gate-lock` (scripts/check_venv_lock.py) now runs first.

These drive the real script through a stand-in `uv` that answers the way
`uv sync --check` does, so every branch is held without building venvs; the
release integration proved the real command on a real drifted venv (fastapi
0.162.2 installed over the lock: "- fastapi==0.162.2 / + fastapi==0.141.1",
exit 1). In the gate itself the script runs against the real venv.

Teeth: drop gate-lock from `gate`, let a dependency difference pass (e.g. count
it as the project's), or drop --locked/--check/--offline, and this fails.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
SCRIPT = REPO_ROOT / "scripts" / "check_venv_lock.py"

_OUTDATED = "The environment is outdated; run `uv sync` to update the environment\n"


def _run(tmp_path: Path, out: str, code: int) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the check with a stand-in uv that prints `out` and exits `code`."""
    record = tmp_path / "uv-called.txt"
    fake = tmp_path / "uv"
    (tmp_path / "uv-out.txt").write_text(out, encoding="utf-8")
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$UV_PROJECT_ENVIRONMENT $*" > "{record}"\n'
        f'cat "{tmp_path / "uv-out.txt"}"\n'
        f"exit {code}\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(venv_bin)],
        env={**os.environ, "UV": str(fake)},
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, record.read_text(encoding="utf-8") if record.exists() else ""


def test_gate_runs_the_lock_check_first() -> None:
    match = re.search(r"^gate:(.*)$", MAKEFILE, re.MULTILINE)
    assert match, "no `gate:` target in the Makefile"
    assert match.group(1).split()[0] == "gate-lock"
    recipe = MAKEFILE.split("\ngate-lock:\n", 1)[1].split("\n\n", 1)[0]
    assert 'scripts/check_venv_lock.py "$(VENV)"' in recipe


def test_a_matching_venv_passes_and_uv_is_asked_the_right_question(tmp_path: Path) -> None:
    proc, called = _run(tmp_path, "Checked 118 packages in 1ms\nWould make no changes\n", 0)
    assert proc.returncode == 0, proc.stderr
    assert "matches uv.lock exactly" in proc.stdout
    # The venv make was handed, checked offline against the lock, changing nothing.
    env_dir, *args = called.split()
    assert env_dir == str(tmp_path / "venv")
    for flag in ("sync", "--locked", "--all-extras", "--check", "--offline"):
        assert flag in args, called


def test_a_drifted_dependency_fails_and_is_named(tmp_path: Path) -> None:
    # What uv printed for the 3.9.0-shaped drift.
    out = (
        "Resolved 122 packages in 1ms\nWould uninstall 1 package\nWould install 1 package\n"
        " - fastapi==0.162.2\n + fastapi==0.141.1\n" + _OUTDATED
    )
    proc, _ = _run(tmp_path, out, 1)
    assert proc.returncode == 1
    assert "- fastapi==0.162.2" in proc.stderr and "+ fastapi==0.141.1" in proc.stderr
    assert "uv sync --frozen --all-extras" in proc.stderr


@pytest.mark.parametrize(
    "lines",
    [
        " - requests==2.33.0\n",  # an extra package the lock does not hold
        " + pydantic==2.13.4\n",  # a locked package missing from the venv
        " - homepilot==3.9.0 (from file:///elsewhere)\n - starlette==1.10.0\n + starlette==1.6.0\n",
    ],
)
def test_any_dependency_difference_fails(tmp_path: Path, lines: str) -> None:
    proc, _ = _run(tmp_path, "Would make changes\n" + lines + _OUTDATED, 1)
    assert proc.returncode == 1, proc.stdout
    assert "does not match uv.lock" in proc.stderr


def test_only_the_project_itself_differing_passes(tmp_path: Path) -> None:
    # A worktree lane borrowing another checkout's venv: its editable install of
    # homepilot points elsewhere. Which source is tested is gate-py's question.
    out = (
        " - homepilot==3.9.0 (from file:///other/checkout)\n"
        " + homepilot==3.10.0 (from file:///this/checkout)\n" + _OUTDATED
    )
    proc, _ = _run(tmp_path, out, 1)
    assert proc.returncode == 0, proc.stderr
    assert "only `homepilot` itself differs" in proc.stdout


def test_a_refusal_without_a_package_fails(tmp_path: Path) -> None:
    # e.g. uv.lock out of date with pyproject.toml under --locked.
    proc, _ = _run(tmp_path, "error: The lockfile at `uv.lock` needs to be updated\n", 2)
    assert proc.returncode == 1
    assert "needs to be updated" in proc.stderr and "uv lock" in proc.stderr
