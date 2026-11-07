"""THE GATE for #712: every drift claim says what was ESTABLISHED, nothing more or less.

Four claims were wrong, found in the 2026-09 audit:

* **A converged artifact could never read in spec.** A step whose precheck was
  asked and answered "already converged" was filed under `skipped_steps`,
  beside the steps nobody could ask; with every step "skipped" the verdict was
  `unknown / nothing_evaluated` - for proxmox-api and http-sequence alike.
* **A 404 was "could not look".** The executor reads a 4xx precheck as an answer
  about the resource (`status_code == 200` says "no, it is not there"), while
  drift read it as a failed probe - so a deleted resource that apply would
  re-create read `unknown`, and real drift was invisible.
* **A cycle in which every check errored reported success**, keeping the
  self-check's reconciler line green, while each artifact kept its last,
  stale verdict because nothing was written on error. The no-executor branch
  did the same.
* **The inventory journal re-reported the same absent hosts every cycle** -
  every host the hypervisor does not report (a manual NAS included) - while the
  sweep had marked none.

The Proxmox answers come from a REAL ProxmoxClient over a mocked transport, so
a 404 reaches the verifier as the ProxmoxError the adapter really raises.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.service import InventoryService
from homepilot.reconciler.drift import DriftReconciler
from homepilot.reconciler.inventory import InventoryReconciler
from homepilot.reconciler.verify import _verify_http_sequence, _verify_proxmox_api

pytestmark = pytest.mark.asyncio

BASE = "https://pve.example:8006"
API = f"{BASE}/api2/json"
STATUS = f"{API}/nodes/pve1/qemu/150/status/current"

PVE_BODY = """
```yaml proxmox-api-spec
steps:
  - id: create-vm-150
    method: POST
    path: /nodes/pve1/qemu
    body: {vmid: 150}
    precheck:
      method: GET
      path: /nodes/pve1/qemu/150/status/current
      skip_if: "response.status_code == 200"
```
"""
FM = {"id": "a", "target": {"kind": "node", "node": "pve1", "name": "pve1"}}


def _executor() -> SimpleNamespace:
    return SimpleNamespace(proxmox=ProxmoxClient(base_url=BASE, token="root@pam!t=uuid"))


class TestProxmoxApiDriftSaysWhatItEstablished:
    async def test_a_converged_artifact_reads_in_spec(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(STATUS).mock(return_value=Response(200, json={"data": {"status": "running"}}))
            result = await _verify_proxmox_api("a", FM, PVE_BODY, _executor())

        assert str(result.state) == "in_spec", result.details
        assert result.details["in_spec_steps"] == ["create-vm-150"]
        assert result.details["skipped_steps"] == []

    async def test_a_deleted_resource_is_drift_as_apply_would_read_it(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(STATUS).mock(return_value=Response(404, text="no such VM"))
            result = await _verify_proxmox_api("a", FM, PVE_BODY, _executor())

        assert str(result.state) == "drifted", (
            f"VM 150 is gone and apply would re-create it, yet drift said {result.state}"
        )

    async def test_a_server_error_is_still_could_not_look(self):
        with respx.mock(assert_all_called=False) as mock:
            mock.get(STATUS).mock(return_value=Response(500, text="proxy loop"))
            result = await _verify_proxmox_api("a", FM, PVE_BODY, _executor())

        assert str(result.state) == "unknown"
        # Asked and unanswered is a failed probe, not "nothing to compare": the
        # Overview reads `nothing_evaluated` as describe-only (fine).
        assert result.details["reason"] == "probe_failed"


HTTP_BODY = """
```yaml http-spec
steps:
  - id: ensure-dns-record
    name: dns
    method: POST
    path: /records
    precheck:
      method: GET
      path: /records/www
      skip_if: "response.status_code == 200"
```
"""


class TestHttpSequenceDriftSaysWhatItEstablished:
    def _executor(self) -> SimpleNamespace:
        vault = MagicMock()
        vault.get_secret = AsyncMock(
            return_value={"base_url": "https://dns.example.com", "headers": {}}
        )
        return SimpleNamespace(vault=vault)

    async def _verify(self, status: int) -> Any:
        executor = self._executor()
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://dns.example.com/records/www").mock(
                return_value=Response(status, json={})
            )
            return await _verify_http_sequence("h", {"id": "h", "target": {}}, HTTP_BODY, executor)

    async def test_a_converged_artifact_reads_in_spec(self):
        result = await self._verify(200)
        assert str(result.state) == "in_spec", result.details

    async def test_a_missing_resource_is_drift(self):
        assert str((await self._verify(404)).state) == "drifted"

    async def test_a_server_error_is_not_an_answer(self):
        assert str((await self._verify(503)).state) == "unknown"


class TestADriftCycleThatCheckedNothingIsNotASuccess:
    async def test_every_check_erroring_is_a_failed_cycle_and_verdicts_go_unknown(self):
        store = MagicMock()
        store.list.return_value = [
            {"id": f"a{i}", "kind": "proxmox-api-sequence"} for i in range(3)
        ]
        repo = AsyncMock()
        rec = DriftReconciler(store=store, repo=repo, executor=MagicMock(), inter_check_delay=0)
        rec.check_single = AsyncMock(side_effect=RuntimeError("PVE unreachable"))

        result = await rec.run()

        assert not result.success, f"0 checked, 3 errors, reported success: {result.details}"
        written = {
            c.kwargs["artifact_id"]: c.kwargs["state"]
            for c in repo.upsert_drift_check.await_args_list
        }
        assert written == {"a0": "unknown", "a1": "unknown", "a2": "unknown"}, (
            "an errored check left the previous verdict standing"
        )

    async def test_some_checks_succeeding_is_still_a_cycle_that_ran(self):
        store = MagicMock()
        store.list.return_value = [
            {"id": f"a{i}", "kind": "proxmox-api-sequence"} for i in range(2)
        ]
        rec = DriftReconciler(
            store=store, repo=AsyncMock(), executor=MagicMock(), inter_check_delay=0
        )
        ok = SimpleNamespace(drifted=False, verification_log="")
        rec.check_single = AsyncMock(side_effect=[ok, RuntimeError("one flaked")])

        assert (await rec.run()).success

    async def test_no_executor_is_not_a_healthy_loop(self):
        store = MagicMock()
        store.list.return_value = [{"id": "a0", "kind": "proxmox-api-sequence"}]
        repo = AsyncMock()
        rec = DriftReconciler(store=store, repo=repo, executor=None, inter_check_delay=0)

        result = await rec.run()

        assert not result.success
        assert repo.upsert_drift_check.await_args.kwargs["state"] == "unknown"


class FakeInventoryPVE:
    """Just enough of the PVE listing for a full inventory sweep."""

    def __init__(self, guests: list[dict[str, Any]]) -> None:
        self.guests = guests

    async def read(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/nodes":
            return {"data": [{"node": "pve1", "status": "online", "ip": "10.0.0.2"}]}
        if path == "/nodes/pve1/qemu":
            return {"data": list(self.guests)}
        if path.endswith("/config"):
            return {"data": {}}
        return {"data": []}


async def test_the_inventory_journal_names_only_the_hosts_marked_this_cycle(tmp_path: Path):
    db = Database(str(tmp_path / "j.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    try:
        await repo.create_host(hostname="nas", host_type="physical", source="manual")
        pve = FakeInventoryPVE([{"vmid": 101, "name": "web", "status": "running"}])
        rec = InventoryReconciler(InventoryService(repo, pve), repo)
        await rec.run()
        pve.guests = []  # web is destroyed
        gone = await rec.run()
        again = await rec.run()

        assert gone.details.get("complete") is True, gone.details
        assert gone.details["absent_hosts"] == 1, gone.details
        web = [h for h in await repo.list_hosts() if h["hostname"] == "web"]
        assert gone.details["absent_host_ids"] == [web[0]["id"]]
        assert again.details["absent_hosts"] == 0, (
            f"the journal re-reported absent hosts nothing marked: {again.details}"
        )
    finally:
        await db.close()
