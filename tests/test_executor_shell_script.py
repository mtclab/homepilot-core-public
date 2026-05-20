from unittest.mock import AsyncMock, Mock

from homepilot.adapters.ssh import GuestHostError, SSHAdapterError
from homepilot.executor.shell_script import execute as shell_script_execute

SHELL_BODY = """\
## Plan
Install something

## Idempotence preamble
This script is idempotent because it checks before writing.

## Spec

```bash shell-spec
#!/bin/bash
set -euo pipefail
if [ ! -f /etc/foo ]; then
  echo "writing /etc/foo"
  echo "config" > /etc/foo
fi
```
"""

ROLLBACK_BODY = """\
## Plan
Rollback

## Spec

```bash shell-rollback
#!/bin/bash
set -euo pipefail
rm -f /etc/foo
```
"""


class TestShellScriptExecutor:
    async def test_success(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}
        )
        result = await shell_script_execute(fm, SHELL_BODY, fm["target"], mock_ssh)
        assert result["success"] is True

    async def test_nonzero_exit(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(1, "", "fail"))
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}
        )
        result = await shell_script_execute(fm, SHELL_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "rc=1" in result["failure_reason"]

    async def test_missing_spec_block(self, mock_ssh, make_frontmatter):
        body = "## Plan\nNo spec\n## Idempotence preamble\nsome text\n"
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}
        )
        result = await shell_script_execute(fm, body, fm["target"], mock_ssh)
        assert result["success"] is False
        assert result["failure_reason"] == "missing spec"

    async def test_missing_idempotence_preamble(self, mock_ssh, make_frontmatter):
        body = """\
## Plan
No preamble here

## Spec

```bash shell-spec
#!/bin/bash
echo hi
```
"""
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}
        )
        result = await shell_script_execute(fm, body, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "Idempotence preamble" in result["failure_reason"]

    async def test_forbidden_target_node(self, mock_ssh, make_frontmatter):
        fm = make_frontmatter(kind="shell-script", target={"kind": "node", "host": "pve1"})
        result = await shell_script_execute(fm, SHELL_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert (
            "forbidden" in result["failure_reason"].lower()
            or "node" in result["failure_reason"].lower()
        )

    async def test_forbidden_target_cluster(self, mock_ssh, make_frontmatter):
        fm = make_frontmatter(kind="shell-script", target={"kind": "cluster"})
        result = await shell_script_execute(fm, SHELL_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert (
            "forbidden" in result["failure_reason"].lower()
            or "cluster" in result["failure_reason"].lower()
        )

    async def test_guest_only_forbidden(self, mock_ssh, make_frontmatter):
        mock_ssh._validate_guest_only = Mock(
            side_effect=GuestHostError("SSH to PVE node 'pve1' forbidden")
        )
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "pve1"}
        )
        result = await shell_script_execute(
            fm, SHELL_BODY, fm["target"], mock_ssh, pve_nodes=["pve1"]
        )
        assert result["success"] is False
        assert result["failure_reason"] == "forbidden target"

    async def test_missing_host(self, mock_ssh, make_frontmatter):
        fm = make_frontmatter(kind="shell-script", target={})
        result = await shell_script_execute(fm, SHELL_BODY, fm.get("target", {}), mock_ssh)
        assert result["success"] is False
        assert result["failure_reason"] == "missing host"

    async def test_ssh_exception(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(side_effect=SSHAdapterError("conn lost"))
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}
        )
        result = await shell_script_execute(fm, SHELL_BODY, fm["target"], mock_ssh)
        assert result["success"] is False
        assert "conn lost" in result["failure_reason"]

    async def test_rollback_skips_preamble_check(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}
        )
        result = await shell_script_execute(
            fm, ROLLBACK_BODY, fm["target"], mock_ssh, rollback=True
        )
        assert result["success"] is True

    async def test_heredoc_escaping(self, mock_ssh, make_frontmatter):
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        body = """\
## Plan
Test heredoc

## Idempotence preamble
Checks before writing.

## Spec

```bash shell-spec
#!/bin/bash
echo "HPEOF"
```
"""
        fm = make_frontmatter(
            kind="shell-script", target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"}
        )
        result = await shell_script_execute(fm, body, fm["target"], mock_ssh)
        assert result["success"] is True
        call_args = mock_ssh.exec.call_args
        command = call_args[0][1]
        # Random marker must start with HPEOF_ and not appear literally in the script
        assert "HPEOF_" in command

    async def test_heredoc_marker_not_in_script(self, mock_ssh, make_frontmatter):
        """Marker chosen must not appear in the script body."""
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        from homepilot.executor.shell_script import _escape_for_heredoc

        script = "echo 'hello world'"
        marker, command = _escape_for_heredoc(script)
        assert marker not in script
        assert command.startswith(f"cat <<'{marker}'")
        assert command.endswith(marker)
