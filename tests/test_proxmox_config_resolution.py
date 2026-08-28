"""Gates for "where does the Proxmox address come from".

The environment is only half the answer: an install claimed through the web UI
stores its hypervisor in the vault under 'proxmox-config', and that secret wins.
Every SURFACE that tells an operator about the hypervisor must agree with the
client that is actually talking to it. Two did not, and both told a working
install it had none (found live on dev 3.6.9):

  * the self-check said "No hypervisor is configured, so inventory stays empty
    and guest provisioning is unavailable" while nine inventory items were
    listed off that address (gated in test_selfcheck.py);
  * `hp status` printed "PVE host: (not configured)" for the same install.

These assert the operator-visible OUTCOME, not that the resolver returned.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app
from homepilot.proxmox_config import resolve_proxmox_config

runner = CliRunner()

VAULT_HOST = "pve.vault.example"
ENV_HOST = "pve.env.example"


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from homepilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**overrides):
    base = {
        "proxmox_host": "",
        "proxmox_port": 8006,
        "proxmox_verify_ssl": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _vault(secret):
    vault = MagicMock()
    vault.get_secret = AsyncMock(return_value=secret)
    return vault


class TestResolverPrecedence:
    async def test_vault_wins_over_env(self):
        config = await resolve_proxmox_config(
            _settings(proxmox_host=ENV_HOST),
            _vault({"host": VAULT_HOST, "port": 8007, "verify_ssl": False}),
        )
        assert config == (VAULT_HOST, 8007, False)

    async def test_env_stands_when_vault_holds_no_secret(self):
        config = await resolve_proxmox_config(_settings(proxmox_host=ENV_HOST), _vault(None))
        assert config.host == ENV_HOST

    async def test_broken_vault_does_not_lose_the_env_answer(self):
        """A locked or corrupt vault must not turn a configured install into none."""
        vault = MagicMock()
        vault.get_secret = AsyncMock(side_effect=RuntimeError("locked"))
        config = await resolve_proxmox_config(_settings(proxmox_host=ENV_HOST), vault)
        assert config.host == ENV_HOST

    async def test_string_verify_ssl_from_the_vault_is_read_as_a_flag(self):
        config = await resolve_proxmox_config(
            _settings(), _vault({"host": VAULT_HOST, "verify_ssl": "false"})
        )
        assert config.verify_ssl is False

    async def test_no_vault_and_no_env_is_genuinely_unconfigured(self):
        assert (await resolve_proxmox_config(_settings(), None)).host == ""


class TestHpStatusTellsTheTruth:
    def _run(self, tmp_path: Path, monkeypatch, host: str | None):
        monkeypatch.setenv("HP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("HP_VAULT_PASSPHRASE", "test-passphrase")
        monkeypatch.setattr(
            "homepilot.cli.main._vault_state", lambda settings: "unlocked", raising=False
        )

        import homepilot.proxmox_config as pc

        async def fake_resolve(settings, vault):
            return pc.ProxmoxConfig(host=host or "", port=8006, verify_ssl=True)

        monkeypatch.setattr(pc, "resolve_proxmox_config", fake_resolve)
        return runner.invoke(app, ["status"])

    def test_vault_configured_host_is_shown_not_called_unconfigured(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, VAULT_HOST)

        assert result.exit_code == 0, result.output
        assert VAULT_HOST in result.output
        assert "(not configured)" not in result.output

    def test_genuinely_unconfigured_still_says_so(self, tmp_path, monkeypatch):
        """The honest arm: no address anywhere must keep reading as unconfigured."""
        result = self._run(tmp_path, monkeypatch, None)

        assert result.exit_code == 0, result.output
        assert "(not configured)" in result.output
