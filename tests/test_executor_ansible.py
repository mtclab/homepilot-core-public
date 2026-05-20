from unittest.mock import AsyncMock, Mock

from homepilot.adapters.ssh import GuestHostError, SSHAdapterError
from homepilot.executor.ansible import execute as ansible_execute

ANSIBLE_BODY = """\
## Plan
Install nginx

## Spec

```yaml ansible-spec
- name: Install nginx
  hosts: all
  tasks:
    - name: Install
      apt:
        name: nginx
        state: present
```
"""

ROLLBACK_BODY = """\
## Plan
Rollback nginx

## Spec

```yaml ansible-rollback
- name: Uninstall nginx
  hosts: all
  tasks:
    - name: Uninstall
      apt:
        name: nginx
        state: absent
```
"""

VARIABLES_BODY = """\
## Plan
Install nginx with vars

## Variables

```yaml
nginx_version: "1.24"
nginx_port: 8080
```

## Spec

```yaml ansible-spec
- name: Install nginx
  hosts: all
  tasks:
    - name: Install
      apt:
        name: nginx
        state: present
```
"""


class TestAnsibleExecutor:
    async def test_success_dry_run_then_apply(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "PLAY OK", ""), (0, "PLAY OK apply", "")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
        assert mock_ssh.exec.call_count == 2

    async def test_dry_run_failure_aborts(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(1, "", "err"))
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "dry-run failed" in result["failure_reason"]
        assert mock_ssh.exec.call_count == 1

    async def test_apply_failure(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "", ""), (2, "", "fail")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "rc=2" in result["failure_reason"]

    async def test_missing_spec_block(self, mock_ssh, make_frontmatter):
        body = "## Plan\nNo spec here\n"
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, body, fm["target"], mock_ssh)
        assert result["success"] is False
        assert result["failure_reason"] == "missing spec"

    async def test_invalid_yaml(self, mock_ssh, make_frontmatter):
        body = """\
## Plan
Bad yaml

## Spec

```yaml ansible-spec
{bad: [
```
"""
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, body, fm["target"], mock_ssh)
        assert result["success"] is False
        assert result["failure_reason"] == "invalid yaml"

    async def test_missing_host(self, mock_ssh, make_frontmatter):
        fm = make_frontmatter(target={})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm.get("target", {}), mock_ssh)
        assert result["success"] is False
        assert result["failure_reason"] == "missing host"

    async def test_guest_only_forbidden(self, mock_ssh, make_frontmatter):
        mock_ssh._validate_guest_only = Mock(
            side_effect=GuestHostError("SSH to PVE node 'pve1' forbidden")
        )
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "pve1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh, pve_nodes=["pve1"])
        assert result["success"] is False
        assert result["failure_reason"] == "forbidden target"

    async def test_invalid_hostname(self, mock_ssh, make_frontmatter):
        fm = make_frontmatter(
            target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "bad;host"}
        )
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "invalid" in result["failure_reason"] and "hostname" in result["failure_reason"]

    async def test_rollback_executes_rollback_spec(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ROLLBACK_BODY, fm["target"], mock_ssh, rollback=True)
        assert result["success"] is True
        assert mock_ssh.exec.call_count == 1

    async def test_extra_variables(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "dry ok", ""), (0, "apply ok", "")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, VARIABLES_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
        assert mock_ssh.exec.call_count == 2

    async def test_ssh_exception(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=SSHAdapterError("conn lost"))
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "conn lost" in result["failure_reason"]

    async def test_playbook_dict_not_list(self, mock_ssh, make_frontmatter):
        body = """\
## Plan
Dict playbook

## Spec

```yaml ansible-spec
name: Single play
hosts: all
tasks:
  - name: Test
    debug:
      msg: hello
```
"""
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, body, fm["target"], mock_ssh)
        assert result["success"] is False
        assert result["failure_reason"] == "invalid playbook"

    async def test_extract_variables_raw_yaml_no_fence(self, mock_ssh, make_frontmatter):
        body = """\
## Plan
Vars without fence

## Variables
nginx_version: "1.24"
nginx_port: 8080

## Spec

```yaml ansible-spec
- name: Install nginx
  hosts: all
  tasks:
    - name: Install
      apt:
        name: nginx
        state: present
```
"""
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        mock_ssh.exec = AsyncMock(side_effect=[(0, "dry ok", ""), (0, "apply ok", "")])
        result = await ansible_execute(fm, body, fm["target"], mock_ssh)
        assert result["success"] is True


class TestAnsibleIntegration:
    async def test_inventory_generation_uses_host(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), (0, "ok", "")])
        fm = make_frontmatter(
            target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "myhost42"}
        )
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
        first_call_host = mock_ssh.exec.call_args_list[0][0][0]
        assert first_call_host == "myhost42"

    async def test_playbook_written_via_tmp_file(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), (0, "ok", "")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
        cmds = [c[0][1] for c in mock_ssh.exec.call_args_list]
        for cmd in cmds:
            assert "hp-inv-" in cmd or "hp-play-" in cmd or "ansible-playbook" in cmd
        dry_run_cmd = cmds[0]
        assert "--check" in dry_run_cmd

    async def test_dry_run_cmd_includes_check_and_diff(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), (0, "ok", "")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        dry_run_cmd = mock_ssh.exec.call_args_list[0][0][1]
        assert "--check" in dry_run_cmd
        assert "--diff" in dry_run_cmd

    async def test_apply_cmd_lacks_check(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), (0, "ok", "")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        apply_cmd = mock_ssh.exec.call_args_list[1][0][1]
        assert "--check" not in apply_cmd

    async def test_subprocess_timeout_passed(self, mock_ssh, make_frontmatter):
        call_args_list = []

        async def _capture_exec(host, command, timeout=30):
            call_args_list.append({"host": host, "command": command, "timeout": timeout})
            return (0, "ok", "")

        mock_ssh.exec = AsyncMock(side_effect=_capture_exec)
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
        assert call_args_list[0]["timeout"] == 120
        assert call_args_list[1]["timeout"] == 300

    async def test_rollback_skips_dry_run(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(0, "ok", ""))
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ROLLBACK_BODY, fm["target"], mock_ssh, rollback=True)
        assert result["success"] is True
        assert mock_ssh.exec.call_count == 1
        cmd = mock_ssh.exec.call_args[0][1]
        assert "--check" not in cmd

    async def test_rollback_failure_returns_error(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(1, "", "rollback failed"))
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ROLLBACK_BODY, fm["target"], mock_ssh, rollback=True)
        assert result["success"] is False
        assert "rc=1" in result["failure_reason"]

    async def test_execution_log_contains_duration(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), (0, "ok", "")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
        assert "duration=" in result["execution_log"]

    async def test_ssh_adapter_error_in_apply_phase(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), SSHAdapterError("timeout")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "timeout" in result["failure_reason"]

    async def test_inventory_includes_proxy_jump(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), (0, "ok", "")])
        fm = make_frontmatter(target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
        for call in mock_ssh.exec.call_args_list:
            cmd = call[0][1]
            if "ansible-playbook" in cmd:
                assert "-i " in cmd

    async def test_node_target_resolves_host(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=[(0, "ok", ""), (0, "ok", "")])
        fm = make_frontmatter(target={"kind": "node", "node": "pve1"})
        result = await ansible_execute(fm, ANSIBLE_BODY, fm["target"], mock_ssh)
        assert result["success"] is True
