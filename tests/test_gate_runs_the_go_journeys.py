"""`make gate` cannot pass by skipping the real-binary Go journeys (#726).

Measured before the fix: on a box whose Go toolchain lived outside PATH, `make
gate GO_BIN=...` built and vetted the agent in gate-go, while gate-py never saw
that toolchain - every journey that drives the shipped hp-agent binary SKIPPED
and the gate printed a green "3072 passed, 49 skipped".

Teeth:
  * drop `pytest_sessionstart`'s call to `check_gate_go` from tests/conftest.py,
    and `test_a_gate_run_without_go_refuses_to_start` fails (the run starts);
  * let a test module resolve Go by itself again (`shutil.which("go")`), and
    `test_no_module_resolves_go_on_its_own` fails;
  * stop exporting HP_GATE / HP_GO_BIN from gate-py, or reintroduce a fixed
    /tmp path in the Makefile, and the Makefile tests fail.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .go_toolchain import (
    DEPENDENT_IMPORT,
    GoToolchainRequiredError,
    check_gate_go,
    go_dependent_modules,
)

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


class TestTheDecision:
    def test_a_gate_run_without_go_is_refused_and_names_what_would_skip(self) -> None:
        with pytest.raises(GoToolchainRequiredError) as excinfo:
            check_gate_go({"HP_GATE": "1"}, "")
        message = str(excinfo.value)
        for module in ("test_agent_hub_tls_journey.py", "test_agent_operations_journey.py"):
            assert module in message, f"the refusal does not name {module}"

    def test_the_opt_out_is_explicit_and_still_says_what_was_skipped(self) -> None:
        warning = check_gate_go({"HP_GATE": "1", "HP_GATE_ALLOW_NO_GO": "1"}, "")
        assert "SKIPPED" in warning
        assert "test_metrics_journey.py" in warning

    def test_a_gate_run_with_a_bogus_go_path_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(GoToolchainRequiredError):
            check_gate_go({"HP_GATE": "1"}, str(tmp_path / "no-such-go"))

    def test_outside_the_gate_a_missing_toolchain_is_an_ordinary_skip(self) -> None:
        assert check_gate_go({}, "") == ""

    def test_the_dependent_list_finds_the_real_binary_journeys(self) -> None:
        found = set(go_dependent_modules())
        expected = {
            "test_agent_hub_tls_journey.py",
            "test_agent_operate_surface.py",
            "test_agent_operations_journey.py",
            "test_enrolment_window.py",
            "test_metrics_journey.py",
            "test_tls_fleet_migration_journey.py",
        }
        assert expected <= found, f"missing: {sorted(expected - found)}"


def test_no_module_resolves_go_on_its_own() -> None:
    """A module that looks Go up by itself bypasses the gate's refusal."""
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name in {"go_toolchain.py", Path(__file__).name}:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"""which\(\s*["']go["']\s*\)|environ(?:\.get\(|\[)\s*["']HP_GO_BIN""", text):
            offenders.append(path.name)
    assert not offenders, (
        f"resolve Go through tests/go_toolchain.py ({DEPENDENT_IMPORT}), not locally: {offenders}"
    )


def test_a_gate_run_without_go_refuses_to_start(tmp_path: Path) -> None:
    """The wiring, end to end: a real pytest run under HP_GATE=1 with no Go on
    PATH and no HP_GO_BIN must refuse before running anything."""
    env = {k: v for k, v in os.environ.items() if k not in {"HP_GO_BIN", "HP_GATE_ALLOW_NO_GO"}}
    env["HP_GATE"] = "1"
    # Only the interpreter's own directory: no `go` can be found on it.
    env["PATH"] = str(Path(sys.executable).parent)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{TESTS_DIR / 'test_gate_tests_this_tree.py'}::test_homepilot_is_imported_from_this_tree",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"a gate run with no Go toolchain started anyway:\n{out}"
    assert "no Go toolchain" in out, out
    assert "passed" not in out, out


class TestTheMakefile:
    def test_gate_py_hands_pytest_the_toolchain_and_the_gate_flag(self) -> None:
        assert "gate-py: export HP_GATE := 1" in MAKEFILE
        assert "gate-py: export HP_GO_BIN := $(HP_GO_BIN)" in MAKEFILE
        assert re.search(r"^HP_GO_BIN := \$\(shell command -v \$\(GO_BIN\)", MAKEFILE, re.M)

    def test_no_fixed_tmp_paths(self) -> None:
        """A fixed /tmp name is shared by every concurrent run and checkout."""
        recipe_lines = [
            line
            for line in MAKEFILE.splitlines()
            if not line.lstrip().startswith("#") and "/tmp/" in line
        ]
        assert not recipe_lines, recipe_lines

    def test_the_local_gate_runs_the_dependency_audit(self) -> None:
        security = MAKEFILE.split("\ngate-security:\n", 1)[1].split("\n\n", 1)[0]
        assert "$(VENV)/pip-audit" in security
