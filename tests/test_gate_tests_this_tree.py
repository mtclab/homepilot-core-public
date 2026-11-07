"""The suite tests the tree it is run from, not another checkout (#653).

The shared `.venv` holds `homepilot` as an editable install whose `.pth` points
at the checkout that installed it. A worktree lane borrowing that venv imported
the SHARED checkout's `src/` and reported "2827 passed" over code that held none
of the lane's changes; it surfaced only because a new test imported a symbol
that did not exist on main.

Teeth: drop `pythonpath = ["src"]` from pyproject's pytest options and run this
file from a worktree whose venv's editable install points elsewhere - the first
test fails, naming the foreign path. Neuter scripts/check_gate_source.py, or
remove it or the PYTHONPATH export from `gate-py`, and the others fail.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import homepilot

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
CHECK = REPO_ROOT / "scripts" / "check_gate_source.py"


def test_homepilot_is_imported_from_this_tree() -> None:
    imported = Path(homepilot.__file__).resolve()
    assert SRC.resolve() in imported.parents, (
        f"the suite is testing {imported}, not this tree's {SRC} - an editable "
        "install from another checkout is shadowing it (#653)"
    )


def _run_check(src: Path, pythonpath: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(pythonpath)}
    return subprocess.run(
        [sys.executable, str(CHECK), str(src)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_gate_source_check_passes_for_this_tree() -> None:
    proc = _run_check(SRC, SRC)
    assert proc.returncode == 0, proc.stderr


def test_the_gate_source_check_fails_loudly_for_another_tree(tmp_path: Path) -> None:
    """The failure the check exists for: the import resolves somewhere other
    than the tree being gated. Simulated by asking it to vouch for an empty
    `src/` while the import still resolves to the real one."""
    other_src = tmp_path / "other-checkout" / "src"
    other_src.mkdir(parents=True)
    proc = _run_check(other_src, SRC)
    assert proc.returncode != 0, "the check vouched for a tree the import does not come from"
    assert "#653" in proc.stderr


def test_gate_py_puts_this_tree_first_and_proves_it() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "gate-py: export PYTHONPATH := $(CURDIR)/src" in makefile
    recipe = makefile.split("\ngate-py:\n", 1)[1].split("\n\n", 1)[0]
    first_step = recipe.strip().splitlines()[0]
    assert "check_gate_source.py" in first_step, (
        "gate-py must prove which source it tests BEFORE running anything else"
    )
