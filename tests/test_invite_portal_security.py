"""Standing gates for the invite portal's trust model (#442 stage 2).

The portal believes a client-certificate identity ONLY when the reverse proxy
asserts it through all three layers at once: the request arrives from a trusted
source, carries the shared secret the proxy sets, and carries a verify header
that says SUCCESS. Each test here forbids a whole class of bypass; each is
written so that removing the corresponding check turns it red.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI

from homepilot.portal.trust import (
    PortalNotConfiguredError,
    PortalTrust,
    PortalUntrustedError,
    assert_trusted_cn,
    extract_cn,
    load_trust,
)

from .portal_support import (
    CN_A,
    CN_B,
    PROXY_IP,
    PROXY_SECRET,
    PUBKEY,
    REDEEM_FORM,
    cert_headers,
    client_for,
    invite_row,
    mint,
    poll_status,
    portal_settings,
    task_rows,
)


class TestSourceAddressLayer:
    async def test_a_forged_cn_from_an_untrusted_source_is_refused(
        self, portal_app: FastAPI, portal_db
    ):
        _, token = await mint(portal_db)

        # Same headers the real proxy sends, but the request did not come
        # through the proxy.
        async with client_for(portal_app, peer="203.0.113.9") as client:
            page = await client.get(f"/invite/{token}", headers=cert_headers())
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())

        assert page.status_code == 403
        assert posted.status_code == 403
        assert "client-certificate gateway" in page.text
        assert task_rows(portal_db.db_path) == []


class TestSharedSecretLayer:
    async def test_a_correct_cn_with_the_wrong_proxy_secret_is_refused(
        self, portal_app: FastAPI, portal_db
    ):
        _, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            wrong = await client.get(
                f"/invite/{token}", headers=cert_headers(secret="not-the-secret")
            )
            missing = await client.get(
                f"/invite/{token}", headers={**cert_headers(), "x-hp-portal-secret": ""}
            )
            posted = await client.post(
                f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers(secret="not-the-secret")
            )

        assert wrong.status_code == 403
        assert missing.status_code == 403
        assert posted.status_code == 403
        assert task_rows(portal_db.db_path) == []


class TestVerifyHeaderLayer:
    @pytest.mark.parametrize("verify", ["FAILED", "NONE", "", "success-ish"])
    async def test_a_certificate_the_proxy_did_not_verify_is_refused(
        self, portal_app: FastAPI, portal_db, verify: str
    ):
        _, token = await mint(portal_db)

        async with client_for(portal_app) as client:
            page = await client.get(f"/invite/{token}", headers=cert_headers(verify=verify))
            posted = await client.post(
                f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers(verify=verify)
            )

        assert page.status_code == 403
        assert posted.status_code == 403
        assert task_rows(portal_db.db_path) == []


class TestUnconfiguredPortalFailsClosed:
    @pytest.mark.parametrize(
        "missing",
        [
            {"portal_trusted_proxy": ""},
            {"portal_proxy_secret": ""},
            {"portal_cn_header": ""},
            {"portal_verify_header": ""},
        ],
    )
    async def test_every_route_503s_when_a_trust_input_is_unset(
        self, portal_app: FastAPI, portal_db, missing: dict[str, str]
    ):
        _, token = await mint(portal_db)
        portal_app.state.settings = portal_settings(**missing)

        async with client_for(portal_app) as client:
            form = await client.get(f"/invite/{token}", headers=cert_headers())
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            status = await client.get(f"/invite/{token}/status", headers=cert_headers())

        for response in (form, posted, status):
            assert response.status_code == 503, response.text
            # The operator must be told WHICH variable is missing.
            assert "HP_PORTAL_" in response.text
        assert task_rows(portal_db.db_path) == []


class TestCnBinding:
    async def test_an_invite_bound_to_one_cn_cannot_be_redeemed_by_another(
        self, portal_app: FastAPI, portal_db
    ):
        invite_id, token = await mint(portal_db, cn=CN_A)

        async with client_for(portal_app) as client:
            form = await client.get(f"/invite/{token}", headers=cert_headers(cn=CN_B))
            posted = await client.post(
                f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers(cn=CN_B)
            )
            status = await client.get(f"/invite/{token}/status", headers=cert_headers(cn=CN_B))

        assert form.status_code == 404
        assert posted.status_code == 404
        assert status.status_code == 404
        # The wrong CN learns nothing about whether this token exists.
        assert "cannot be used" in form.text
        assert CN_A not in form.text
        assert task_rows(portal_db.db_path) == []
        assert invite_row(portal_db.db_path, invite_id)["redeemed_at"] is None

    async def test_the_page_for_a_bad_token_is_the_same_page_as_for_a_wrong_cn(
        self, portal_app: FastAPI, portal_db
    ):
        _, token = await mint(portal_db, cn=CN_A)

        async with client_for(portal_app) as client:
            wrong_cn = await client.get(f"/invite/{token}", headers=cert_headers(cn=CN_B))
            no_such = await client.get("/invite/hpi_deadbeefdeadbeef", headers=cert_headers())

        assert wrong_cn.status_code == no_such.status_code == 404
        assert wrong_cn.text == no_such.text


class TestExpiryAndRevocation:
    async def test_an_expired_invite_cannot_be_redeemed(self, portal_app: FastAPI, portal_db):
        _, token = await mint(portal_db, ttl=timedelta(seconds=-60))

        async with client_for(portal_app) as client:
            form = await client.get(f"/invite/{token}", headers=cert_headers())
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())

        assert form.status_code == 404
        assert posted.status_code == 404
        assert task_rows(portal_db.db_path) == []

    async def test_a_revoked_invite_cannot_be_redeemed(self, portal_app: FastAPI, portal_db):
        from homepilot.portal.repository import InviteRepository

        invite_id, token = await mint(portal_db)
        invites = InviteRepository(portal_db)
        assert await invites.revoke(token[:16]) is True
        # Revoking twice reports False rather than silently re-stamping.
        assert await invites.revoke(token[:16]) is False

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())

        assert posted.status_code == 404
        assert task_rows(portal_db.db_path) == []
        assert invite_row(portal_db.db_path, invite_id)["redeemed_at"] is None


class TestSubmittedFieldsAreValidated:
    @pytest.mark.parametrize(
        "bad_key",
        [
            "",
            "not-a-key",
            "ssh-ed25519",
            "ssh-dss AAAAB3NzaC1kc3MAAACBAJ==",
            f"{PUBKEY}\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq attacker@elsewhere",
        ],
    )
    async def test_a_bad_ssh_key_never_reaches_provisioning(
        self, portal_app: FastAPI, portal_db, bad_key: str
    ):
        _, token = await mint(portal_db)
        payload = {**REDEEM_FORM, "ssh_authorized_key": bad_key}

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=payload, headers=cert_headers())

        assert posted.status_code == 400
        assert "<form" in posted.text  # the form comes back with the error, not a dead end
        assert task_rows(portal_db.db_path) == []

    @pytest.mark.parametrize("bad_user", ["ro", "Olli", "root; rm -rf /", "-lead", ""])
    async def test_a_bad_username_never_reaches_provisioning(
        self, portal_app: FastAPI, portal_db, bad_user: str
    ):
        _, token = await mint(portal_db)
        payload = {**REDEEM_FORM, "ciuser": bad_user}

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=payload, headers=cert_headers())

        assert posted.status_code == 400
        assert task_rows(portal_db.db_path) == []

    @pytest.mark.parametrize("bad_tskey", ["hunter2", "tskey-auth-x y", "tskey-", "tskey-$(id)"])
    async def test_a_value_that_is_not_a_tailscale_key_is_refused(
        self, portal_app: FastAPI, portal_db, bad_tskey: str
    ):
        _, token = await mint(portal_db)
        payload = {**REDEEM_FORM, "tailscale_auth_key": bad_tskey}

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=payload, headers=cert_headers())

        assert posted.status_code == 400
        assert task_rows(portal_db.db_path) == []

    async def test_a_real_shaped_tailscale_key_is_accepted_and_never_rendered(
        self, portal_app: FastAPI, portal_db
    ):
        invite_id, token = await mint(portal_db)
        secret_key = "tskey-auth-k7Ab3CNTRL-9xQwErTyUiOpAsDfGhJk"
        payload = {**REDEEM_FORM, "tailscale_auth_key": secret_key}

        async with client_for(portal_app) as client:
            posted = await client.post(f"/invite/{token}", data=payload, headers=cert_headers())
            assert posted.status_code == 303
            status = await poll_status(client, token)

        assert "Ready" in status.text
        assert "Tailnet" in status.text
        # The key is the requester's own credential: never echoed back to the
        # page, never written to the task row, never written to the invite row.
        assert secret_key not in status.text
        assert secret_key not in str(task_rows(portal_db.db_path))
        assert secret_key not in str(invite_row(portal_db.db_path, invite_id))


class TestRedemptionRateLimit:
    async def test_a_grinder_is_cut_off_without_locking_out_another_certificate(
        self, portal_app: FastAPI, portal_db
    ):
        from homepilot.portal.router import _REDEEM_LIMIT

        _, token_a = await mint(portal_db, cn=CN_A)
        _, token_b = await mint(portal_db, cn=CN_B)
        # Attempts that fail validation still count: a grinder must not get
        # unlimited tries by sending rubbish.
        payload = {**REDEEM_FORM, "ssh_authorized_key": "not-a-key"}

        async with client_for(portal_app) as client:
            for _ in range(_REDEEM_LIMIT):
                spent = await client.post(
                    f"/invite/{token_a}", data=payload, headers=cert_headers()
                )
                assert spent.status_code == 400
            blocked = await client.post(
                f"/invite/{token_a}", data=REDEEM_FORM, headers=cert_headers()
            )
            # The bucket is per certificate: another friend is unaffected.
            other = await client.post(
                f"/invite/{token_b}", data=REDEEM_FORM, headers=cert_headers(cn=CN_B)
            )

        assert blocked.status_code == 429
        assert other.status_code == 303
        assert len(task_rows(portal_db.db_path)) == 1


class TestPagesCarryNoAdminData:
    async def test_the_form_shows_caps_but_no_operator_facts(self, portal_app: FastAPI, portal_db):
        invite_id, token = await mint(portal_db)
        row = invite_row(portal_db.db_path, invite_id)

        async with client_for(portal_app) as client:
            form = await client.get(f"/invite/{token}", headers=cert_headers())

        assert form.status_code == 200
        for leak in (row["token_hash"], row["created_by"], row["node"], str(row["template_vmid"])):
            assert leak not in form.text, f"portal page leaked {leak!r}"

    async def test_the_status_page_shows_only_this_invites_machine(
        self, portal_app: FastAPI, portal_db
    ):
        _, token = await mint(portal_db)
        async with client_for(portal_app) as client:
            await client.post(f"/invite/{token}", data=REDEEM_FORM, headers=cert_headers())
            status = await poll_status(client, token)

        assert "Ready" in status.text
        # Task ids, host ids and node names are the operator's view, not theirs.
        row = task_rows(portal_db.db_path)[0]
        assert row["id"] not in status.text
        assert "pve1" not in status.text


class TestDistinguishedNameParsing:
    @pytest.mark.parametrize(
        ("dn", "expected"),
        [
            ("CN=friend-a", "friend-a"),
            ("CN=friend-a,OU=lab,O=MTC Lab", "friend-a"),
            ("OU=lab,CN=friend-a,O=MTC Lab", "friend-a"),
            ("/C=FI/O=MTC/CN=friend-a", "friend-a"),
            ("/C=FI/O=MTC, Lab/CN=friend-a", "friend-a"),
            (r"CN=Smith\, John,OU=lab", "Smith, John"),
            (r"CN=Smith\2C John,OU=lab", "Smith, John"),
            ('CN="friend-a",OU=lab', "friend-a"),
            ("cn=friend-a,ou=lab", "friend-a"),
            ("  CN=friend-a , OU=lab ", "friend-a"),
        ],
    )
    def test_the_cn_is_extracted_from_every_dn_form_a_proxy_may_send(self, dn: str, expected: str):
        assert extract_cn(dn) == expected

    @pytest.mark.parametrize(
        "dn",
        [
            "",
            "OU=lab,O=MTC",
            # Two CNs: which identity would we be trusting? Refuse.
            "CN=friend-a,CN=friend-b",
            "CN=friend-a+CN=friend-b",
            "CN=",
            "garbage",
        ],
    )
    def test_an_ambiguous_or_absent_cn_yields_nothing(self, dn: str):
        assert extract_cn(dn) is None

    def test_an_escaped_comma_cannot_smuggle_a_second_cn(self):
        # A naive split on ',' would read this as CN='a' plus a stray 'CN=b'.
        assert extract_cn(r"CN=a\,CN=b,OU=lab") == "a,CN=b"


class TestTrustPrimitives:
    def _trust(self) -> PortalTrust:
        return load_trust(portal_settings())

    def test_load_trust_names_every_missing_variable(self):
        with pytest.raises(PortalNotConfiguredError) as excinfo:
            load_trust(portal_settings(portal_proxy_secret="", portal_trusted_proxy=""))
        message = str(excinfo.value)
        assert "HP_PORTAL_PROXY_SECRET" in message
        assert "HP_PORTAL_TRUSTED_PROXY" in message

    def test_a_cidr_range_admits_the_proxy_and_nothing_outside_it(self):
        trust = load_trust(portal_settings(portal_trusted_proxy="10.9.9.0/29, 192.0.2.7"))
        headers = {
            "x-hp-portal-secret": PROXY_SECRET,
            "ssl-client-verify": "SUCCESS",
            "ssl-client-subject-dn": f"CN={CN_A}",
        }
        assert assert_trusted_cn("10.9.9.6", headers, trust) == CN_A
        assert assert_trusted_cn("192.0.2.7", headers, trust) == CN_A
        with pytest.raises(PortalUntrustedError):
            assert_trusted_cn("10.9.9.8", headers, trust)
        with pytest.raises(PortalUntrustedError):
            assert_trusted_cn(None, headers, trust)

    def test_a_secret_that_is_merely_a_prefix_does_not_pass(self):
        trust = self._trust()
        headers = {
            "x-hp-portal-secret": PROXY_SECRET[:-1],
            "ssl-client-verify": "SUCCESS",
            "ssl-client-subject-dn": f"CN={CN_A}",
        }
        with pytest.raises(PortalUntrustedError):
            assert_trusted_cn(PROXY_IP, headers, trust)
