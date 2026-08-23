"""A credential in an artifact is a reference, never the value (#505).

Vault was wired into `http_sequence` and `proxmox_api` only. `host_provision` and
`shell_script` - the two kinds most likely to need a credential - received no
vault at all, so a database password in a config file or an API token in a script
was written as LITERAL TEXT into the artifact body. The artifact store is a git
repository designed to be pushed to a remote, so the credential is in history from
the first commit, and `git push` is a one-way door.

The gates below assert the three properties that make a reference safe:

1. it resolves at execute time, so the stored body holds only the reference;
2. the resolved value never reaches a log, a result or an error - all three are
   read back by an operator, and one is persisted on purpose (#487);
3. a body that carries a literal credential is refused at PROPOSE, which is the
   last moment before it is committed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.executor.host_provision import execute as host_provision_execute
from homepilot.executor.secrets import (
    SecretResolutionError,
    literal_secrets,
    redact,
    references,
    resolve,
)
from homepilot.executor.shell_script import execute as shell_script_execute

pytestmark = pytest.mark.asyncio

SECRET = "s3cr3t-database-password"
TARGET = {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}

CONFIG_BODY = """\
## Plan
Write the app config
## Spec
```yaml host-provision-spec
config_files:
  - path: /etc/app.conf
    content: |
      password = {{ vault.appdb.password }}
    mode: '0600'
```
"""

SCRIPT_BODY = """\
## Plan
Seed the database
## Idempotence preamble
Idempotent: guarded by a marker file.
## Spec
```bash shell-spec
#!/bin/bash
psql "postgres://app:{{ vault.appdb.password }}@db01/app" -c 'select 1'
```
"""


def _vault() -> AsyncMock:
    vault = AsyncMock()
    vault.get_secret = AsyncMock(return_value={"password": SECRET})
    return vault


def _agent() -> AsyncMock:
    agent = AsyncMock()
    agent.install_package = AsyncMock(return_value={"changed": True})
    agent.manage_service = AsyncMock(return_value={"changed": True})
    agent.write_config = AsyncMock(return_value={"changed": True})
    agent.write_file = AsyncMock(return_value={"ok": True})
    agent.exec = AsyncMock(return_value=(0, "done", ""))
    agent.exec_readonly = AsyncMock(return_value=(1, "", ""))
    agent.read_file = AsyncMock(side_effect=FileNotFoundError("absent"))
    return agent


class TestTheReferenceResolvesAtExecuteTime:
    async def test_a_config_file_gets_the_real_value(self):
        agent = _agent()

        result = await host_provision_execute(
            {"id": "a1"}, CONFIG_BODY, TARGET, agent, vault=_vault()
        )

        assert result["success"], result
        written = agent.write_config.await_args.args[2]
        assert SECRET in written, "the config was written with the reference, not the value"

    async def test_a_script_gets_the_real_value(self):
        agent = _agent()

        result = await shell_script_execute(
            {"id": "a1"}, SCRIPT_BODY, TARGET, agent, vault=_vault()
        )

        assert result["success"], result
        shipped = agent.write_file.await_args.args[2]
        assert SECRET in shipped

    async def test_a_missing_credential_refuses_rather_than_substituting_nothing(self):
        """A config written with an empty password is a working-LOOKING file that
        fails at 3am."""
        agent = _agent()
        vault = AsyncMock()
        vault.get_secret = AsyncMock(side_effect=RuntimeError("not found"))

        result = await host_provision_execute({"id": "a1"}, CONFIG_BODY, TARGET, agent, vault=vault)

        assert result["success"] is False
        assert "appdb" in result["failure_reason"]
        agent.write_config.assert_not_awaited()

    async def test_no_vault_configured_is_a_refusal(self):
        agent = _agent()

        result = await shell_script_execute({"id": "a1"}, SCRIPT_BODY, TARGET, agent, vault=None)

        assert result["success"] is False
        agent.write_file.assert_not_awaited()

    async def test_a_body_with_no_references_needs_no_vault(self):
        """The guard must not make an ordinary artifact require a vault."""
        plain = SCRIPT_BODY.replace("{{ vault.appdb.password }}", "hunter")
        agent = _agent()

        result = await shell_script_execute({"id": "a1"}, plain, TARGET, agent, vault=None)

        assert result["success"] is True


class TestTheValueNeverReachesAnOperatorSurface:
    async def test_it_is_not_in_the_execution_log(self):
        """The execution log is persisted on purpose (#487) and read back in the
        UI - a secret in there is a secret in the database."""
        agent = _agent()
        agent.exec = AsyncMock(return_value=(0, f"connected as app:{SECRET}", ""))

        result = await shell_script_execute(
            {"id": "a1"}, SCRIPT_BODY, TARGET, agent, vault=_vault()
        )

        assert SECRET not in result["execution_log"]
        assert "***" in result["execution_log"]

    async def test_it_is_not_in_a_failure_reason(self):
        agent = _agent()
        agent.exec = AsyncMock(side_effect=RuntimeError(f"auth failed for {SECRET}"))

        result = await shell_script_execute(
            {"id": "a1"}, SCRIPT_BODY, TARGET, agent, vault=_vault()
        )

        assert result["success"] is False
        assert SECRET not in result["failure_reason"]
        assert SECRET not in result["execution_log"]

    async def test_a_host_provision_log_is_redacted_too(self):
        agent = _agent()
        agent.write_config = AsyncMock(return_value={"changed": True, "detail": SECRET})

        result = await host_provision_execute(
            {"id": "a1"}, CONFIG_BODY, TARGET, agent, vault=_vault()
        )

        assert SECRET not in result["execution_log"]


class TestALiteralCredentialIsRefusedAtPropose:
    @pytest.fixture
    def lifecycle(self, tmp_path: Path) -> ArtifactLifecycle:
        return ArtifactLifecycle(store=ArtifactStore(tmp_path / "artifacts"))

    def _spec(self, artifact_id: str, kind: str, body: str) -> dict:
        return {
            "id": artifact_id,
            "kind": kind,
            "intent": "Secrets",
            "body": body,
            "target": TARGET,
            "idempotence": "via-precheck",
            "produced_by": {"session": "s", "agent": "a", "user": "u"},
        }

    async def test_a_password_written_out_in_full_is_refused(self, lifecycle):
        leaky = CONFIG_BODY.replace("{{ vault.appdb.password }}", "hunter2hunter2")

        with pytest.raises(LifecycleError) as exc:
            await lifecycle.propose(self._spec("2026-08-21-leaky-config", "host-provision", leaky))

        assert "literal credential" in str(exc.value)
        assert "vault" in str(exc.value)

    async def test_the_reference_form_is_accepted(self, lifecycle):
        artifact_id = await lifecycle.propose(
            self._spec("2026-08-21-referenced", "host-provision", CONFIG_BODY)
        )

        assert artifact_id == "2026-08-21-referenced"

    async def test_a_placeholder_is_not_treated_as_a_secret(self, lifecycle):
        """A guard that fires on prose trains people to work around it."""
        placeholder = CONFIG_BODY.replace("{{ vault.appdb.password }}", "changeme")

        artifact_id = await lifecycle.propose(
            self._spec("2026-08-21-placeholder", "host-provision", placeholder)
        )

        assert artifact_id


class TestTheHelpersThemselves:
    async def test_references_reads_name_and_field(self):
        assert references("{{ vault.db.password }} {{vault.tok}}") == [
            ("db", "password"),
            ("tok", "value"),
        ]

    async def test_redact_replaces_every_occurrence(self):
        assert redact(f"a {SECRET} b {SECRET}", [SECRET]) == "a *** b ***"

    async def test_a_secret_containing_regex_syntax_survives_substitution(self):
        """A value with a backslash or `\\g` in it would be mangled by a plain
        string replacement in `re.sub`."""
        vault = AsyncMock()
        vault.get_secret = AsyncMock(return_value={"value": r"pa\\ss\\g<0>word"})

        resolved, values = await resolve("x={{ vault.tok }}", vault)

        assert resolved == r"x=pa\\ss\\g<0>word"
        assert values == [r"pa\\ss\\g<0>word"]

    async def test_a_missing_field_is_an_error(self):
        vault = AsyncMock()
        vault.get_secret = AsyncMock(return_value={"value": "x"})

        with pytest.raises(SecretResolutionError, match="no field"):
            await resolve("{{ vault.tok.password }}", vault)

    async def test_literal_secrets_ignores_a_vault_reference(self):
        assert literal_secrets("password: {{ vault.db.password }}") == []
