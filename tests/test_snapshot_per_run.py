"""THE GATE for #709: every run of an artifact takes its OWN pre-apply snapshot.

The defect: the pre-apply snapshot was named per ARTIFACT (`hp-pre-<id>`) and
nothing ever deletes it. So the second run of the same vm/lxc artifact - a
replay, or a re-apply after a failed apply was edited and re-approved - asked
PVE for a snapshot name the first run had already taken. PVE refuses a duplicate
snapname, the executor (rightly) refuses to mutate a guest with no rollback
point, and the run died before step 1. On replay it was worse: the replay's
error handler called `mark_failed` on an APPLIED artifact, the lifecycle
refused `applied -> failed`, and THAT error replaced the real reason on the
task.

The fake PVE here refuses a duplicate snapshot name exactly as PVE does, so the
assertions are about outcomes: the second run SUCCEEDS, the guest ends up with
one snapshot per run, and a replay that fails leaves an applied artifact
applied with the real reason on the task.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from homepilot.adapters.proxmox import ProxmoxError
from homepilot.auth.principal import Principal
from homepilot.executor.orchestrator import (
    PVE_SNAPNAME_MAX,
    ExecutionResult,
    snapshot_name_for,
    snapshot_run_tag,
)
from tests.test_one_apply_engine import _spec, engine  # noqa: F401 - the real runner fixture

pytestmark = pytest.mark.asyncio

# PVE's `pve-configid` format for snapname: a letter, then letters, digits, _ -.
_PVE_SNAPNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]+$")
VM = {"kind": "vm", "vmid": 100, "node": "pve1", "name": "g"}


class SnapPVE:
    """PVE's snapshot endpoint, holding real state: a name is taken once per guest."""

    def __init__(self) -> None:
        self.snaps: list[tuple[int, str]] = []

    async def snapshot(self, node, vmid, name, guest_type=None):
        if (vmid, name) in self.snaps:
            raise ProxmoxError(
                "POST",
                f"nodes/{node}/qemu/{vmid}/snapshot",
                500,
                f"snapshot name '{name}' already used",
            )
        if len(name) > PVE_SNAPNAME_MAX or not _PVE_SNAPNAME.match(name):
            raise ProxmoxError("POST", f"nodes/{node}/qemu/{vmid}/snapshot", 400, "bad snapname")
        self.snaps.append((vmid, name))
        return {"data": None}


@pytest.fixture
def clock(monkeypatch):
    """Each run starts a second after the last: a real replay is never in the
    same second as the apply it follows, and the name must not depend on luck."""
    ticks = iter(range(10_000))

    def tag(now=None):
        return snapshot_run_tag(datetime(2026, 9, 26, 12, 0, next(ticks) % 60, tzinfo=UTC))

    monkeypatch.setattr("homepilot.executor.orchestrator.snapshot_run_tag", tag)


async def test_a_replay_of_an_applied_artifact_takes_its_own_snapshot(engine, clock):  # noqa: F811
    runner, store, lifecycle, executor = engine
    pve = SnapPVE()
    executor.proxmox = pve
    aid = "2026-09-26-snap-reuse"
    await lifecycle.propose(_spec(aid, target=VM))
    await lifecycle.approve(aid, Principal.operator("operator"))

    first = await runner.await_task((await runner.start_apply(aid, "operator"))["task_id"], 10)
    second = await runner.await_task((await runner.start_replay(aid))["task_id"], 10)

    assert first["status"] == "succeeded", first["error"]
    assert second["status"] == "succeeded", f"the replay was refused: {second['error']}"
    names = [n for v, n in pve.snaps if v == 100]
    assert len(names) == 2 and len(set(names)) == 2, names
    assert store.read(aid)[0]["status"] == "applied"


async def test_a_reapply_after_a_fixed_failure_takes_its_own_snapshot(engine, clock):  # noqa: F811
    runner, _store, lifecycle, executor = engine
    executor.proxmox = SnapPVE()
    aid = "2026-09-26-snap-reapply"
    await lifecycle.propose(_spec(aid, target=VM))
    await lifecycle.approve(aid, Principal.operator("operator"))
    executor._dispatch.return_value = ExecutionResult(
        success=False, execution_log="step 2 failed", failure_reason="boom"
    )
    await runner.await_task((await runner.start_apply(aid, "operator"))["task_id"], 10)
    await lifecycle.edit(aid)
    await lifecycle.approve(aid, Principal.operator("operator"))
    executor._dispatch.return_value = ExecutionResult(success=True, execution_log="ok")

    again = await runner.await_task((await runner.start_apply(aid, "operator"))["task_id"], 10)

    assert again["status"] == "succeeded", (
        f"re-apply after a fixed failure refused: {again['error']}"
    )


async def test_a_replay_that_raises_keeps_the_real_reason_and_the_artifact_applied(
    engine,  # noqa: F811
    clock,
):
    runner, store, lifecycle, executor = engine
    executor.proxmox = SnapPVE()
    aid = "2026-09-26-replay-raises"
    await lifecycle.propose(_spec(aid, target=VM))
    await lifecycle.approve(aid, Principal.operator("operator"))
    await runner.await_task((await runner.start_apply(aid, "operator"))["task_id"], 10)
    executor._dispatch.side_effect = RuntimeError("the guest agent went away")

    replay = await runner.await_task((await runner.start_replay(aid))["task_id"], 10)

    assert replay["status"] == "failed"
    assert "the guest agent went away" in (replay["error"] or "")
    assert "Invalid transition" not in (replay["error"] or "")
    assert store.read(aid)[0]["status"] == "applied"


@pytest.mark.parametrize(
    "artifact_id",
    ["2026-08-29-x", "2026-08-29-rm101", "2026-08-28-cancel-timing-probe-cleanup-long-name-test"],
)
async def test_a_per_run_name_is_one_pve_accepts_and_differs_per_run(artifact_id: str):
    first = snapshot_name_for(artifact_id, snapshot_run_tag(datetime(2026, 9, 26, 12, 0, 0)))
    second = snapshot_name_for(artifact_id, snapshot_run_tag(datetime(2026, 9, 26, 12, 0, 1)))
    for name in (first, second):
        assert len(name) <= PVE_SNAPNAME_MAX
        assert _PVE_SNAPNAME.match(name), name
        assert name.startswith("hp-pre-")
    assert first != second
