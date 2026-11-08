"""`make gate-mirror` works in both trees it ships in (3.7.0 follow-up).

`make gate` includes `gate-mirror`, which runs scripts/mirror_tree_check.py -
and scripts/scrub-for-public.sh deletes that script from every export. So in the
public mirror `make gate` failed on a missing file. In the mirror the target
must say it has nothing to do (the tree already IS the mirror tree) and pass;
in the private tree it must still run the script and fail when the script does.

These drive the REAL Makefile through `make`, with a stand-in interpreter
(``VENV``) that records how it was called - so the guard's two branches are
tested as make runs them, not by reading the recipe. And when this suite runs
in the mirror's tree (``make gate-mirror`` builds it), the last test runs the
real target in that tree.

Teeth: remove the PROVENANCE/script guard from the Makefile and the mirror-shape
test fails (make exits non-zero on the missing script); make the guard
unconditional and the private-shape tests fail (the script never runs).
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.tree import IS_PUBLIC_MIRROR, REPO_ROOT

MAKEFILE = REPO_ROOT / "Makefile"
SKIP_TEXT = "gate-mirror: skipped - this is the public mirror's tree"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def _fake_venv(root: Path, exit_code: int) -> tuple[Path, Path]:
    """A VENV dir whose `python` records its arguments and exits `exit_code`."""
    bindir = root / "fakevenv"
    bindir.mkdir()
    record = root / "python-was-called"
    python = bindir / "python"
    python.write_text(
        f'#!/bin/sh\necho "$@" > "{record}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, record


def _tree(root: Path, *, provenance: bool, script: bool) -> Path:
    tree = root / "tree"
    tree.mkdir()
    shutil.copy(MAKEFILE, tree / "Makefile")
    if provenance:
        (tree / "PROVENANCE").write_text("source_commit=test\n", encoding="utf-8")
    if script:
        (tree / "scripts").mkdir()
        (tree / "scripts" / "mirror_tree_check.py").write_text("# stand-in\n", encoding="utf-8")
    return tree


def _make(tree: Path, venv: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", "gate-mirror", f"VENV={venv}", "GO_BIN=true"],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_in_a_mirror_tree_gate_mirror_passes_and_says_why(tmp_path: Path) -> None:
    venv, record = _fake_venv(tmp_path, exit_code=7)
    result = _make(_tree(tmp_path, provenance=True, script=False), venv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert SKIP_TEXT in result.stdout
    assert "PROVENANCE present" in result.stdout
    assert not record.exists(), "the mirror branch must not run the (absent) script"


def test_in_the_private_tree_it_runs_the_script(tmp_path: Path) -> None:
    venv, record = _fake_venv(tmp_path, exit_code=0)
    result = _make(_tree(tmp_path, provenance=False, script=True), venv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert SKIP_TEXT not in result.stdout
    assert record.read_text(encoding="utf-8").strip() == "scripts/mirror_tree_check.py"


def test_in_the_private_tree_a_failing_script_fails_the_gate(tmp_path: Path) -> None:
    venv, record = _fake_venv(tmp_path, exit_code=3)
    result = _make(_tree(tmp_path, provenance=False, script=True), venv)
    assert result.returncode != 0
    assert record.exists()
    assert "gate: all sub-gates passed" not in result.stdout


def test_the_guard_matches_the_tree_this_suite_runs_in() -> None:
    """In the mirror (gate-mirror's run, and the mirror's own CI): the real target
    in the real tree passes. In the private tree: the scrub really does delete
    the script, which is what makes the mirror branch the one that runs there."""
    if IS_PUBLIC_MIRROR:
        assert not (REPO_ROOT / "scripts" / "mirror_tree_check.py").exists()
        result = subprocess.run(
            ["make", "--no-print-directory", "gate-mirror", "GO_BIN=true"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert SKIP_TEXT in result.stdout
    else:
        scrub = (REPO_ROOT / "scripts" / "scrub-for-public.sh").read_text(encoding="utf-8")
        assert re.search(r"^\s*rm -f scripts/mirror_tree_check\.py\s*$", scrub, re.M)
