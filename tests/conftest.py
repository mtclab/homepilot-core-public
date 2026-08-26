from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="session", autouse=True)
def _reset_global_rate_limits():
    """Clear in-memory rate limit state before test session."""
    from homepilot import main as main_mod
    from homepilot.auth import router as auth_router

    auth_router._token_create_attempts.clear()
    main_mod._RATE_WINDOW.clear()
    yield


# ── Invite portal (#442 stage 2) ─────────────────────────────────────────────
# Named portal_* so no portal test module has to shadow another's fixture; the
# helpers they build on live in tests/portal_support.py.


@pytest.fixture
def portal_pve():
    from .portal_support import FakePVE

    return FakePVE()


@pytest.fixture
def portal_proxmox(portal_pve):
    import httpx

    from homepilot.adapters.proxmox import ProxmoxClient

    client = ProxmoxClient(base_url="https://pve.example:8006", token="root@pam!t=uuid")
    transport = httpx.MockTransport(portal_pve.handle)
    fake = httpx.AsyncClient(base_url="https://pve.example:8006/api2/json", transport=transport)
    client._client = fake
    client._write_client = fake
    return client


@pytest.fixture
async def portal_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    database = Database(str(tmp_path / "portal.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def portal_app(portal_db, portal_proxmox):
    from fastapi import FastAPI

    from homepilot.db.repository import Repository
    from homepilot.portal import router as portal_router_module
    from homepilot.portal.repository import InviteRepository
    from homepilot.portal.router import router as portal_router
    from homepilot.provision.service import ProvisionService
    from homepilot.tasks.repository import TaskRepository

    from .portal_support import portal_settings

    # The portal's per-CN redemption limiter is module state; a test must never
    # inherit another test's attempts.
    portal_router_module._redeem_attempts.clear()

    application = FastAPI()
    application.include_router(portal_router, prefix="/invite")
    task_repo = TaskRepository(portal_db)
    application.state.repo = Repository(portal_db)
    application.state.task_repo = task_repo
    application.state.invite_repo = InviteRepository(portal_db)
    application.state.settings = portal_settings()
    application.state.provision_service = ProvisionService(
        proxmox=portal_proxmox,
        task_repo=task_repo,
        repo=Repository(portal_db),
        poll_interval=0.01,
        task_timeout_s=5.0,
        ip_wait_s=2.0,
        ip_interval=0.05,
    )
    yield application
    portal_router_module._redeem_attempts.clear()


# ── The real hp-agent binary ─────────────────────────────────────────────────
# Session-scoped and shared: the journey gates drive the SHIPPED artifact, and
# building it once per module put two cold Go builds in one run, which is enough
# to trip the 120s per-test timeout on the test that happens to own the fixture.


@pytest.fixture(scope="session")
def hp_agent_binary(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build agent/go once per session; skip when no Go toolchain is present."""
    import os
    import shutil
    import subprocess

    go = os.environ.get("HP_GO_BIN") or shutil.which("go") or ""
    if not go:
        pytest.skip("Go toolchain not available")
    repo_root = Path(__file__).resolve().parents[1]
    out = tmp_path_factory.mktemp("hp-agent-build") / "hp-agent"
    env = {
        **os.environ,
        "CGO_ENABLED": "0",
        # An ambient GOCACHE (the repo's documented Go invocation sets one) makes
        # this a warm build; without it, a throwaway cache still works.
        "GOCACHE": os.environ.get("GOCACHE") or str(tmp_path_factory.mktemp("gocache")),
        "GOFLAGS": "-mod=mod",
    }
    proc = subprocess.run(
        [go, "build", "-o", str(out), "."],
        cwd=str(repo_root / "agent" / "go"),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(f"could not build hp-agent: {proc.stderr}")
    return str(out)


@pytest.fixture
def tmp_artifacts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


@pytest.fixture
def tmp_vault_dir(tmp_path: Path) -> Path:
    d = tmp_path / "vault_data"
    d.mkdir()
    return d


@pytest.fixture
def test_settings():
    from homepilot.config import Settings

    return Settings(
        data_dir="/tmp/hp-test",
        artifacts_dir="/tmp/hp-test/artifacts",
    )


@pytest.fixture
def mock_store(tmp_artifacts_dir: Path) -> MagicMock:
    store = MagicMock(spec=[])
    _storage: dict[str, tuple[dict, str]] = {}

    def _exists(id_str: str) -> bool:
        return id_str in _storage

    def _read(id_str: str) -> tuple[dict, str]:
        if id_str not in _storage:
            raise FileNotFoundError(id_str)
        return _storage[id_str]

    def _write(id_str: str, fm_yml: str, body: str, event: str):
        import yaml

        fm = yaml.safe_load(fm_yml)
        _storage[id_str] = (fm, body)
        p = tmp_artifacts_dir / f"{id_str}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\n{fm_yml}---\n\n{body}", encoding="utf-8")

    def _list(status: str | None = None, kind: str | None = None) -> list[dict]:
        results = []
        for fm, _body in _storage.values():
            if status is not None and fm.get("status") != status:
                continue
            if kind is not None and fm.get("kind") != kind:
                continue
            results.append(fm)
        return results

    def _resolve_path(id_str: str) -> Path:
        return tmp_artifacts_dir / f"{id_str}.md"

    def _relative_path(id_str: str) -> str:
        return f"{id_str}.md"

    store.exists = _exists
    store.read = _read
    store.write = _write
    store.list = _list
    store.resolve_path = _resolve_path
    store.relative_path = _relative_path
    store._storage = _storage
    return store


@pytest.fixture
def real_store(tmp_artifacts_dir: Path):
    from homepilot.artifacts.store import ArtifactStore

    return ArtifactStore(tmp_artifacts_dir)


@pytest.fixture
def mock_ssh():
    from unittest.mock import AsyncMock

    ssh = AsyncMock()
    ssh.exec = AsyncMock(return_value=(0, "ok stdout", ""))
    ssh.exec_readonly = AsyncMock(return_value=(0, "readonly stdout", ""))
    ssh.read_file = AsyncMock(return_value="file content")
    ssh.write_file = AsyncMock(
        return_value={"before_hash": None, "after_hash": "abc", "changed": True}
    )
    return ssh


@pytest.fixture
def mock_proxmox():
    from unittest.mock import AsyncMock

    px = AsyncMock()
    px.call = AsyncMock(return_value={"data": {}})
    px.read = AsyncMock(return_value={"data": []})
    px.snapshot = AsyncMock(return_value={"data": "snap1"})
    px.delete_snapshot = AsyncMock(return_value={"data": None})
    return px


@pytest.fixture
def mock_vault():
    from unittest.mock import AsyncMock

    v = AsyncMock()
    v.get_secret = AsyncMock(
        return_value={
            "base_url": "https://example.com",
            "headers": {"Authorization": "Bearer tok"},
            "verify_tls": False,
        }
    )
    v.store_secret = AsyncMock()
    return v


@pytest.fixture
def mock_repo():
    from unittest.mock import AsyncMock

    r = AsyncMock()
    r.create_doc_metadata = AsyncMock(return_value=1)
    r.log_audit = AsyncMock()
    r.db = AsyncMock()
    r.db.conn = AsyncMock()
    r.db.conn.execute = AsyncMock()
    r.db.conn.commit = AsyncMock()
    return r


@pytest.fixture
def make_frontmatter():
    def _make(kind="ansible-playbook", artifact_id="2025-01-01-test-abc123", target=None, **extras):
        fm = {
            "id": artifact_id,
            "kind": kind,
            "intent": f"Test {kind}",
            "mutating": kind != "kb-note",
            "status": "approved",
            "hash": "sha256:fake",
            "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
        }
        if target:
            fm["target"] = target
        if kind != "kb-note":
            fm["idempotence"] = "via-precheck"
        fm.update(extras)
        return fm

    return _make


@pytest.fixture(scope="session", autouse=True)
def _no_leftover_db_worker_threads():
    """Fail the run if a suite leaves an aiosqlite worker thread alive (#496).

    Those threads are NON-DAEMON, so CPython joins them at interpreter exit: one
    left behind means pytest prints "1934 passed" and then never returns. That
    is not a test failure anyone can see - it looks like a frozen CI job, and it
    cost hours of exactly that before it was understood. Asserting it here turns
    it back into a normal red run.
    """
    yield
    import threading
    import time

    def _leftovers():
        return [
            t
            for t in threading.enumerate()
            if t.is_alive() and "_connection_worker_thread" in t.name
        ]

    # A worker that is on its way out is fine; one that is still there after a
    # grace period is what wedges the exit.
    deadline = time.monotonic() + 10
    while _leftovers() and time.monotonic() < deadline:
        time.sleep(0.2)

    stuck = _leftovers()
    assert not stuck, (
        f"{len(stuck)} aiosqlite worker thread(s) outlived the suite: "
        f"{[t.name for t in stuck]}. They are non-daemon, so the process cannot exit - "
        "a Database was closed in a way that never ended its worker, or never closed."
    )
