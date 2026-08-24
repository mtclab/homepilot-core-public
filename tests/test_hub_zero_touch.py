"""The agent hub must configure itself on a default install (ADR-004 S3, #454).

The bug these gates forbid: `HP_AGENT_HUB_ENABLED` defaulted to false, and
turning it on demanded a shared token plus either a TLS cert/key pair or the
`HP_HUB_ALLOW_INSECURE` override, because the hub fails closed on a plaintext
non-loopback bind. The fail-closed check is correct. What was wrong is that
satisfying it required an operator decision, which is why the dev box could not
run any 2.6.0+ image from compose at all.

These assert the OUTCOME, not the call:
  * the hub actually LISTENS and completes a real TLS handshake on a default
    config - no config flag is trusted as evidence;
  * the certificate an agent pinned is still the certificate served after a
    restart;
  * the fail-closed guard still refuses plaintext, proven by constructing that
    case directly rather than by reading the code.

Teeth: delete the `ensure_hub_certificate` call in
``app_state.create_app_state`` and ``test_default_config_hub_serves_tls`` fails
with the fail-closed RuntimeError - the exact error #454 was stuck on.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import socket
import ssl
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from homepilot.agent_hub.selfconfig import certificate_fingerprint, ensure_hub_certificate
from homepilot.agent_hub.server import AgentHubServer
from homepilot.app_state import create_app_state
from homepilot.config import Settings


@pytest.fixture
def hp_dir() -> Iterator[str]:
    """A data dir that exists only for one test.

    Must live under $HOME: create_app_state refuses an artifacts_dir under /tmp
    and friends."""
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-zerotouch-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(hp_dir: str, **overrides: object) -> Settings:
    """A DEFAULT install: nothing about the hub is configured. No TLS env, no
    token, and the stock non-loopback bind - the exact shape that used to
    refuse to start."""
    return Settings(
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        agent_hub_port=_free_port(),
        **overrides,  # type: ignore[arg-type]
    )


async def _served_cert_fingerprint(host: str, port: int, ca_file: str | None) -> str:
    """Complete a real TLS handshake against the hub and return the sha256 of
    the certificate it actually presented.

    When ``ca_file`` is given the handshake is FULLY verified (chain + hostname)
    against it, so a passing call proves the served certificate is the generated
    one and that it is usable as its own trust anchor."""
    if ca_file:
        ctx = ssl.create_default_context(cafile=ca_file)
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(host, port, ssl=ctx, server_hostname="localhost")
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(True)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, ssl.SSLError):
            await asyncio.wait_for(writer.wait_closed(), timeout=5)
    del reader
    assert der is not None
    return hashlib.sha256(der).hexdigest()


async def _start_hub(settings: Settings):
    state = await create_app_state(settings)
    assert state.agent_hub is not None, "hub must be enabled on a default install"
    await state.agent_hub.start()
    return state


async def _shutdown(state) -> None:
    if state.agent_hub is not None:
        await state.agent_hub.stop()
    await state.database.close()


class TestDefaultInstallStarts:
    async def test_default_config_hub_serves_tls(self, hp_dir: str):
        """A default install starts AND the hub is reachable over TLS.

        The evidence is a completed, fully verified handshake against the
        generated certificate - not `settings.agent_hub_tls`."""
        settings = _settings(hp_dir)
        assert settings.agent_hub_enabled is True
        # A comparison against the stock wildcard bind, not a bind.
        assert settings.agent_hub_host == "0.0.0.0"
        assert not settings.agent_hub_tls_cert and not settings.agent_hub_tls_key
        assert not settings.agent_hub_allow_insecure

        state = await _start_hub(settings)
        try:
            cert_file = str(Path(hp_dir) / "hub" / "hub-cert.pem")
            served = await _served_cert_fingerprint("127.0.0.1", settings.agent_hub_port, cert_file)
            assert served == state.agent_hub.cert_fingerprint
            assert state.agent_hub.tls_enabled
        finally:
            await _shutdown(state)

    async def test_shared_token_is_generated_and_persisted(self, hp_dir: str):
        """An operator supplies no token, yet enrolment has one - and the same
        one after a restart, so a pending install one-liner stays valid."""
        first = _settings(hp_dir)
        assert first.agent_hub_auth_token, "hub token must self-generate"
        token_file = Path(hp_dir) / ".agent_hub_token"
        assert token_file.stat().st_mode & 0o777 == 0o600

        second = _settings(hp_dir)
        assert second.agent_hub_auth_token == first.agent_hub_auth_token

    def test_explicit_token_is_never_overwritten(self, hp_dir: str):
        configured = _settings(hp_dir, agent_hub_auth_token="operator-chose-this")
        assert configured.agent_hub_auth_token == "operator-chose-this"
        assert not (Path(hp_dir) / ".agent_hub_token").exists()

    def test_generated_key_is_private(self, hp_dir: str):
        _cert, key = ensure_hub_certificate(Path(hp_dir))
        assert key.stat().st_mode & 0o777 == 0o600


class TestCertificatePersists:
    async def test_second_startup_serves_the_same_certificate(self, hp_dir: str):
        """An agent that pinned the hub at enrolment must still recognise it
        after a restart, so the certificate is reused rather than reissued."""
        first = await _start_hub(_settings(hp_dir))
        try:
            first_fp = first.agent_hub.cert_fingerprint
        finally:
            await _shutdown(first)

        cert_path = Path(hp_dir) / "hub" / "hub-cert.pem"
        mtime = cert_path.stat().st_mtime_ns

        settings = _settings(hp_dir)
        second = await _start_hub(settings)
        try:
            served = await _served_cert_fingerprint(
                "127.0.0.1", settings.agent_hub_port, str(cert_path)
            )
            assert served == first_fp, "restart served a different certificate"
            assert cert_path.stat().st_mtime_ns == mtime, "certificate was rewritten"
        finally:
            await _shutdown(second)

    def test_expired_certificate_is_replaced(self, hp_dir: str):
        """Reuse must not outlive validity: an expired certificate is the one
        case that DOES regenerate."""
        import datetime as dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        hub_dir = Path(hp_dir) / "hub"
        hub_dir.mkdir(parents=True)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "stale")])
        past = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
        stale = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(past - dt.timedelta(days=1))
            .not_valid_after(past)
            .sign(key, hashes.SHA256())
        )
        (hub_dir / "hub-cert.pem").write_bytes(stale.public_bytes(serialization.Encoding.PEM))
        (hub_dir / "hub-key.pem").write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        stale_fp = certificate_fingerprint(hub_dir / "hub-cert.pem")

        cert, _key = ensure_hub_certificate(Path(hp_dir))
        assert certificate_fingerprint(cert) != stale_fp


class TestOperatorSettingsStillWin:
    async def test_supplied_cert_and_key_beat_the_generated_one(self, hp_dir: str):
        """An operator-supplied certificate is what gets served, and no
        generated pair is created behind their back."""
        supplied_dir = Path(hp_dir) / "supplied"
        supplied_dir.mkdir()
        cert, key = ensure_hub_certificate(supplied_dir)
        supplied_fp = certificate_fingerprint(cert)

        settings = _settings(
            hp_dir,
            agent_hub_tls_cert=str(cert),
            agent_hub_tls_key=str(key),
        )
        state = await _start_hub(settings)
        try:
            served = await _served_cert_fingerprint("127.0.0.1", settings.agent_hub_port, str(cert))
            assert served == supplied_fp
            assert state.agent_hub.cert_fingerprint == supplied_fp
            assert not (Path(hp_dir) / "hub" / "hub-cert.pem").exists()
        finally:
            await _shutdown(state)

    async def test_explicit_plaintext_install_keeps_plaintext(
        self, hp_dir: str, monkeypatch: pytest.MonkeyPatch
    ):
        """Upgrade path: an install that already chose the insecure override
        must not be silently switched to TLS - its agents dial plaintext.

        The override is env-only (it is declared with an AliasChoices
        validation alias), which is also how a real install sets it."""
        monkeypatch.setenv("HP_HUB_ALLOW_INSECURE", "1")
        settings = _settings(hp_dir)
        assert settings.agent_hub_allow_insecure is True
        state = await _start_hub(settings)
        try:
            assert not state.agent_hub.tls_enabled
            assert state.agent_hub.cert_fingerprint == ""
            assert not (Path(hp_dir) / "hub" / "hub-cert.pem").exists()
        finally:
            await _shutdown(state)

    async def test_explicit_tls_beats_the_insecure_override(
        self, hp_dir: str, monkeypatch: pytest.MonkeyPatch
    ):
        """The override only preserves plaintext for an install that never
        touched HP_AGENT_HUB_TLS. Asking for TLS explicitly gets TLS."""
        monkeypatch.setenv("HP_HUB_ALLOW_INSECURE", "1")
        settings = _settings(hp_dir, agent_hub_tls=True)
        state = await _start_hub(settings)
        try:
            assert state.agent_hub.tls_enabled
            served = await _served_cert_fingerprint(
                "127.0.0.1", settings.agent_hub_port, str(Path(hp_dir) / "hub" / "hub-cert.pem")
            )
            assert served == state.agent_hub.cert_fingerprint
        finally:
            await _shutdown(state)


class TestEnrollmentHandsOutTheTrustAnchor:
    """An agent cannot verify a self-signed hub unless it is TOLD which
    certificate is the hub's. The enrollment endpoints carry that fingerprint,
    which the installer turns into HP_AGENT_TLS_PIN.

    Teeth: drop ``hub_cert_sha256`` from the enrollment payload and the one-liner
    can only enroll agents that skip verification."""

    async def test_enrollment_payload_pins_the_served_certificate(self, hp_dir: str):
        from homepilot.agent_hub import router

        settings = _settings(hp_dir)
        state = await _start_hub(settings)
        try:
            fields = router._hub_tls_fields(state.agent_hub)
            assert fields["hub_tls"] is True
            served = await _served_cert_fingerprint(
                "127.0.0.1",
                settings.agent_hub_port,
                str(Path(hp_dir) / "hub" / "hub-cert.pem"),
            )
            assert fields["hub_cert_sha256"] == served
            assert len(fields["hub_cert_sha256"]) == 64
        finally:
            await _shutdown(state)

    def test_no_pin_is_advertised_without_tls(self):
        from homepilot.agent_hub import router

        hub = AgentHubServer(host="127.0.0.1", port=8443, ssl_context=None)
        assert router._hub_tls_fields(hub) == {"hub_tls": False, "hub_cert_sha256": ""}


class TestFailClosedGuardIntact:
    def test_plaintext_non_loopback_still_refuses_to_start(self):
        """Constructed directly, so the guard is proven rather than assumed:
        no TLS context, a routable bind, no override."""
        hub = AgentHubServer(host="10.0.0.5", port=8443, ssl_context=None, allow_insecure=False)
        with pytest.raises(RuntimeError, match="refusing to start"):
            hub.check_transport_security()

    async def test_explicit_tls_false_still_refuses_the_hub(self, hp_dir: str):
        """Turning TLS off explicitly on a routable bind is still refused - the
        new default did not turn the guard into a formality.

        The HUB is refused. The control plane is not: see
        ``TestHubRefusalNeverKillsTheControlPlane`` below, which is the half this
        test used to get wrong by asserting create_app_state raised."""
        settings = _settings(hp_dir, agent_hub_tls=False)
        state = await create_app_state(settings)
        assert state.agent_hub is None, "plaintext on a routable bind must not serve"
        assert "refusing to start" in state.agent_hub_disabled_reason


class TestHubRefusalNeverKillsTheControlPlane:
    """#468: a misconfigured optional subsystem must not take the product down.

    The old behaviour let ``check_transport_security`` raise out of
    ``create_app_state``, so an install carrying ``HP_AGENT_HUB_TLS=false`` -
    the correct setting before 2.6.0, and what the dev box had - died on
    startup: no API, no UI, no inventory, no provisioning, container
    restart-looping. Reproduced live against the real dev database before
    these gates were written.

    Teeth: restore the bare ``agent_hub.check_transport_security()`` call in
    ``app_state.create_app_state`` and every test in this class fails with the
    fail-closed RuntimeError instead of getting a usable AppState back.
    """

    async def test_control_plane_survives_a_refused_hub(self, hp_dir: str):
        """The OUTCOME an operator needs: the rest of the product still works.

        Asserting only that create_app_state returned would not catch a state
        object whose database was never opened, so the database is exercised."""
        settings = _settings(hp_dir, agent_hub_tls=False)
        state = await create_app_state(settings)

        assert state.agent_hub is None
        assert state.repo is not None
        row = await state.database.fetchone("SELECT COUNT(*) c FROM hosts")
        assert row["c"] == 0, "the control plane's database must be live and queryable"

    async def test_the_refusal_names_the_setting_to_change(self, hp_dir: str):
        """A dark subsystem must say WHY, in the operator's words (S6).

        'Enabled but not listening' is true and useless; the reported sentence
        has to name the way out, or the operator is left guessing at the same
        wall #454 sat behind."""
        from homepilot import selfcheck

        settings = _settings(hp_dir, agent_hub_tls=False)
        state = await create_app_state(settings)

        report = await selfcheck.selfcheck_report(state, settings)
        hub = next(s for s in report["subsystems"] if s["name"] == "agent_hub")

        assert hub["state"] != "ok"
        assert "HP_AGENT_HUB_TLS" in hub["consequence"]
        assert "HP_HUB_ALLOW_INSECURE" in hub["consequence"]

    async def test_a_healthy_hub_reports_no_disabled_reason(self, hp_dir: str):
        """The reason field must not be a permanent scar: a default install
        generates its certificate, serves, and carries no refusal text."""
        state = await create_app_state(_settings(hp_dir))
        try:
            assert state.agent_hub is not None
            assert state.agent_hub_disabled_reason == ""
        finally:
            await _shutdown(state)

    def test_loopback_plaintext_is_still_allowed(self):
        hub = AgentHubServer(host="127.0.0.1", port=8443, ssl_context=None, allow_insecure=False)
        hub.check_transport_security()
