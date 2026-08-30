"""THE JOURNEY GATE for the invite portal (#442 stage 2).

Asserts the FRIEND'S GOAL is reached, not that a handler returned a redirect:
mint an invite, walk the real HTML pages through the trusted-proxy headers, and
require that a machine with the INVITE'S caps was actually built and that the
page tells them how to get in.

Wiring is real end to end - real portal router, real Jinja templates, real
migrated SQLite, real InviteRepository/TaskRepository/ProvisionService, real
ProxmoxClient. Only the HTTP transport under the Proxmox client is faked, so URL
building, JSON bodies and UPID handling are exercised for real.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import FastAPI

from homepilot.db.connection import Database

from .portal_support import (
    CN_A,
    REDEEM_FORM,
    FakePVE,
    cert_headers,
    client_for,
    invite_row,
    mint,
    poll_for_task_end,
    poll_status,
    strip_task_result,
    task_rows,
)


class TestInviteJourney:
    async def test_friend_gets_a_machine_with_the_invites_caps(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            form = await client.get(f"/invite/{token}", headers=cert_headers())
            assert form.status_code == 200, form.text
            # The caps they will get are shown, read-only.
            assert "<dd>2</dd>" in form.text
            assert "2048 MB" in form.text and "20 GB" in form.text
            assert "<form" in form.text and 'name="ssh_authorized_key"' in form.text

            redeemed = await client.post(
                f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers()
            )
            assert redeemed.status_code == 303, redeemed.text
            assert redeemed.headers["location"] == f"/invite/{token}/status"

            status = await poll_status(client, token)

        # THE GOAL: a real machine, built to the INVITE'S caps, and the page says
        # how to reach it.
        assert "Ready" in status.text
        assert "105" in status.text
        assert "10.0.0.42" in status.text
        assert "ssh olli@10.0.0.42" in status.text

        clone_body = portal_pve.bodies["/nodes/pve1/qemu/9000/clone"]
        assert clone_body["name"] == "lab-box"
        config_body = portal_pve.bodies["/nodes/pve1/qemu/105/config"]
        # Caps came from the invite, NOT from the hostile form fields.
        assert config_body["cores"] == 2
        assert config_body["memory"] == 2048
        assert config_body["ciuser"] == "olli"
        assert portal_pve.bodies["/nodes/pve1/qemu/105/resize"]["size"] == "20G"
        assert ("POST", "/nodes/pve1/qemu/9000/clone") in portal_pve.seen
        assert not any(path.startswith("/nodes/not-a-node") for _, path in portal_pve.seen)

        row = invite_row(portal_db.db_path, invite_id)
        assert row["redeemed_at"] is not None
        assert row["redeemed_cn"] == CN_A
        assert row["resulting_task_id"] is not None
        assert row["resulting_host_id"] is not None

        tasks = task_rows(portal_db.db_path)
        assert len(tasks) == 1
        assert tasks[0]["action"] == "provision"
        assert tasks[0]["status"] == "succeeded"

    async def test_a_second_post_with_the_same_token_is_refused(
        self, portal_app: FastAPI, portal_db: Database
    ):
        _, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            first = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            assert first.status_code == 303
            await poll_status(client, token)

            second = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())

        assert second.status_code == 404
        assert "cannot be used" in second.text
        assert len(task_rows(portal_db.db_path)) == 1

    async def test_two_simultaneous_redemptions_build_exactly_one_machine(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        _, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            first, second = await asyncio.gather(
                client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers()),
                client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers()),
            )
            # Assert the property BEFORE waiting for the build, so a broken
            # single-use latch fails here by name rather than as a late symptom.
            codes = sorted([first.status_code, second.status_code])
            assert codes == [303, 404], f"expected one redemption and one refusal, got {codes}"
            assert len(task_rows(portal_db.db_path)) == 1

            await poll_status(client, token)

        assert len([1 for _, path in portal_pve.seen if path.endswith("/9000/clone")]) == 1

    async def test_a_failed_build_tells_the_friend_without_leaking_operator_detail(
        self, portal_app: FastAPI, portal_db: Database
    ):
        _, token = await mint(portal_db)

        async def failing_wait(*args: Any, **kwargs: Any) -> dict[str, Any]:
            from homepilot.adapters.proxmox import ProxmoxError

            raise ProxmoxError("GET", "/tasks", 0, "exitstatus 'no space left on device'")

        portal_app.state.provision_service.proxmox.wait_for_task = failing_wait

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            assert posted.status_code == 303
            status = await poll_status(client, token)

        assert "Did not finish" in status.text
        # The operator's diagnosis is not the friend's business.
        assert "no space left on device" not in status.text
        assert "clone:" not in status.text

    async def test_no_hostname_still_yields_a_valid_unique_guest_name(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        _, token = await mint(portal_db)
        payload = {k: v for k, v in REDEEM_FORM.items() if k != "hostname"}

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=payload, headers=cert_headers())
            assert posted.status_code == 303
            await poll_status(client, token)

        name = portal_pve.bodies["/nodes/pve1/qemu/9000/clone"]["name"]
        assert name.startswith("friend-a-")
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", name), name


class TestAFailedBuildDoesNotBurnTheLink:
    """#625: the first real redemption on prod burned a friend's only link.

    They claimed the invite (atomically - correct), the build failed on the
    OPERATOR's side (a stale write token), and the invite stayed `redeemed`
    forever. A blameless person could not retry, and the operator had to mint a
    fresh link out of band for a fault that was never theirs.

    The link comes back ONLY when nothing was built, and that has to be
    ESTABLISHED rather than assumed - which is why the provision task now
    records its unwind structurally (`nothing_created` / `deleted` / `failed`)
    instead of saying it in English inside the error string, where nothing
    downstream could read it.

    Teeth: drop the `reopen_after_failed_build` call and the first test fails on
    the still-redeemed invite; drop the `cleanup` guard and the second fails,
    because an orphaned guest would hand out a second machine past the quota.
    """

    async def test_a_build_that_created_nothing_gives_the_link_back(
        self, portal_app: FastAPI, portal_db: Database
    ):
        invite_id, token = await mint(portal_db)

        async def refuse_clone(*args: Any, **kwargs: Any) -> dict[str, Any]:
            from homepilot.adapters.proxmox import ProxmoxError

            # 401 on the clone: the exact prod shape - the operator's write
            # token is stale, so nothing is ever created.
            raise ProxmoxError("POST", "/nodes/pve1/qemu/9000/clone", 401, "authentication failure")

        portal_app.state.provision_service.proxmox.clone_vm = refuse_clone

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            assert posted.status_code == 303
            status = await poll_status(client, token)

            # The friend is told, in their own words, and the link is back.
            assert "your link still works" in status.text.lower(), status.text
            assert "authentication failure" not in status.text

            # THE GOAL: the link actually works again - the form, not a refusal.
            again = await client.get(f"/invite/{token}", headers=cert_headers())
            assert again.status_code == 200, again.text
            assert "<form" in again.text

        row = invite_row(portal_db.db_path, invite_id)
        assert row["redeemed_at"] is None
        assert row["resulting_task_id"] is None

    async def test_a_failure_that_may_have_left_a_guest_keeps_the_link_burned(
        self, portal_app: FastAPI, portal_db: Database
    ):
        """The other direction, and the dangerous one.

        A build that got as far as creating a guest and could not take it back
        has left a machine. Handing the link back there would let one invite
        produce two machines - past the quota the operator set - so the invite
        stays claimed and this becomes an operator's clean-up.
        """
        invite_id, token = await mint(portal_db)

        async def failing_wait(*args: Any, **kwargs: Any) -> dict[str, Any]:
            from homepilot.adapters.proxmox import ProxmoxError

            raise ProxmoxError("GET", "/tasks", 0, "exitstatus 'no space left on device'")

        portal_app.state.provision_service.proxmox.wait_for_task = failing_wait

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            assert posted.status_code == 303
            status = await poll_status(client, token)

            assert "your link still works" not in status.text.lower()

            # The link stays spent: a second machine is not on offer.
            again = await client.get(f"/invite/{token}", headers=cert_headers())
            assert "<form" not in again.text

        row = invite_row(portal_db.db_path, invite_id)
        assert row["redeemed_at"] is not None

    async def test_a_failed_task_that_says_nothing_keeps_the_link_burned(
        self, portal_app: FastAPI, portal_db: Database
    ):
        """A task row written by an older build carries no cleanup verdict.

        Silence is not "nothing remains". This is the case the structural
        verdict actually guards: with no `result_json` at all there is no vmid
        to notice either, so the ONLY thing standing between a friend and a
        second machine is refusing to read an absent answer as a good one - the
        same rule the whole of #648 keeps arriving at.
        """
        invite_id, token = await mint(portal_db)

        async def refuse_clone(*args: Any, **kwargs: Any) -> dict[str, Any]:
            from homepilot.adapters.proxmox import ProxmoxError

            raise ProxmoxError("POST", "/nodes/pve1/qemu/9000/clone", 401, "authentication failure")

        portal_app.state.provision_service.proxmox.clone_vm = refuse_clone

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            assert posted.status_code == 303
            await poll_for_task_end(portal_db, invite_id)

            # Blank the verdict the way an upgrade-straddling row would be.
            strip_task_result(portal_db.db_path, invite_id)

            status = await client.get(f"/invite/{token}/status", headers=cert_headers())
            assert "your link still works" not in status.text.lower()

        row = invite_row(portal_db.db_path, invite_id)
        assert row["redeemed_at"] is not None
