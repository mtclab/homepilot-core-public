from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from homepilot.inventory.service import (
    InventoryService,
    _guess_ip,
    _infer_role,
    derive_status,
    verify_connectivity,
)

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
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_conn:
            mock_conn.return_value = (mock_reader, mock_writer)
            result = await verify_connectivity("192.168.1.1", port=22)
        await mock_writer.wait_closed()  # consume await in verify_connectivity
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
        await repo.create_host(hostname="media-lxc", host_type="lxc", role="guest")
        await repo.create_host(hostname="vaultwarden-lxc", host_type="lxc", role="guest")
        result = await svc.query_inventory(filter={"hostname": "media"})
        assert len(result) == 1
        assert "media" in result[0]["hostname"]


class TestWritesArePersisted:
    """Regression for the adopt-revert bug: host writes must be committed to
    disk, not left in the connection's implicit transaction (lost on restart).
    A second connection to the same file must observe the write."""

    async def _fresh_count(self, tmp_path: Path) -> list[dict]:
        from homepilot.db.connection import Database

        other = Database(str(tmp_path / "test.db"))
        await other.connect()
        rows = await other.fetchall("SELECT id, source, managed, import_state FROM hosts")
        await other.close()
        return rows

    async def test_create_host_committed(self, repo, tmp_path):
        await repo.create_host(hostname="ct-1", host_type="lxc", role="guest", proxmox_id=201)
        rows = await self._fresh_count(tmp_path)
        assert len(rows) == 1

    async def test_adopt_update_committed(self, repo, tmp_path):
        host_id = await repo.create_host(
            hostname="ct-2", host_type="lxc", role="guest", proxmox_id=202, source="discovered"
        )
        await repo.update_host(host_id, managed=1, source="imported", import_state="adopted")
        rows = await self._fresh_count(tmp_path)
        assert rows[0]["source"] == "imported"
        assert rows[0]["managed"] == 1
        assert rows[0]["import_state"] == "adopted"


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
        assert guest["pve_status"] == "running"

    async def test_refresh_derives_status_for_stopped_vm(self, repo):
        # A stopped guest must surface as "offline", not "unknown", on refresh
        # alone — without requiring a separate enrichment pass.
        def _proxmox_read(path):
            if path == "/nodes":
                return {"data": [{"node": "pve1", "ip": "10.0.0.1"}]}
            if path == "/nodes/pve1/qemu":
                return {"data": [{"vmid": 101, "name": "off-vm", "status": "stopped"}]}
            return {"data": []}

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=_proxmox_read)
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        # create path
        await svc.refresh_inventory()
        guest = await repo.get_host_by_proxmox_id(101)
        assert guest["pve_status"] == "stopped"
        assert guest["status"] == "offline"
        # update path (existing host) must also re-derive
        await svc.refresh_inventory()
        guest = await repo.get_host_by_proxmox_id(101)
        assert guest["status"] == "offline"

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


class TestInferRole:
    async def test_infers_database_from_description(self):
        assert _infer_role("vm1", "postgres db") == "database"

    async def test_infers_web_server_from_hostname(self):
        assert _infer_role("web-nginx-01") == "web-server"

    async def test_defaults_to_guest(self):
        assert _infer_role("random-vm") == "guest"

    async def test_description_takes_priority(self):
        assert _infer_role("random-vm", "monitoring zabbix") == "monitoring"


class TestDeriveStatus:
    async def test_stopped_is_offline(self):
        assert derive_status("stopped", None) == "offline"

    async def test_running_with_ip_is_online(self):
        assert derive_status("running", "10.0.0.1") == "online"

    async def test_running_without_ip_is_unknown(self):
        assert derive_status("running", None) == "unknown"

    async def test_unknown_pve_status_is_unknown(self):
        assert derive_status("unknown", None) == "unknown"


class TestEnrichment:
    async def test_enrich_infers_role(self, repo):
        svc = InventoryService(repo=repo)
        host_id = await repo.create_host(
            hostname="db-postgres-01",
            host_type="qemu",
            pve_status="running",
            role_source="inferred",
        )
        await svc._enrich_single_host(await repo.get_host(host_id))
        updated = await repo.get_host(host_id)
        assert updated["role"] == "database"
        assert updated["status"] == "unknown"

    async def test_enrich_preserves_user_role(self, repo):
        svc = InventoryService(repo=repo)
        host_id = await repo.create_host(
            hostname="db-postgres-01",
            host_type="qemu",
            pve_status="running",
            role="web-server",
            role_source="user",
        )
        await svc._enrich_single_host(await repo.get_host(host_id))
        updated = await repo.get_host(host_id)
        assert updated["role"] == "web-server"
        assert updated["role_source"] == "user"

    async def test_enrich_derives_status_with_ip(self, repo):
        svc = InventoryService(repo=repo)
        host_id = await repo.create_host(
            hostname="web1",
            host_type="qemu",
            pve_status="running",
            ip_address="10.0.0.1",
            ip_source="user",
            role_source="inferred",
        )
        await svc._enrich_single_host(await repo.get_host(host_id))
        updated = await repo.get_host(host_id)
        assert updated["status"] == "online"

    async def test_enrich_skips_user_ip(self, repo):
        svc = InventoryService(repo=repo)
        host_id = await repo.create_host(
            hostname="web1",
            host_type="qemu",
            pve_status="running",
            ip_address="10.0.0.5",
            ip_source="user",
            role_source="inferred",
        )
        with patch(
            "homepilot.inventory.service._guess_ip",
            new_callable=AsyncMock,
            return_value="10.0.0.99",
        ):
            await svc._enrich_single_host(await repo.get_host(host_id))
        updated = await repo.get_host(host_id)
        assert updated["ip_address"] == "10.0.0.5"
        assert updated["ip_source"] == "user"

    async def test_enrich_sets_dns_ip(self, repo):
        svc = InventoryService(repo=repo)
        host_id = await repo.create_host(
            hostname="web1",
            host_type="qemu",
            pve_status="running",
            role_source="inferred",
        )
        with patch(
            "homepilot.inventory.service._guess_ip",
            new_callable=AsyncMock,
            return_value="10.0.0.99",
        ):
            await svc._enrich_single_host(await repo.get_host(host_id))
        updated = await repo.get_host(host_id)
        assert updated["ip_address"] == "10.0.0.99"
        assert updated["ip_source"] == "dns"


class TestRefreshInventoryNewColumns:
    async def test_refresh_sets_pve_status_and_source(self, repo):
        def _proxmox_read(path):
            if path == "/nodes":
                return {"data": [{"node": "pve1"}]}
            if path == "/nodes/pve1/qemu":
                return {"data": [{"vmid": 100, "name": "web-vm", "status": "running"}]}
            return {"data": []}

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=_proxmox_read)
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        await svc.refresh_inventory()
        guest = await repo.get_host_by_proxmox_id(100)
        assert guest["pve_status"] == "running"
        assert guest["source"] == "discovered"
        assert guest["import_state"] == "pending"
        assert guest["role_source"] == "inferred"

    async def test_refresh_updates_existing_without_overwriting_user(self, repo):
        await repo.create_host(
            hostname="web-vm",
            host_type="qemu",
            proxmox_id=100,
            role="api-server",
            role_source="user",
            ip_address="1.2.3.4",
            ip_source="user",
            source="imported",
        )

        def _proxmox_read(path):
            if path == "/nodes":
                return {"data": [{"node": "pve1"}]}
            if path == "/nodes/pve1/qemu":
                return {"data": [{"vmid": 100, "name": "web-vm", "status": "stopped"}]}
            return {"data": []}

        mock_proxmox = AsyncMock()
        mock_proxmox.read = AsyncMock(side_effect=_proxmox_read)
        svc = InventoryService(repo=repo, proxmox=mock_proxmox)
        await svc.refresh_inventory()
        guest = await repo.get_host_by_proxmox_id(100)
        assert guest["pve_status"] == "stopped"
        assert guest["role"] == "api-server"
        assert guest["role_source"] == "user"
        assert guest["ip_address"] == "1.2.3.4"
        assert guest["ip_source"] == "user"
        assert guest["source"] == "imported"
