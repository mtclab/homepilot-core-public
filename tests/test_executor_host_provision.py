from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.adapters.agent import AgentAdapterError
from homepilot.artifacts.models import parse_host_provision_spec
from homepilot.artifacts.validator import validate_host_provision_spec
from homepilot.executor.host_provision import execute as host_provision_execute
from homepilot.reconciler.verify import verify_artifact

# A well-formed host-provision artifact body: one package, one service, one config.
GOOD_BODY = """\
## Plan
Ensure nginx is installed, running, and configured.

```yaml host-provision-spec
packages:
  - nginx
services:
  - name: nginx
    state: started
config_files:
  - path: /etc/nginx/conf.d/app.conf
    content: |
      server { listen 80; }
    mode: "0644"
```
"""

VM_TARGET = {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}


def _config_content() -> str:
    return parse_host_provision_spec(GOOD_BODY).config_files[0].content


def _apply_agent() -> AsyncMock:
    agent = AsyncMock()
    agent.install_package = AsyncMock(return_value={"changed": True, "detail": "installed"})
    agent.manage_service = AsyncMock(return_value={"changed": True, "detail": "started"})
    agent.write_config = AsyncMock(return_value={"changed": True, "detail": "written"})
    # Apply now READS the host before it writes, to capture what a rollback would
    # have to put back (#426). A bare AsyncMock returns a MagicMock from
    # exec_readonly, which does not unpack into (rc, stdout, stderr).
    agent.exec_readonly = AsyncMock(return_value=(1, "", ""))
    agent.read_file = AsyncMock(side_effect=FileNotFoundError("absent"))
    # The capture reads through the write path's snapshot (#853).
    agent.snapshot_file = AsyncMock(side_effect=FileNotFoundError("absent"))
    return agent


class TestHostProvisionApply:
    async def test_applies_all_three_actions_with_right_args(self, make_frontmatter):
        agent = _apply_agent()
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)

        result = await host_provision_execute(fm, GOOD_BODY, fm["target"], agent)

        assert result["success"] is True
        agent.install_package.assert_awaited_once_with("web1", "nginx")
        agent.manage_service.assert_awaited_once_with("web1", "nginx", "started")
        agent.write_config.assert_awaited_once_with(
            "web1", "/etc/nginx/conf.d/app.conf", _config_content(), "0644"
        )

    async def test_failing_action_yields_failure_and_reason(self, make_frontmatter):
        agent = _apply_agent()
        agent.install_package = AsyncMock(side_effect=AgentAdapterError("apt update failed"))
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)

        result = await host_provision_execute(fm, GOOD_BODY, fm["target"], agent)

        assert result["success"] is False
        assert "apt update failed" in result["failure_reason"]
        # hard-stopped on the first failure — later items never ran
        agent.manage_service.assert_not_awaited()
        agent.write_config.assert_not_awaited()

    async def test_forbidden_on_pve_node_target(self, make_frontmatter):
        agent = _apply_agent()
        fm = make_frontmatter(kind="host-provision", target={"kind": "node", "node": "pve1"})

        result = await host_provision_execute(fm, GOOD_BODY, fm["target"], agent)

        assert result["success"] is False
        assert "node" in result["failure_reason"].lower()
        agent.install_package.assert_not_awaited()
        agent.manage_service.assert_not_awaited()
        agent.write_config.assert_not_awaited()

    async def test_forbidden_on_pve_node_by_hostname_guard(self, make_frontmatter):
        agent = _apply_agent()
        fm = make_frontmatter(
            kind="host-provision",
            target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "pve1"},
        )

        result = await host_provision_execute(
            fm, GOOD_BODY, fm["target"], agent, pve_nodes=["pve1"]
        )

        assert result["success"] is False
        assert result["failure_reason"] == "forbidden target"
        agent.install_package.assert_not_awaited()

    async def test_rollback_without_a_capture_refuses_rather_than_claiming_success(
        self, make_frontmatter
    ):
        """This test used to assert `success is True` for a rollback that did
        NOTHING - the defect written down as the requirement (#426). A revoke
        with nothing to invert to must say so, because the caller turns that
        answer into "reversed" or "relabelled" for the operator.
        """
        agent = _apply_agent()
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)

        result = await host_provision_execute(
            fm, GOOD_BODY, fm["target"], agent, rollback=True, pre_state=None
        )

        assert result["success"] is False
        assert "captured" in result["execution_log"]
        agent.install_package.assert_not_awaited()

    async def test_rollback_restores_a_config_file_it_overwrote(self, make_frontmatter):
        agent = _apply_agent()
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
        captured = [
            {
                "kind": "config",
                "name": "/etc/nginx/conf.d/app.conf",
                "existed": True,
                "prior_content": "the original bytes",
                "prior_mode": "0600",
            }
        ]

        result = await host_provision_execute(
            fm, GOOD_BODY, fm["target"], agent, rollback=True, pre_state=captured
        )

        assert result["success"] is True
        agent.write_config.assert_awaited_once_with(
            "web1", "/etc/nginx/conf.d/app.conf", "the original bytes", "0600"
        )

    async def test_rollback_reports_what_it_cannot_undo(self, make_frontmatter):
        """The agent has no package-removal or file-deletion verb. Guessing at
        them - `apt-get remove`, `rm` - is how an undo takes out a dependency or
        a file somebody else wrote."""
        agent = _apply_agent()
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
        captured = [
            {"kind": "package", "name": "nginx", "was_installed": False},
            {"kind": "config", "name": "/etc/nginx/conf.d/app.conf", "existed": False},
        ]

        result = await host_provision_execute(
            fm, GOOD_BODY, fm["target"], agent, rollback=True, pre_state=captured
        )

        assert result["success"] is False
        assert "nginx" in result["failure_reason"]
        assert "app.conf" in result["failure_reason"]

    async def test_rollback_puts_a_service_back_the_way_it_was(self, make_frontmatter):
        agent = _apply_agent()
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
        captured = [
            {
                "kind": "service",
                "name": "nginx",
                "desired": "started",
                "was_active": "inactive",
                "was_enabled": "disabled",
            }
        ]

        result = await host_provision_execute(
            fm, GOOD_BODY, fm["target"], agent, rollback=True, pre_state=captured
        )

        assert result["success"] is True
        states = [call.args[2] for call in agent.manage_service.await_args_list]
        assert "stopped" in states, "a service that was inactive was left running"
        assert "disabled" in states, "a service that was disabled was left enabled"

    async def test_apply_captures_the_prior_state(self, make_frontmatter):
        """Nothing else records it, and after the apply the prior bytes are gone."""
        agent = _apply_agent()
        agent.snapshot_file = AsyncMock(return_value="original config")
        agent.exec_readonly = AsyncMock(return_value=(0, "install ok installed", ""))
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)

        result = await host_provision_execute(fm, GOOD_BODY, fm["target"], agent)

        # Keyed by (kind, name): the package and the service are both "nginx".
        captured = {(item["kind"], item["name"]): item for item in result["pre_state"]}
        assert captured[("package", "nginx")]["was_installed"] is True
        assert (
            captured[("config", "/etc/nginx/conf.d/app.conf")]["prior_content"] == "original config"
        )


class TestHostProvisionSpecValidation:
    async def test_unknown_service_state_rejected_before_any_action(self, make_frontmatter):
        agent = _apply_agent()
        body = """\
## Plan
bad

```yaml host-provision-spec
services:
  - name: nginx
    state: teleported
```
"""
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
        result = await host_provision_execute(fm, body, fm["target"], agent)

        assert result["success"] is False
        assert "spec" in result["failure_reason"].lower() or "state" in result["failure_reason"]
        agent.install_package.assert_not_awaited()
        agent.manage_service.assert_not_awaited()
        agent.write_config.assert_not_awaited()

    async def test_malformed_item_missing_field_rejected(self, make_frontmatter):
        agent = _apply_agent()
        body = """\
## Plan
bad

```yaml host-provision-spec
services:
  - name: nginx
```
"""
        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
        result = await host_provision_execute(fm, body, fm["target"], agent)

        assert result["success"] is False
        agent.manage_service.assert_not_awaited()

    def test_metachar_package_name_rejected(self):
        body = """\
```yaml host-provision-spec
packages:
  - "nginx; rm -rf /"
```
"""
        with pytest.raises(ValueError, match="invalid package name"):
            parse_host_provision_spec(body)

    def test_metachar_config_path_rejected(self):
        body = """\
```yaml host-provision-spec
config_files:
  - path: "/etc/../etc/shadow"
    content: x
```
"""
        with pytest.raises(ValueError, match="invalid config_file path"):
            parse_host_provision_spec(body)

    def test_empty_spec_rejected(self):
        body = "```yaml host-provision-spec\npackages: []\n```\n"
        with pytest.raises(ValueError, match="at least one"):
            parse_host_provision_spec(body)

    def test_validator_wraps_error_as_lifecycle_error(self):
        from homepilot.artifacts.models import LifecycleError

        body = """\
```yaml host-provision-spec
services:
  - name: nginx
    state: nope
```
"""
        with pytest.raises(LifecycleError):
            validate_host_provision_spec(body)


def _drift_agent(
    *,
    pkg_installed: bool = True,
    service_active: bool = True,
    config_matches: bool = True,
) -> AsyncMock:
    """A read-only mock: exec_readonly answers dpkg/systemctl probes, read_file
    answers config compares. The mutating actions are present so the test can
    prove drift NEVER calls them."""
    agent = AsyncMock()

    async def _exec_readonly(host: str, command: str):
        if command.startswith("dpkg -s"):
            if pkg_installed:
                return (0, "Status: install ok installed\n", "")
            return (1, "dpkg-query: package is not installed\n", "")
        if command.startswith("systemctl is-active"):
            return (0, "active\n", "") if service_active else (3, "inactive\n", "")
        if command.startswith("systemctl is-enabled"):
            return (0, "enabled\n", "") if service_active else (1, "disabled\n", "")
        raise AssertionError(f"unexpected probe: {command}")

    async def _read_file(host: str, path: str):
        return _config_content() if config_matches else "TAMPERED"

    agent.exec_readonly = AsyncMock(side_effect=_exec_readonly)
    agent.read_file = AsyncMock(side_effect=_read_file)
    # mutating actions — must stay untouched by a drift check
    agent.install_package = AsyncMock()
    agent.manage_service = AsyncMock()
    agent.write_config = AsyncMock()
    agent.write_file = AsyncMock()
    return agent


def _fake_store(fm: dict) -> MagicMock:
    store = MagicMock()
    store.read = MagicMock(return_value=(fm, GOOD_BODY))
    return store


def _applied_fm(make_frontmatter) -> dict:
    fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
    fm["status"] = "applied"
    return fm


def _assert_no_mutation(agent: AsyncMock) -> None:
    agent.install_package.assert_not_awaited()
    agent.manage_service.assert_not_awaited()
    agent.write_config.assert_not_awaited()
    agent.write_file.assert_not_awaited()


class TestHostProvisionDrift:
    async def test_all_in_desired_state_not_drifted(self, make_frontmatter):
        agent = _drift_agent()
        executor = SimpleNamespace(host_adapter=agent, pve_nodes=[])
        fm = _applied_fm(make_frontmatter)

        result = await verify_artifact(
            "2025-01-01-test-abc123", MagicMock(), _fake_store(fm), executor
        )

        assert result.drifted is False
        _assert_no_mutation(agent)

    async def test_missing_package_drifts(self, make_frontmatter):
        agent = _drift_agent(pkg_installed=False)
        executor = SimpleNamespace(host_adapter=agent, pve_nodes=[])
        fm = _applied_fm(make_frontmatter)

        result = await verify_artifact(
            "2025-01-01-test-abc123", MagicMock(), _fake_store(fm), executor
        )

        assert result.drifted is True
        assert "package:nginx" in result.details["drifted_items"]
        _assert_no_mutation(agent)

    async def test_inactive_service_drifts(self, make_frontmatter):
        agent = _drift_agent(service_active=False)
        executor = SimpleNamespace(host_adapter=agent, pve_nodes=[])
        fm = _applied_fm(make_frontmatter)

        result = await verify_artifact(
            "2025-01-01-test-abc123", MagicMock(), _fake_store(fm), executor
        )

        assert result.drifted is True
        assert "service:nginx" in result.details["drifted_items"]
        _assert_no_mutation(agent)

    async def test_config_differs_drifts(self, make_frontmatter):
        agent = _drift_agent(config_matches=False)
        executor = SimpleNamespace(host_adapter=agent, pve_nodes=[])
        fm = _applied_fm(make_frontmatter)

        result = await verify_artifact(
            "2025-01-01-test-abc123", MagicMock(), _fake_store(fm), executor
        )

        assert result.drifted is True
        assert "config:/etc/nginx/conf.d/app.conf" in result.details["drifted_items"]
        _assert_no_mutation(agent)


# #853: the agent returns unit files with their Environment= values redacted.
# A rollback snapshot must not take those placeholders for the bytes that were
# there (writing them back would replace a service's real environment), and
# drift must compare like with like instead of reporting every unit with an
# environment as drifted.
UNIT_BODY = """\
## Plan
Run the app with its token.

```yaml host-provision-spec
config_files:
  - path: /etc/systemd/system/app.service.d/override.conf
    content: |
      [Service]
      Environment=TOKEN=value1 MODE=prod
    mode: "0644"
```
"""


class TestUnitFilesTheAgentRedacts:
    @staticmethod
    def _agent(on_host: str) -> AsyncMock:
        """An agent from before snapshot_file: the capture falls back to the
        read tier, which redacts."""
        from homepilot.adapters.agent import AgentTooOldError

        agent = _apply_agent()
        agent.read_file = AsyncMock(return_value=on_host)
        agent.snapshot_file = AsyncMock(side_effect=AgentTooOldError("no snapshot_file"))
        agent.exec_readonly = AsyncMock(return_value=(0, "644", ""))
        return agent

    async def test_a_redacted_prior_read_is_unknown_and_never_written_back(self, make_frontmatter):
        """Teeth: drop the unit_read_is_redacted check from capture_pre_state and
        the snapshot holds the placeholders, which the rollback then writes."""
        from homepilot.adapters.readonly_policy import redact_unit_text
        from homepilot.executor.host_provision import capture_pre_state

        real = "[Service]\nEnvironment=TOKEN=value0 MODE=dev\n"
        agent = self._agent(redact_unit_text(real)[0])
        spec = parse_host_provision_spec(UNIT_BODY)

        captured = await capture_pre_state(agent, "web1", spec)

        assert captured[0]["existed"] is None
        assert captured[0]["prior_content"] is None
        assert "redacts" in captured[0]["read_error"]

        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
        result = await host_provision_execute(
            fm, UNIT_BODY, fm["target"], agent, rollback=True, pre_state=captured
        )
        agent.write_config.assert_not_awaited()
        assert result["success"] is False
        assert "override.conf" in result["failure_reason"]
        assert "redacts" in result["failure_reason"]

    async def test_an_older_agents_real_bytes_stay_restorable(self):
        from homepilot.executor.host_provision import capture_pre_state

        real = "[Service]\nEnvironment=TOKEN=value0 MODE=dev\n"
        captured = await capture_pre_state(
            self._agent(real), "web1", parse_host_provision_spec(UNIT_BODY)
        )

        assert captured[0]["existed"] is True
        assert captured[0]["prior_content"] == real

    async def test_drift_compares_a_redacted_unit_redacted(self):
        """Teeth: drop the redacted comparison from probe and an applied unit
        with an environment reads "differs" for ever."""
        from homepilot.adapters.readonly_policy import redact_unit_text
        from homepilot.executor.host_provision import probe

        spec = parse_host_provision_spec(UNIT_BODY)
        desired = spec.config_files[0].content

        [item] = await probe(self._agent(redact_unit_text(desired)[0]), "web1", spec)
        assert item["changes"] is False and item["established"] is True
        assert item["observed"].startswith("matches")
        assert "not compared" in item["observed"]

        # Every byte outside the redacted values is still compared.
        other = desired.replace("[Service]", "[Service]\nUser=nobody")
        [item] = await probe(self._agent(redact_unit_text(other)[0]), "web1", spec)
        assert item["changes"] is True and item["observed"] == "differs"

        # An older agent's real bytes compare as bytes.
        [item] = await probe(self._agent(desired), "web1", spec)
        assert item["observed"] == "matches"


class TestRollbackThroughTheWritePathsSnapshot:
    """#853: the read tier now redacts every unit file's Exec*= arguments too,
    which would make the revoke of nearly any unit an artifact overwrote
    "cannot put back". A current agent answers the capture through
    snapshot_file - write_config's own read, privileged agents only - so the
    exact bytes are kept for the revoke while the read tier never shows them."""

    RAW = (
        "[Service]\nEnvironment=TOKEN=value0\n"
        "ExecStart=/usr/bin/app --token=value0 --port 80\n"
        "ExecStartPre=-/usr/bin/prep --key value0\n"
    )

    def _agent(self) -> AsyncMock:
        from homepilot.adapters.readonly_policy import redact_unit_text

        agent = _apply_agent()
        agent.snapshot_file = AsyncMock(return_value=self.RAW)
        agent.read_file = AsyncMock(return_value=redact_unit_text(self.RAW)[0])
        agent.exec_readonly = AsyncMock(return_value=(0, "644", ""))
        return agent

    async def test_the_revoke_puts_the_exact_bytes_back(self, make_frontmatter):
        """Teeth: capture through read_file again and the prior content is
        unknown, so the revoke refuses instead of restoring."""
        from homepilot.executor.host_provision import capture_pre_state

        agent = self._agent()
        captured = await capture_pre_state(agent, "web1", parse_host_provision_spec(UNIT_BODY))
        assert captured[0]["existed"] is True
        assert captured[0]["prior_content"] == self.RAW
        agent.read_file.assert_not_awaited()

        fm = make_frontmatter(kind="host-provision", target=VM_TARGET)
        result = await host_provision_execute(
            fm, UNIT_BODY, fm["target"], agent, rollback=True, pre_state=captured
        )
        assert result["success"] is True, result
        agent.write_config.assert_awaited_once_with(
            "web1", "/etc/systemd/system/app.service.d/override.conf", self.RAW, "644"
        )

    async def test_drift_of_a_unit_with_exec_arguments_compares_redacted(self):
        from homepilot.adapters.readonly_policy import redact_unit_text
        from homepilot.executor.host_provision import probe

        body = UNIT_BODY.replace(
            "      Environment=TOKEN=value1 MODE=prod\n",
            "      Environment=TOKEN=value1 MODE=prod\n      ExecStart=/usr/bin/app --token=value1\n",
        )
        spec = parse_host_provision_spec(body)
        desired = spec.config_files[0].content
        agent = self._agent()
        agent.read_file = AsyncMock(return_value=redact_unit_text(desired)[0])
        [item] = await probe(agent, "web1", spec)
        assert item["changes"] is False and "Exec arguments" in item["observed"]
        # Drift is read-tier only: it never asks for the raw bytes.
        agent.snapshot_file.assert_not_awaited()


def test_only_the_pre_apply_capture_asks_for_a_snapshot() -> None:
    """snapshot_file returns unredacted bytes, so nothing a read-scope caller
    reaches may call it: no MCP tool, no route, no drift or plan probe. Teeth:
    call it from anywhere else in src/ and this fails."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "homepilot"
    callers = sorted(
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if re.search(r"\.snapshot_file\(", p.read_text("utf-8"))
    )
    assert callers == ["executor/host_provision.py"], callers
    text = (src / "executor" / "host_provision.py").read_text("utf-8")
    assert text.count(".snapshot_file(") == 1
    assert "async def _read_prior" in text
