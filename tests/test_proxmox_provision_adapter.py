"""ProxmoxClient clone/wait/config/resize/start additions (#442 stage 1).

Exercised through a mocked HTTP transport rather than a mocked ``call``, so the
assertions cover what actually goes on the wire: URL construction, UPID quoting
and request-body shape.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import pytest
import respx
from httpx import Response

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError

BASE = "https://pve.example:8006"
API = f"{BASE}/api2/json"
UPID = "UPID:pve1:0000A1B2:0123ABCD:65F0C0DE:qmclone:9000:root@pam:"


@pytest.fixture
def client() -> ProxmoxClient:
    return ProxmoxClient(base_url=BASE, token="root@pam!t=uuid", verify_ssl=False)


class TestCloneVm:
    async def test_posts_clone_and_returns_upid(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(f"{API}/nodes/pve1/qemu/9000/clone").mock(
                return_value=Response(200, json={"data": UPID})
            )
            upid = await client.clone_vm(
                node="pve1", template_vmid=9000, new_vmid=105, name="web-01"
            )

        assert upid == UPID
        body = json.loads(route.calls[0].request.read())
        assert body["newid"] == 105
        assert body["name"] == "web-01"
        assert body["full"] == 1
        assert "pool" not in body

    async def test_linked_clone_and_pool(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(f"{API}/nodes/pve1/qemu/9000/clone").mock(
                return_value=Response(200, json={"data": UPID})
            )
            await client.clone_vm(
                node="pve1",
                template_vmid=9000,
                new_vmid=105,
                name="web-01",
                full=False,
                pool="lab",
            )

        body = json.loads(route.calls[0].request.read())
        assert body["full"] == 0
        assert body["pool"] == "lab"

    async def test_http_error_is_wrapped(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{API}/nodes/pve1/qemu/9000/clone").mock(
                return_value=Response(500, text="no such template")
            )
            with pytest.raises(ProxmoxError):
                await client.clone_vm(node="pve1", template_vmid=9000, new_vmid=105, name="web-01")


class TestWaitForTask:
    async def test_polls_until_stopped_and_quotes_upid(self, client: ProxmoxClient):
        responses = [
            Response(200, json={"data": {"status": "running"}}),
            Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}}),
        ]
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(url__regex=rf"{API}/nodes/pve1/tasks/.*/status").mock(
                side_effect=responses
            )
            data = await client.wait_for_task("pve1", UPID, timeout_s=5, poll_interval=0.01)

        assert data["exitstatus"] == "OK"
        assert len(route.calls) == 2
        raw_path = route.calls[0].request.url.raw_path.decode()
        # The UPID's ':' separators must be percent-encoded, or the path splits
        # into extra segments and PVE 501s.
        assert ":" not in raw_path.split("/tasks/")[1]
        assert unquote(raw_path.split("/tasks/")[1].removesuffix("/status")) == UPID

    async def test_non_ok_exitstatus_raises_carrying_the_status(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__regex=rf"{API}/nodes/pve1/tasks/.*/status").mock(
                return_value=Response(
                    200,
                    json={"data": {"status": "stopped", "exitstatus": "unable to create VM"}},
                )
            )
            with pytest.raises(ProxmoxError) as excinfo:
                await client.wait_for_task("pve1", UPID, timeout_s=5, poll_interval=0.01)

        assert "unable to create VM" in str(excinfo.value)

    async def test_timeout_raises(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__regex=rf"{API}/nodes/pve1/tasks/.*/status").mock(
                return_value=Response(200, json={"data": {"status": "running"}})
            )
            with pytest.raises(ProxmoxError) as excinfo:
                await client.wait_for_task("pve1", UPID, timeout_s=0.0, poll_interval=0.01)

        assert "did not finish" in str(excinfo.value)


class TestSetVmConfig:
    async def test_sshkeys_url_encoded_exactly_once(self, client: ProxmoxClient):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB+slash/and+plus user@host"
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(f"{API}/nodes/pve1/qemu/105/config").mock(
                return_value=Response(200, json={"data": None})
            )
            await client.set_vm_config("pve1", 105, {"name": "web-01", "sshkeys": key})

        payload = json.loads(route.calls[0].request.read())
        # PVE uri_unescape()s the stored value once; JSON transport adds no
        # encoding of its own, so ONE decode must return the original key.
        assert unquote(payload["sshkeys"]) == key
        assert payload["sshkeys"] != key
        # Not double-encoded: a '+' becomes '%2B', never '%252B'.
        assert "%25" not in payload["sshkeys"]
        assert payload["name"] == "web-01"

    async def test_caller_dict_is_not_mutated(self, client: ProxmoxClient):
        config = {"sshkeys": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA k@h"}
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{API}/nodes/pve1/qemu/105/config").mock(
                return_value=Response(200, json={"data": None})
            )
            await client.set_vm_config("pve1", 105, config)

        assert config["sshkeys"] == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA k@h"


class TestResizeAndStart:
    async def test_resize_disk_puts_disk_and_size(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            route = mock.put(f"{API}/nodes/pve1/qemu/105/resize").mock(
                return_value=Response(200, json={"data": None})
            )
            await client.resize_disk("pve1", 105, "scsi0", "20G")

        body = json.loads(route.calls[0].request.read())
        assert body["disk"] == "scsi0"
        assert body["size"] == "20G"

    async def test_start_vm_returns_upid(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{API}/nodes/pve1/qemu/105/status/start").mock(
                return_value=Response(200, json={"data": UPID})
            )
            assert await client.start_vm("pve1", 105) == UPID

    async def test_get_vm_current(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{API}/nodes/pve1/qemu/105/status/current").mock(
                return_value=Response(200, json={"data": {"status": "running"}})
            )
            result = await client.get_vm_current("pve1", 105)

        assert result["data"]["status"] == "running"


class TestAgentNetwork:
    async def test_returns_payload(self, client: ProxmoxClient):
        payload = {"data": {"result": [{"name": "eth0", "ip-addresses": []}]}}
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{API}/nodes/pve1/qemu/105/agent/network-get-interfaces").mock(
                return_value=Response(200, json=payload)
            )
            assert await client.get_vm_agent_network("pve1", 105) == payload

    async def test_absent_agent_returns_none_not_error(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{API}/nodes/pve1/qemu/105/agent/network-get-interfaces").mock(
                return_value=Response(500, text="QEMU guest agent is not running")
            )
            assert await client.get_vm_agent_network("pve1", 105) is None
