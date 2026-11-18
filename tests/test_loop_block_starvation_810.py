"""A starved box is reported as starved, a real block still fails (#810).

The loop-block detector times wall clock, so on a box shared with heavy jobs a
callback the OS did not schedule for 0.4 s failed the gate as "event loop
blocked 481 ms" - on a different test each run, on unmodified main too. The
detector now reads the loop thread's run-queue wait (schedstat) over the
callback; when the callback's own time (wall minus waiting-for-a-CPU) fits the
budget it is ``INCONCLUSIVE: starved (...)`` in the session summary, not a
failure. The budget is not raised and nothing is allowlisted.

Proven three ways:

* the decision itself, with injected readings;
* a child pytest session with the REAL reader: ``time.sleep`` on the loop in
  product code still fails (sleeping is not waiting for a CPU);
* the same session shape with a SYNTHETIC reader that says the thread waited
  for a CPU most of the time: reported inconclusive, the test passes - and with
  a reader that says it barely waited, it fails, naming the wait.

Teeth: make ``starved()`` judge on the overrun alone (``waited >= elapsed -
budget`` with no own-time check) and the decision cases fail; drop the starved
branch from ``_timed_run`` and the synthetic-starvation session fails its test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from tests import loop_block_detector as detector

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("elapsed", "budget", "waited", "is_starved"),
    [
        # The #810 report: 481 ms against 250, ~400 ms of it not scheduled.
        (0.481, 0.250, 0.400, True),
        # Exactly the budget of its own: starved.
        (0.500, 0.250, 0.250, True),
        # A real 400 ms block that also waited 100 ms: 380 ms of its own.
        (0.480, 0.250, 0.100, False),
        # Waited most of the OVERRUN but still over budget on its own time.
        (0.900, 0.250, 0.600, False),
        # No reading (no schedstat): no verdict of starvation.
        (0.481, 0.250, None, False),
        # The unsampled budget applies the same rule.
        (0.900, 0.500, 0.450, True),
    ],
)
def test_the_decision(
    elapsed: float, budget: float, waited: float | None, is_starved: bool
) -> None:
    assert detector.starved(elapsed, budget, waited) is is_starved


def test_the_reader_reads_this_thread() -> None:
    wait = detector.runqueue_wait_ns(threading.get_native_id())
    if sys.platform.startswith("linux") and os.path.exists("/proc/self/task"):
        assert isinstance(wait, int) and wait >= 0
    assert detector.runqueue_wait_ns(-1) is None
    assert "PSI" in detector.cpu_pressure()


_CONFTEST = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {tests_dir!r})
    import loop_block_detector

    FRACTION = {fraction!r}
    if FRACTION is not None:
        # A synthetic schedstat: the thread spent FRACTION of all wall time
        # runnable but waiting for a CPU.
        loop_block_detector.runqueue_wait_ns = lambda native_id: int(
            time.monotonic_ns() * FRACTION
        )

    def pytest_configure(config):
        loop_block_detector.install()
        config.pluginmanager.register(loop_block_detector, "hp-loop-block-detector")
    """
)

# Lives outside tests/, so the detector attributes the sleep to product code.
_TESTS = textwrap.dedent(
    """
    import time

    async def test_holds_the_loop():
        time.sleep(0.45)
    """
)


def _child(tmp_path: Path, fraction: float | None) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(
        _CONFTEST.format(tests_dir=str(Path(__file__).parent), fraction=fraction)
    )
    (tmp_path / "test_child.py").write_text(_TESTS)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "-rf",
            str(tmp_path / "test_child.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )


def test_a_real_sleep_on_the_loop_still_fails(tmp_path: Path) -> None:
    proc = _child(tmp_path, fraction=None)
    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    assert "FAILED test_child.py::test_holds_the_loop" in out, out
    assert "event loop blocked" in out, out
    assert "INCONCLUSIVE" not in out, out


def test_a_starved_window_is_reported_inconclusive_not_failed(tmp_path: Path) -> None:
    proc = _child(tmp_path, fraction=0.95)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "1 passed" in out, out
    assert "INCONCLUSIVE: starved (callback" in out, out
    assert "waiting for a CPU" in out and "PSI" in out, out


def test_a_block_that_barely_waited_fails_naming_the_wait(tmp_path: Path) -> None:
    proc = _child(tmp_path, fraction=0.05)
    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    assert "FAILED test_child.py::test_holds_the_loop" in out, out
    assert "waiting for a CPU" in out, out
    assert "INCONCLUSIVE" not in out, out


# ── The `make gate` precheck ─────────────────────────────────────────────────


def _quiet_box():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_quiet_box", REPO_ROOT / "scripts" / "check_quiet_box.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gate_starts_with_the_box_check() -> None:
    import re

    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^gate:(.*)$", makefile, re.MULTILINE)
    assert match and match.group(1).split()[0] == "gate-box"
    recipe = makefile.split("\ngate-box:\n", 1)[1].split("\n\n", 1)[0]
    assert "scripts/check_quiet_box.py" in recipe


@pytest.mark.parametrize(
    ("psi", "load1", "cores", "warns"),
    [
        (0.5, 2.0, 20, False),
        (35.0, 2.0, 20, True),  # CPU pressure alone
        (0.5, 18.0, 20, True),  # load per core alone
        (None, None, 20, False),  # nothing readable: nothing to say
    ],
)
def test_the_box_check_warns_plainly_when_loaded(psi, load1, cores, warns) -> None:
    message = _quiet_box().verdict(psi, load1, cores, psi_warn=20.0, load_warn=0.7)
    assert (message is not None) is warns
    if warns:
        assert message.startswith("gate: WARNING - this box is loaded")
        assert "INCONCLUSIVE" in message


def test_the_box_check_reads_psi_and_never_fails(tmp_path: Path) -> None:
    psi = tmp_path / "cpu"
    psi.write_text(
        "some avg10=42.50 avg60=30.00 avg300=20.00 total=1\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    module = _quiet_box()
    assert module.read_psi_avg10(str(psi)) == 42.5
    assert module.read_psi_avg10(str(tmp_path / "missing")) is None
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_quiet_box.py")],
        env={**os.environ, "HP_GATE_LOAD_WARN": "-1", "HP_GATE_PSI_WARN": "-1"},
        capture_output=True,
        text=True,
    )
    # Over every threshold on purpose: it warns and still exits 0.
    assert proc.returncode == 0
    assert "WARNING - this box is loaded" in proc.stderr
