"""THE GATES for the first-run claim (#458 S1 + S2).

Asserts the OPERATOR'S GOAL is reached, never that a handler returned 200: a
fresh instance with an empty database is claimed from a browser ON ITS OWN
NETWORK with nothing to type, the credential it hands back ACTUALLY
AUTHENTICATES an admin-scoped request, and the Proxmox address/token supplied in
the same step are verified, stored, and live on the inventory service.

The second half of the file is the hardened path: an instance reached from
outside its network refuses the codeless claim and demands the code, and a
forged X-Forwarded-For cannot talk its way into the local path.

Wiring is real end to end - the real claim router, the real admin router (its
verify/store/reload path is what S2 reuses), real migrated SQLite, the real
ProxmoxClient. Only the HTTP transport under the Proxmox client is faked, so URL
building, auth headers and error handling are exercised for real.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from homepilot.admin.router import router as admin_router
from homepilot.auth.tokens import generate_api_token, hash_token
from homepilot.claim import router as claim_router_module
from homepilot.claim.repository import ClaimRepository
from homepilot.claim.router import router as claim_router
from homepilot.claim.startup import claim_code_path, ensure_claim_code
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

PVE_HOST = "pve.test"
PVE_TOKEN = "root@pam!hp=11111111-2222-3333-4444-555555555555"  # pragma: allowlist secret
# The operator's laptop on the same LAN as the instance.
OPERATOR_IP = "10.20.30.40"
# Somewhere on the internet: TEST-NET-3, guaranteed never to be a local network.
PUBLIC_IP = "203.0.113.7"
# A reverse proxy the operator listed in HP_TRUSTED_PROXIES.
PROXY_IP = "10.9.9.1"
PROXY_CIDR = "10.9.9.0/24"


class MemoryVault:
    """The vault surface admin/router.py actually uses, backed by a dict."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}

    async def get_secret(self, name: str) -> dict[str, Any]:
        from homepilot.vault import VaultError

        if name not in self.secrets:
            raise VaultError(f"Secret '{name}' not found")
        return dict(self.secrets[name])

    async def store_secret(self, name: str, value: dict[str, Any]) -> None:
        self.secrets[name] = dict(value)

    async def list_secrets(self) -> list[str]:
        return list(self.secrets)


class FakePVE:
    """A Proxmox that answers /version, faked at the HTTP transport.

    Everything above it is the real thing: the real ProxmoxClient builds the
    URL, sets the PVEAPIToken header and turns a 401 into a ProxmoxError, so a
    verification failure fails for the reason it would fail in production.
    """

    def __init__(self) -> None:
        self.status_code = 200
        self.body: dict[str, Any] = {"data": {"version": "8.2.2", "release": "8.2"}}
        self.seen: list[httpx.Request] = []

    def rejects(self) -> None:
        self.status_code = 401
        self.body = {"data": None, "errors": "authentication failure"}

    def accepts(self) -> None:
        self.status_code = 200
        self.body = {"data": {"version": "8.2.2", "release": "8.2"}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(self.status_code, json=self.body)


@pytest.fixture
def pve(monkeypatch: pytest.MonkeyPatch) -> FakePVE:
    from homepilot.adapters import proxmox as proxmox_module

    fake = FakePVE()
    real_client_cls = proxmox_module.ProxmoxClient

    class MockedProxmoxClient(real_client_cls):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            transport = httpx.MockTransport(fake.handle)
            self._client = httpx.AsyncClient(
                base_url=f"{self._base_url}/api2/json",
                headers={"Authorization": f"PVEAPIToken={self._token}"},
                transport=transport,
            )
            self._write_client = self._client

    monkeypatch.setattr(proxmox_module, "ProxmoxClient", MockedProxmoxClient)
    return fake


@pytest.fixture
async def fresh_db(tmp_path: Path):
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def instance(fresh_db: Database, tmp_path: Path, pve: FakePVE) -> Any:
    """A backend with an EMPTY database: no users, no tokens, no settings."""
    claim_router_module._claim_attempts.clear()

    app = FastAPI()
    app.include_router(claim_router)
    app.include_router(admin_router, prefix="/admin")
    app.state.repo = Repository(fresh_db)
    app.state.claim_repo = ClaimRepository(fresh_db)
    app.state.vault = MemoryVault()
    app.state.settings = SimpleNamespace(
        data_dir=str(tmp_path),
        proxmox_host="",
        proxmox_port=8006,
        proxmox_verify_ssl=False,
        admin_secret="",
    )
    app.state.proxmox = None
    # No trusted proxies by default - the shipped default of HP_TRUSTED_PROXIES.
    app.state.trusted_proxy_networks = []
    # The reconciler holds this object; S2 must rebind .proxmox onto it without
    # a restart, so the test watches the very instance the reconciler would use.
    app.state.inventory_service = SimpleNamespace(proxmox=None)
    yield SimpleNamespace(app=app, db=fresh_db, data_dir=tmp_path)
    claim_router_module._claim_attempts.clear()


def client_for(app: FastAPI, peer: str = OPERATOR_IP) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer, 51000))
    return httpx.AsyncClient(transport=transport, base_url="http://hp.test")


def trust_proxy(instance: Any, cidr: str = PROXY_CIDR) -> None:
    """What HP_TRUSTED_PROXIES=<cidr> produces at startup."""
    import ipaddress

    instance.app.state.trusted_proxy_networks = [ipaddress.ip_network(cidr)]


async def boot(instance: Any) -> str | None:
    """Run the startup step that a fresh container runs."""
    return await ensure_claim_code(instance.app.state.claim_repo, instance.data_dir)


async def token_count(db: Database) -> int:
    rows = await db.fetchall("SELECT id FROM api_tokens")
    return len(rows)


async def install_admin_token(db: Database) -> str:
    """What every pre-claim install already has: an admin-scoped API token."""
    repo = Repository(db)
    full_token, prefix, token_hash = generate_api_token()
    user_id = await repo.create_user(display_name="admin", auth_source="api_token")
    await repo.create_api_token(
        user_id=user_id,
        token_type="personal",
        prefix=prefix,
        hash=token_hash,
        scope="full",
        label="admin",
    )
    return full_token


class TestClaimJourney:
    async def test_fresh_instance_is_claimed_from_a_browser_and_ends_up_working(
        self, instance: Any
    ):
        await boot(instance)

        async with client_for(instance.app) as client:
            before = await client.get("/claim/status")
            assert before.status_code == 200
            # On its own network there is nothing for the operator to fetch.
            assert before.json() == {"state": "unclaimed", "code_required": False}

            claimed = await client.post(
                "/claim",
                json={
                    "label": "olli",
                    "proxmox_host": PVE_HOST,
                    "proxmox_token": PVE_TOKEN,
                    "proxmox_verify_ssl": False,
                },
            )
            assert claimed.status_code == 200, claimed.text
            minted = claimed.json()["token"]

            # THE GOAL, part 1: the returned credential actually opens an
            # admin-scoped route. A 200 from /claim proves nothing on its own.
            settings = await client.get(
                "/admin/settings/proxmox", headers={"Authorization": f"Bearer {minted}"}
            )
            assert settings.status_code == 200, settings.text
            body = settings.json()

            # THE GOAL, part 2: the Proxmox credentials the operator typed once
            # are stored and in use.
            assert body["host"] == PVE_HOST
            assert body["token_configured"] is True
            assert body["connection_status"] == "ok"

            after = await client.get("/claim/status")
            assert after.json() == {"state": "claimed"}

        vault = instance.app.state.vault
        assert vault.secrets["proxmox-config"]["host"] == PVE_HOST
        assert vault.secrets["pve-token"]["token"] == PVE_TOKEN

        # THE GOAL, part 3: the inventory reconciler holds a live Proxmox client
        # WITHOUT a restart.
        assert instance.app.state.inventory_service.proxmox is not None
        assert instance.app.state.proxmox is not None

        # The operator-facing copy of the code is gone once it is spent.
        assert not claim_code_path(instance.data_dir).exists()

    async def test_claiming_without_proxmox_leaves_a_usable_instance(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app) as client:
            claimed = await client.post("/claim", json={})
            assert claimed.status_code == 200, claimed.text
            assert claimed.json()["proxmox_configured"] is False
            minted = claimed.json()["token"]

            # Usable: the minted token reaches an admin route, which reports
            # Proxmox as simply not configured yet.
            settings = await client.get(
                "/admin/settings/proxmox", headers={"Authorization": f"Bearer {minted}"}
            )
            assert settings.status_code == 200, settings.text
            assert settings.json()["connection_status"] == "not_configured"
            assert (await client.get("/claim/status")).json() == {"state": "claimed"}

    async def test_the_box_gets_its_own_autocreated_admin_credential(self, instance: Any):
        """Owner rule (2026-08-26): "I know only the login token - the rest
        should be autocreated and live somewhere safe so they won't be deleted".

        Minting now requires an authenticated admin, so the CLI on the box needs a
        credential of its own. The claim writes one; the operator is never asked
        to manage it, and their own login token is never put on disk.
        """
        await boot(instance)

        async with client_for(instance.app) as client:
            claimed = await client.post("/claim", json={"label": "olli"})
            assert claimed.status_code == 200, claimed.text
            operator_token = claimed.json()["token"]

            stored_path = instance.data_dir / "api-token"
            assert stored_path.exists(), "the box was left with no credential of its own"
            stored = stored_path.read_text(encoding="utf-8").strip()
            assert stored.startswith("hp_")
            # The operator's own credential is NOT what got written to disk.
            assert stored != operator_token
            assert stored_path.stat().st_mode & 0o777 == 0o600

            # THE GOAL: the stored credential is an admin - it opens an
            # admin-scoped route, which is what `hp token create` needs.
            resp = await client.get(
                "/admin/settings/proxmox", headers={"Authorization": f"Bearer {stored}"}
            )
            assert resp.status_code == 200, resp.text

        # It is a real token, listed and revocable like any other.
        rows = await instance.db.fetchall("SELECT label FROM api_tokens")
        assert "local-cli" in {r["label"] for r in rows}

    async def test_half_a_proxmox_pair_is_refused_and_does_not_claim(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app) as client:
            partial = await client.post("/claim", json={"proxmox_host": PVE_HOST})
            assert partial.status_code == 400
            assert (await client.get("/claim/status")).json()["state"] == "unclaimed"
            assert await token_count(instance.db) == 0


class TestClaimIsSingleUse:
    async def test_second_post_with_the_same_correct_code_is_gone_and_mints_nothing(
        self, instance: Any
    ):
        code = await boot(instance)

        async with client_for(instance.app) as client:
            first = await client.post("/claim", json={"code": code})
            assert first.status_code == 200, first.text
            # Two: the operator's own token, and the box's autocreated CLI
            # credential (owner rule 2026-08-26 - the human keeps one token,
            # anything internal is autocreated and persisted).
            minted_once = await token_count(instance.db)
            assert minted_once == 2

            second = await client.post("/claim", json={"code": code})
            assert second.status_code == 410, second.text
            assert "token" not in second.json()

        assert await token_count(instance.db) == minted_once

    async def test_a_second_codeless_claim_from_the_same_network_is_gone(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app) as client:
            assert (await client.post("/claim", json={})).status_code == 200
            second = await client.post("/claim", json={})

        assert second.status_code == 410, second.text
        # The operator's token plus the box's autocreated CLI credential.
        assert await token_count(instance.db) == 2

    async def test_a_claimed_instance_keeps_refusing_after_a_restart(self, instance: Any):
        code = await boot(instance)
        async with client_for(instance.app) as client:
            assert (await client.post("/claim", json={"code": code})).status_code == 200

        # Restart: startup must not resurrect the claim path or print a code.
        assert await boot(instance) is None
        async with client_for(instance.app) as client:
            assert (await client.get("/claim/status")).json() == {"state": "claimed"}
            assert (await client.post("/claim", json={"code": code})).status_code == 410
            assert (await client.post("/claim", json={})).status_code == 410


class TestWrongCode:
    """A code that IS presented must be right, even from the local network:
    silently ignoring a wrong one would teach an operator that any code works."""

    async def test_wrong_code_does_not_claim_and_reveals_nothing(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app) as client:
            bad = await client.post("/claim", json={"code": "hpc_" + "0" * 32})
            assert bad.status_code == 401
            # Uniform text: nothing about whether a code exists, how long it is,
            # or how many tries are left.
            assert bad.json()["detail"] == "Invalid claim code"
            assert (await client.get("/claim/status")).json()["state"] == "unclaimed"

        assert await token_count(instance.db) == 0

    async def test_wrong_code_of_the_right_shape_reads_the_same_as_nonsense(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app) as client:
            shaped = await client.post("/claim", json={"code": "hpc_" + "a" * 32})
            garbage = await client.post("/claim", json={"code": "not-a-code"})

        assert shaped.status_code == garbage.status_code == 401
        assert shaped.json() == garbage.json()

    async def test_guessing_is_rate_limited(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app) as client:
            statuses = [
                (await client.post("/claim", json={"code": f"hpc_{i:032d}"})).status_code
                for i in range(8)
            ]

        assert statuses[:5] == [401] * 5
        assert statuses[5:] == [429] * 3
        assert await token_count(instance.db) == 0

    async def test_the_limit_is_per_client_not_global(self, instance: Any):
        """One stranger hammering the endpoint must not lock the operator out of
        their own first login."""
        code = await boot(instance)

        async with client_for(instance.app, peer="10.0.0.9") as attacker:
            for i in range(6):
                await attacker.post("/claim", json={"code": f"hpc_{i:032d}"})

        async with client_for(instance.app, peer=OPERATOR_IP) as operator:
            allowed = await operator.post("/claim", json={"code": code})
        assert allowed.status_code == 200, allowed.text


class TestRestartSafety:
    async def test_an_unclaimed_restart_keeps_the_same_code(self, instance: Any):
        first_code = await boot(instance)
        stored = await instance.app.state.claim_repo.get()

        second_code = await boot(instance)
        after = await instance.app.state.claim_repo.get()

        assert second_code == first_code
        assert after["code_hash"] == stored["code_hash"]
        assert after["created_at"] == stored["created_at"]

        # The code printed at FIRST boot still works after the restart - an
        # operator scrolling back must never find a stale code.
        async with client_for(instance.app) as client:
            assert (await client.post("/claim", json={"code": first_code})).status_code == 200

    async def test_the_stored_code_is_only_a_hash(self, instance: Any):
        code = await boot(instance)
        row = await instance.app.state.claim_repo.get()

        assert code not in str(dict(row))
        assert row["code_hash"] == hash_token(code)
        assert row["code_prefix"] == code[:16]

    async def test_a_lost_code_file_rotates_loudly_rather_than_dead_ending(
        self, instance: Any, caplog: pytest.LogCaptureFixture
    ):
        first_code = await boot(instance)
        claim_code_path(instance.data_dir).unlink()

        with caplog.at_level(logging.WARNING, logger="homepilot.claim.startup"):
            second_code = await boot(instance)

        assert second_code != first_code
        assert "NEW code" in caplog.text
        async with client_for(instance.app) as client:
            assert (await client.post("/claim", json={"code": first_code})).status_code == 401
            assert (await client.post("/claim", json={"code": second_code})).status_code == 200


class TestExistingInstalls:
    async def test_an_instance_with_an_admin_token_never_prints_a_code(
        self, instance: Any, caplog: pytest.LogCaptureFixture
    ):
        await install_admin_token(instance.db)

        with caplog.at_level(logging.DEBUG, logger="homepilot.claim.startup"):
            assert await boot(instance) is None

        # Nothing that could BE a code reaches the log, and none is written.
        assert "hpc_" not in caplog.text
        assert not claim_code_path(instance.data_dir).exists()

    async def test_an_admin_credential_seen_at_boot_latches_the_claim_shut(self, instance: Any):
        """Review #648: revoking the last admin token used to REOPEN the claim.

        `is_claimed()` answers True as soon as any admin-capable token exists,
        and on an instance bootstrapped by `hp init` (or by a token minted in the
        console, or on an install predating the claim) that was the ONLY thing
        holding the claim shut - `claimed_at` stayed NULL for ever. Delete that
        token, which is the ordinary first half of a rotation, and the instance
        became claimable again; on a private network, claimable with NO CODE,
        handing a fresh superuser token to whoever asked first.

        TEETH: remove the `latch_claimed_externally` call from
        `ensure_claim_code` and this fails - the status flips back to unclaimed
        and the codeless POST mints a token.
        """
        await install_admin_token(instance.db)
        await boot(instance)

        claims = instance.app.state.claim_repo
        row = await claims.get()
        assert row is not None and row["claimed_at"] is not None, (
            "boot saw an admin credential and did not latch the claim shut"
        )

        # The operator rotates: revoke first, mint second. Between the two the
        # instance holds no admin token at all.
        await instance.db.execute("DELETE FROM api_tokens")
        await instance.db.conn.commit()
        assert await token_count(instance.db) == 0

        async with client_for(instance.app) as client:
            assert (await client.get("/claim/status")).json() == {"state": "claimed"}
            reopened = await client.post("/claim", json={"label": "attacker"})
            assert reopened.status_code == 410, (
                "the claim path reopened when the last admin token was revoked - "
                f"a local caller just minted {reopened.json()}"
            )
        assert await token_count(instance.db) == 0

    async def test_a_stale_claim_code_file_is_removed_once_the_instance_is_claimed(
        self, instance: Any
    ):
        """The code file is documented as "deleted the moment the claim
        succeeds", and POST /claim does delete it - but an instance claimed by
        any OTHER route never ran that cleanup, so the plaintext code sat in the
        data directory for the life of the instance. Found on dev at 3.6.14,
        where `.claim_code` was still present two days after the instance was
        in use."""
        await boot(instance)
        assert claim_code_path(instance.data_dir).exists()

        await install_admin_token(instance.db)
        await boot(instance)

        assert not claim_code_path(instance.data_dir).exists(), (
            "the plaintext claim code outlived the claim"
        )

    async def test_an_instance_with_an_admin_token_reports_claimed(self, instance: Any):
        await install_admin_token(instance.db)

        async with client_for(instance.app) as client:
            assert (await client.get("/claim/status")).json() == {"state": "claimed"}
            gone = await client.post("/claim", json={"code": "hpc_" + "0" * 32})
            assert gone.status_code == 410

    async def test_a_token_minted_elsewhere_closes_the_claim_without_a_restart(self, instance: Any):
        """`hp init` stays supported: the moment it mints an admin token, the
        claim path shuts, with no restart in between."""
        code = await boot(instance)

        async with client_for(instance.app) as client:
            assert (await client.get("/claim/status")).json()["state"] == "unclaimed"
            await install_admin_token(instance.db)
            assert (await client.get("/claim/status")).json() == {"state": "claimed"}
            assert (await client.post("/claim", json={"code": code})).status_code == 410


class TestProxmoxVerification:
    async def test_bad_credentials_do_not_consume_the_claim_or_store_anything(
        self, instance: Any, pve: FakePVE
    ):
        code = await boot(instance)
        pve.rejects()

        async with client_for(instance.app) as client:
            refused = await client.post(
                "/claim",
                json={
                    "code": code,
                    "proxmox_host": PVE_HOST,
                    "proxmox_token": "root@pam!typo=nope",  # pragma: allowlist secret
                },
            )
            assert refused.status_code == 400, refused.text
            assert "Proxmox" in refused.json()["detail"]

            # The claim is untouched...
            assert (await client.get("/claim/status")).json()["state"] == "unclaimed"

        # ...nothing was stored...
        assert instance.app.state.vault.secrets == {}
        assert await token_count(instance.db) == 0

    async def test_the_operator_can_retry_with_corrected_values(self, instance: Any, pve: FakePVE):
        code = await boot(instance)

        pve.rejects()
        async with client_for(instance.app) as client:
            failed = await client.post(
                "/claim",
                json={"code": code, "proxmox_host": PVE_HOST, "proxmox_token": "wrong"},
            )
            assert failed.status_code == 400

            pve.accepts()
            retried = await client.post(
                "/claim",
                json={"code": code, "proxmox_host": PVE_HOST, "proxmox_token": PVE_TOKEN},
            )
            assert retried.status_code == 200, retried.text

            minted = retried.json()["token"]
            settings = await client.get(
                "/admin/settings/proxmox", headers={"Authorization": f"Bearer {minted}"}
            )
            assert settings.json()["host"] == PVE_HOST


class TestLocalPathIsTheNormalPath:
    """The appliance model: on its own network, an unclaimed instance is claimed
    from the browser with nothing to type."""

    async def test_loopback_claims_with_no_code_at_all(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app, peer="127.0.0.1") as client:
            assert (await client.get("/claim/status")).json()["code_required"] is False
            claimed = await client.post("/claim", json={})
            assert claimed.status_code == 200, claimed.text

            # THE GOAL: the codeless claim produced a working admin credential.
            minted = claimed.json()["token"]
            settings = await client.get(
                "/admin/settings/proxmox", headers={"Authorization": f"Bearer {minted}"}
            )
            assert settings.status_code == 200, settings.text

    @pytest.mark.parametrize("peer", ["127.0.0.1", "10.1.2.3", "172.16.9.9", "192.168.1.50", "::1"])
    async def test_every_local_range_gets_the_codeless_path(self, instance: Any, peer: str):
        await boot(instance)

        async with client_for(instance.app, peer=peer) as client:
            assert (await client.post("/claim", json={})).status_code == 200


class TestHardenedPathWhenExposed:
    async def test_a_public_source_cannot_claim_without_the_code(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app, peer=PUBLIC_IP) as client:
            status = await client.get("/claim/status")
            assert status.json() == {"state": "unclaimed", "code_required": True}

            refused = await client.post("/claim", json={})
            assert refused.status_code == 403, refused.text
            assert "claim code is required" in refused.json()["detail"]
            assert "hp claim-code" in refused.json()["detail"]

        assert await token_count(instance.db) == 0

    async def test_the_same_public_source_claims_with_the_correct_code(self, instance: Any):
        code = await boot(instance)

        async with client_for(instance.app, peer=PUBLIC_IP) as client:
            assert (await client.post("/claim", json={})).status_code == 403
            claimed = await client.post("/claim", json={"code": code})
            assert claimed.status_code == 200, claimed.text

            # THE GOAL: the hardened path ends in the same working credential.
            minted = claimed.json()["token"]
            settings = await client.get(
                "/admin/settings/proxmox", headers={"Authorization": f"Bearer {minted}"}
            )
            assert settings.status_code == 200, settings.text


class TestForgedForwardingHeaders:
    """The one that matters most: a header a client can set must never promote
    that client to 'on my network'."""

    async def test_a_forged_x_forwarded_for_does_not_buy_the_codeless_path(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app, peer=PUBLIC_IP) as client:
            forged = await client.post(
                "/claim", json={}, headers={"X-Forwarded-For": "192.168.1.10"}
            )
            assert forged.status_code == 403, forged.text
            status = await client.get("/claim/status", headers={"X-Forwarded-For": "192.168.1.10"})
            assert status.json()["code_required"] is True

        assert await token_count(instance.db) == 0

    async def test_a_forged_x_real_ip_does_not_buy_the_codeless_path(self, instance: Any):
        await boot(instance)

        async with client_for(instance.app, peer=PUBLIC_IP) as client:
            forged = await client.post("/claim", json={}, headers={"X-Real-IP": "10.0.0.5"})
            assert forged.status_code == 403, forged.text

    async def test_a_forwarding_header_from_an_untrusted_local_peer_fails_closed(
        self, instance: Any
    ):
        """An unlisted relay on the LAN forwarding a public client: the peer is
        private, so judging the peer would hand the codeless path to the
        internet. The header's presence alone makes the source untrusted."""
        await boot(instance)

        async with client_for(instance.app, peer="10.20.30.40") as client:
            refused = await client.post("/claim", json={}, headers={"X-Forwarded-For": PUBLIC_IP})
            assert refused.status_code == 403, refused.text

    async def test_a_forged_header_never_blocks_the_legitimate_coded_claim(self, instance: Any):
        code = await boot(instance)

        async with client_for(instance.app, peer=PUBLIC_IP) as client:
            claimed = await client.post(
                "/claim", json={"code": code}, headers={"X-Forwarded-For": "192.168.1.10"}
            )
            assert claimed.status_code == 200, claimed.text


class TestTrustedProxy:
    async def test_a_genuinely_local_client_through_a_trusted_proxy_gets_the_local_path(
        self, instance: Any
    ):
        await boot(instance)
        trust_proxy(instance)

        async with client_for(instance.app, peer=PROXY_IP) as client:
            headers = {"X-Forwarded-For": "192.168.1.10, 10.9.9.1"}
            status = await client.get("/claim/status", headers=headers)
            assert status.json()["code_required"] is False

            claimed = await client.post("/claim", json={}, headers=headers)
            assert claimed.status_code == 200, claimed.text

            minted = claimed.json()["token"]
            probe = await client.get(
                "/admin/settings/proxmox", headers={"Authorization": f"Bearer {minted}"}
            )
            assert probe.status_code == 200, probe.text

    async def test_a_public_client_through_a_trusted_proxy_still_needs_the_code(
        self, instance: Any
    ):
        code = await boot(instance)
        trust_proxy(instance)

        async with client_for(instance.app, peer=PROXY_IP) as client:
            headers = {"X-Forwarded-For": PUBLIC_IP}
            assert (await client.post("/claim", json={}, headers=headers)).status_code == 403
            assert (
                await client.post("/claim", json={"code": code}, headers=headers)
            ).status_code == 200

    async def test_a_trusted_proxy_that_forwards_no_client_fails_closed(self, instance: Any):
        """The proxy's own address is not the client's. With nothing forwarded
        there is nobody to evaluate, so the codeless path is refused."""
        await boot(instance)
        trust_proxy(instance)

        async with client_for(instance.app, peer=PROXY_IP) as client:
            assert (await client.post("/claim", json={})).status_code == 403
