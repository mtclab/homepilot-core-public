"""THE JOURNEY GATE for the tailnet retry (#628, second half).

Asserts THE REDEEMER'S GOAL - "my machine is on my tailnet" - is reached by
somebody who has only a browser and a fresh auth key. Not that a handler
returned a 303.

The story it walks is the real one. A friend redeems an invite with a tailscale
key. The key has already been used, so the join is refused inside the guest, and
the machine comes up without a tailnet. Before this work that was the end of it:
the status page said "join failed - run tailscale up yourself" to somebody who
had just been handed a machine they could not necessarily reach, and nothing in
the product would ever try again. Now they mint a fresh key, paste it into the
page they are already looking at, and the machine joins.

Real wiring end to end: the real portal router, the real Jinja templates, real
migrated SQLite, real InviteRepository/TaskRepository/ProvisionService, real
ProxmoxClient. Only the HTTP transport under the Proxmox client is faked - and
that fake answers the guest-agent exec/exec-status pair for real, so `agent_run`
does its actual wait-and-read-the-exit-status loop.

WHAT THIS FORBIDS:
  * a failed join with no way back (delete the form and the journey fails);
  * a retry that cannot change the answer (make the re-join task invisible to
    the status page and the journey fails);
  * the fresh key reaching disk (it is asserted absent from the invite row, the
    task rows and the rendered page).
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI

from homepilot.db.connection import Database

from .portal_support import (
    REDEEM_FORM,
    FakePVE,
    cert_headers,
    client_for,
    invite_row,
    mint,
    poll_status,
    task_rows,
)

USED_KEY = "tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk"
FRESH_KEY = "tskey-auth-m4Zz9QWERT-1aSdFgHjKlZxCvBnM"


async def _poll_until_the_join_settles(client, token: str, timeout: float = 10.0) -> str:
    """The status page, refreshed until no join is in flight on it.

    The page's own `<meta refresh>` does this for the friend; a test that read it
    once would assert on "joining now" and call that the outcome.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/invite/{token}/status", headers=cert_headers())
        assert response.status_code == 200, response.text
        if "joining now" not in response.text and "Building" not in response.text:
            return response.text
        await asyncio.sleep(0.05)
    raise AssertionError("the status page never stopped saying a join was running")


async def _redeem_with_a_refused_key(client, portal_pve: FakePVE, token: str) -> str:
    portal_pve.tailscale_up_rc = 1
    portal_pve.tailscale_up_err = "backend error: invalid key: already used"
    redeemed = await client.post(
        f"/invite/{token}",
        data={**REDEEM_FORM, "tailscale_auth_key": USED_KEY},
        headers=cert_headers(),
    )
    assert redeemed.status_code == 303, redeemed.text
    status = await poll_status(client, token)
    return status.text


class TestTheRedeemerCanRetryTheirOwnTailnetJoin:
    async def test_a_refused_key_is_explained_and_can_be_replaced(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            page = await _redeem_with_a_refused_key(client, portal_pve, token)

            # The machine exists and the page says so...
            assert "Ready" in page
            # ...and it says the tailnet is NOT up, with the guest's own reason -
            # not a bare "failed", which is what the first live run left behind.
            assert "not joined" in page
            assert "already used" in page, "the redeemer was not told why"
            # ...and offers the one thing that fixes it.
            assert 'name="tailscale_auth_key"' in page
            assert f"/invite/{token}/tailnet-join" in page

            # THE RETRY, with a key that works this time.
            portal_pve.tailscale_up_rc = 0
            portal_pve.tailscale_up_err = ""
            retried = await client.post(
                f"/invite/{token}/tailnet-join",
                data={"tailscale_auth_key": FRESH_KEY},
                headers=cert_headers(),
            )
            assert retried.status_code == 303, retried.text
            assert retried.headers["location"] == f"/invite/{token}/status"

            after = await _poll_until_the_join_settles(client, token)

        # THE GOAL: the page the friend is looking at now says their machine is
        # on their tailnet, and the form is gone because there is nothing to fix.
        assert "joined" in after
        assert "not joined" not in after
        assert 'name="tailscale_auth_key"' not in after

        # The retry ran as its own task against the machine this invite built.
        joins = [t for t in task_rows(portal_db.db_path) if t["action"] == "tailnet_join"]
        assert len(joins) == 1
        assert joins[0]["status"] == "succeeded"
        assert '"tailnet": "joined"' in joins[0]["result_json"]

        row = invite_row(portal_db.db_path, invite_id)
        assert row["rejoin_task_id"] == joins[0]["id"]
        # The provision task is untouched: it is what the page renders the
        # machine's own details out of.
        assert row["resulting_task_id"] != row["rejoin_task_id"]

        # NEITHER key reached disk, in any column of any row.
        for key in (USED_KEY, FRESH_KEY):
            assert key not in str(dict(row))
            assert key not in str(task_rows(portal_db.db_path))
            assert key not in after

    async def test_the_join_actually_carried_the_fresh_key_into_the_guest(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        """Test the goal, not the call: a retry that sent the OLD key is no retry.

        The key never appears in the command, so what proves it arrived is the
        file-write body - the one place it is supposed to be.
        """
        _invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            await _redeem_with_a_refused_key(client, portal_pve, token)
            portal_pve.tailscale_up_rc = 0
            await client.post(
                f"/invite/{token}/tailnet-join",
                data={"tailscale_auth_key": FRESH_KEY},
                headers=cert_headers(),
            )
            await _poll_until_the_join_settles(client, token)

        written = portal_pve.bodies["/nodes/pve1/qemu/105/agent/file-write"]
        assert written["content"] == FRESH_KEY, "the retry re-used the key that had failed"
        assert written["file"] == "/run/hp-tailscale.key"

    async def test_a_key_that_is_not_a_key_comes_back_on_the_page_not_a_dead_end(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        _invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            await _redeem_with_a_refused_key(client, portal_pve, token)
            bad = "not-a-tailscale-key-at-all"
            refused = await client.post(
                f"/invite/{token}/tailnet-join",
                data={"tailscale_auth_key": bad},
                headers=cert_headers(),
            )

        assert refused.status_code == 400
        # Still the status page, with the machine's details and the form on it.
        assert "Your machine" in refused.text
        assert 'name="tailscale_auth_key"' in refused.text
        assert "tailscale_auth_key" in refused.text
        assert bad not in refused.text, "the rejected value was handed back to the browser"

    async def test_a_machine_already_on_the_tailnet_is_offered_no_form(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        _invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            redeemed = await client.post(
                f"/invite/{token}",
                data={**REDEEM_FORM, "tailscale_auth_key": USED_KEY},
                headers=cert_headers(),
            )
            assert redeemed.status_code == 303
            page = (await poll_status(client, token)).text

        assert "joined" in page
        assert 'name="tailscale_auth_key"' not in page

    async def test_a_redemption_with_no_key_is_offered_no_form(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        """Nothing to retry: this invite was never about a tailnet."""
        _invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            page = (await poll_status(client, token)).text

        assert "Tailnet" not in page
        assert 'name="tailscale_auth_key"' not in page


class TestTheRetryIsAsGuardedAsTheRedemption:
    async def test_another_certificate_cannot_retry_this_invites_join(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        from .portal_support import CN_B

        _invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            await _redeem_with_a_refused_key(client, portal_pve, token)
            stolen = await client.post(
                f"/invite/{token}/tailnet-join",
                data={"tailscale_auth_key": FRESH_KEY},
                headers=cert_headers(cn=CN_B),
            )

        assert stolen.status_code == 404
        assert [t for t in task_rows(portal_db.db_path) if t["action"] == "tailnet_join"] == []

    async def test_no_client_certificate_is_refused(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        _invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            await _redeem_with_a_refused_key(client, portal_pve, token)
            headers = cert_headers()
            headers.pop("ssl-client-subject-dn")
            refused = await client.post(
                f"/invite/{token}/tailnet-join",
                data={"tailscale_auth_key": FRESH_KEY},
                headers=headers,
            )

        assert refused.status_code == 403
        assert [t for t in task_rows(portal_db.db_path) if t["action"] == "tailnet_join"] == []

    async def test_a_revoked_invite_starts_nothing_new(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        """Revoking is an operator saying "this person is done here".

        The machine keeps running - revoking an invite has never destroyed one -
        but the portal starts nothing further from that invite.
        """
        invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            await _redeem_with_a_refused_key(client, portal_pve, token)
            await portal_db.execute(
                "UPDATE invites SET revoked_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                (invite_id,),
            )
            await portal_db.conn.commit()
            refused = await client.post(
                f"/invite/{token}/tailnet-join",
                data={"tailscale_auth_key": FRESH_KEY},
                headers=cert_headers(),
            )

        assert refused.status_code == 404
        assert [t for t in task_rows(portal_db.db_path) if t["action"] == "tailnet_join"] == []

    async def test_retries_are_rate_limited_on_their_own_budget(
        self, portal_app: FastAPI, portal_db: Database, portal_pve: FakePVE
    ):
        """A retry storm must not lock the friend out of redeeming, or vice versa."""
        from homepilot.portal import router as portal_router_module

        _invite_id, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            await _redeem_with_a_refused_key(client, portal_pve, token)
            codes = []
            for _ in range(portal_router_module._REJOIN_LIMIT + 2):
                resp = await client.post(
                    f"/invite/{token}/tailnet-join",
                    data={"tailscale_auth_key": "nope"},
                    headers=cert_headers(),
                )
                codes.append(resp.status_code)

        assert 429 in codes, "the retry form has no rate limit at all"
        # The redemption budget is untouched: they are separate buckets.
        assert portal_router_module._redeem_attempts.allow("anything")
