"""The loop-block detector times HomePilot's loops, not the e2e test driver's.

3.9.0's public e2e run failed a fixture with "event loop blocked 62027 ms ...
by <Handle wait_for_future.<locals>.resume ...> at playwright/_impl/_greenlets.py".
That loop was Playwright's: its SYNC API pumps a private asyncio loop on the
test's thread, and the test's own code runs inside one of that loop's callbacks
between two Playwright calls - so a fixture's `time.sleep` was timed as a loop
block. tests/loop_block_detector.py now tags the loop of every sync-mode
Playwright connection and does not time it.

Both halves are proven in one child pytest, shaped like an e2e run (one sync
driver for the session, started on the main thread, as pytest-playwright does):

* a test whose fixture and body block INSIDE the driver loop passes;
* in the same session, a callback that holds an ordinary asyncio loop still
  fails its test - and its report names that block alone, not the driver's.

Teeth: revert the `_DRIVER_LOOPS` check in `_timed_run` and both fail - the
first test errors in setup exactly as 3.9.0 did, and the second report carries
the driver's "block" beside the real one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

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

_CHILD_TESTS = textwrap.dedent(
    """
    import asyncio, threading, time
    import pytest
    from playwright.sync_api import sync_playwright

    BLOCK_S = {block_s!r}

    @pytest.fixture(scope="session")
    def driver():
        # What pytest-playwright's session `playwright` fixture does.
        pw = sync_playwright().start()
        yield pw
        pw.stop()

    def roundtrip(pw):
        # A real request to the driver: it switches to the dispatcher, which
        # ends the loop callback the test's code was running inside.
        pw.request.new_context().dispose()

    @pytest.fixture
    def waits_in_setup(driver):
        # tests/test_e2e.py's session_auth waiting out a 429, in miniature.
        time.sleep(BLOCK_S)
        roundtrip(driver)

    def test_driver_loop(driver, waits_in_setup):
        time.sleep(BLOCK_S)
        roundtrip(driver)

    def test_ordinary_loop(driver):
        time.sleep(BLOCK_S)
        roundtrip(driver)

        def hold_an_ordinary_loop():
            loop = asyncio.new_event_loop()
            try:
                loop.call_soon(time.sleep, BLOCK_S)
                loop.run_until_complete(asyncio.sleep(0))
            finally:
                loop.close()

        worker = threading.Thread(target=hold_an_ordinary_loop, name="ordinary-loop")
        worker.start()
        worker.join()
    """
)


@pytest.fixture(scope="module")
def child_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str | None]:
    """Each child test's failure/error text by name (None when it passed)."""
    tmp_path = tmp_path_factory.mktemp("driver-loop")
    (tmp_path / "conftest.py").write_text(
        _CHILD_CONFTEST.format(tests_dir=str(Path(__file__).parent))
    )
    (tmp_path / "test_child.py").write_text(_CHILD_TESTS.format(block_s=_BLOCK_S))
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
            str(tmp_path / "test_child.py"),
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
        results[case.attrib["name"]] = (
            # The element text is the full report; its `message` repeats it.
            "\n".join(el.text or el.attrib.get("message", "") for el in problems)
            if problems
            else None
        )
    assert set(results) == {"test_driver_loop", "test_ordinary_loop"}, proc.stdout + proc.stderr
    return results


def test_a_block_inside_playwrights_sync_loop_is_not_recorded(
    child_run: dict[str, str | None],
) -> None:
    assert child_run["test_driver_loop"] is None, child_run["test_driver_loop"]


def test_a_real_block_on_an_ordinary_loop_in_the_same_session_still_is(
    child_run: dict[str, str | None],
) -> None:
    report = child_run["test_ordinary_loop"]
    assert report is not None, "an ordinary loop held for 0.6 s was not reported"
    # Exactly the ordinary loop's block - the driver's time in the same test
    # is not on the bill.
    assert report.count("event loop blocked ") == 1, report
    assert "in thread 'ordinary-loop'" in report, report
