"""Fail any test that blocks an event loop (architecture review F1 / R1).

Two detectors, both feeding one per-phase verdict:

1. **Time budget.** ``asyncio.events.Handle._run`` - the single place every loop
   in this process (pytest-asyncio's, each TestClient portal's, an in-process
   uvicorn's) runs a callback or a task step - is wrapped with a timer. A
   callback that holds its loop longer than ``HP_LOOP_BLOCK_BUDGET_MS``
   (default 250) is recorded against the running test. This is asyncio debug
   mode's ``slow_callback_duration`` check, applied to every loop rather than
   only to loops someone remembered to put in debug mode.

2. **On-loop primitives.** The blocking primitives themselves (git, the
   artifact file lock, ...) call ``homepilot.runtime.blocking.assert_off_loop``.
   Here that report is recorded too, so a git call made on the loop fails its
   test even when the box is fast enough to finish it inside the budget.

Both attribute a block before failing on it. A test's own coroutine body
calling something synchronous (building ``Settings`` or an ``ArtifactStore``,
seeding a file) blocks only the loop the test made for itself, as setup; so
does a CLI command's coroutine in its one-shot process. What fails is a block
whose running coroutine - the innermost coroutine frame, sampled while it
blocks - is product code: a route, a reconciler, the lifecycle, startup, even
when a test awaited it. Time spent in garbage collection is not counted. A
block whose stack could not be sampled held the GIL throughout (a long C call
or a starved box, never I/O or Python code) and fails past
``HP_LOOP_BLOCK_UNSAMPLED_MS`` (default 500). Loops that exist to CONTAIN
blocking code on a worker thread (``_CONTAINMENT_THREAD_PREFIXES``) are not the
shared loop and are not timed against it. Nor is the loop Playwright's SYNC API
pumps (``_DRIVER_LOOPS``): it belongs to the e2e test driver, and the test's
own synchronous code runs inside one of its callbacks.

A recorded block turns that phase's report (setup, call or teardown) into a
failure naming the callback, its duration and its loop thread. Blocks recorded
outside any test phase fail the session.

``ALLOWLIST`` holds node ids allowed to block. It starts empty and should stay
that way: an entry is a known freeze, written down.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import os
import sys
import threading
import time
import traceback
import weakref
from collections.abc import Callable, Generator
from types import FrameType
from typing import Any

import pytest

import homepilot.cli as cli_package
from homepilot.runtime import blocking

ALLOWLIST: frozenset[str] = frozenset()

# 250 ms, measured rather than guessed: on a fully loaded box the residual
# callbacks over 100 ms in this suite are one-time warm-ups in the fresh app
# each test builds - pydantic/FastAPI schema construction and first imports,
# up to ~240 ms - not blocking I/O. Blocking I/O does not need the clock: the
# primitives report themselves at ANY duration (assert_off_loop, below).
BUDGET_S = float(os.environ.get("HP_LOOP_BLOCK_BUDGET_MS", "250")) / 1000.0
# A block the sampler could not catch in the act held the GIL throughout: not
# I/O (sleep, a subprocess, a lock and a socket all release it) and not Python
# code (which yields it every few ms) - a long C call, or the box not
# scheduling us at all. Those fail only past this longer budget, so a busy
# machine cannot flake the gate while a real C-level freeze still fails it.
UNSAMPLED_BUDGET_S = float(os.environ.get("HP_LOOP_BLOCK_UNSAMPLED_MS", "500")) / 1000.0

# Worker threads that run a PRIVATE loop on purpose to contain blocking code:
# adapters/pve_sdn.py's OffLoopClient runs each call of the synchronous
# proxmox library under its own asyncio.run on a "pve-library" thread, so that
# blocking never reaches the shared loop. Blocking that loop is the design.
_CONTAINMENT_THREAD_PREFIXES = ("pve-library",)

# Loops that belong to the TEST DRIVER, not to HomePilot. Playwright's sync API
# (pytest-playwright's `page`, `browser`, ... and tests/test_e2e.py) owns a
# private asyncio loop on the calling thread and runs it in a dispatcher
# greenlet. A sync call switches to that greenlet until its reply arrives; the
# reply wakes the test's greenlet from INSIDE a loop callback
# (`wait_for_future.<locals>.resume`), and the test's own code then runs until
# its next Playwright call - so every line of a sync e2e test, a fixture's
# `time.sleep` included, is timed as one "callback" of that loop. The loop
# serves nothing but the driver (HomePilot runs in its own process there), so
# nothing on it is a HomePilot loop block. Tagged by the loop itself, at the
# moment Playwright builds its sync connection (_tag_playwright_sync_loops) -
# not by test id, thread or callback name - so a block on ANY other loop in
# the same session, the same thread included, is still timed.
_DRIVER_LOOPS: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()
_original_connection_init: Callable[..., None] | None = None

_violations: list[str] = []
_violations_lock = threading.Lock()
_original_run: Callable[[asyncio.Handle], None] | None = None


def _record(message: str) -> None:
    with _violations_lock:
        _violations.append(message)


def _take() -> list[str]:
    with _violations_lock:
        taken = list(_violations)
        _violations.clear()
    return taken


# Callbacks running right now, per loop thread: thread id -> (start, token).
# A sampler thread grabs the stack of any callback still running at half the
# budget, WHILE it runs, so a report names the blocking line - a handle's repr
# after the fact only names the task that woke up - and so the block can be
# attributed to the coroutine that made it.
_running: dict[int, tuple[float, object]] = {}
# Seconds spent in garbage collection, per thread. A collection pauses the
# whole interpreter wherever it happens to trigger; it is not the callback
# blocking, so it is taken out of the callback's time.
_gc = threading.local()
_samples: dict[object, tuple[str, str]] = {}
_sampler: threading.Thread | None = None
_SAMPLE_EVERY_S = max(0.005, BUDGET_S / 10)
_COROUTINE_FLAGS = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE


def _blocking_coroutine(frame: FrameType | None) -> str:
    """The file of the innermost coroutine on the stack: the one whose
    synchronous code is holding the loop right now."""
    while frame is not None:
        if frame.f_code.co_flags & _COROUTINE_FLAGS:
            return os.path.abspath(frame.f_code.co_filename)
        frame = frame.f_back
    return ""


def _sample_forever() -> None:
    while True:
        time.sleep(_SAMPLE_EVERY_S)
        now = time.perf_counter()
        frames = None
        for tid, (start, token) in list(_running.items()):
            if now - start <= BUDGET_S / 2 or token in _samples:
                continue
            frames = frames or sys._current_frames()
            frame = frames.get(tid)
            if frame is not None:
                stack = traceback.format_list(traceback.extract_stack(frame)[-20:])
                _samples[token] = ("".join(stack), _blocking_coroutine(frame))


def _test_owned(path: str) -> bool:
    """A block made directly by a test's own coroutine body (setup it chose to
    do synchronously - building Settings, an ArtifactStore, seeding files), or
    by a CLI command's coroutine (a one-shot process: nothing else shares that
    loop). Product coroutines awaited from a test are NOT owned: their frames
    sit between the test and the blocking code, so they are what is found."""
    return path.startswith(_TESTS_DIR) or path.startswith(_CLI_DIR)


def _gc_callback(phase: str, _info: dict[str, Any]) -> None:
    if phase == "start":
        _gc.started = time.perf_counter()
    elif getattr(_gc, "started", None) is not None:
        _gc.total = getattr(_gc, "total", 0.0) + time.perf_counter() - _gc.started
        _gc.started = None


def _timed_run(self: asyncio.Handle) -> None:
    assert _original_run is not None
    tid = threading.get_ident()
    token = object()
    gc_before = getattr(_gc, "total", 0.0)
    start = time.perf_counter()
    previous = _running.get(tid)
    _running[tid] = (start, token)
    try:
        _original_run(self)
    finally:
        elapsed = time.perf_counter() - start - (getattr(_gc, "total", 0.0) - gc_before)
        if previous is None:
            _running.pop(tid, None)
        else:
            _running[tid] = previous
        sample = _samples.pop(token, None)
        if (
            elapsed > (BUDGET_S if sample else UNSAMPLED_BUDGET_S)
            and not (sample and _test_owned(sample[1]))
            and not threading.current_thread().name.startswith(_CONTAINMENT_THREAD_PREFIXES)
            and getattr(self, "_loop", None) not in _DRIVER_LOOPS
        ):
            _record(
                f"event loop blocked {elapsed * 1000:.0f} ms (budget {BUDGET_S * 1000:.0f} ms) "
                f"in thread {threading.current_thread().name!r} by {self!r}"
                + (
                    f"; blocking coroutine in {sample[1] or '?'}; stack while blocked:\n{sample[0]}"
                    if sample
                    else "; no stack could be sampled (the GIL was held throughout)"
                )
            )


# Frames that belong to the blocking primitive itself, skipped to find WHO
# called it.
_PRIMITIVE_FILES = (
    os.path.join("homepilot", "artifacts", "store.py"),
    os.path.join("homepilot", "artifacts", "file_store.py"),
    os.path.join("homepilot", "runtime", "blocking.py"),
    "contextlib.py",
)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
_CLI_DIR = os.path.join(os.path.abspath(cli_package.__path__[0]), "")


def _on_loop(what: str, stack: traceback.StackSummary) -> None:
    """Record an on-loop blocking primitive - unless a TEST called it directly.

    An async test or fixture that builds an ``ArtifactStore`` or seeds a file
    with ``store.write`` blocks its own loop on purpose, as setup; that is not
    the product blocking a loop. What counts is the first caller outside the
    primitive: a test file means the test did it, anything else (a route, a
    reconciler, the lifecycle) is the product, and fails the test.
    """
    caller = next(
        (frame for frame in reversed(stack) if not frame.filename.endswith(_PRIMITIVE_FILES)),
        None,
    )
    if caller is not None and _test_owned(os.path.abspath(caller.filename)):
        return
    _record(f"blocking call on the event loop: {what}\n" + "".join(stack.format()))


def _tag_playwright_sync_loops() -> None:
    """Record the loop of every SYNC-mode Playwright connection in _DRIVER_LOOPS.

    ``playwright._impl._connection.Connection`` is the one place both APIs build
    a driver connection, and its ``dispatcher_fiber`` is what tells them apart:
    the sync API passes the greenlet that pumps its private loop, the async API
    passes None and runs on the caller's loop - a loop the detector must keep
    timing. The parameters are read by NAME from the real signature, so a
    Playwright that renames them fails here, loudly, instead of silently
    exempting nothing (tests/test_loop_block_detector_driver.py also proves the
    tagging against the installed Playwright).
    """
    global _original_connection_init
    try:
        from playwright._impl import _connection
    except ImportError:  # no Playwright installed: no driver loop to exempt
        return
    original = _connection.Connection.__init__
    signature = inspect.signature(original)
    missing = {"dispatcher_fiber", "loop"} - set(signature.parameters)
    if missing:
        raise RuntimeError(
            f"playwright Connection.__init__ has no {sorted(missing)} parameter; "
            "tests/loop_block_detector.py cannot tell Playwright's sync driver loop apart"
        )

    def tagging_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs).arguments
        if bound["dispatcher_fiber"] is not None:
            _DRIVER_LOOPS.add(bound["loop"])

    _original_connection_init = original
    _connection.Connection.__init__ = tagging_init  # type: ignore[method-assign]


def _untag_playwright_sync_loops() -> None:
    global _original_connection_init
    if _original_connection_init is None:
        return
    from playwright._impl import _connection

    _connection.Connection.__init__ = _original_connection_init  # type: ignore[method-assign]
    _original_connection_init = None


def install() -> None:
    global _original_run
    if _original_run is not None:
        return
    global _sampler
    _tag_playwright_sync_loops()
    _original_run = asyncio.events.Handle._run
    asyncio.events.Handle._run = _timed_run  # type: ignore[method-assign]
    gc.callbacks.append(_gc_callback)
    if _sampler is None:
        _sampler = threading.Thread(target=_sample_forever, name="loop-block-sampler", daemon=True)
        _sampler.start()
    blocking.set_on_loop_reporter(_on_loop)


def uninstall() -> None:
    global _original_run
    if _original_run is None:
        return
    asyncio.events.Handle._run = _original_run  # type: ignore[method-assign]
    _original_run = None
    _untag_playwright_sync_loops()
    if _gc_callback in gc.callbacks:
        gc.callbacks.remove(_gc_callback)
    blocking.set_on_loop_reporter(None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, Any, None]:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    found = _take()
    if not found or item.nodeid in ALLOWLIST or report.failed:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"The event loop was blocked during {report.when} "
        f"(tests/loop_block_detector.py):\n\n" + "\n\n".join(found)
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    stray = _take()
    if stray:
        session.config.stash[_STRAY_KEY] = stray
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


_STRAY_KEY = pytest.StashKey[list[str]]()


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    stray = config.stash.get(_STRAY_KEY, [])
    if stray:
        terminalreporter.write_line(
            "Event loop blocked outside any test phase (tests/loop_block_detector.py):",
            red=True,
            bold=True,
        )
        for message in stray:
            terminalreporter.write_line(message, red=True)
