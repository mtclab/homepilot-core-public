"""Adoption-time introspection: persistence, journey, idempotence, boundary (#397).

These exercise the OBSERVED-state capture end to end:

* the service records listening ports + docker containers as ``services`` rows
  marked observed, plus an "as-found" KB note;
* the adopt ROUTE drives it through the real ``InventoryService`` and the real
  read-only ``AgentAdapter`` (a fake hub supplies canned probe output), and the
  adopted host still returns 200 even when introspection raises or no agent is
  connected;
* re-adopting is idempotent (no duplicate observed rows);
* introspection NEVER creates an artifact (the hard observed-vs-authored line).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_token
from homepilot.inventory.router import router as inventory_router
from homepilot.inventory.service import OBSERVED_MARKER, InventoryService

# ── canned probe outputs (real adapter rejects `docker ps`, so it is omitted) ────

_OS_RELEASE = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\n'
_UNAME = "Linux vm1 6.1.0-13-amd64 #1 SMP x86_64 GNU/Linux"
_DPKG = (
    "||/ Name Version Arch Description\nii  bash  5.2  amd64  shell\nii  curl  7.88  amd64  tool\n"
)
_SS = (
    "State  Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
    "LISTEN 0      128    0.0.0.0:22         0.0.0.0:*\n"
    "LISTEN 0      128    0.0.0.0:443        0.0.0.0:*\n"
)
_HOSTNAME = "vm1\n"

_HUB_OUTPUTS: dict[str, tuple[int, str]] = {
    "uname": (0, _UNAME),
    "cat /etc/os-release": (0, _OS_RELEASE),
    "dpkg -l": (0, _DPKG),
    "ss -tln": (0, _SS),
    "cat /etc/hostname": (0, _HOSTNAME),
}


class _FakeAgent:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


class _FakeRegistry:
    def get_by_hostname(self, host: str) -> _FakeAgent | None:
        return _FakeAgent("agent-1")


class _FakeHub:
    """Minimal stand-in for the agent hub the real AgentAdapter talks to."""

    def __init__(self) -> None:
        self.registry = _FakeRegistry()

    async def send_command(self, agent_id: str, command: str, timeout: int) -> dict[str, Any]:
        for prefix, (rc, out) in _HUB_OUTPUTS.items():
            if command.startswith(prefix):
                return {"exit_code": rc, "stdout": out, "stderr": ""}
        return {"exit_code": 127, "stdout": "", "stderr": "not found"}


# ── fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
async def repo(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository

    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await run_migrations(db)
    yield Repository(db)
    await db.close()


def _service_adapter() -> Any:
    """A fake adapter for direct service-level tests. Unlike the real adapter it
    permits `docker ps`, so docker-container persistence can be exercised."""
    outputs = dict(_HUB_OUTPUTS)
    outputs["docker ps"] = (
        0,
        "CONTAINER ID   IMAGE   COMMAND   CREATED   STATUS   PORTS   NAMES\n"
        'x1   nginx:latest   "run"   1h ago   Up 1h   80/tcp   web\n',
    )

    class _Adapter:
        async def test_connection(self, host: str) -> bool:
            return True

        async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]:
            for prefix, (rc, out) in outputs.items():
                if command.startswith(prefix):
                    return rc, out, ""
            return 127, "", "not found"

    return _Adapter()


# ── service-level persistence + boundary ────────────────────────────────────────


async def test_introspect_and_record_writes_observed_services_and_note(repo) -> None:
    host_id = await repo.create_host(hostname="vm1", host_type="qemu")
    host = await repo.get_host(host_id)

    svc = InventoryService(repo=repo)
    summary = await svc.introspect_and_record(host, _service_adapter())

    assert summary["os"] == "Debian GNU/Linux 12 (bookworm)"

    services = await repo.list_services(host_id=host_id)
    observed = [s for s in services if s["managed_by"] == OBSERVED_MARKER]
    # Two listening ports + one docker container.
    assert len(observed) == 3
    assert all(s["name"].startswith("observed:") for s in observed)
    names = {s["name"] for s in observed}
    assert "observed:listen:0.0.0.0:22" in names
    assert "observed:container:web" in names

    docs = await repo.search_docs_by_source(f"introspect:{host_id}")
    assert len(docs) == 1
    assert docs[0]["kind"] == "observed-state"
    assert "OBSERVED" in docs[0]["content"]
    assert docs[0]["target"] == "vm1"


async def test_observed_rows_carry_the_observed_marker(repo) -> None:
    # Revert-proof for the marker: if OBSERVED_MARKER stopped being written to
    # managed_by, these rows would default to 'user' and this fails.
    host_id = await repo.create_host(hostname="vm1", host_type="qemu")
    host = await repo.get_host(host_id)
    svc = InventoryService(repo=repo)
    await svc.introspect_and_record(host, _service_adapter())

    services = await repo.list_services(host_id=host_id)
    assert services  # something was written
    assert all(s["managed_by"] == OBSERVED_MARKER for s in services)


async def test_introspection_creates_no_artifact(repo) -> None:
    # The hard observed-vs-authored boundary: introspection records observed
    # state but must NEVER author an artifact.
    host_id = await repo.create_host(hostname="vm1", host_type="qemu")
    host = await repo.get_host(host_id)
    svc = InventoryService(repo=repo)

    before = await repo.list_artifacts()
    await svc.introspect_and_record(host, _service_adapter())
    after = await repo.list_artifacts()

    assert before == []
    assert after == []


async def test_readopt_is_idempotent_no_duplicate_services(repo) -> None:
    host_id = await repo.create_host(hostname="vm1", host_type="qemu")
    host = await repo.get_host(host_id)
    svc = InventoryService(repo=repo)

    await svc.introspect_and_record(host, _service_adapter())
    first = await repo.list_services(host_id=host_id)
    await svc.introspect_and_record(host, _service_adapter())
    second = await repo.list_services(host_id=host_id)

    # Revert-proof: drop the clear-then-write and the count doubles.
    assert len(first) == 3
    assert len(second) == 3
    docs = await repo.search_docs_by_source(f"introspect:{host_id}")
    assert len(docs) == 1


async def test_skipped_when_no_agent_persists_nothing(repo) -> None:
    host_id = await repo.create_host(hostname="vm1", host_type="qemu")
    host = await repo.get_host(host_id)
    svc = InventoryService(repo=repo)

    summary = await svc.introspect_and_record(host, None)

    assert summary["skipped"] == "no agent connected"
    assert await repo.list_services(host_id=host_id) == []
    assert await repo.search_docs_by_source(f"introspect:{host_id}") == []


# ── adopt ROUTE journey ─────────────────────────────────────────────────────────


def _write_token() -> dict[str, Any]:
    return {
        "user_id": "1",
        "token_id": "1",
        "scope": "write",
        "role": "admin",
        "display_name": "admin",
    }


@pytest.fixture
async def journey_client(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository

    db = Database(str(tmp_path / "journey.db"))
    await db.connect()
    await run_migrations(db)
    real_repo = Repository(db)

    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = real_repo
    app.state.inventory_service = InventoryService(repo=real_repo)
    app.state.agent_hub = _FakeHub()

    client = TestClient(app)
    app.dependency_overrides[require_token] = _write_token
    try:
        yield client, real_repo
    finally:
        app.dependency_overrides.clear()
        await db.close()


async def test_adopt_route_captures_observed_state_end_to_end(journey_client) -> None:
    client, real_repo = journey_client
    host_id = await real_repo.create_host(hostname="vm1", host_type="qemu", import_state="pending")

    resp = client.post(f"/inventory/{host_id}/adopt")
    assert resp.status_code == 200
    body = resp.json()
    assert body["import_state"] == "adopted"

    intro = body["introspection"]
    # Through the REAL adapter: os/kernel/hostname/packages/ports succeed;
    # `docker ps` is narrowed out of the read-only path, so it is unavailable.
    assert intro["probes"]["os"] == "ok"
    assert intro["probes"]["listening_ports"] == "ok"
    assert intro["probes"]["docker"] == "unavailable"
    assert intro["os"] == "Debian GNU/Linux 12 (bookworm)"
    assert intro["listening_port_count"] == 2

    services = await real_repo.list_services(host_id=host_id)
    observed = [s for s in services if s["managed_by"] == OBSERVED_MARKER]
    assert len(observed) == 2  # two listening ports, no docker
    docs = await real_repo.search_docs_by_source(f"introspect:{host_id}")
    assert len(docs) == 1
    # Boundary holds on the real journey too.
    assert await real_repo.list_artifacts() == []

    # Idempotent on re-adopt through the route.
    resp2 = client.post(f"/inventory/{host_id}/adopt")
    assert resp2.status_code == 200
    services2 = await real_repo.list_services(host_id=host_id)
    assert len([s for s in services2 if s["managed_by"] == OBSERVED_MARKER]) == 2


# ── adopt must survive introspection failure ────────────────────────────────────


def _mock_app(introspect: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = MagicMock()
    app.state.repo.get_host = AsyncMock(
        return_value={"id": "h1", "hostname": "vm1", "import_state": "pending"}
    )
    app.state.repo.update_host = AsyncMock(return_value=None)
    svc = MagicMock()
    svc.introspect_and_record = introspect
    app.state.inventory_service = svc
    return app


def test_adopt_succeeds_when_introspection_raises() -> None:
    app = _mock_app(AsyncMock(side_effect=RuntimeError("agent exploded")))
    client = TestClient(app)
    app.dependency_overrides[require_token] = _write_token
    try:
        resp = client.post("/inventory/h1/adopt")
    finally:
        app.dependency_overrides.clear()
    # Revert-proof: remove the try/except around introspection and this 500s.
    assert resp.status_code == 200
    app.state.repo.update_host.assert_awaited_once_with(
        "h1", managed=1, source="imported", import_state="adopted"
    )
    assert "introspection" not in resp.json()


def test_adopt_returns_summary_when_introspection_succeeds() -> None:
    summary = {"host": "vm1", "os": "Debian", "probes": {"os": "ok"}}
    app = _mock_app(AsyncMock(return_value=summary))
    client = TestClient(app)
    app.dependency_overrides[require_token] = _write_token
    try:
        resp = client.post("/inventory/h1/adopt")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["introspection"] == summary


def test_bulk_adopt_succeeds_when_introspection_raises() -> None:
    app = _mock_app(AsyncMock(side_effect=RuntimeError("agent exploded")))
    client = TestClient(app)
    app.dependency_overrides[require_token] = _write_token
    try:
        resp = client.post("/inventory/bulk", json={"action": "adopt", "host_ids": ["h1"]})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    # Introspection failure must NOT count the host as failed.
    assert data == {"succeeded": 1, "failed": 0}
