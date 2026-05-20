from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from homepilot.inventory.service import InventoryService, _guess_ip, verify_connectivity

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def real_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def repo(real_db):
    from homepilot.db.repository import Repository

    return Repository(real_db)


@pytest.fixture
async def svc(repo):
    return InventoryService(repo=repo)


# ── _guess_ip ─────────────────────────────────────────────────────────────────


class TestGuessIp:
    async def test_resolves_localhost(self):
        ip = await _guess_ip("localhost")
        assert ip == "127.0.0.1"

    async def test_returns_none_on_unresolvable(self):
        ip = await _guess_ip("this-host-does-not-exist.invalid")
        assert ip is None

    async def test_returns_ip_passthrough(self):
        ip = await _guess_ip("127.0.0.1")
        assert ip == "127.0.0.1"


# ── verify_connectivity ───────────────────────────────────────────────────────


class TestVerifyConnectivity:
    async def test_returns_true_when_port_open(self):
        with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_conn:
            mock_writer = AsyncMock()
            mock_reader = AsyncMock()
            mock_conn.return_value = (mock_reader, mock_writer)
            result = await verify_connectivity("192.168.1.1", port=22)
        assert result is True

    async def test_returns_false_on_connection_refused(self):
        result = await verify_connectivity("127.0.0.1", port=1, timeout=0.1)
        assert result is False

    async def test_returns_false_on_timeout(self):
        with patch("asyncio.open_connection", new_callable=AsyncMock, side_effect=TimeoutError):
            result = await verify_connectivity("10.0.0.1", port=22, timeout=0.01)
        assert result is False


# ── InventoryService.query_inventory ─────────────────────────────────────────


class TestQueryInventory:
    async def test_empty_returns_no_hosts(self, svc):
        result = await svc.query_inventory()
        assert result == []

    async def test_returns_created_host(self, svc, repo):
        host_id = await repo.create_host(hostname="pve1", host_type="node", role="node")
        result = await svc.query_inventory()
        assert len(result) == 1
        assert result[0]["id"] == host_id
        assert result[0]["hostname"] == "pve1"

    async def test_filter_by_role(self, svc, repo):
        await repo.create_host(hostname="pve1", host_type="node", role="node")
        await repo.create_host(hostname="vm100", host_type="qemu", role="guest")
        result = await svc.query_inventory(filter={"role": "node"})
        assert len(result) == 1
        assert result[0]["hostname"] == "pve1"

    async def test_filter_by_status(self, svc, repo):
        hid = await repo.create_host(hostname="vm100", host_type="qemu", role="guest")
        await repo.update_host(hid, status="running")
        result = await svc.query_inventory(filter={"status": "running"})
        assert len(result) == 1

    async def test_host_includes_services(self, svc, repo):
        host_id = await repo.create_host(hostname="web1", host_type="lxc", role="guest")
        await repo.create_service(host_id=host_id, name="nginx", runtime="systemd")
        result = await svc.query_inventory()
        assert len(result[0]["services"]) == 1
        assert result[0]["services"][0]["name"] == "nginx"

    async def test_filter_by_hostname_partial(self, svc, repo):
        await repo.create_host(hostname="jellyfin-lxc", host_type="lxc", role="guest")
        await repo.create_host(hostname="vaultwarden-lxc", host_type="lxc", role="guest")
        result = await svc.query_inventory(filter={"hostname": "jellyfin"})
        assert len(result) == 1
        assert "jellyfin" in result[0]["hostname"]


# ── InventoryService.refresh_inventory ───────────────────────────────────────


class TestRefreshInventory:
    async def test_no_proxmox_returns_empty(self, svc):
        result = await svc.refresh_inventory()
        assert result == {"hosts": 0, "services": 0, "proxmox_host_ids": []}

    async def test_refresh_creates_node(self, repo):
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(
            side_effect=lambda path: (
                {"data": [{"node": "pve1", "ip": "10.0.0.1"}]} if path == "/nodes" else {"data": []}
            )
        )
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        result = await svc.refresh_inventory()
        assert result["hosts"] >= 1
        host = await repo.get_host_by_hostname("pve1")
        assert host is not None
        assert host["ip_address"] == "10.0.0.1"

    async def test_refresh_creates_vm_guests(self, repo):
        def _proxmox_read(path):
            if path == "/nodes":
                return {"data": [{"node": "pve1", "ip": "10.0.0.1"}]}
            if path == "/nodes/pve1/qemu":
                return {"data": [{"vmid": 100, "name": "web-vm", "status": "running"}]}
            return {"data": []}

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=_proxmox_read)
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        # First refresh creates guest; second refresh updates status
        await svc.refresh_inventory()
        await svc.refresh_inventory()
        guest = await repo.get_host_by_proxmox_id(100)
        assert guest is not None
        assert guest["hostname"] == "web-vm"
        assert guest["status"] == "running"

    async def test_refresh_uses_guess_ip_when_no_ip(self, repo):
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(
            side_effect=lambda path: (
                {"data": [{"node": "pve1"}]} if path == "/nodes" else {"data": []}
            )
        )
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        with patch(
            "homepilot.inventory.service._guess_ip",
            new_callable=AsyncMock,
            return_value="10.0.0.42",
        ):
            await svc.refresh_inventory()
        host = await repo.get_host_by_hostname("pve1")
        assert host["ip_address"] == "10.0.0.42"

    async def test_refresh_updates_existing_host(self, repo):
        await repo.create_host(hostname="pve1", host_type="node", role="node", ip_address="")
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(
            side_effect=lambda path: (
                {"data": [{"node": "pve1", "ip": "10.0.0.1"}]} if path == "/nodes" else {"data": []}
            )
        )
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        await svc.refresh_inventory()
        host = await repo.get_host_by_hostname("pve1")
        assert host["ip_address"] == "10.0.0.1"

    async def test_refresh_scope_filters_nodes(self, repo):
        call_log: list[str] = []

        def _proxmox_read(path):
            call_log.append(path)
            if path == "/nodes":
                return {"data": [{"node": "pve1"}, {"node": "pve2"}]}
            return {"data": []}

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=_proxmox_read)
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        await svc.refresh_inventory(scope="pve1")
        assert not any("pve2" in p for p in call_log)


# ── InventoryService._fetch_node_ip ──────────────────────────────────────────


class TestFetchNodeIp:
    async def test_returns_first_non_loopback_address(self, repo):
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(
            return_value={
                "data": [
                    {"name": "lo", "type": "loopback", "address": "127.0.0.1"},
                    {"name": "eth0", "type": "eth", "address": "pve.example.local"},
                ]
            }
        )
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        ip = await svc._fetch_node_ip("pve")
        assert ip == "pve.example.local"

    async def test_skips_loopback_type(self, repo):
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(
            return_value={
                "data": [
                    {"name": "lo", "type": "loopback", "address": "127.0.0.1"},
                ]
            }
        )
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        ip = await svc._fetch_node_ip("pve")
        assert ip == ""

    async def test_skips_127_prefix_addresses(self, repo):
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(
            return_value={
                "data": [
                    {"name": "lo", "type": "eth", "address": "127.0.0.1"},
                    {"name": "vmbr0", "type": "bridge", "address": "192.168.1.10"},
                ]
            }
        )
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        ip = await svc._fetch_node_ip("pve")
        assert ip == "192.168.1.10"

    async def test_returns_empty_on_api_error(self, repo):
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=httpx.ConnectError("API error"))
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        ip = await svc._fetch_node_ip("pve")
        assert ip == ""

    async def test_returns_empty_when_no_proxmox(self, svc):
        ip = await svc._fetch_node_ip("pve")
        assert ip == ""

    async def test_returns_empty_when_no_interfaces(self, repo):
        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(return_value={"data": []})
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        ip = await svc._fetch_node_ip("pve")
        assert ip == ""


class TestRefreshInventoryIpFallback:
    async def test_uses_proxmox_host_as_last_resort(self, repo):
        def _proxmox_read(path):
            if path == "/nodes":
                return {"data": [{"node": "pve"}]}
            return {"data": []}

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=_proxmox_read)
        svc = InventoryService(repo=repo, proxmox=mock_proxmox, proxmox_host="pve.example.local")
        with patch(
            "homepilot.inventory.service._guess_ip",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await svc.refresh_inventory()
        host = await repo.get_host_by_hostname("pve")
        assert host is not None
        assert host["ip_address"] == "pve.example.local"

    async def test_network_api_ip_takes_priority_over_proxmox_host(self, repo):
        def _proxmox_read(path):
            if path == "/nodes":
                return {"data": [{"node": "pve"}]}
            if path == "/nodes/pve/network":
                return {"data": [{"name": "eth0", "type": "eth", "address": "192.168.1.5"}]}
            return {"data": []}

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=_proxmox_read)
        svc = InventoryService(repo=repo, proxmox=mock_proxmox, proxmox_host="pve.example.local")
        await svc.refresh_inventory()
        host = await repo.get_host_by_hostname("pve")
        assert host["ip_address"] == "192.168.1.5"
