import os
from pathlib import Path

import pytest

from homepilot.vault.manager import VaultError, VaultManager


class TestSecretNameValidation:
    def _vm(self) -> VaultManager:
        return VaultManager(Path("/tmp/vault-test"), "passphrase123")

    def test_alphanumeric_passes(self):
        self._vm()._validate_secret_name("my-secret-01")

    def test_with_underscore(self):
        self._vm()._validate_secret_name("my_secret")

    def test_with_hyphen(self):
        self._vm()._validate_secret_name("authentik-admin")

    def test_rejects_dotdot_slash(self):
        with pytest.raises(VaultError, match="Invalid secret name"):
            self._vm()._validate_secret_name("../../etc/passwd")

    def test_rejects_spaces(self):
        with pytest.raises(VaultError, match="Invalid secret name"):
            self._vm()._validate_secret_name("my secret")

    def test_rejects_path_separator(self):
        with pytest.raises(VaultError, match="Invalid secret name"):
            self._vm()._validate_secret_name("path/to/secret")

    def test_rejects_empty(self):
        with pytest.raises(VaultError, match="Invalid secret name"):
            self._vm()._validate_secret_name("")

    def test_rejects_special_chars(self):
        with pytest.raises(VaultError, match="Invalid secret name"):
            self._vm()._validate_secret_name("secret!@#")

    def test_rejects_dot(self):
        with pytest.raises(VaultError, match="Invalid secret name"):
            self._vm()._validate_secret_name("secret.name")


class TestEncryptionRoundTrip:
    def test_protect_unprotect_identity(self, tmp_path):
        vm = VaultManager(tmp_path, "test-passphrase")
        identity_data = b"# age identity file\n# public key: age1test123\nPRIVATE_KEY_DATA\n"
        salt = os.urandom(16)
        protected = vm._protect_identity(identity_data, salt)
        decrypted = vm._unprotect_identity(protected)
        assert decrypted == identity_data

    def test_wrong_passphrase_fails(self, tmp_path):
        vm1 = VaultManager(tmp_path, "correct-passphrase")
        identity_data = b"# age identity file\n# public key: age1test\nPRIVATE_KEY\n"
        salt = os.urandom(16)
        protected = vm1._protect_identity(identity_data, salt)

        vm2 = VaultManager(tmp_path, "wrong-passphrase")
        with pytest.raises(VaultError, match="Failed to decrypt"):
            vm2._unprotect_identity(protected)

    def test_parse_public_key_old_format(self):
        identity_data = b"# age identity file\n# public key: age1abc123def\nAGE-SECRET-KEY-1XXXX\n"
        result = VaultManager._parse_public_key(identity_data)
        assert result == "age1abc123def"

    def test_parse_public_key_new_format(self):
        identity = VaultManager._generate_identity()
        pubkey = VaultManager._parse_public_key(identity)
        assert pubkey.startswith("age1")

    def test_parse_public_key_invalid(self):
        with pytest.raises(VaultError, match="Could not parse"):
            VaultManager._parse_public_key(b"not a valid identity\n")

    def test_too_short_protected_data(self, tmp_path):
        vm = VaultManager(tmp_path, "passphrase")
        with pytest.raises(VaultError, match="too short"):
            vm._unprotect_identity(b"short")

    def test_different_salt_different_ciphertext(self, tmp_path):
        vm = VaultManager(tmp_path, "passphrase")
        identity_data = b"# age identity file\n# public key: age1test\nKEY\n"
        salt1 = os.urandom(16)
        salt2 = os.urandom(16)
        while salt1 == salt2:
            salt2 = os.urandom(16)
        p1 = vm._protect_identity(identity_data, salt1)
        p2 = vm._protect_identity(identity_data, salt2)
        assert p1 != p2
        assert vm._unprotect_identity(p1) == identity_data
        assert vm._unprotect_identity(p2) == identity_data

    async def test_store_secret_validation(self, tmp_path):
        vm = VaultManager(tmp_path, "passphrase")
        with pytest.raises(VaultError, match="Invalid secret name"):
            await vm.store_secret("bad/name", {"key": "val"})

    async def test_full_roundtrip(self, tmp_path):
        vm = VaultManager(tmp_path, "test-passphrase-xyz")
        await vm.store_secret("mydb", {"password": "s3cr3t", "host": "db.local"})
        result = await vm.get_secret("mydb")
        assert result == {"password": "s3cr3t", "host": "db.local"}

    async def test_list_secrets(self, tmp_path):
        vm = VaultManager(tmp_path, "test-passphrase")
        await vm.store_secret("alpha", {"x": 1})
        await vm.store_secret("beta", {"y": 2})
        secrets = await vm.list_secrets()
        assert sorted(secrets) == ["alpha", "beta"]

    async def test_delete_secret(self, tmp_path):
        vm = VaultManager(tmp_path, "test-passphrase")
        await vm.store_secret("todelete", {"v": "val"})
        await vm.delete_secret("todelete")
        with pytest.raises(VaultError, match="not found"):
            await vm.get_secret("todelete")

    async def test_rotate_passphrase(self, tmp_path):
        vm = VaultManager(tmp_path, "old-passphrase")
        await vm.store_secret("cred", {"token": "abc"})
        await vm.rotate_passphrase("new-passphrase")
        result = await vm.get_secret("cred")
        assert result == {"token": "abc"}
