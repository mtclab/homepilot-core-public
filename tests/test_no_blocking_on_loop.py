"""Nothing blocking runs on the event loop (architecture review F1 / R1).

#795: a WAL checkpoint inside a sqlite close ran on the loop and froze every
endpoint for 16 s. The review found the same shape everywhere git was used:
the artifact store's commits and pushes, its `time.sleep` lock spin, a router's
`git diff` - all called from request handlers, on the loop.

What these tests hold:

* the API keeps answering while git is SLOW: a propose, an approve and a
  preview each wait on a deliberately slowed `git`, and a read issued
  meanwhile is served at once, with the loop never stalled - the user-facing
  outcome, not "the helper was called";
* the git pool runs repository work strictly in order, one job at a time,
  which is what keeps commits from interleaving now that they are off the loop;
* the loop-stall watchdog names the blocking call by its stack while it blocks;
* the suite's own detector fails a test that blocks (proven in a child pytest).
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.runtime import blocking
from homepilot.runtime.blocking import POOL_SIZES, run_blocking, shutdown_pools

from . import loop_block_detector
from .test_background_shutdown import _LoopStallDetector

REPO_ROOT = Path(__file__).resolve().parents[1]

# Each git invocation sleeps this long first. A propose runs `git add` and
# `git commit`, so it takes at least twice this - long enough that a blocked
# loop is unmistakable, short enough to keep the suite quick.
GIT_DELAY_S = 0.4

# The loop may never go this long without running a callback while git works.
LOOP_GAP_BUDGET_S = 0.1


def _spec(n: int = 1) -> dict[str, Any]:
    return {
        "id": f"2026-11-08-slow-git-{n:06x}",
        "kind": "shell-script",
        "intent": "Exercise the artifact store while git is slow",
        "body": (
            "Touch a marker.\nIdempotent: guarded by a test.\n"
            "```bash shell-spec\ntouch /tmp/marker\n```\n"
        ),
        "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
        "idempotence": "via-precheck",
    }


class _LoopGapProbe:
    """Measures the longest time the loop went without running this task."""

    def __init__(self) -> None:
        self.max_gap = 0.0
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            self.max_gap = max(self.max_gap, now - last - 0.005)
            last = now

    def __enter__(self) -> _LoopGapProbe:
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()


@pytest.fixture
def slow_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_git = shutil.which("git")
    assert real_git, "git is required"
    shim_dir = tmp_path / "slow-git-bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(f'#!/bin/sh\nsleep {GIT_DELAY_S}\nexec "{real_git}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
async def api(tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    # Built the way startup builds it: the constructor runs git.
    store = await run_blocking("git", ArtifactStore, tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
    app.state.repo = repo

    user_id = await repo.create_user(display_name="owner", auth_source="api_token")
    full, prefix, token_hash = generate_api_token()
    await repo.create_api_token(
        user_id=str(user_id),
        token_type="personal",
        prefix=prefix,
        hash=token_hash,
        scope="admin",
        label="operator",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {full}"},
    ) as client:
        yield client, store
    await db.close()


async def _while_git_runs(client: AsyncClient, slow_call: Any) -> tuple[Any, float, float]:
    """Start `slow_call`, read the artifact list while it is in flight.

    Returns (slow response, read seconds, longest loop gap)."""
    with _LoopGapProbe() as probe:
        slow = asyncio.create_task(slow_call)
        await asyncio.sleep(0.05)
        started = time.monotonic()
        listing = await client.get("/artifacts")
        read_s = time.monotonic() - started
        assert listing.status_code == 200, listing.text
        # The read finished while git was still working: it did not queue
        # behind the write, and the write really was slow.
        assert not slow.done(), "git finished before the read; the delay did not apply"
        response = await slow
    return response, read_s, probe.max_gap


class TestApiAnswersWhileGitIsSlow:
    async def test_propose_approve_preview(self, api, slow_git) -> None:
        client, store = api

        resp, read_s, gap = await _while_git_runs(client, client.post("/artifacts", json=_spec()))
        assert resp.status_code == 200, resp.text
        aid = resp.json()["id"]
        assert read_s < 0.3, f"a read waited {read_s:.2f}s behind a git commit"
        assert gap < LOOP_GAP_BUDGET_S, f"the loop stalled {gap * 1000:.0f} ms during propose"

        resp, read_s, gap = await _while_git_runs(
            client, client.post(f"/artifacts/{aid}/approve", json={})
        )
        assert resp.status_code == 200, resp.text
        assert read_s < 0.3
        assert gap < LOOP_GAP_BUDGET_S, f"the loop stalled {gap * 1000:.0f} ms during approve"

        resp, read_s, gap = await _while_git_runs(client, client.post(f"/artifacts/{aid}/preview"))
        assert resp.status_code == 200, resp.text
        assert gap < LOOP_GAP_BUDGET_S, f"the loop stalled {gap * 1000:.0f} ms during preview"
        assert "status: approved" in resp.json()["diff"]

        # The outcome, not just the responses: both transitions are committed.
        git_log = await run_blocking(
            "io",
            subprocess.run,
            ["git", "log", "--format=%s"],
            cwd=store.root,
            capture_output=True,
            text=True,
        )
        log = git_log.stdout
        assert f"propose: {aid}" in log
        assert f"approve: {aid}" in log


class TestTheGitPool:
    async def test_runs_jobs_one_at_a_time_in_submission_order(self) -> None:
        events: list[tuple[str, int]] = []

        def job(i: int) -> str:
            events.append(("start", i))
            time.sleep(0.005)
            events.append(("end", i))
            return threading.current_thread().name

        names = await asyncio.gather(*(run_blocking("git", job, i) for i in range(12)))
        assert events == [e for i in range(12) for e in (("start", i), ("end", i))]
        assert all(name.startswith("hp-git") for name in names)
        assert POOL_SIZES["git"] == 1

    async def test_slow_git_does_not_starve_io(self) -> None:
        gate = threading.Event()
        held = asyncio.ensure_future(run_blocking("git", gate.wait, 5))
        try:
            started = time.monotonic()
            name = await run_blocking("io", lambda: threading.current_thread().name)
            assert name.startswith("hp-io")
            assert time.monotonic() - started < 0.5
        finally:
            gate.set()
            await held


class TestThePoolsShutDown:
    """main.lifespan retires the pools after the drains, before the checkpoint (3.9.0)."""

    async def test_in_flight_work_finishes_and_the_pools_are_replaced(self) -> None:
        done = threading.Event()

        def commit() -> None:
            time.sleep(0.3)
            done.set()

        job = asyncio.ensure_future(run_blocking("git", commit))
        await asyncio.sleep(0.05)  # on the pool, nobody awaiting it here

        assert await shutdown_pools(timeout=5.0) == []

        assert done.is_set(), "shutdown returned before the in-flight commit finished"
        assert blocking._pools == {}
        await job
        # A later app in the same process (the suite builds many) gets fresh pools.
        name = await run_blocking("git", lambda: threading.current_thread().name)
        assert name.startswith("hp-git")

    async def test_the_wait_is_bounded_and_off_the_loop(self) -> None:
        release = threading.Event()
        job = asyncio.ensure_future(run_blocking("io", release.wait, 10))
        await asyncio.sleep(0.05)
        try:
            async with _LoopStallDetector() as stalls:
                started = time.monotonic()
                busy = await shutdown_pools(timeout=0.3)
                waited = time.monotonic() - started
        finally:
            release.set()
            await job
        assert busy == ["io"]
        assert waited < 2.0, f"shutdown_pools waited {waited:.1f}s past a 0.3s budget"
        assert stalls.worst < 0.2, f"the loop stalled {stalls.worst:.2f}s while pools shut down"


class TestAssertOffLoop:
    def test_silent_off_the_loop(self) -> None:
        seen: list[str] = []
        blocking.set_on_loop_reporter(lambda what, stack: seen.append(what))
        try:
            blocking.assert_off_loop("x")
        finally:
            blocking.set_on_loop_reporter(loop_block_detector._on_loop)
        assert seen == []

    async def test_reports_on_the_loop_with_the_callers_stack(self) -> None:
        seen: list[tuple[str, str]] = []
        blocking.set_on_loop_reporter(
            lambda what, stack: seen.append((what, "".join(stack.format())))
        )
        try:
            blocking.assert_off_loop("git commit")
        finally:
            blocking.set_on_loop_reporter(loop_block_detector._on_loop)
        assert len(seen) == 1
        assert seen[0][0] == "git commit"
        assert "test_reports_on_the_loop_with_the_callers_stack" in seen[0][1]

    async def test_the_wal_checkpoint_reports_itself_on_the_loop(self, tmp_path: Path) -> None:
        """#800's checkpoint met #801's rule in 3.9.0: its sqlite call is guarded.

        `Database.checkpoint` runs it on a daemon thread (silent); a direct call
        from a coroutine is reported, however fast it happens to be.
        """
        db = Database(str(tmp_path / "hp.db"))
        await db.connect()
        seen: list[str] = []
        blocking.set_on_loop_reporter(lambda what, stack: seen.append(what))
        try:
            assert await db.checkpoint() is True
            assert seen == []
            Database._checkpoint_file(db.db_path, 1.0)
        finally:
            blocking.set_on_loop_reporter(loop_block_detector._on_loop)
            await db.close()
        assert seen == ["sqlite WAL checkpoint"]


_WATCHDOG_SCRIPT = textwrap.dedent(
    """
    import asyncio, logging, sys, time
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
    from homepilot.runtime.loop_watchdog import LoopStallWatchdog

    def a_blocking_call_on_the_loop():
        time.sleep(0.6)

    async def main():
        dog = LoopStallWatchdog(150)
        dog.start()
        await asyncio.sleep(0.3)
        quiet = dog.snapshot()
        a_blocking_call_on_the_loop()
        await asyncio.sleep(0.3)
        print("SNAP", quiet["stalls"], dog.snapshot()["stalls"], dog.snapshot()["worst_ms"] >= 400)
        await dog.stop()

    asyncio.run(main())
    """
)


def test_watchdog_logs_the_blocking_stack_while_blocked() -> None:
    # A child process: this test blocks a loop ON PURPOSE, which the suite's
    # detector would (rightly) fail in-process.
    proc = subprocess.run(
        [sys.executable, "-c", _WATCHDOG_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Caught in the act: the stack names the function that was blocking.
    assert "Event loop blocked for" in out
    assert "a_blocking_call_on_the_loop" in out
    # Idle costs no false alarm; the stall is counted once, with its size.
    assert "SNAP 0 1 True" in out


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
    import asyncio, time
    import pytest
    from homepilot.runtime.blocking import assert_off_loop, run_blocking

    async def test_sleeps_on_the_loop():
        time.sleep(0.3)

    async def test_product_primitive_on_the_loop():
        def product_code():
            assert_off_loop("git commit subprocess")
        product_code()

    async def test_offloaded_sleep_is_fine():
        await run_blocking("io", time.sleep, 0.3)

    def test_sync_test_may_sleep():
        time.sleep(0.3)

    @pytest.fixture
    async def blocks_in_teardown():
        yield
        time.sleep(0.3)

    async def test_teardown_blocks(blocks_in_teardown):
        pass
    """
)


def test_the_detector_fails_a_test_that_blocks_the_loop(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        _CHILD_CONFTEST.format(tests_dir=str(Path(__file__).parent))
    )
    # `product_code` must look like product code to the detector, not the
    # test: it is attributed by file, so it lives outside any tests/ dir.
    (tmp_path / "test_child.py").write_text(_CHILD_TESTS)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "-rfE",
            str(tmp_path / "test_child.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    assert "FAILED test_child.py::test_sleeps_on_the_loop" in out
    assert "FAILED test_child.py::test_product_primitive_on_the_loop" in out
    assert "ERROR test_child.py::test_teardown_blocks" in out or (
        "FAILED test_child.py::test_teardown_blocks" in out
    )
    assert "event loop blocked" in out
    assert "blocking call on the event loop: git commit subprocess" in out
    # Off-loaded blocking and a sync test's own sleep are not loop blocks (the
    # third pass is test_teardown_blocks' call phase; its teardown is the error).
    assert "2 failed, 3 passed, 1 error" in out, out


# Every blocking primitive in the product, by file, with how it is kept off the
# loop. A NEW one - or one more in a listed file - fails until it is routed
# through run_blocking and recorded here; that review is the point. The runtime
# half (assert_off_loop + the detector) proves the listed ones stay off-loop.
_BLOCKING_PRIMITIVE = re.compile(
    r"\bsubprocess\.(run|call|check_call|check_output|Popen)\(|\btime\.sleep\("
    r"|\bfcntl\.(flock|lockf)\(|\bsqlite3\.connect\(|\bhashlib\.(pbkdf2_hmac|scrypt)\("
    r"|\bsocket\.(getaddrinfo|gethostbyname|create_connection)\(|\burlopen\("
    r"|\brequests\.(get|post|put|delete|request)\("
)
_REVIEWED: dict[str, tuple[int, str]] = {
    "artifacts/store.py": (4, "sync git API; loop callers use run_blocking('git'/'io'); guarded"),
    "artifacts/file_store.py": (2, "file lock; taken inside run_serialized (git pool); guarded"),
    "app_state.py": (3, "_init_artifacts_repo, run with run_blocking('git') at startup"),
    "db/backup.py": (4, "sync sqlite3 for the CLI; the reconciler uses run_blocking('io')"),
    "vault/manager.py": (1, "PBKDF2; every path reaches it through run_blocking('io')"),
    "instance_lock.py": (2, "flock LOCK_NB: never waits"),
    "agent_hub/selfconfig.py": (1, "getaddrinfo in cert SANs; ensure_hub_certificate is off-loop"),
    "adapters/pve_firewall_guard.py": (
        1,
        "getaddrinfo; management_source uses run_blocking('net')",
    ),
    "provision/service.py": (1, "inside a shell script string sent to a guest, not Python"),
    "db/connection.py": (
        1,
        "WAL checkpoint (#800): Database.checkpoint runs it on its own daemon thread; guarded",
    ),
}


def test_every_blocking_primitive_is_reviewed() -> None:
    src = REPO_ROOT / "src" / "homepilot"
    found: dict[str, int] = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel.startswith(("cli/", "runtime/")):
            continue  # the CLI runs no event loop; runtime/ is the helper itself
        count = len(_BLOCKING_PRIMITIVE.findall(path.read_text(encoding="utf-8")))
        if count:
            found[rel] = count
    expected = {rel: count for rel, (count, _why) in _REVIEWED.items()}
    assert found == expected, (
        "Blocking primitives changed. Route any new one through "
        "homepilot.runtime.blocking.run_blocking (and assert_off_loop in the "
        f"primitive), then update _REVIEWED. Found: {found}"
    )


# An import main.lifespan does itself runs ON the loop. Most are a lookup or a
# small module whose dependencies are loaded already (< 1 ms measured); one that
# costs real CPU belongs at module level. 100 ms leaves a loaded box headroom
# and still sits far below the one it caught (~440 ms).
_LIFESPAN_IMPORT_BUDGET_MS = 100.0


def test_the_lifespan_imports_nothing_costly_on_the_loop() -> None:
    """Each module main.lifespan imports is cheap once `homepilot.main` is loaded.

    uvicorn imports homepilot.main before it starts the event loop; what the
    lifespan imports first is imported ON the loop, at every start, with the
    agent hub and the loop watchdog already running. The MCP server's import
    (~0.4 s) did that until 3.9.0 - caught by the detector on #800's shutdown
    journey when that test happened to run first in a process. Measured in a
    fresh interpreter, in the lifespan's order, so the suite's own import order
    cannot hide one.
    """
    tree = ast.parse((REPO_ROOT / "src/homepilot/main.py").read_text(encoding="utf-8"))
    (lifespan,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    ]
    imported: list[str] = []
    for node in ast.walk(lifespan):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            name = "homepilot." + node.module
            if name not in imported:
                imported.append(name)
    assert "homepilot.mcp.server" in imported, "the lifespan no longer reads the way this gate does"
    probe = textwrap.dedent(
        """
        import importlib, json, sys, time
        import homepilot.main
        cost = {}
        for name in sys.argv[1:]:
            started = time.perf_counter()
            importlib.import_module(name)
            cost[name] = (time.perf_counter() - started) * 1000
        print(json.dumps(cost))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, *imported],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    cost: dict[str, float] = json.loads(out.stdout.strip().splitlines()[-1])
    costly = {m: round(ms) for m, ms in cost.items() if ms > _LIFESPAN_IMPORT_BUDGET_MS}
    assert costly == {}, (
        f"main.lifespan imports these on the event loop at every start (ms): {costly}. "
        "Import them at module level in homepilot/main.py."
    )


def test_the_pve_library_is_loaded_before_the_loop() -> None:
    """The PVE library is in memory once `homepilot.main` is, before any request.

    The claim's (and Settings') write-token probe and the guest-network gateway
    build proxmox_mcp's client inside a request, on the event loop. The library
    used to be imported right there - proxmox_mcp, proxmoxer, requests, charset
    detection - and on a fresh venv that held the loop 265 ms in the 3.10.0 gate
    (test_first_run_claim's claim journey). Measured in a fresh interpreter, so
    the suite's own import order cannot hide it.

    Teeth: move the library imports back into pve_sdn's functions, or drop the
    pve_sdn import from homepilot/main.py, and the library is not loaded.
    """
    probe = textwrap.dedent(
        """
        import importlib, json, sys, time
        import homepilot.main
        loaded = [m for m in sys.argv[1:] if m in sys.modules]
        started = time.perf_counter()
        for name in ("homepilot.adapters.pve_access", *sys.argv[1:]):
            importlib.import_module(name)
        print(json.dumps({"loaded": loaded, "ms": (time.perf_counter() - started) * 1000}))
        """
    )
    library = [
        "proxmox_mcp.config",
        "proxmox_mcp.multi_client",
        "proxmox_mcp.sdn",
        "proxmox_mcp.firewall",
        "proxmox_mcp.exceptions",
        "proxmoxer",
        "requests",
    ]
    out = subprocess.run(
        [sys.executable, "-c", probe, *library],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert sorted(result["loaded"]) == sorted(library), (
        "imported on the event loop at first use, not with homepilot.main: "
        f"{sorted(set(library) - set(result['loaded']))}"
    )
    assert result["ms"] <= _LIFESPAN_IMPORT_BUDGET_MS, result
