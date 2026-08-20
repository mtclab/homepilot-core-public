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
    poll_status,
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
