"""The hub's two "I could not look" decisions (#642 A5 and A10, epic #648).

Both were on #642's outstanding list and both were still live at 3.6.14. They
share one shape: a read that FAILS is answered as though it had succeeded, and
the confident default is the one that breaks the fleet.

* ``tls_mode`` counted the enrolled agents; a failed count came back as ``0``,
  which reads as "new install", so TLS-by-default was taken AND PERSISTED - and
  the plaintext fleet it was written to protect is stranded at ``EOF`` with its
  only repair channel being the transport that just flipped.
* ``selfconfig`` regenerated the hub certificate when it could not read the one
  on disk. Agents pin that certificate by sha256 at enrolment, so an errno on a
  volume re-pins every managed host and each one has to be re-enrolled by hand.

The fail-safe direction is the one `reconciler/verify.py` already states: "I
looked and it matches" and "I could not look" are different answers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from homepilot.agent_hub.selfconfig import (
    HubCertificateError,
    certificate_fingerprint,
    ensure_hub_certificate,
)
from homepilot.agent_hub.tls_mode import (
    MODE_LEGACY_PLAINTEXT,
    MODE_TLS,
    SETTING_KEY,
    resolve_hub_tls_mode,
)


class _Db:
    def __init__(self, answer: Any = None, raises: BaseException | None = None):
        self._answer = answer
        self._raises = raises

    async def fetchone(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._answer


class _Repo:
    """Just enough Repository for the TLS-mode decision, recording every write."""

    def __init__(self, db: _Db):
        self.db = db
        self.settings: dict[str, Any] = {}
        self.set_calls: list[tuple[str, Any]] = []

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        return self.settings.get(key)

    async def set_setting(self, key: str, value: Any) -> None:
        self.set_calls.append((key, value))
        self.settings[key] = {"value": value}


class TestFleetSizeUnknown:
    """#642 A5: a count that could not be read must not decide the transport."""

    async def test_a_failed_count_keeps_plaintext_and_records_nothing(self):
        repo = _Repo(_Db(raises=RuntimeError("database is locked")))

        mode = await resolve_hub_tls_mode(repo, tls_set_explicitly=False, bind="0.0.0.0:8443")

        assert mode == MODE_LEGACY_PLAINTEXT, (
            "an unknown fleet size must keep the transport a pre-existing fleet "
            "would be speaking, not assume there is no fleet"
        )
        assert repo.set_calls == [], (
            "the decision is PERMANENT once written - it must never be taken "
            "from a read that did not happen"
        )

    async def test_an_empty_row_is_also_unknown(self):
        repo = _Repo(_Db(answer=None))

        mode = await resolve_hub_tls_mode(repo, tls_set_explicitly=False, bind="0.0.0.0:8443")

        assert mode == MODE_LEGACY_PLAINTEXT
        assert repo.set_calls == []

    async def test_a_real_fleet_still_keeps_plaintext_and_is_recorded(self):
        repo = _Repo(_Db(answer={"c": 4}))

        mode = await resolve_hub_tls_mode(repo, tls_set_explicitly=False, bind="0.0.0.0:8443")

        assert mode == MODE_LEGACY_PLAINTEXT
        assert repo.set_calls == [(SETTING_KEY, MODE_LEGACY_PLAINTEXT)]

    async def test_a_genuinely_new_install_still_gets_tls_by_default(self):
        """The ADR-004 S3 behaviour this must not regress."""
        repo = _Repo(_Db(answer={"c": 0}))

        mode = await resolve_hub_tls_mode(repo, tls_set_explicitly=False, bind="0.0.0.0:8443")

        assert mode == MODE_TLS
        assert repo.set_calls == [(SETTING_KEY, MODE_TLS)]

    async def test_an_operator_who_set_the_env_var_is_obeyed_without_a_read(self):
        repo = _Repo(_Db(raises=AssertionError("the count must not be read at all")))

        assert await resolve_hub_tls_mode(repo, tls_set_explicitly=True) == MODE_TLS
        assert repo.set_calls == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 0000 file")
class TestHubCertificateUnreadable:
    """#642 A10: an unreadable certificate must not be replaced."""

    def test_an_unreadable_certificate_is_not_regenerated(self, tmp_path: Path):
        cert, key = ensure_hub_certificate(tmp_path)
        pinned = certificate_fingerprint(cert)
        before = cert.read_bytes()

        cert.chmod(0o000)
        try:
            with pytest.raises(HubCertificateError) as refusal:
                ensure_hub_certificate(tmp_path)
        finally:
            cert.chmod(0o644)

        assert "re-pins every enrolled agent" in str(refusal.value)
        assert cert.read_bytes() == before, "the certificate on disk was replaced"
        assert certificate_fingerprint(cert) == pinned, "the fleet's pin changed"
        assert key.exists()

    def test_an_unstattable_path_is_not_read_as_absent(self, tmp_path: Path):
        """`Path.exists()` answers False to an EACCES on the parent - which here
        means "there is no certificate, mint one". It is not the same answer."""
        data = tmp_path / "data"
        cert, _key = ensure_hub_certificate(data)
        pinned = certificate_fingerprint(cert)

        hub_dir = data / "hub"
        hub_dir.chmod(0o000)
        try:
            with pytest.raises(HubCertificateError) as refusal:
                ensure_hub_certificate(data)
        finally:
            hub_dir.chmod(0o700)

        assert "could not determine whether" in str(refusal.value)
        assert certificate_fingerprint(cert) == pinned

    def test_a_corrupt_certificate_is_still_replaced(self, tmp_path: Path):
        """Unparseable is NOT unreadable: the bytes are there and they are not a
        certificate, so regenerating is the only way forward."""
        cert, _key = ensure_hub_certificate(tmp_path)
        old = certificate_fingerprint(cert)
        cert.write_bytes(b"this is not a certificate\n")

        cert2, _key2 = ensure_hub_certificate(tmp_path)

        assert certificate_fingerprint(cert2) != old
        assert cert2 == cert

    def test_a_healthy_certificate_is_reused_unchanged(self, tmp_path: Path):
        cert, _key = ensure_hub_certificate(tmp_path)
        pinned = certificate_fingerprint(cert)
        cert2, _key2 = ensure_hub_certificate(tmp_path)
        assert certificate_fingerprint(cert2) == pinned
