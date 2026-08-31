"""The tailnet join, run for real against a shell (#628, #642).

3.6.12 shipped an install-then-join that had never been run against a guest. The
first live run failed 28 seconds in on the first real one it met, and the whole
suite was green - because every fake in it answered `agent_run` with a tuple
somebody had typed. A fake that decides the answer cannot discover that the
question was wrong.

So the guest here is `tests/guest_shell.ShellGuest`: a real `/bin/sh`, a PATH of
fake binaries the test composes, and the ACTUAL scripts from
`homepilot.provision.service`. Every exit code in these tests is one `sh`
computed.

The gates, each with the defect it forbids and the revert that makes it fail:

* ``TestTheInstallerCannotSucceedWithoutInstalling`` - `curl … | sh` cannot
  fail. Revert `_TAILSCALE_INSTALL_SCRIPT` to the 3.6.12 one-liner and
  `test_a_download_that_fails_is_not_an_install` fails: the pipeline exits 0 and
  the join reports "joined".
* ``test_an_installer_that_installs_nothing_is_not_a_success`` - drop the
  trailing `command -v tailscale || exit 93` and it fails: an installer that
  exits 0 having done nothing is reported as tailscale being present.
* ``test_the_fetcher_is_whatever_the_guest_has`` / ``…_no_fetcher_…`` - hardcode
  `curl` back and the wget/python3/none cases fail.
* ``TestAFailureSaysWhy`` - blank any `detail` and the matching case fails.
* ``TestTheJoinWaitsForTheGuestAgent`` - delete `_wait_for_agent` and
  `test_a_slow_agent_is_waited_for` fails: the join gives up on a guest that was
  about to answer.
* ``TestNothingReadIsNotAVerdict`` - report FAILED instead of UNKNOWN on the
  paths that established nothing and each case fails.
* ``TestTheKeyStaysOutOfEverything`` - put the key on the argv and it fails.
* ``TestOneJoinPerGuest`` - drop `_reserve_join` and the second join is accepted,
  which is two shells fighting over one staged key file.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.service import (
    ProvisionService,
    TailnetJoinConflictError,
    TailnetOutcome,
)
from homepilot.tasks.repository import TaskRepository
from tests.guest_shell import KEY_PATH, ShellGuest

KEY = "tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk"
FRESH_KEY = "tskey-auth-m4Zz9QWERT-1aSdFgHjKlZxCvBnM"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def proxmox() -> AsyncMock:
    return AsyncMock(spec=ProxmoxClient)


@pytest_asyncio.fixture
async def service(db: Database, proxmox: AsyncMock) -> ProvisionService:
    return ProvisionService(
        proxmox=proxmox,
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=2.0,
        ip_wait_s=0.0,
        ip_interval=0.01,
        agent_wait_s=0.5,
        agent_interval=0.01,
        cloud_init_wait_s=2.0,
        tailscale_timeout_s=5.0,
        tailscale_install_timeout_s=5.0,
    )


async def _join(service: ProvisionService, guest: ShellGuest, key: str = KEY) -> tuple[str, str]:
    guest.bind(service.proxmox)
    return await service.join_tailnet(node="pve1", vmid=105, hostname="web-01", key=key)


class TestTheInstallerCannotSucceedWithoutInstalling:
    """#628 - the load-bearing defect: an install that failed, reported as done.

    WHAT THIS FORBIDS: taking any signal short of "the binary is there" as
    evidence that tailscale is installed. `curl … | sh` was the worst case (a
    pipeline's status is its last command's, so a failed download exits 0), but
    an installer that exits 0 on an unrecognised distribution is the same
    mistake wearing different clothes (#642).
    """

    async def test_a_download_that_fails_is_not_an_install(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, fetch_ok=False, has_getent=True, dns_ok=True)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED, (
            "a failed download was reported as a successful install; the pipeline "
            "form of this script cannot fail"
        )
        assert "download" in detail.lower()
        assert not any("tailscale up" in s for s in guest.scripts), (
            "the join ran `tailscale up` at a guest that never got tailscale"
        )

    async def test_an_installer_that_installs_nothing_is_not_a_success(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, installer_installs=False)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "still not on the guest's PATH" in detail
        assert not any("tailscale up" in s for s in guest.scripts)

    async def test_an_installer_that_exits_nonzero_is_reported_with_its_code(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, installer_rc=42)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "42" in detail

    async def test_an_install_that_works_leads_to_a_join(self, service: ProvisionService, tmp_path):
        guest = ShellGuest(tmp_path, has_tailscale=False)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.JOINED, detail
        installed = next(i for i, s in enumerate(guest.scripts) if "install.sh" in s)
        joined = next(i for i, s in enumerate(guest.scripts) if "tailscale up" in s)
        assert installed < joined, "installing after the join is no use to anyone"

    async def test_a_guest_that_already_has_tailscale_is_not_reinstalled(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, has_tailscale=True)
        outcome, _detail = await _join(service, guest)

        assert outcome == TailnetOutcome.JOINED
        assert not any("install.sh" in s for s in guest.scripts)


class TestTheFetcherIsWhateverTheGuestHas:
    """#628 - the installer hard-required curl, and a cloud image need not have one.

    WHAT THIS FORBIDS: assuming one fetcher. The set of images that ship
    qemu-guest-agent (without which none of this runs at all) is not the set of
    images that ship curl.
    """

    @pytest.mark.parametrize("fetcher", ["curl", "wget", "python3"])
    async def test_any_of_the_three_gets_the_installer(
        self, service: ProvisionService, tmp_path, fetcher: str
    ):
        guest = ShellGuest(tmp_path, fetcher=fetcher)
        outcome, detail = await _join(service, guest)
        assert outcome == TailnetOutcome.JOINED, f"{fetcher}: {detail}"

    async def test_no_fetcher_at_all_is_named_not_guessed(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, fetcher=None)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "curl, wget or python3" in detail


class TestAFailureSaysWhy:
    """#628 - the first live failure recorded `tailnet: "failed"` and nothing else.

    WHAT THIS FORBIDS: an outcome with no reason. "Your key was already used" is
    actionable; "failed" sent an operator to rebuild a guest to find out what had
    happened.
    """

    async def test_a_refused_key_carries_tailscales_own_words(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(
            tmp_path,
            has_tailscale=True,
            tailscale_up_rc=1,
            tailscale_up_stderr="backend error: invalid key: unknown key",
        )
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "invalid key: unknown key" in detail

    async def test_dns_is_named_separately_from_a_dead_route(
        self, service: ProvisionService, tmp_path
    ):
        """The static-IP work defaults its nameserver to empty (#642's neighbour).

        A guest with an address and no resolver must not be reported as "no
        route out": the fix is a nameserver, and the message has to point there.
        """
        guest = ShellGuest(tmp_path, fetch_ok=False, has_getent=True, dns_ok=False)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "resolve" in detail and "DNS" in detail

    async def test_a_guest_with_no_getent_is_not_accused_of_a_dns_fault(
        self, service: ProvisionService, tmp_path
    ):
        """#642 - the lookup we could not run is not evidence of anything."""
        guest = ShellGuest(tmp_path, fetch_ok=False, has_getent=False)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "DNS" not in detail
        assert "Name resolution was not shown to be the problem" in detail

    async def test_the_fetchers_own_error_reaches_the_reason(
        self, service: ProvisionService, tmp_path
    ):
        """WHICH stage failed is ours to say; WHY is the guest's.

        WHAT THIS FORBIDS: swallowing the fetcher's stderr behind a tidy
        sentence. "Connection timed out" and "SSL certificate problem" are the
        same exit code and send an operator to two different places - and this
        gate exists because a live dev run reported "no route out" while the
        curl behind it had never been quoted.
        """
        guest = ShellGuest(
            tmp_path, fetch_ok=False, fetch_error="curl: (60) SSL certificate problem"
        )
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "SSL certificate problem" in detail

    async def test_install_switched_off_says_which_setting(
        self, service: ProvisionService, tmp_path
    ):
        service.tailscale_install = False
        guest = ShellGuest(tmp_path, has_tailscale=False)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert "provision_tailscale_install" in detail
        assert not any("install.sh" in s for s in guest.scripts)


class TestTheJoinWaitsForTheGuestAgent:
    """#628 - the live failure, exactly: the join fired before the guest could answer.

    WHAT THIS FORBIDS: treating the first refused exec as the guest's answer. The
    machine had booted, an IP had come back, and `command -v tailscale` was
    refused - and the provision recorded a tailnet failure 28 seconds in about a
    guest that was perfectly capable of joining.
    """

    async def test_a_slow_agent_is_waited_for(self, service: ProvisionService, tmp_path):
        guest = ShellGuest(tmp_path, has_tailscale=True, agent_ready_after=5)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.JOINED, detail
        assert guest.pings > 5, "the join did not wait for the agent at all"

    async def test_an_agent_that_never_answers_is_reported_as_unknown(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, agent_ready_after=10_000)
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.UNKNOWN
        assert "qemu-guest-agent" in detail
        assert guest.scripts == [], "commands were sent to a guest that never answered"


class TestNothingReadIsNotAVerdict:
    """#642 - "I could not look" must not be reported as "I looked and it says no".

    WHAT THIS FORBIDS: a confident FAILED off a read that never happened. It
    matters to the person holding the key: FAILED means "your key was refused,
    get a new one", UNKNOWN means "a new key will not help, something else is
    wrong". Getting that backwards burns a fresh key on a problem it cannot fix.
    """

    async def test_an_agent_that_refuses_exec_is_unknown_not_failed(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(
            tmp_path, exec_error="The command guest-exec has been disabled for this instance"
        )
        outcome, detail = await _join(service, guest)

        assert outcome == TailnetOutcome.UNKNOWN, (
            "a guest agent that refused to run anything was reported as a refused key"
        )
        assert "guest-exec" in detail

    async def test_a_join_that_times_out_is_unknown_because_it_may_yet_succeed(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, has_tailscale=True)
        real_run = guest.agent_run

        async def run(node, vmid, script, **kwargs):
            if "tailscale up" in script:
                raise TimeoutError("the guest command did not finish within 5s")
            return await real_run(node, vmid, script, **kwargs)

        service.proxmox.agent_run = run
        service.proxmox.agent_ping = guest.agent_ping
        service.proxmox.agent_write_file = guest.agent_write_file

        outcome, detail = await service.join_tailnet(
            node="pve1", vmid=105, hostname="web-01", key=KEY
        )

        assert outcome == TailnetOutcome.UNKNOWN
        assert "still be running" in detail

    async def test_a_key_that_cannot_be_staged_is_unknown(
        self, service: ProvisionService, tmp_path
    ):
        guest = ShellGuest(tmp_path, has_tailscale=True)
        guest.bind(service.proxmox)
        service.proxmox.agent_write_file = AsyncMock(side_effect=RuntimeError("file-write refused"))

        outcome, detail = await service.join_tailnet(
            node="pve1", vmid=105, hostname="web-01", key=KEY
        )

        assert outcome == TailnetOutcome.UNKNOWN
        assert "never ran" in detail


class TestTheKeyStaysOutOfEverything:
    """The one property this whole code path exists to protect."""

    async def test_the_key_is_a_file_never_an_argument(self, service: ProvisionService, tmp_path):
        guest = ShellGuest(tmp_path, has_tailscale=True)
        outcome, _detail = await _join(service, guest)

        assert outcome == TailnetOutcome.JOINED
        join = next(s for s in guest.scripts if "tailscale up" in s)
        assert KEY not in join, "the auth key must never appear in an argv"
        assert KEY_PATH in join and f"rm -f {KEY_PATH}" in join, (
            "the staged key file must be deleted by the same shell that reads it"
        )
        assert not guest.key_file_exists(), "the staged key was left on the guest's disk"

    async def test_a_failed_join_still_shreds_the_key_and_waits_to_find_out(
        self, service: ProvisionService, tmp_path
    ):
        """Acceptance is not completion, on the cleanup too.

        WHAT THIS FORBIDS: firing `rm` through the fire-and-forget `agent_exec`,
        which answers with a pid. What is left behind when that call is not
        waited for is the requester's auth key on a disk they never asked us to
        write it to.
        """
        guest = ShellGuest(tmp_path, has_tailscale=True, tailscale_up_rc=1)
        outcome, _detail = await _join(service, guest)

        assert outcome == TailnetOutcome.FAILED
        assert not guest.key_file_exists()
        assert any(s.startswith("rm -f") for s in guest.scripts), (
            "the key file was not shredded through a call whose result is read"
        )

    async def test_the_key_never_reaches_the_detail_a_caller_is_shown(
        self, service: ProvisionService, tmp_path
    ):
        """A guest quoting the key back must not put it in the task record."""
        guest = ShellGuest(
            tmp_path,
            has_tailscale=True,
            tailscale_up_rc=1,
            tailscale_up_stderr=f"failed with --auth-key={KEY} and also tskey-auth-OTHER-1234",
        )
        _outcome, detail = await _join(service, guest)

        assert KEY not in detail
        assert "tskey-" not in detail
        assert "<redacted>" in detail


class TestOneJoinPerGuest:
    """Two joins on one guest fight over one staged key file.

    WHAT THIS FORBIDS: serving a second join while the first is in flight. Both
    write `/run/hp-tailscale.key`, and the shell that reads it deletes it - so
    one of the two would `tailscale up` with the other's key, or with none.
    """

    async def test_a_second_rejoin_on_the_same_guest_is_refused(
        self, service: ProvisionService, db: Database, tmp_path
    ):
        guest = ShellGuest(tmp_path, has_tailscale=True, agent_ready_after=3)
        guest.bind(service.proxmox)

        first = await service.start_tailnet_join("pve1", 105, "web-01", KEY)
        with pytest.raises(TailnetJoinConflictError):
            await service.start_tailnet_join("pve1", 105, "web-01", FRESH_KEY)

        for task in list(service._running_tasks):
            await task
        row = await TaskRepository(db).get_task(first)
        assert row is not None and row["status"] == "succeeded"

    async def test_a_different_guest_is_not_blocked(self, service: ProvisionService, tmp_path):
        guest = ShellGuest(tmp_path, has_tailscale=True, agent_ready_after=3)
        guest.bind(service.proxmox)

        await service.start_tailnet_join("pve1", 105, "web-01", KEY)
        await service.start_tailnet_join("pve1", 106, "web-02", FRESH_KEY)
        for task in list(service._running_tasks):
            await task


class TestTheRejoinTaskIsAlwaysTerminal:
    """#386 on the new task kind: a row stuck at 'running' never stops being in flight."""

    async def test_a_refused_key_is_a_succeeded_task_carrying_a_failed_join(
        self, service: ProvisionService, db: Database, tmp_path
    ):
        guest = ShellGuest(
            tmp_path,
            has_tailscale=True,
            tailscale_up_rc=1,
            tailscale_up_stderr="invalid key: already used",
        )
        guest.bind(service.proxmox)

        task_id = await service.start_tailnet_join("pve1", 105, "web-01", KEY)
        for task in list(service._running_tasks):
            await task

        row = await TaskRepository(db).get_task(task_id)
        assert row is not None
        # The RETRY did what it was asked - it ran, and it found out - so the
        # task succeeded. What failed is the join, and that is in the result.
        assert row["status"] == "succeeded", row["error"]
        assert row["action"] == "tailnet_join"
        result = json.loads(row["result_json"])
        assert result["tailnet"] == "failed"
        assert "already used" in result["tailnet_detail"]
        assert KEY not in str(row)

    async def test_an_unexpected_error_lands_the_task_failed_and_says_unknown(
        self, service: ProvisionService, db: Database, tmp_path
    ):
        service.proxmox.agent_ping = AsyncMock(side_effect=RuntimeError("PVE fell over"))

        task_id = await service.start_tailnet_join("pve1", 105, "web-01", KEY)
        for task in list(service._running_tasks):
            await task

        row = await TaskRepository(db).get_task(task_id)
        assert row is not None
        assert row["status"] == "failed"
        result = json.loads(row["result_json"])
        # Not "failed": nothing about this guest's tailnet was established (#642).
        assert result["tailnet"] == "unknown"

    async def test_the_audit_row_carries_the_outcome_and_never_the_key(
        self, service: ProvisionService, db: Database, tmp_path
    ):
        guest = ShellGuest(tmp_path, has_tailscale=True)
        guest.bind(service.proxmox)

        await service.start_tailnet_join("pve1", 105, "web-01", KEY, actor="invite:hpi_abc")
        for task in list(service._running_tasks):
            await task

        rows = await Repository(db).query_audit_log(action="tailnet_join")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "invite:hpi_abc"
        assert rows[0]["target_host"] == "web-01"
        assert KEY not in json.dumps(dict(rows[0]))


class TestAJoinNeverFailsTheProvision:
    """The guest EXISTS by the time the join runs, and must survive the join going wrong."""

    async def test_an_exception_inside_the_join_is_reported_not_raised(
        self, service: ProvisionService, tmp_path
    ):
        from homepilot.provision.models import ProvisionRequest

        service.proxmox.agent_ping = AsyncMock(side_effect=RuntimeError("PVE fell over"))
        request = ProvisionRequest(
            name="web-01", node="pve1", template_vmid=9000, tailscale_auth_key=KEY
        )

        outcome, detail = await service._join_tailnet(request, 105)

        assert outcome == TailnetOutcome.UNKNOWN
        assert detail
        assert KEY not in detail


class TestTheScriptsAreShellValid:
    """A syntax error in a guest script is invisible until a guest runs it."""

    @pytest.mark.parametrize(
        "script_name",
        ["_TAILSCALE_INSTALL_SCRIPT", "_CLOUD_INIT_WAIT_SCRIPT", "_TAILSCALE_PROBE_SCRIPT"],
    )
    async def test_sh_parses_it(self, script_name: str) -> None:
        import subprocess  # nosec B404 - parsing the guest's shell IS the test

        from homepilot.provision import service as svc

        script: Any = getattr(svc, script_name)
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["/bin/sh", "-n"], input=script, text=True, capture_output=True, check=False
        )
        assert proc.returncode == 0, f"{script_name} is not valid sh: {proc.stderr}"


class TestNoKeyMeansNoGuestCommands:
    async def test_a_provision_without_a_key_touches_nothing_inside_the_guest(
        self, service: ProvisionService, tmp_path
    ):
        from homepilot.provision.models import ProvisionRequest

        guest = ShellGuest(tmp_path, has_tailscale=True)
        guest.bind(service.proxmox)

        outcome, detail = await service._join_tailnet(
            ProvisionRequest(name="web-01", node="pve1", template_vmid=9000), 105
        )

        assert outcome is None and detail == ""
        assert guest.scripts == []
        assert guest.pings == 0


class TestAConfinedAgentIsNotANetworkFault:
    """The failure that cost an afternoon on dev, named properly (#628/#642).

    On an SELinux-enforcing guest, qemu-guest-agent runs as `virt_qemu_ga_t`
    and may not open http/https. From the fetcher's side that is
    indistinguishable from a broken route - `curl` simply cannot connect - so
    the installer reported "the route out is the thing to look at" and sent the
    operator to fix a network that was working. Proven live on dev: from the
    same guest, TCP to 1.1.1.1:53 connects and :443 returns EPERM.

    Attributing a real failure to the wrong cause is the most expensive form of
    the #642 mistake, because it costs someone a day fixing what was never
    broken.
    """

    async def test_a_confined_agent_is_reported_as_confined_not_as_a_route_problem(
        self, tmp_path, service, proxmox
    ):
        guest = ShellGuest(
            tmp_path, fetcher="curl", fetch_ok=False, dns_ok=True, selinux_confined=True
        ).bind(proxmox)

        outcome, detail = await service.join_tailnet(node="pve", vmid=101, hostname="g", key=KEY)

        assert outcome == "failed"
        assert "SELinux" in detail and "virt_qemu_ga_t" in detail
        assert "network is fine" in detail
        assert "route out is the thing to look at" not in detail, (
            "a confined agent was still reported as a routing problem"
        )
        assert guest.scripts, "the installer never ran"

    async def test_an_unconfined_guest_still_reports_a_real_route_problem(
        self, tmp_path, service, proxmox
    ):
        """The honest other arm: where SELinux is NOT the cause, the reason must
        still point at the route, or the new branch would swallow real faults."""
        ShellGuest(
            tmp_path, fetcher="curl", fetch_ok=False, dns_ok=True, selinux_confined=False
        ).bind(proxmox)

        outcome, detail = await service.join_tailnet(node="pve", vmid=101, hostname="g", key=KEY)

        assert outcome == "failed"
        assert "SELinux" not in detail
        assert "route out" in detail


class TestSilenceIsNotACause:
    """A silent guest agent has more than one explanation (#648, from a real user).

    A redeemer pressed "retry tailnet join" twice against a guest that had been
    DESTROYED three days earlier. Both times HomePilot answered:

        "The guest's qemu-guest-agent never answered... The template needs
         qemu-guest-agent installed, started, and allowed to run commands."

    The template was fine - its agent had demonstrably run commands inside a
    guest cloned from it. The machine simply did not exist. The operator went
    looking at the image, which is exactly where that sentence sends someone.

    Silence establishes that nothing answered. It does not establish WHY, and
    the three whys send an operator to three different places.

    Teeth: make `_why_no_agent` return the template sentence unconditionally and
    the first two tests fail.
    """

    @staticmethod
    def _service(monkeypatch, current):
        from unittest.mock import AsyncMock

        from homepilot.provision.service import ProvisionService

        svc = ProvisionService.__new__(ProvisionService)
        svc.proxmox = AsyncMock()
        svc.proxmox.get_vm_current = current
        return svc

    async def test_a_destroyed_machine_is_named_as_gone(self, monkeypatch):
        from unittest.mock import AsyncMock

        from homepilot.adapters.proxmox import ProxmoxError

        svc = self._service(
            monkeypatch,
            AsyncMock(side_effect=ProxmoxError("GET", "/nodes/n/qemu/116/status/current", 500, "")),
        )
        detail = await svc._why_no_agent("elizabeth", 116)

        assert "no longer exists" in detail
        # And it must NOT send them to the template, which is the wrong turn.
        assert "qemu-guest-agent installed" not in detail

    async def test_a_stopped_machine_is_named_as_stopped(self):
        from unittest.mock import AsyncMock

        svc = self._service(None, AsyncMock(return_value={"data": {"status": "stopped"}}))
        detail = await svc._why_no_agent("elizabeth", 116)

        assert "stopped" in detail
        assert "qemu-guest-agent installed" not in detail

    async def test_a_running_machine_really_does_point_at_the_template(self):
        """The original sentence is RIGHT in the one case it was written for, so
        the fix must not make a genuine missing-agent undiagnosable."""
        from unittest.mock import AsyncMock

        svc = self._service(None, AsyncMock(return_value={"data": {"status": "running"}}))
        detail = await svc._why_no_agent("elizabeth", 116)

        assert "qemu-guest-agent installed" in detail

    async def test_a_cluster_that_cannot_be_asked_names_no_cause_at_all(self):
        """Unknown is unknown. Guessing here would rebuild the whole defect."""
        from unittest.mock import AsyncMock

        svc = self._service(None, AsyncMock(side_effect=RuntimeError("connection reset")))
        detail = await svc._why_no_agent("elizabeth", 116)

        assert "could not be" in detail
        assert "no longer exists" not in detail
        assert "qemu-guest-agent installed" not in detail
