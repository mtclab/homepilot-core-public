import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ["HP_SECRET_KEY"] = "test-secret-key-for-pytest-only-not-for-production"


@pytest.fixture(scope="session", autouse=True)
def _reset_global_rate_limits():
    """Clear in-memory rate limit state before test session."""
    from homepilot import main as main_mod
    from homepilot.auth import router as auth_router

    auth_router._token_create_attempts.clear()
    main_mod._RATE_WINDOW.clear()
    yield


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
        secret_key="test-secret-key-for-pytest-only-not-for-production",
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

    store.exists = _exists
    store.read = _read
    store.write = _write
    store.list = _list
    store.resolve_path = _resolve_path
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
