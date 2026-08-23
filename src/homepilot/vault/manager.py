from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pyrage import IdentityError as _AgeIdentityError
from pyrage import decrypt as age_decrypt
from pyrage import encrypt as age_encrypt
from pyrage import x25519


class VaultError(Exception):
    pass


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically, never leaving a partial file (#431).

    `store_secret` and `rotate_passphrase` both wrote in place and chmod'd
    AFTERWARDS, so there were two windows: a crash mid-write truncated the only
    copy of a secret (or of the master identity), and the file existed at 0644
    until the chmod landed.

    Written to a temp file in the SAME directory - `os.replace` is only atomic
    within a filesystem - created 0600 from the start, fsync'd before the
    rename so the rename cannot expose a file whose contents are still in cache.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class VaultManager:
    """Manages age-encrypted secrets with AES-GCM identity file protection.

    Identity files are protected by deriving an AES-256-GCM key from the
    master passphrase via PBKDF2-SHA256. Per-secret naming allows vault
    entries like 'authentik-admin', 'pve-node1', etc.
    """

    def __repr__(self) -> str:
        return f"VaultManager(data_dir={self.data_dir!r}, _master_passphrase=***REDACTED***)"

    def __init__(self, data_dir: Path, master_passphrase: str):
        self.data_dir = data_dir / "vault"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._master_passphrase = master_passphrase
        self._identities_dir = self.data_dir / "identities"
        self._identities_dir.mkdir(parents=True, exist_ok=True)
        self._secrets_dir = self.data_dir / "secrets"
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        self._master_identity: str | None = None
        # Cache of the UNWRAPPED master identity bytes. Deriving the wrapping key
        # is PBKDF2-SHA256 at 600k iterations (~234ms) and previously ran on
        # EVERY decrypt()/get_secret(); caching the unwrapped identity means we
        # pay it at most once per process (#387).
        self._master_identity_data: bytes | None = None

    def _derive_wrapping_key(self, salt: bytes) -> bytes:
        import hashlib

        return hashlib.pbkdf2_hmac(
            "sha256",
            self._master_passphrase.encode("utf-8"),
            salt,
            600_000,
            dklen=32,
        )

    def _protect_identity(self, identity_data: bytes, salt: bytes) -> bytes:
        key = self._derive_wrapping_key(salt)
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, identity_data, None)
        return salt + nonce + ciphertext

    def _unprotect_identity(self, protected_data: bytes) -> bytes:
        if len(protected_data) < 16 + 12 + 1:
            raise VaultError("Protected identity data too short or corrupted")
        salt = protected_data[:16]
        nonce = protected_data[16:28]
        ciphertext = protected_data[28:]
        key = self._derive_wrapping_key(salt)
        aesgcm = AESGCM(key)
        try:
            return aesgcm.decrypt(nonce, ciphertext, None)
        except InvalidTag as e:
            raise VaultError(f"Failed to decrypt identity (wrong passphrase?): {e}") from e
        except (ValueError, TypeError) as e:
            raise VaultError(f"Failed to decrypt identity (corrupted data): {e}") from e

    def _load_master_identity_data(self) -> bytes:
        """Return the unwrapped master identity, deriving the wrapping key at
        most once per process. Raises VaultError if no master identity exists or
        the passphrase is wrong (an InvalidTag surfaces from _unprotect_identity).
        """
        if self._master_identity_data is not None:
            return self._master_identity_data
        protected_file = self._identities_dir / "master.protected"
        if not protected_file.exists():
            raise VaultError("No master identity found")
        identity_data = self._unprotect_identity(protected_file.read_bytes())
        self._master_identity_data = identity_data
        return identity_data

    async def ensure_master_identity(self) -> str:
        if self._master_identity is not None:
            return self._master_identity

        identity_file = self._identities_dir / "master.age"
        protected_file = self._identities_dir / "master.protected"

        if protected_file.exists():
            identity_data = self._load_master_identity_data()
            self._master_identity = self._parse_public_key(identity_data)
            return self._master_identity

        if identity_file.exists():
            identity_data = identity_file.read_bytes()
            self._master_identity = self._parse_public_key(identity_data)
            salt = os.urandom(16)
            protected_data = self._protect_identity(identity_data, salt)
            protected_file.write_bytes(protected_data)
            os.chmod(str(protected_file), 0o600)
            identity_file.unlink()
            self._master_identity_data = identity_data
            return self._master_identity

        identity_data = self._generate_identity()
        salt = os.urandom(16)
        protected_data = self._protect_identity(identity_data, salt)
        protected_file.write_bytes(protected_data)
        os.chmod(str(protected_file), 0o600)
        self._master_identity = self._parse_public_key(identity_data)
        self._master_identity_data = identity_data
        return self._master_identity

    @staticmethod
    def _generate_identity() -> bytes:
        identity = x25519.Identity.generate()
        return str(identity).encode("utf-8")

    @staticmethod
    def _parse_public_key(identity_data: bytes) -> str:
        text = identity_data.decode("utf-8").strip()
        # Old format: age-keygen output with "# public key: age1..." comment
        for line in text.splitlines():
            if line.startswith("# public key: "):
                return line.split(": ", 1)[1].strip()
        # New format: identity_data is just the AGE-SECRET-KEY-1... string
        try:
            identity = x25519.Identity.from_str(text)
            return str(identity.to_public())
        except (ValueError, UnicodeDecodeError, _AgeIdentityError) as e:
            raise VaultError(f"Could not parse public key from identity: {e}") from e

    @staticmethod
    def _extract_private_key(identity_data: bytes) -> str:
        text = identity_data.decode("utf-8").strip()
        # Old format: age-keygen output — extract the AGE-SECRET-KEY-1... line
        for line in text.splitlines():
            if line.startswith("AGE-SECRET-KEY-"):
                return line.strip()
        # New format: whole text is the private key
        return text

    async def encrypt(self, plaintext: str, recipient: str | None = None) -> bytes:
        pubkey = recipient or await self.ensure_master_identity()
        r = x25519.Recipient.from_str(pubkey)
        result: bytes = age_encrypt(plaintext.encode("utf-8"), [r])
        return result

    async def decrypt(self, ciphertext: bytes) -> str:
        if self._master_identity_data is not None:
            identity_data = self._master_identity_data
        else:
            # Cold path: the ~234ms PBKDF2 derivation runs off the event loop so
            # it doesn't block other coroutines; the result is then cached.
            identity_data = await asyncio.to_thread(self._load_master_identity_data)
        private_key = self._extract_private_key(identity_data)
        try:
            identity = x25519.Identity.from_str(private_key)
            plaintext: bytes = age_decrypt(ciphertext, [identity])
            return plaintext.decode("utf-8")
        except VaultError:
            raise
        except (ValueError, UnicodeDecodeError, _AgeIdentityError) as e:
            raise VaultError(f"age decrypt failed (corrupted data or wrong key): {e}") from e

    def _validate_secret_name(self, name: str) -> None:
        if not re.match(r"^[a-zA-Z0-9_-]+$", name):
            raise VaultError(
                f"Invalid secret name '{name}': must contain only "
                "alphanumeric characters, hyphens, and underscores"
            )

    async def store_secret(self, name: str, value: dict[str, Any]) -> None:
        self._validate_secret_name(name)
        recipient = await self.ensure_master_identity()
        encrypted = await self.encrypt(json.dumps(value), recipient)
        secret_file = self._secrets_dir / f"{name}.age"
        _atomic_write(secret_file, encrypted)

    async def get_secret(self, name: str) -> dict[str, Any]:
        self._validate_secret_name(name)
        secret_file = self._secrets_dir / f"{name}.age"
        if not secret_file.exists():
            raise VaultError(f"Secret '{name}' not found")
        encrypted = secret_file.read_bytes()
        decrypted = await self.decrypt(encrypted)
        result: dict[str, Any] = json.loads(decrypted)
        return result

    async def delete_secret(self, name: str) -> None:
        self._validate_secret_name(name)
        secret_file = self._secrets_dir / f"{name}.age"
        if secret_file.exists():
            secret_file.unlink()

    async def list_secrets(self) -> list[str]:
        return [f.stem for f in self._secrets_dir.glob("*.age")]

    async def rotate_passphrase(self, new_passphrase: str) -> None:
        old_protected = self._identities_dir / "master.protected"
        if not old_protected.exists():
            raise VaultError("No master identity to rotate")

        identity_data = self._unprotect_identity(old_protected.read_bytes())

        self._master_passphrase = new_passphrase
        salt = os.urandom(16)
        new_protected_data = self._protect_identity(identity_data, salt)

        # ATOMIC, with a backup (#431). This rewrote the master identity IN
        # PLACE: a crash or a full disk mid-write destroyed the only copy, and
        # every secret in the vault became unrecoverable. There is no second
        # chance at this file.
        backup = old_protected.with_suffix(".protected.bak")
        backup.write_bytes(old_protected.read_bytes())
        os.chmod(str(backup), 0o600)
        try:
            _atomic_write(old_protected, new_protected_data)
        except Exception:
            # Put the old wrapping back before re-raising: a half-written
            # identity is the one state from which nothing can be recovered.
            old_protected.write_bytes(backup.read_bytes())
            os.chmod(str(old_protected), 0o600)
            raise
        finally:
            backup.unlink(missing_ok=True)
        # The identity bytes are unchanged by rotation (only the wrapping key
        # changed), so refresh the cache directly rather than re-deriving.
        self._master_identity = None
        self._master_identity_data = identity_data
        await self.ensure_master_identity()
