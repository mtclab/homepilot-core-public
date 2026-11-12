"""The loop-block detector does not time the e2e module's process.

tests/test_e2e.py drives HomePilot in ANOTHER process (HTTP and a browser), so
no asyncio loop in the test process is HomePilot's. Timing those loops failed
two releases: 3.9.0 (a fixture's sleep inside Playwright's private loop,
"blocked 62027 ms") and 3.10.0 (public CI installed a newer Playwright that the
exemption, keyed on Playwright internals, no longer recognised).

The rule is now the module: while a test of test_e2e.py runs, nothing is timed.
Proven in one child pytest with no Playwright in it at all:

* a test in a module named test_e2e.py holds a loop for 0.6 s and passes;
* the same body in any other module, in the same session, fails - before and
  after the e2e module ran, so the exemption does not leak past its tests.

Teeth: drop the `_in_out_of_process_item` check in `_timed_run` and the e2e
module's test fails; make the flag stick once set (in `pytest_runtest_logstart`)
and the "after" module's block goes unreported, which fails its test here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Comfortably past the detector's 250 ms budget, and sampled (time.sleep
# releases the GIL), so the budget - not the unsampled one - applies.
_BLOCK_S = 0.6

_CHILD_CONFTEST = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {tests_dir!r})
    import loop_block_detector

    def pytest_configure(config):
        loop_block_detector.install()
        config.pluginmanager.register(loop_block_detector, "hp-loop-block-detector")
    """
)

_CHILD_BODY = textwrap.dedent(
    """
    import asyncio, time

    def test_holds_a_loop():
        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(time.sleep, {block_s!r})
            loop.run_until_complete(asyncio.sleep(0))
        finally:
            loop.close()
    """
)

# pytest runs the files in the order given: other module, e2e module, other.
_CHILD_FILES = ("test_a_before.py", "test_e2e.py", "test_z_after.py")


@pytest.fixture(scope="module")
def child_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str | None]:
    """Each child module's failure/error text (None when its test passed)."""
    tmp_path = tmp_path_factory.mktemp("e2e-module")
    (tmp_path / "conftest.py").write_text(
        _CHILD_CONFTEST.format(tests_dir=str(Path(__file__).parent))
    )
    for name in _CHILD_FILES:
        (tmp_path / name).write_text(_CHILD_BODY.format(block_s=_BLOCK_S))
    junit = tmp_path / "junit.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *(str(tmp_path / name) for name in _CHILD_FILES),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert junit.exists(), proc.stdout + proc.stderr
    results: dict[str, str | None] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        problems = [el for el in case if el.tag in ("failure", "error")]
        results[case.attrib["classname"].rsplit(".", 1)[-1] + ".py"] = (
            "\n".join(el.text or el.attrib.get("message", "") for el in problems)
            if problems
            else None
        )
    assert set(results) == set(_CHILD_FILES), proc.stdout + proc.stderr
    return results


def test_a_loop_held_in_the_e2e_module_is_not_recorded(
    child_run: dict[str, str | None],
) -> None:
    assert child_run["test_e2e.py"] is None, child_run["test_e2e.py"]


@pytest.mark.parametrize("module", ["test_a_before.py", "test_z_after.py"])
def test_the_same_block_in_any_other_module_still_fails(
    child_run: dict[str, str | None], module: str
) -> None:
    report = child_run[module]
    assert report is not None, f"a loop held for {_BLOCK_S} s in {module} was not reported"
    assert report.count("event loop blocked ") == 1, report
