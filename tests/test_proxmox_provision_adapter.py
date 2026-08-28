"""ProxmoxClient clone/wait/config/resize/start additions (#442 stage 1).

Exercised through a mocked HTTP transport rather than a mocked ``call``, so the
assertions cover what actually goes on the wire: URL construction, UPID quoting
and request-body shape.
"""

from __future__ import annotations

import json
from typing import ClassVar
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

    @pytest.mark.parametrize("storage", [None, "", "local-zfs"])
    async def test_the_clone_is_always_a_full_clone_whatever_the_storage(
        self, client: ProxmoxClient, storage: str | None
    ):
        """THE STANDING GATE (#618): `full=1` is in every clone body.

        A linked clone binds the guest to its template forever - the template
        can never be deleted or moved, and the guest cannot leave the
        template's storage - which is both the owner's standing "never a linked
        clone" rule and the reason a target storage means anything at all: PVE
        only honours `storage` on a full clone. Adding the storage option must
        not become the change that quietly makes a linked clone reachable.
        """
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(f"{API}/nodes/pve1/qemu/9000/clone").mock(
                return_value=Response(200, json={"data": UPID})
            )
            await client.clone_vm(
                node="pve1", template_vmid=9000, new_vmid=105, name="web-01", storage=storage
            )

        body = json.loads(route.calls[0].request.read())
        assert body["full"] == 1

    async def test_a_storage_is_sent_only_when_one_is_given(self, client: ProxmoxClient):
        """Absent, not empty: PVE's own meaning for a missing `storage` is
        "put the disks where the template's are", which is exactly the
        behaviour every install had before #618. An empty string sent instead
        would be a value PVE has to interpret."""
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(f"{API}/nodes/pve1/qemu/9000/clone").mock(
                return_value=Response(200, json={"data": UPID})
            )
            await client.clone_vm(
                node="pve1", template_vmid=9000, new_vmid=105, name="web-01", storage="local-zfs"
            )
            await client.clone_vm(
                node="pve1", template_vmid=9000, new_vmid=106, name="web-02", storage=None
            )

        with_storage = json.loads(route.calls[0].request.read())
        without = json.loads(route.calls[1].request.read())
        assert with_storage["storage"] == "local-zfs"
        assert "storage" not in without

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


class TestCancelUnwind:
    """stop_task / delete_vm — the two calls a cancelled provision unwinds with (#452)."""

    async def test_stop_task_deletes_the_task_and_quotes_the_upid(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            route = mock.delete(url__regex=rf"{API}/nodes/pve1/tasks/.*").mock(
                return_value=Response(200, json={"data": None})
            )
            await client.stop_task("pve1", UPID)

        assert len(route.calls) == 1
        raw_path = route.calls[0].request.url.raw_path.decode()
        # Same rule as wait_for_task: an unescaped UPID splits the path into
        # extra segments and PVE 501s.
        assert ":" not in raw_path.split("/tasks/")[1]
        assert unquote(raw_path.split("/tasks/")[1]) == UPID

    async def test_stop_task_propagates_a_pve_error(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.delete(url__regex=rf"{API}/nodes/pve1/tasks/.*").mock(
                return_value=Response(500, text="no such task")
            )
            with pytest.raises(ProxmoxError):
                await client.stop_task("pve1", UPID)

    async def test_delete_vm_purges_and_takes_unreferenced_disks(self, client: ProxmoxClient):
        destroy_upid = UPID.replace("qmclone", "qmdestroy")
        with respx.mock(assert_all_called=False) as mock:
            route = mock.delete(f"{API}/nodes/pve1/qemu/105").mock(
                return_value=Response(200, json={"data": destroy_upid})
            )
            upid = await client.delete_vm("pve1", 105)

        assert upid == destroy_upid
        params = route.calls[0].request.url.params
        # Without BOTH, unwinding a half-created guest leaves the debris the
        # cancel was meant to remove: pool/job/HA references, and the disks.
        assert params["purge"] == "1"
        assert params["destroy-unreferenced-disks"] == "1"

    async def test_delete_vm_propagates_a_pve_error(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.delete(f"{API}/nodes/pve1/qemu/105").mock(
                return_value=Response(500, text="storage is busy")
            )
            with pytest.raises(ProxmoxError) as excinfo:
                await client.delete_vm("pve1", 105)

        assert "storage is busy" in str(excinfo.value)


class TestSnapshotGuestType:
    """A VMID is not a guest type (#617).

    The snapshot calls used to try /qemu/ and fall back to /lxc/ on ANY error,
    so a QEMU guest whose snapshot was refused (401) was reported as an /lxc/
    failure. The collection is resolved now, and the failure says which one it
    used and what PVE answered.
    """

    RESOURCES: ClassVar[dict] = {
        "data": [
            {"vmid": 9001, "type": "qemu", "node": "elizabeth"},
            {"vmid": 9002, "type": "lxc", "node": "elizabeth"},
        ]
    }

    async def test_stated_vm_kind_snapshots_the_qemu_collection(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            qemu = mock.post(f"{API}/nodes/elizabeth/qemu/9001/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            lxc = mock.post(f"{API}/nodes/elizabeth/lxc/9001/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            await client.snapshot("elizabeth", 9001, "hp-pre-x", guest_type="vm")

        assert qemu.called
        assert not lxc.called

    async def test_stated_lxc_kind_snapshots_the_lxc_collection(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            qemu = mock.post(f"{API}/nodes/elizabeth/qemu/9002/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            lxc = mock.post(f"{API}/nodes/elizabeth/lxc/9002/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            await client.snapshot("elizabeth", 9002, "hp-pre-x", guest_type="lxc")

        assert lxc.called
        assert not qemu.called

    async def test_unstated_kind_asks_the_cluster(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{API}/cluster/resources").mock(
                return_value=Response(200, json=self.RESOURCES)
            )
            qemu = mock.post(f"{API}/nodes/elizabeth/qemu/9001/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            lxc = mock.post(f"{API}/nodes/elizabeth/lxc/9001/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            await client.snapshot("elizabeth", 9001, "hp-pre-x")

        assert qemu.called
        assert not lxc.called

    async def test_unknown_vmid_fails_instead_of_guessing(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{API}/cluster/resources").mock(
                return_value=Response(200, json=self.RESOURCES)
            )
            qemu = mock.post(f"{API}/nodes/elizabeth/qemu/4242/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            lxc = mock.post(f"{API}/nodes/elizabeth/lxc/4242/snapshot").mock(
                return_value=Response(200, json={"data": "UPID:snap"})
            )
            with pytest.raises(ProxmoxError) as excinfo:
                await client.snapshot("elizabeth", 4242, "hp-pre-x")

        assert "4242" in str(excinfo.value)
        assert not qemu.called
        assert not lxc.called

    async def test_401_names_the_path_status_and_the_write_token(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{API}/nodes/elizabeth/qemu/9001/snapshot").mock(
                return_value=Response(401, json={"errors": "permission denied"})
            )
            with pytest.raises(ProxmoxError) as excinfo:
                await client.snapshot("elizabeth", 9001, "hp-pre-x", guest_type="vm")

        message = str(excinfo.value)
        assert "nodes/elizabeth/qemu/9001/snapshot" in message
        assert "401" in message
        assert "write token" in message
        assert "qemu" in message

    async def test_delete_snapshot_uses_the_stated_collection(self, client: ProxmoxClient):
        with respx.mock(assert_all_called=False) as mock:
            qemu = mock.delete(f"{API}/nodes/elizabeth/qemu/9001/snapshot/hp-pre-x").mock(
                return_value=Response(200, json={"data": None})
            )
            lxc = mock.delete(f"{API}/nodes/elizabeth/lxc/9001/snapshot/hp-pre-x").mock(
                return_value=Response(200, json={"data": None})
            )
            await client.delete_snapshot("elizabeth", 9001, "hp-pre-x", guest_type="vm")

        assert qemu.called
        assert not lxc.called
