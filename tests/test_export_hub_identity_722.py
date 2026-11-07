"""A restore keeps the agent fleet's hub identity (#722).

`hp export --include-secrets` archived the vault, keys and tokens - and not the
agent hub's certificate, key or shared token. Restored onto a new host, the hub
minted a fresh certificate and token on first boot. Every agent pins the hub
certificate by fingerprint (HP_AGENT_TLS_PIN, the installer default), so every
one refused the new hub; an env-pinned agent cannot be re-pinned remotely, and
could not have been reached to try. The whole fleet needed re-enrolling by hand.

This gate restores an export into a CLEAN directory and asserts the outcome an
agent depends on: the hub presents the same certificate fingerprint, from the
same key, and authenticates with the same token. Teeth: drop `hub` or
`.agent_hub_token` from SECRET_PATHS and it fails.
"""

from __future__ import annotations

import json
import os
import stat
import tarfile
from pathlib import Path
from unittest.mock import patch

from homepilot.agent_hub.selfconfig import (
    CERT_FILENAME,
    HUB_DIR_NAME,
    KEY_FILENAME,
    certificate_fingerprint,
    ensure_hub_certificate,
)
from homepilot.config import Settings

from .test_cli_backup_restore import PASSPHRASE, _export, _import, _out, _seed_host

HUB_TOKEN = "hub-token-for-the-722-gate"  # pragma: allowlist secret - test fixture


def _seed_fleet_host(data_dir: Path) -> tuple[str, bytes]:
    _seed_host(data_dir)
    cert, key = ensure_hub_certificate(data_dir)
    (data_dir / ".agent_hub_token").write_text(HUB_TOKEN + "\n", encoding="utf-8")
    return certificate_fingerprint(cert), key.read_bytes()


def _resolved_hub_token(data_dir: Path) -> str:
    """The token the restored backend would actually authenticate agents with."""
    with patch.dict(os.environ, {"HP_VAULT_PASSPHRASE": PASSPHRASE}, clear=True):
        settings = Settings(
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
    return settings.agent_hub_auth_token


def test_a_restore_onto_a_clean_host_keeps_the_hub_identity(tmp_path: Path) -> None:
    source = tmp_path / "old-host"
    tarball = tmp_path / "backup.tar.gz"
    fingerprint, key_bytes = _seed_fleet_host(source)

    result = _export(source, tarball, "--include-secrets")
    assert result.exit_code == 0, _out(result)
    with tarfile.open(tarball, "r:gz") as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
    assert HUB_DIR_NAME in manifest["secret_paths"]
    assert ".agent_hub_token" in manifest["secret_paths"]

    target = tmp_path / "new-host"  # never booted: nothing there to keep
    result = _import(target, tarball, "--restore-secrets")
    assert result.exit_code == 0, _out(result)

    # What the restored hub serves on first boot: the SAME certificate, because
    # `ensure_hub_certificate` finds a usable pair and does not regenerate.
    cert, key = ensure_hub_certificate(target)
    assert certificate_fingerprint(cert) == fingerprint, (
        "the restored hub presents a new certificate - every pinned agent refuses it"
    )
    assert key.read_bytes() == key_bytes
    assert stat.S_IMODE(key.stat().st_mode) == 0o600, "the hub key was restored readable"

    assert _resolved_hub_token(target) == HUB_TOKEN, (
        "the restored backend minted a new hub token - no enrolled agent can authenticate"
    )


def test_restore_replaces_a_hub_identity_minted_before_the_restore(tmp_path: Path) -> None:
    """The real-world order: the new host booted once (minting its own hub cert
    and token) before the operator imported. The fleet's identity must win, and
    the throwaway one is kept in the pre-import backup, not destroyed."""
    source = tmp_path / "old-host"
    tarball = tmp_path / "backup.tar.gz"
    fingerprint, _ = _seed_fleet_host(source)
    assert _export(source, tarball, "--include-secrets").exit_code == 0

    target = tmp_path / "new-host"
    target.mkdir()
    minted_cert, _ = ensure_hub_certificate(target)
    minted = certificate_fingerprint(minted_cert)
    (target / ".agent_hub_token").write_text("minted-on-first-boot\n", encoding="utf-8")
    assert minted != fingerprint

    result = _import(target, tarball, "--restore-secrets")
    assert result.exit_code == 0, _out(result)

    assert certificate_fingerprint(target / HUB_DIR_NAME / CERT_FILENAME) == fingerprint
    assert (target / HUB_DIR_NAME / KEY_FILENAME).exists()
    assert _resolved_hub_token(target) == HUB_TOKEN
    stashed = list((target / "backups").rglob(f"secrets/{HUB_DIR_NAME}/{CERT_FILENAME}"))
    assert stashed and certificate_fingerprint(stashed[0]) == minted, (
        "the identity the restore replaced was not kept in the pre-import backup"
    )
