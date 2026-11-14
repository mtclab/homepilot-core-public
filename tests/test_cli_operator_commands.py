"""The operator commands of `hp`, driven as an operator drives them (#remote-cli).

Each journey runs the installed ``hp`` console script as a subprocess, from an
isolated workstation logged in to ``homepilot.main:app`` (tests/e2e_fleet_server.py:
the real app over the suite's fake Proxmox - gone machines old-web/old-db/
old-cache, a friend's alice-web, an approved destroy of probe-1). Assertions are
on what the operator sees and on the estate afterwards: `hp hosts forget --gone
--yes` removes exactly the gone machines and prints them, a change proposed and
applied lands in the knowledge base, a shutdown leaves the guest stopped.

Every run goes through ``Workstation.hp``, which also asserts the token is in
neither stdout nor stderr - the secret gate for every command group.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .cli_live_server import Fleet, fleet_server, run_hp

pytestmark = pytest.mark.timeout(900)


@dataclass
class Workstation:
    fleet: Fleet
    home: Path

    def hp(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        result = run_hp(*args, home=self.home, stdin=stdin)
        assert self.fleet.token not in result.stdout + result.stderr, f"token leaked: {args}"
        return result

    def ok(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        result = self.hp(*args, stdin=stdin)
        assert result.returncode == 0, f"{args}: {result.stderr}"
        return result

    def json(self, *args: str) -> Any:
        return json.loads(self.ok(*args, "--json").stdout)


def _workstation(fleet: Fleet, home: Path) -> Workstation:
    home.mkdir(parents=True, exist_ok=True)
    login = run_hp("login", "--url", fleet.base, home=home, stdin=fleet.token)
    assert login.returncode == 0, login.stderr
    return Workstation(fleet, home)


@pytest.fixture(scope="module")
def ws(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workstation]:
    with fleet_server() as fleet:
        yield _workstation(fleet, tmp_path_factory.mktemp("operator"))


@pytest.fixture
def fresh(tmp_path: Path) -> Iterator[Workstation]:
    """Its own fleet, for journeys that change the estate others read."""
    with fleet_server() as fleet:
        yield _workstation(fleet, tmp_path / "ws")


GONE = {"old-web", "old-db", "old-cache"}


# ── hp status ────────────────────────────────────────────────────────────────


def test_status_says_what_needs_you(ws: Workstation) -> None:
    out = ws.ok("status").stdout
    assert "Needs you:" in out
    assert "3 machine(s) Proxmox no longer reports: old-cache, old-db, old-web" in out
    assert "`hp hosts forget --gone`" in out
    assert "1 approved change(s) are ready to apply" in out
    assert "Alerts firing: 0" in out
    data = ws.json("status")
    from homepilot import __version__

    assert data["updates"]["running_version"] == __version__
    assert any("no longer reports" in line for line in data["needs_you"])


# ── hp hosts ─────────────────────────────────────────────────────────────────


def test_hosts_list_filters(ws: Workstation) -> None:
    assert {h["hostname"] for h in ws.json("hosts", "list", "--gone")} == GONE
    running = {h["hostname"] for h in ws.json("hosts", "list", "--running")}
    assert {"atlas", "relay", "llm", "dns"} <= running
    assert not running & {"forge-dev", "mirror"} and not running & GONE
    raw = ws.ok("hosts", "list", "--json").stdout
    assert raw.count("\n") == 1 and len(raw) > 200, "JSON must come out as one unwrapped line"
    table = ws.ok("hosts", "list", "--gone").stdout
    assert "Gone since" in table and "old-web" in table
    assert ws.json("hosts", "show", "atlas")["hostname"] == "atlas"
    missing = ws.hp("hosts", "show", "no-such-host")
    assert missing.returncode == 1 and "not found" in missing.stderr


def test_hosts_forget_gone_removes_exactly_the_gone_machines(fresh: Workstation) -> None:
    before = {h["hostname"] for h in fresh.json("hosts", "list")}
    refused = fresh.hp("hosts", "forget", "--gone")
    assert refused.returncode == 1
    assert "Forget 3 host(s): " in refused.stderr and "Pass --yes" in refused.stderr
    for name in GONE:
        assert name in refused.stderr
    assert {h["hostname"] for h in fresh.json("hosts", "list")} == before, "refused, yet removed"

    done = fresh.ok("hosts", "forget", "--gone", "--yes")
    assert done.stdout.startswith("Forgot 3 host(s): ")
    assert set(done.stdout.split(": ", 1)[1].strip().split(", ")) == GONE
    after = {h["hostname"] for h in fresh.json("hosts", "list")}
    assert after == before - GONE, "forget --gone must remove the gone machines and nothing else"
    assert fresh.json("hosts", "list", "--gone") == []
    assert "no longer reports" not in fresh.ok("status").stdout

    again = fresh.ok("hosts", "forget", "--gone", "--yes")
    assert "No gone machines to forget." in again.stdout


def test_hosts_forget_by_name_forgets_exactly_those_and_relays_refusals(
    fresh: Workstation,
) -> None:
    unknown = fresh.hp("hosts", "forget", "old-web", "nope", "--yes")
    assert unknown.returncode == 1 and "No host named 'nope'" in unknown.stderr
    assert "old-web" in {h["hostname"] for h in fresh.json("hosts", "list")}, "half-done forget"

    mixed = fresh.hp("hosts", "forget", "old-web", "atlas", "--yes")
    assert mixed.returncode == 1, "a refused host makes the command fail"
    assert "Forgot 1 host(s): old-web" in mixed.stdout
    assert "Could not forget atlas: atlas is still reported by the hypervisor" in mixed.stderr
    names = {h["hostname"] for h in fresh.json("hosts", "list")}
    assert "old-web" not in names and {"atlas", "old-db", "old-cache"} <= names
    assert fresh.hp("hosts", "forget").returncode == 2


# ── hp changes / hp tasks / hp kb ────────────────────────────────────────────


def _start_spec(tmp_path: Path, name: str, vmid: int) -> Path:
    """A change that RUNS on the fake Proxmox: start one stopped guest (the same
    proxmox-api-sequence tests/test_e2e.py applies from the UI). JSON is YAML."""
    spec = {
        "kind": "proxmox-api-sequence",
        "intent": f"Start {name}",
        "body": (
            f"Start {name}.\n\n```yaml proxmox-api-spec\nsteps:\n  - id: start\n"
            "    method: POST\n"
            "    path: /nodes/{{ target.node }}/qemu/{{ target.vmid }}/status/start\n```\n"
        ),
        "target": {"kind": "vm", "vmid": vmid, "node": "pve1", "host": name},
        "idempotence": "declared-natural",
        "requires_snapshot": False,
    }
    path = tmp_path / f"start-{name}.yaml"
    path.write_text(json.dumps(spec))
    return path


def _propose(ws: Workstation, spec: Path) -> str:
    out = ws.ok("changes", "propose", str(spec)).stdout
    return out.split("Proposed: ", 1)[1].strip()


def _power_state(ws: Workstation, name: str) -> str:
    host = ws.json("hosts", "show", name)
    return str(
        json.loads(ws.ok("api", "inventory", "get-power-state", host["id"]).stdout)["status"]
    )


def test_changes_inbox_and_show(ws: Workstation) -> None:
    inbox = ws.json("changes", "list", "--inbox")
    ids = {c["id"] for c in inbox}
    assert any(i.endswith("destroy-probe-1-e2e813") for i in ids)
    table = ws.ok("changes", "list", "--inbox").stdout
    assert "Destroy probe-1" in table and "approved" in table
    probe = next(i for i in ids if i.endswith("destroy-probe-1-e2e813"))
    shown = ws.ok("changes", "show", probe).stdout
    assert "Destroy probe-1" in shown and "approved" in shown
    assert ws.json("changes", "show", probe)["frontmatter"]["status"] == "approved"


def test_propose_then_approve_and_apply_runs_it(ws: Workstation, tmp_path: Path) -> None:
    assert _power_state(ws, "llm-test") == "stopped"
    change = _propose(ws, _start_spec(tmp_path, "llm-test", 121))
    waiting = {c["id"]: c["status"] for c in ws.json("changes", "list", "--inbox")}
    assert waiting.get(change) == "proposed"

    applied = ws.ok("changes", "approve-and-apply", change)
    assert f"Approved and applied: {change}" in applied.stdout
    assert "succeeded" in applied.stderr, "the live status lines go to stderr"
    assert ws.json("changes", "show", change)["frontmatter"]["status"] == "applied"
    assert _power_state(ws, "llm-test") == "running", "applied, yet the guest never started"
    inbox = ws.json("changes", "list", "--inbox")
    assert change not in {c["id"] for c in inbox}, "an applied change is not waiting"
    assert {c["status"] for c in inbox} <= {"proposed", "approved"}
    assert change in {c["id"] for c in ws.json("changes", "list")}

    runs = ws.json("tasks", "list", "--change", change)
    assert runs and runs[0]["status"] == "succeeded"
    followed = ws.ok("tasks", "follow", runs[0]["id"])
    assert "succeeded" in followed.stdout
    assert change in ws.ok("tasks", "list").stdout


def test_reject_and_withdraw_run_nothing(ws: Workstation, tmp_path: Path) -> None:
    rejected = _propose(ws, _start_spec(tmp_path, "forge-dev", 103))
    ws.ok("changes", "reject", rejected, "--reason", "not needed")
    assert ws.json("changes", "show", rejected)["frontmatter"]["status"] == "rejected"

    withdrawn = _propose(ws, _start_spec(tmp_path, "batch-2", 142))
    ws.ok("changes", "approve", withdrawn)
    ws.ok("changes", "withdraw", withdrawn, "--reason", "changed my mind")
    assert ws.json("changes", "show", withdrawn)["frontmatter"]["status"] == "withdrawn"
    assert ws.hp("changes", "apply", withdrawn).returncode == 1, "a withdrawn change ran"
    assert _power_state(ws, "forge-dev") == "stopped"
    assert _power_state(ws, "batch-2") == "stopped"


def test_apply_no_wait_starts_and_hands_back_the_task(ws: Workstation, tmp_path: Path) -> None:
    change = _propose(ws, _start_spec(tmp_path, "batch-1", 141))
    ws.ok("changes", "approve", change)
    started = ws.ok("changes", "apply", change, "--no-wait").stdout
    task = started.split("Started task ", 1)[1].split(" ", 1)[0]
    assert ws.ok("tasks", "follow", task).returncode == 0
    assert ws.json("changes", "show", change)["frontmatter"]["status"] == "applied"
    assert _power_state(ws, "batch-1") == "running"


def test_tasks_cancel_asks_first(ws: Workstation) -> None:
    refused = ws.hp("tasks", "cancel", "no-such-task")
    assert refused.returncode == 1 and "Pass --yes" in refused.stderr
    missing = ws.hp("tasks", "cancel", "no-such-task", "--yes")
    assert missing.returncode == 1 and "refused (404)" in missing.stderr


def test_kb_add_list_show_search(ws: Workstation) -> None:
    added = ws.ok(
        "kb", "add", "-", "--target", "relay", "--kind", "decision",
        stdin="# relay\n\nWe keep relay on the infra VLAN: lapwing.\n",
    )  # fmt: skip
    assert "Recorded decision for relay: KB entry" in added.stdout
    entries = ws.json("kb", "list", "--target", "relay")
    entry = next(e for e in entries if "lapwing" in json.dumps(e) or e.get("kind") == "decision")
    shown = ws.ok("kb", "show", str(entry["id"])).stdout
    assert "lapwing" in shown
    assert any("lapwing" in json.dumps(r) for r in ws.json("kb", "search", "lapwing"))
    empty = ws.hp("kb", "add", "-", stdin="   \n")
    assert empty.returncode == 2


# ── hp alerts / hp channels / hp guests ──────────────────────────────────────


def test_alerts_channels_and_guests(ws: Workstation) -> None:
    assert ws.ok("alerts").stdout.strip() == "No alert is firing."
    assert ws.json("alerts") == []
    assert "No notification channels." in ws.ok("channels", "list").stdout
    created = ws.ok(
        "api", "notifications", "create-channel",
        "--name", "phone", "--kind", "ntfy", "--url", "https://ntfy.example.com/hp-test",
    )  # fmt: skip
    channel = str(json.loads(created.stdout)["id"])
    listed = ws.ok("channels", "list").stdout
    assert "phone" in listed and "hp-test" not in listed, "the endpoint must be redacted"
    tested = ws.hp("channels", "test", channel)
    assert tested.returncode == 1 and "did NOT deliver" in tested.stderr

    guests = ws.ok("guests", "list").stdout
    assert "alice: no budget set" in guests
    assert "alice-web (VMID 130 on pve1): stopped" in guests
    assert ws.json("guests", "list")[0]["cn"] == "alice"


# ── hp power ─────────────────────────────────────────────────────────────────


def test_power_guards_and_outcome(fresh: Workstation) -> None:
    def pve_status(name: str) -> str:
        host = fresh.json("hosts", "show", name)
        state = fresh.ok("api", "inventory", "get-power-state", host["id"]).stdout
        return str(json.loads(state)["status"])

    stop = fresh.hp("power", "atlas", "stop")
    assert stop.returncode == 1 and "--confirm-name atlas" in stop.stderr
    wrong = fresh.hp("power", "atlas", "stop", "--confirm-name", "relay")
    assert wrong.returncode == 1 and "left alone" in wrong.stderr
    shutdown = fresh.hp("power", "atlas", "shutdown")
    assert shutdown.returncode == 1 and "Pass --yes" in shutdown.stderr
    assert pve_status("atlas") == "running", "a refused power action reached the guest"

    done = fresh.ok("power", "atlas", "shutdown", "--yes")
    assert "shutdown atlas: ok" in done.stdout
    assert pve_status("atlas") == "stopped"
    started = fresh.ok("power", "forge-dev", "start")
    assert "start forge-dev: ok" in started.stdout
    assert pve_status("forge-dev") == "running"
    unavailable = fresh.hp("power", "forge-dev", "start")
    assert unavailable.returncode == 1 and "Cannot start forge-dev" in unavailable.stderr
    usage = fresh.hp("power", "atlas", "explode")
    assert usage.returncode == 2


# ── hp agents ────────────────────────────────────────────────────────────────


def test_agents_say_why_not_without_a_hub(ws: Workstation) -> None:
    """The fleet server runs without the agent hub, so the refusals are what an
    operator meets: the server's own reason, exit 1."""
    upgrade = ws.hp("agents", "upgrade", "--all")
    assert upgrade.returncode == 1 and "Agent hub not enabled" in upgrade.stderr
    install = ws.hp("agents", "install", "atlas")
    assert install.returncode == 1
    assert "Cannot install on atlas: The agent hub is not running" in install.stderr
    usage = ws.hp("agents", "upgrade")
    assert usage.returncode == 2


def test_agents_upgrade_goes_one_host_at_a_time_and_stops_at_the_first_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loop itself, with the API answered in-process: the hub-less fleet
    server has no agents behind to upgrade."""
    import typer

    from homepilot.cli import ops

    behind = [
        {
            "hostname": h,
            "host_id": f"id-{h}",
            "agent_version": "3.10.0",
            "control_version": "3.12.0",
        }
        for h in ("alpha", "bravo", "charlie")
    ]
    monkeypatch.setattr(ops, "api", lambda *a, **k: behind)
    attempted: list[str] = []

    def install(host_id: str, label: str, upgrade: bool) -> int:
        attempted.append(host_id)
        return 1 if host_id == "id-bravo" else 0

    monkeypatch.setattr(ops, "_install", install)
    with pytest.raises(typer.Exit) as stopped:
        ops.agents_upgrade([], all_behind=True)
    assert stopped.value.exit_code == 1
    assert attempted == ["id-alpha", "id-bravo"], "it must stop at bravo and never try charlie"
    err = capsys.readouterr().err
    assert "[1/3] alpha" in err and "[2/3] bravo" in err
    assert "Stopped at bravo; 1 agent(s) not attempted." in err

    attempted.clear()
    monkeypatch.setattr(
        ops, "_install", lambda host_id, label, upgrade: attempted.append(host_id) or 0
    )
    ops.agents_upgrade(["charlie", "alpha"], all_behind=False)
    assert attempted == ["id-charlie", "id-alpha"]
    with pytest.raises(typer.Exit):
        ops.agents_upgrade(["delta"], all_behind=False)


# ── Local mode is unchanged ──────────────────────────────────────────────────


def test_old_group_names_still_work(ws: Workstation) -> None:
    assert {h["hostname"] for h in json.loads(ws.ok("inventory", "list", "-o", "json").stdout)}
    assert ws.ok("artifacts", "list", "--inbox", "--json").stdout.startswith("[")
    hidden = ws.ok("--help").stdout
    assert " hosts " in hidden and " inventory " not in hidden
