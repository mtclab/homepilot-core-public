"""Artifacts may not reference the platform's own vault credentials (#684).

`{{ vault.NAME }}` accepted ANY secret name, including the ones HomePilot itself
runs on: `admin-secret` (mints API tokens), `pve-token` / `pve-write-token`
(the Proxmox cluster). A body such as

    echo {{ vault.admin-secret }} | base64

passes the literal-credential check at propose (the Jinja validator happens to
trip over the hyphen, but `{% raw %}...{% endraw %}` walks straight past it,
and the executor's own regex still finds the reference), resolves at execute,
and the
base64 of the admin secret sails past `redact()` (which only knows the exact
value) into the execution log, which operators and read tokens can read.

Redaction is best-effort. The boundary is which names an artifact may name at
all, enforced at propose AND at execute (an artifact written before this rule, or
edited on disk, still cannot resolve one):

* the platform's own credentials are refused by name, always;
* `HP_ARTIFACT_VAULT_ALLOWLIST`, when set, narrows the rest to exactly the
  names listed (a platform name in it is still refused).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.executor.secrets import SecretResolutionError, resolve

PLATFORM = [
    "admin-secret",
    "agent-hub-token",
    "n8n-key",
    "proxmox-config",
    "pve-token",
    "pve-write-token",
    "webhook-secret",
]


class _Vault:
    """A vault that holds everything asked of it - and records every read, so a
    test can prove a refused name was never even decrypted."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    async def get_secret(self, name: str) -> dict[str, Any]:
        self.reads.append(name)
        return {"value": f"SECRET-OF-{name}", "token": f"TOKEN-OF-{name}"}


def _shell(body_line: str, n: int) -> dict[str, Any]:
    return {
        "id": f"2026-09-24-vault-ref-{n:06x}",
        "kind": "shell-script",
        "intent": "reads a vault credential",
        "body": (
            "## Plan\nx\n## Idempotence preamble\nIdempotent: guarded.\n## Spec\n"
            f"```bash shell-spec\n#!/bin/bash\n{body_line}\n```\n"
        ),
        "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }


def _http(cred: str, n: int) -> dict[str, Any]:
    return {
        "id": f"2026-09-24-vault-http-{n:06x}",
        "kind": "http-sequence",
        "intent": "calls a service with a vault credential",
        "body": (
            "```yaml http-spec\nsteps:\n"
            f"  - id: s1\n    name: {cred}\n    method: GET\n    path: /health\n```\n"
        ),
        "target": {"kind": "service", "service": "demo", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }


@pytest.fixture
def lifecycle(tmp_path: Path) -> ArtifactLifecycle:
    return ArtifactLifecycle(ArtifactStore(tmp_path / "artifacts"))


def _set_allowlist(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    from homepilot.config import get_settings

    if value is None:
        monkeypatch.delenv("HP_ARTIFACT_VAULT_ALLOWLIST", raising=False)
    else:
        monkeypatch.setenv("HP_ARTIFACT_VAULT_ALLOWLIST", value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_allowlist(monkeypatch: pytest.MonkeyPatch):
    from homepilot.config import get_settings

    _set_allowlist(monkeypatch, None)
    yield
    monkeypatch.delenv("HP_ARTIFACT_VAULT_ALLOWLIST", raising=False)
    get_settings.cache_clear()


# ── propose ──────────────────────────────────────────────────────────────────


class TestProposeRefusesPlatformCredentials:
    @pytest.mark.parametrize("name", PLATFORM)
    async def test_shell_body_naming_a_platform_secret_is_refused(self, lifecycle, name):
        with pytest.raises(LifecycleError, match=re.escape(name)):
            await lifecycle.propose(
                _shell(f"echo {{{{ vault.{name} }}}} | base64", PLATFORM.index(name))
            )

    @pytest.mark.parametrize("name", PLATFORM)
    async def test_raw_block_smuggling_is_refused(self, lifecycle, name):
        """The shape that got past propose before this fix: Jinja sees a raw
        block, the executor's regex sees a vault reference."""
        with pytest.raises(LifecycleError, match=re.escape(name)):
            await lifecycle.propose(
                _shell(
                    f"echo {{% raw %}}{{{{ vault.{name} }}}}{{% endraw %}} | base64",
                    0x100 + PLATFORM.index(name),
                )
            )

    async def test_the_reported_exfil_body_is_refused(self, lifecycle):
        with pytest.raises(LifecycleError, match="admin-secret"):
            await lifecycle.propose(
                _shell("echo {% raw %}{{ vault.admin-secret }}{% endraw %} | base64", 99)
            )

    async def test_case_variants_are_refused(self, lifecycle):
        with pytest.raises(LifecycleError):
            await lifecycle.propose(
                _shell("echo {% raw %}{{ vault.Admin-Secret.value }}{% endraw %}", 98)
            )

    async def test_http_sequence_credential_naming_a_platform_secret_is_refused(self, lifecycle):
        with pytest.raises(LifecycleError, match="pve-write-token"):
            await lifecycle.propose(_http("pve-write-token", 1))

    async def test_an_operator_secret_is_still_referenceable(self, lifecycle):
        aid = await lifecycle.propose(_shell("PGPASSWORD={{ vault.app_db.password }} psql", 2))
        assert aid

    async def test_allowlist_narrows_what_may_be_referenced(self, lifecycle, monkeypatch):
        _set_allowlist(monkeypatch, "app_db, backup_key")
        assert await lifecycle.propose(_shell("x={{ vault.app_db.password }}", 3))
        with pytest.raises(LifecycleError, match="other_thing"):
            await lifecycle.propose(_shell("x={{ vault.other_thing }}", 4))

    async def test_allowlisting_a_platform_secret_does_not_unlock_it(self, lifecycle, monkeypatch):
        _set_allowlist(monkeypatch, "admin-secret,app_db")
        with pytest.raises(LifecycleError, match="admin-secret"):
            await lifecycle.propose(_shell("echo {% raw %}{{ vault.admin-secret }}{% endraw %}", 5))


# ── execute ──────────────────────────────────────────────────────────────────


class TestExecuteRefusesPlatformCredentials:
    """Propose is not the only way a body reaches the executor: an artifact
    proposed before this rule, or edited in the store, must still not resolve."""

    @pytest.mark.parametrize("name", PLATFORM)
    async def test_resolve_refuses_and_never_reads_the_vault(self, name):
        vault = _Vault()
        with pytest.raises(SecretResolutionError, match=re.escape(name)):
            await resolve(f"echo {{{{ vault.{name} }}}} | base64", vault)
        assert name not in vault.reads, "a refused platform credential was decrypted"

    async def test_resolve_respects_the_allowlist(self, monkeypatch):
        _set_allowlist(monkeypatch, "app-db")
        vault = _Vault()
        text, values = await resolve("p={{ vault.app-db }}", vault)
        assert text == "p=SECRET-OF-app-db" and values == ["SECRET-OF-app-db"]
        with pytest.raises(SecretResolutionError, match="not-listed"):
            await resolve("p={{ vault.not-listed }}", vault)
        assert "not-listed" not in vault.reads

    async def test_http_sequence_step_credential_is_refused_at_execute(self):
        from homepilot.executor import http_sequence

        vault = _Vault()
        result = await http_sequence.execute(
            {"id": "a", "kind": "http-sequence"},
            _http("pve-token", 1)["body"],
            {"kind": "service", "service": "demo"},
            vault,
        )
        assert result["success"] is False
        assert "pve-token" not in vault.reads, "a refused platform credential was decrypted"


# ── the registry cannot fall behind the code ─────────────────────────────────

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"
# Every way the platform names a vault entry in its own code: a literal passed to
# get_secret/store_secret, a VAULT_KEY-style constant, or the settings helper.
_LITERAL_NAME_RE = re.compile(
    r"""(?:get_secret|store_secret|_try_vault_secret)\(\s*["']([A-Za-z0-9_-]+)["']"""
    r"""|VAULT_KEY\s*=\s*["']([A-Za-z0-9_-]+)["']"""
)


def _platform_names_in_source() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        for m in _LITERAL_NAME_RE.finditer(path.read_text(encoding="utf-8")):
            names.add(m.group(1) or m.group(2))
    return names


class TestTheDenyListCoversEveryPlatformCredential:
    def test_every_vault_name_the_platform_uses_is_denied(self):
        """A new platform credential stored under a new name must be added to
        the deny list in the same change, or this fails naming it."""
        from homepilot.executor.secrets import PLATFORM_SECRET_NAMES

        found = _platform_names_in_source()
        assert "admin-secret" in found, "the source scan found nothing; the gate has no teeth"
        missing = sorted(found - PLATFORM_SECRET_NAMES)
        assert not missing, f"platform vault names not in PLATFORM_SECRET_NAMES: {missing}"

    def test_the_scan_has_teeth(self):
        planted = 'x = await vault.get_secret("brand-new-platform-key")'
        assert _LITERAL_NAME_RE.search(planted), "the scan cannot see a new platform name"
