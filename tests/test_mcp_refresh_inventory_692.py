"""#692 - MCP ``refresh_inventory`` must really refresh the inventory.

WHAT THIS FORBIDS: a refresh tool that reads ``/version`` + ``/nodes``, writes
nothing, ignores its ``scope`` and answers ``{"status": "refreshed"}``. The
assistant then plans around an inventory that was never re-pulled. The REST twin
(``POST /inventory/refresh``) calls ``InventoryService.refresh_inventory``; the
MCP tool must drive the same sweep and report its real outcome, including
``complete``.

The assertions are on the OUTCOME - host rows in a real SQLite inventory - not on
a status string.

Teeth: restore the old handler body and every test here fails (no rows are
written, the result carries no ``complete``, and a scoped refresh touches nothing
it was asked for).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from homepilot.inventory.service import InventoryService


def _pve() -> AsyncMock:
    def _read(path: str) -> dict:
        if path == "/nodes":
            return {
                "data": [
                    {"node": "pve1", "ip": "10.0.0.1", "status": "online"},
                    {"node": "pve2", "ip": "10.0.0.2", "status": "online"},
                ]
            }
        if path == "/nodes/pve1/qemu":
            return {"data": [{"vmid": 100, "name": "web-vm", "status": "running"}]}
        if path == "/nodes/pve2/lxc":
            return {"data": [{"vmid": 200, "name": "db-ct", "status": "running"}]}
        if path == "/version":
            return {"data": {"version": "8.2"}}
        return {"data": []}

    px = AsyncMock()
    px.read = AsyncMock(side_effect=_read)
    return px


@pytest.fixture
async def repo(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository

    db = Database(str(tmp_path / "inv.db"))
    await db.connect()
    await run_migrations(db)
    yield Repository(db)
    await db.close()


def _ctx(repo, proxmox) -> dict:
    return {
        "repo": repo,
        "proxmox": proxmox,
        "inventory_service": InventoryService(repo=repo, proxmox=proxmox),
    }


async def _call(arguments: dict, ctx: dict):
    from homepilot.mcp.server import _handle_tool

    return await _handle_tool("refresh_inventory", arguments, ctx)


async def _hostnames(repo) -> set[str]:
    return {h["hostname"] for h in await repo.list_hosts()}


async def test_full_refresh_upserts_nodes_and_guests(repo):
    assert await _hostnames(repo) == set()

    result = await _call({}, _ctx(repo, _pve()))

    assert await _hostnames(repo) == {"pve1", "pve2", "web-vm", "db-ct"}, (
        "refresh_inventory reported success but wrote nothing to the inventory"
    )
    assert result["complete"] is True
    assert result["hosts"] == 4


async def test_full_scope_word_means_everything(repo):
    """The schema advertises "full"; it must not be treated as a node name."""
    result = await _call({"scope": "full"}, _ctx(repo, _pve()))

    assert await _hostnames(repo) == {"pve1", "pve2", "web-vm", "db-ct"}
    assert result["complete"] is True


async def test_scope_limits_the_sweep_to_one_node(repo):
    result = await _call({"scope": "pve2"}, _ctx(repo, _pve()))

    assert await _hostnames(repo) == {"pve2", "db-ct"}
    assert result["complete"] is True


async def test_unknown_scope_is_an_error_not_an_empty_success(repo):
    with pytest.raises(RuntimeError, match="no Proxmox node named"):
        await _call({"scope": "hosts"}, _ctx(repo, _pve()))
    assert await _hostnames(repo) == set()


async def test_existing_host_is_updated_not_duplicated(repo):
    ctx = _ctx(repo, _pve())
    await _call({}, ctx)
    await _call({}, ctx)

    names = [h["hostname"] for h in await repo.list_hosts()]
    assert sorted(names) == sorted(["pve1", "pve2", "web-vm", "db-ct"])


async def test_a_partial_sweep_reports_incomplete(repo):
    px = _pve()
    inner = px.read.side_effect

    def _read(path: str):
        if path == "/nodes/pve2/lxc":
            raise RuntimeError("node is rebooting")
        return inner(path)

    px.read = AsyncMock(side_effect=_read)
    result = await _call({}, _ctx(repo, px))

    assert result["complete"] is False
    assert "web-vm" in await _hostnames(repo)


async def test_no_proxmox_is_an_error(repo):
    with pytest.raises(RuntimeError, match="no Proxmox is configured"):
        await _call({}, _ctx(repo, None))
