"""The Hosts list's row menu, server side (UI slice 5, 3.10.0).

What the fleet list needs from the backend to power a guest from its row and
show what happened without waiting for the next sync:

* after an operator's Start the ROW says running - the list read the menu's
  journey re-reads is the server's own, written from Proxmox's answer after the
  task settled (``PowerService._record_state``);
* a start whose outcome Proxmox did not confirm leaves the row as it was: an
  unread state is never written as a guess (#774);
* the row carries a Console door for a guest that can have one (noVNC for a
  VM, xterm.js for a container) and none for a template, a gone guest, a node
  or a hand-added device.

Teeth (evidence in the PR):
  * drop the ``_record_state`` call in ``PowerService.execute`` ->
    ``test_an_operator_start_leaves_the_row_running`` fails;
  * write the state the ACTION promised (start -> running) instead of the one
    Proxmox answered -> ``test_an_unconfirmed_start_leaves_the_row_as_it_was``
    fails;
  * drop ``console_url`` from the list route ->
    ``test_the_list_carries_a_console_door_for_guests_only`` fails.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from homepilot.inventory.fleet_model import proxmox_console_url
from homepilot.inventory.power import PowerService
from tests.test_power_controls import (  # noqa: F401 - fixtures
    _id,
    _sync,
    http,
    lxc_net,
    qemu_net,
    world,
)

pytestmark = pytest.mark.asyncio


def _row(listing: dict, name: str) -> dict:
    (row,) = [h for h in listing["items"] if h["hostname"] == name]
    return row


class TestTheRowFollowsTheAction:
    async def test_an_operator_start_leaves_the_row_running(self, http):  # noqa: F811
        client, _token, _ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "web-01")
        await repo.update_host(hid, ip_address="192.0.2.10")
        before = _row((await client.get("/inventory")).json(), "web-01")
        assert before["pve_status"] == "stopped"

        resp = await client.post(f"/inventory/{hid}/power", json={"action": "start"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "ok"

        # No sync in between: the list the row re-reads already says so.
        after = _row((await client.get("/inventory")).json(), "web-01")
        assert after["pve_status"] == "running"
        assert after["status"] == "online"

    async def test_an_unconfirmed_start_leaves_the_row_as_it_was(self, world):  # noqa: F811
        repo, pve, svc, _power = world
        power = PowerService(repo, pve, poll_interval=0, start_stop_wait_s=0)
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"), hang=True)
        await _sync(svc)
        hid = await _id(repo, "web-01")

        result = await power.execute(hid, "start", actor="o", source="api")

        assert result.outcome == "unknown"
        assert (await repo.get_host(hid))["pve_status"] == "stopped"

    async def test_a_shutdown_leaves_the_row_stopped(self, world):  # noqa: F811
        repo, pve, svc, power = world
        pve.add(101, "web-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "web-01")

        result = await power.execute(hid, "shutdown", actor="o", source="api")

        assert result.outcome == "ok"
        row = await repo.get_host(hid)
        assert row["pve_status"] == "stopped"
        assert row["status"] == "offline"


class TestTheConsoleDoor:
    async def test_the_list_carries_a_console_door_for_guests_only(self, http):  # noqa: F811
        client, _token, _ctx, _repo, pve, svc = http
        pve.add(101, "web-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(
            102, "dns-ct", "running", lxc_net("192.0.2.11", "BC:24:11:00:00:02"), guest_type="lxc"
        )
        await _sync(svc)
        added = await client.post(
            "/inventory", json={"hostname": "nas01", "ip_address": "192.0.2.50"}
        )
        assert added.status_code in (200, 201), added.text

        listing = (await client.get("/inventory")).json()

        vm = _row(listing, "web-01")["console_url"]
        assert vm.startswith("https://pve.example:8006/?console=kvm&novnc=1&vmid=101")
        assert "&vmname=web-01&node=pve1" in vm
        ct = _row(listing, "dns-ct")["console_url"]
        assert ct.startswith("https://pve.example:8006/?console=lxc&xtermjs=1&vmid=102")
        assert _row(listing, "nas01")["console_url"] is None
        assert _row(listing, "pve1")["console_url"] is None

    async def test_no_console_for_a_template_or_a_gone_guest(self):
        pve = SimpleNamespace(api_host="pve.example", api_port=8006)
        guest = {"host_type": "qemu", "proxmox_id": 101, "node": "pve1", "hostname": "web-01"}
        assert proxmox_console_url(pve, guest) is not None
        assert proxmox_console_url(pve, {**guest, "is_template": 1}) is None
        assert proxmox_console_url(pve, {**guest, "absent_since": "2026-11-01"}) is None
        assert proxmox_console_url(pve, {**guest, "node": None}) is None
        assert proxmox_console_url(None, guest) is None
