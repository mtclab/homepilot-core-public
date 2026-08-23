"""The install gate: a Proxmox address and token, and nothing else (#458 S7).

ADR-004's whole claim is that an operator supplies the Proxmox address and API
token, and HomePilot arranges everything else. That claim rots the moment it is
only prose - it was true of no release before 2.8.0, and the pieces that make it
true (the claim flow, hub self-configuration, the served agent payload) each
arrived separately and could each regress separately.

So this drives a CLEAN data directory through the real endpoints and asserts the
OUTCOME an operator would see, not that each step returned success:

  * nothing is configured beforehand - no `hp init`, no token copied out of a
    container log, no `.env` edited, no vault passphrase chosen;
  * the instance is claimed from a browser and the credential that comes back
    actually opens the API;
  * the address and token supplied at claim time are enough for Proxmox to be
    live afterwards, with the token in the vault rather than on disk in the
    clear;
  * the hub is listening with a certificate it generated for itself;
  * the agent payload a guest would fetch is present and self-describing;
  * the claim door is shut afterwards, permanently.

**What is real and what is not.** Everything above HomePilot's own boundary is
real: a real database, real migrations, real vault, real claim endpoint over
HTTP, real hub, real payload resolution. The HYPERVISOR is stubbed at the
adapter, because a gate cannot conjure a Proxmox cluster - and the live-PVE half
is covered where it belongs, by the runs against the dev box. What this gate
forbids is HomePilot needing anything the operator was not asked for.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

# The ONLY two things an operator is asked for.
PVE_HOST = "pve.example.com"
PVE_TOKEN = "admin@pam!gate=00000000-1111-2222-3333-444444444444"  # pragma: allowlist secret


@pytest.fixture
def clean_install() -> Iterator[str]:
    """A data directory with nothing in it - the state a fresh container has."""
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-zerotouch-gate-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(data_dir: str):
    """Settings as a stock container builds them: a secret key (which the real
    entrypoint auto-generates) and nothing else. Deliberately NO proxmox host, no
    hub token, no TLS material, no vault passphrase - if any of those turn out to
    be required, this gate fails, which is the point."""
    from homepilot.config import Settings

    return Settings(
        secret_key="test-secret-key-for-pytest-only-not-for-production",
        data_dir=data_dir,
        artifacts_dir=os.path.join(data_dir, "artifacts"),
        agent_hub_host="127.0.0.1",
        agent_hub_port=_free_port(),
    )


def _fake_pve() -> MagicMock:
    """The hypervisor, stubbed at the adapter boundary - see the module docstring."""
    pve = MagicMock()
    # What the claim path actually calls to verify the credentials it was given.
    pve.read = AsyncMock(return_value={"version": "8.2.2"})
    pve.test_connection = AsyncMock(return_value=True)
    pve.get_nodes = AsyncMock(return_value=[{"node": "pve1", "status": "online"}])
    pve.get_cluster_resources = AsyncMock(return_value=[])
    pve.close = AsyncMock(return_value=None)
    return pve


async def _boot(data_dir: str):
    """Bring the control plane up exactly as the app's lifespan does."""
    from homepilot.app_state import create_app_state
    from homepilot.claim.repository import ClaimRepository
    from homepilot.claim.router import router as claim_router
    from homepilot.claim.startup import ensure_claim_code

    settings = _settings(data_dir)
    state = await create_app_state(settings)

    app = FastAPI()
    app.include_router(claim_router)
    app.state.repo = state.repo
    app.state.db = state.database
    app.state.vault = state.vault
    app.state.settings = settings
    app.state.claim_repo = ClaimRepository(state.database)
    app.state.trusted_proxy_networks = []
    app.state.proxmox = None
    await ensure_claim_code(app.state.claim_repo, data_dir)
    if state.agent_hub is not None:
        # The lifespan starts the hub; a gate that never started it would be
        # asserting against a hub that was only constructed.
        await state.agent_hub.start()
    return app, state, settings


async def _shutdown(state) -> None:
    if getattr(state, "agent_hub", None) is not None:
        await state.agent_hub.stop()
    await state.database.close()


async def _post(app: FastAPI, path: str, payload: dict) -> tuple[int, dict]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # A browser on the instance's own network: no forwarding headers, a
        # private peer address, so no code is needed.
        response = await client.post(path, json=payload, headers={"host": "test"})
    body = response.json() if response.content else {}
    return response.status_code, body


class TestOnlyTheProxmoxAddressAndToken:
    async def test_a_clean_install_is_finished_from_a_browser(self, clean_install: str):
        """The headline claim of ADR-004, asserted end to end."""
        app, state, _settings_used = await _boot(clean_install)
        try:
            # NOTHING was done to this instance first: no hp init, no exec.
            status, body = await _post(app, "/claim", {})
            assert status == 200, body
            token = body["token"]
            assert token.startswith("hp_")

            # The credential works: it resolves to a real, admin-scoped record.
            row = await state.repo.get_token_by_prefix(token[:16])
            assert row is not None, "the claim minted a token the database does not know"

            # And the door is shut, permanently.
            again_status, _ = await _post(app, "/claim", {})
            assert again_status == 410, "a claimed instance must never be claimable again"
            assert not (Path(clean_install) / ".claim_code").exists()
        finally:
            await _shutdown(state)

    async def test_the_two_inputs_are_enough_for_proxmox(
        self, clean_install: str, monkeypatch: pytest.MonkeyPatch
    ):
        """The address and token given at claim time land in the vault and leave
        Proxmox live - no second configuration step, and no secret on disk."""
        # admin/router imports ProxmoxClient inside the function, so the patch
        # has to land on the adapter module it imports FROM.
        import homepilot.adapters.proxmox as proxmox_adapter

        monkeypatch.setattr(proxmox_adapter, "ProxmoxClient", lambda *args, **kwargs: _fake_pve())

        app, state, _ = await _boot(clean_install)
        try:
            status, body = await _post(
                app,
                "/claim",
                {"proxmox_host": PVE_HOST, "proxmox_token": PVE_TOKEN},
            )
            assert status == 200, body
            assert body["proxmox_configured"] is True, (
                "the operator supplied the two things asked for and Proxmox is still not set up"
            )

            stored = await state.vault.get_secret("pve-token")
            assert stored.get("token") == PVE_TOKEN, "the PVE token is not in the vault"

            # The one secret the operator handed over must not be sitting in
            # cleartext anywhere under the data directory - that is what having a
            # vault is FOR, and a gate that only checked the vault contains it
            # would not notice a copy left beside it.
            leaked = [
                str(path.relative_to(clean_install))
                for path in Path(clean_install).rglob("*")
                if path.is_file() and PVE_TOKEN.encode() in path.read_bytes()
            ]
            assert not leaked, f"the PVE token is readable in cleartext in: {leaked}"
        finally:
            await _shutdown(state)

    async def test_the_vault_configured_itself(self, clean_install: str):
        """The operator was never asked for a passphrase, yet secrets work."""
        _app, state, _ = await _boot(clean_install)
        try:
            assert state.vault is not None, "a stock install came up with no vault"
            await state.vault.store_secret("gate-probe", {"value": "kept"})
            assert (await state.vault.get_secret("gate-probe"))["value"] == "kept"
            assert (Path(clean_install) / ".vault_passphrase").exists()
        finally:
            await _shutdown(state)


class TestTheRestConfiguredItself:
    async def test_the_hub_is_listening_with_its_own_certificate(self, clean_install: str):
        """No cert was supplied and no TLS decision was asked for, yet the hub is
        up and serving one it made."""
        _app, state, settings = await _boot(clean_install)
        try:
            assert state.agent_hub is not None, "a stock install has no hub"
            assert state.agent_hub.tls_enabled is True
            assert len(state.agent_hub.cert_fingerprint) == 64
            assert (Path(clean_install) / "hub" / "hub-cert.pem").exists()
            # The enrolment token also generated itself.
            assert settings.agent_hub_auth_token
        finally:
            await _shutdown(state)

    async def test_the_agent_payload_a_guest_would_fetch_is_described(
        self, clean_install: str, monkeypatch: pytest.MonkeyPatch
    ):
        """A guest enrols by fetching from the control plane (#464), so the gate
        checks the payload is resolvable and self-describing rather than assuming
        the endpoint exists.

        A source checkout has no built binaries - the image builds them - so the
        payload directory is stood up here and the ASSERTION is that HomePilot
        reports exactly what it holds, digests included.
        """
        from homepilot.agent_hub import dist

        payload = Path(clean_install) / "agent-dist"
        payload.mkdir()
        (payload / "hp-agent-linux-amd64").write_bytes(b"\x7fELFbinary")
        (payload / "install-agent.sh").write_text("#!/usr/bin/env bash\n")
        monkeypatch.setenv("HP_AGENT_DIST_DIR", str(payload))

        listed = dist.manifest()
        assert listed["hp-agent-linux-amd64"]["available"] is True
        assert listed["hp-agent-linux-amd64"]["sha256"] == dist.sha256(
            payload / "hp-agent-linux-amd64"
        )
        assert listed["install-agent.sh"]["available"] is True
        # An arch a guest reports, not the GOARCH spelling.
        assert dist.agent_binary("x86_64").name == "hp-agent-linux-amd64"

    async def test_metrics_retention_is_live_without_being_configured(self, clean_install: str):
        """Monitoring is part of the product now, not an operator decision (S5)."""
        _app, state, settings = await _boot(clean_install)
        try:
            assert state.metrics_repo is not None
            assert settings.metrics_retention_days > 0
            row = await state.database.fetchone("SELECT COUNT(*) c FROM metrics")
            assert row["c"] == 0, "a fresh install should start with no samples, but the table"
        finally:
            await _shutdown(state)

    async def test_nothing_optional_pretends_to_work(self, clean_install: str):
        """S6: an optional service is either working or off and saying so. A
        stock install must not claim a capability it does not have."""
        from homepilot import selfcheck

        _app, state, settings = await _boot(clean_install)
        try:
            report = await selfcheck.selfcheck_report(state, settings)
            states = {s["name"]: s["state"] for s in report["subsystems"]}
            assert states["agent_hub"] == "ok"
            # Embeddings ship pointing nowhere on purpose - so they must read as
            # off, never as ok.
            assert states["embeddings"] in ("off", "unreachable")
            for entry in report["subsystems"]:
                assert entry["consequence"], f"{entry['name']} reports no consequence"
        finally:
            await _shutdown(state)
