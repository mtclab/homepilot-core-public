"""#431's code findings: the ones that destroy or hide something.

Each of these is small, and each fails in a way that is either permanent or
silent:

* the vault rewrote the MASTER IDENTITY in place, so a crash mid-rotation lost
  every secret it protects - there is no second copy;
* `store_secret` wrote non-atomically and chmod'd afterwards, leaving a window at
  0644 and a truncatable file;
* a webhook delivery task was held only by a local, and asyncio keeps a weak
  reference - so an in-flight delivery could be garbage-collected and its row sat
  `pending` forever, because nothing redrives pending;
* `HP_SECRET_KEY_FILE` was read from `os.environ` rather than the parsed field,
  so the documented Docker-secrets pattern silently did nothing and every token
  was invalidated on a fresh volume;
* a bare `except: return ""` made a wrong vault passphrase indistinguishable
  from "not configured";
* `proxmox_host` was bound inside a `try` and read after it, so the ImportError
  path died with a NameError naming the wrong thing;
* `hp status` reported "Vault: unlocked" from a non-empty passphrase, never
  checking whether the identity could be unwrapped.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from homepilot.vault.manager import VaultManager, _atomic_write

pytestmark = pytest.mark.asyncio

PASSPHRASE = "a-test-passphrase-that-is-long-enough"


@pytest.fixture
async def vault(tmp_path: Path) -> VaultManager:
    manager = VaultManager(tmp_path, PASSPHRASE)
    await manager.ensure_master_identity()
    return manager


class TestVaultWritesAreAtomic:
    async def test_a_secret_file_is_never_world_readable(self, vault, tmp_path):
        await vault.store_secret("db", {"password": "hunter2"})

        mode = os.stat(tmp_path / "vault" / "secrets" / "db.age").st_mode & 0o777
        assert mode == 0o600, f"secret file is {oct(mode)} - it was chmod'd after the write"

    async def test_a_failed_write_leaves_the_previous_secret_intact(self, vault, tmp_path):
        await vault.store_secret("db", {"password": "original"})
        target = tmp_path / "vault" / "secrets" / "db.age"
        before = target.read_bytes()

        with (
            patch("homepilot.vault.manager.os.replace", side_effect=OSError("disk full")),
            pytest.raises(OSError),
        ):
            await vault.store_secret("db", {"password": "replacement"})

        assert target.read_bytes() == before, "a failed write truncated the stored secret"
        assert (await vault.get_secret("db"))["password"] == "original"

    async def test_no_temp_file_is_left_behind(self, vault, tmp_path):
        with (
            patch("homepilot.vault.manager.os.replace", side_effect=OSError("disk full")),
            pytest.raises(OSError),
        ):
            await vault.store_secret("db", {"password": "x"})

        leftovers = list((tmp_path / "vault" / "secrets").glob(".*tmp"))
        assert leftovers == [], f"temp files left behind: {leftovers}"

    async def test_atomic_write_creates_at_0600_from_the_start(self, tmp_path):
        target = tmp_path / "thing"

        _atomic_write(target, b"data")

        assert target.read_bytes() == b"data"
        assert os.stat(target).st_mode & 0o777 == 0o600


class TestRotatingThePassphraseCannotLoseTheIdentity:
    async def test_a_failed_rotation_leaves_the_old_wrapping_usable(self, vault, tmp_path):
        """The identity file is the only copy. A crash mid-rewrite used to make
        every secret it protects permanently unrecoverable."""
        await vault.store_secret("db", {"password": "hunter2"})
        protected = tmp_path / "vault" / "identities" / "master.protected"
        before = protected.read_bytes()

        with (
            patch("homepilot.vault.manager.os.replace", side_effect=OSError("disk full")),
            pytest.raises(OSError),
        ):
            await vault.rotate_passphrase("a-new-passphrase-long-enough")

        assert protected.read_bytes() == before
        reopened = VaultManager(tmp_path, PASSPHRASE)
        assert (await reopened.get_secret("db"))["password"] == "hunter2"

    async def test_a_successful_rotation_reopens_under_the_new_passphrase(self, vault, tmp_path):
        await vault.store_secret("db", {"password": "hunter2"})

        await vault.rotate_passphrase("a-new-passphrase-long-enough")

        reopened = VaultManager(tmp_path, "a-new-passphrase-long-enough")
        assert (await reopened.get_secret("db"))["password"] == "hunter2"

    async def test_no_backup_is_left_lying_around(self, vault, tmp_path):
        await vault.rotate_passphrase("a-new-passphrase-long-enough")

        leftovers = list((tmp_path / "vault" / "identities").glob("*.bak"))
        assert leftovers == [], "the rotation backup is still on disk, wrapped by the OLD key"


class TestAnInFlightWebhookIsNotCollected:
    async def test_the_delivery_task_is_held_strongly(self):
        """asyncio keeps only a WEAK reference to a running task, and nothing
        ever redrives a delivery row left `pending`."""
        from homepilot import events

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow(**_kwargs: object) -> bool:
            started.set()
            await release.wait()
            return True

        repo = _StubRepo()
        with patch.object(events, "deliver_with_retry", _slow):
            await events.emit_event("artifact_applied", {"id": "a1"}, repo=repo)
            await asyncio.wait_for(started.wait(), timeout=2)

            assert events._IN_FLIGHT_DELIVERIES, (
                "the delivery task is not referenced anywhere - the loop may collect it"
            )
            release.set()
            await asyncio.sleep(0)
            await asyncio.gather(*list(events._IN_FLIGHT_DELIVERIES), return_exceptions=True)
            # done callbacks run on the next loop iteration.
            await asyncio.sleep(0)

        assert not events._IN_FLIGHT_DELIVERIES, "finished deliveries are never released"


class _StubDb:
    class _Conn:
        async def commit(self) -> None:
            return None

    conn = _Conn()


class _StubRepo:
    db = _StubDb()

    async def get_webhook_configs_for_event(self, _event_type: str) -> list[dict[str, object]]:
        return [
            {
                "id": 1,
                "url": "https://example.test/hook",
                "secret": None,
                "max_retries": 0,
                "events": None,
                "enabled": 1,
            }
        ]

    async def create_webhook_delivery(self, **_kwargs: object) -> int:
        return 1

    async def update_webhook_delivery(self, *_args: object, **_kwargs: object) -> None:
        return None


class TestConfigStopsHidingThings:
    async def test_the_parsed_secret_key_file_is_used(self, tmp_path, monkeypatch):
        """Reading `os.environ` directly ignored the field however it was set, so
        the documented Docker-secrets pattern silently did nothing and every
        token was invalidated on a fresh volume."""
        from homepilot.config import Settings

        key_file = tmp_path / "secret_key"
        key_file.write_text("a-stable-secret-key-from-a-file\n")
        monkeypatch.delenv("HP_SECRET_KEY_FILE", raising=False)
        monkeypatch.delenv("HP_SECRET_KEY", raising=False)

        settings = Settings(secret_key_file=str(key_file), data_dir=str(tmp_path), secret_key="")

        assert settings.secret_key == "a-stable-secret-key-from-a-file"

    async def test_a_failing_vault_lookup_is_logged_not_swallowed(self, tmp_path, caplog):
        """A wrong passphrase was indistinguishable from "not configured": the
        admin secret came back empty and nothing said why."""
        from homepilot.config import Settings

        settings = Settings(data_dir=str(tmp_path), vault_passphrase="wrong-passphrase")
        (tmp_path / "vault").mkdir(parents=True, exist_ok=True)

        with (
            caplog.at_level("WARNING"),
            patch(
                "homepilot.vault.VaultManager.get_secret",
                side_effect=RuntimeError("bad passphrase"),
            ),
        ):
            value = settings._try_vault_secret("admin-secret")

        assert value == ""
        assert any("vault lookup" in r.getMessage() for r in caplog.records), (
            "the failure was swallowed silently"
        )


class TestTheProxmoxImportPathNamesTheRealProblem:
    async def test_proxmox_host_is_bound_before_the_try(self):
        """It was assigned INSIDE the try and read after it, so the ImportError
        path raised a NameError naming the wrong thing entirely."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "homepilot" / "app_state.py"
        ).read_text(encoding="utf-8")
        binding = source.index("proxmox_host = settings.proxmox_host")
        try_start = source.index("    try:\n        from .adapters.proxmox import ProxmoxClient")

        assert binding < try_start, (
            "proxmox_host is bound inside the try again - the ImportError path "
            "will die with a NameError about the wrong thing"
        )
