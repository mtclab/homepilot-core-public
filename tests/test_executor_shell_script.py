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

    async def test_ships_script_via_write_file_and_metachar_free_exec(
        self, mock_ssh, make_frontmatter
    ):
        # #388: the executor must NOT send a piped heredoc (rejected by the agent
        # allowlist's shell-metachar filter). It ships the body via write_file to
        # /opt/homepilot and runs a metachar-free `bash <path>`.
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        fm = make_frontmatter(
            kind="shell-script",
            target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
        )
        result = await shell_script_execute(fm, SHELL_BODY, fm["target"], mock_ssh)
        assert result["success"] is True

        # script was written to the HP write prefix, content = the raw script body
        assert mock_ssh.write_file.await_count == 1
        w_host, w_path, w_content = mock_ssh.write_file.await_args.args
        assert w_host == "web1"
        assert w_path.startswith("/opt/homepilot/")
        assert w_path.endswith(".sh")
        assert "if [ ! -f /etc/foo ]" in w_content

        # exec ran `bash <that path>` with no shell metacharacters
        exec_cmd = mock_ssh.exec.await_args.args[1]
        assert exec_cmd == f"bash {w_path}"
        for meta in ("|", "<", ">", ";", "&", "$", "`"):
            assert meta not in exec_cmd

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

    async def test_rollback_and_apply_use_distinct_stable_paths(self, mock_ssh, make_frontmatter):
        # Apply and rollback write to distinct, stable (token-free) paths so a
        # re-apply overwrites rather than accumulating orphaned scripts.
        from homepilot.executor.shell_script import _remote_script_path

        apply_path = _remote_script_path("art-123", rollback=False)
        rollback_path = _remote_script_path("art-123", rollback=True)
        assert apply_path != rollback_path
        assert apply_path == _remote_script_path("art-123", rollback=False)  # stable
        assert apply_path.startswith("/opt/homepilot/") and apply_path.endswith(".sh")
        # id is sanitised to the allowlist character class (no metachars leak in)
        assert _remote_script_path("a/b;c$d", rollback=False).count(".sh") == 1
        for meta in ("|", "<", ">", ";", "&", "$", "`", ".."):
            assert meta not in _remote_script_path("a;b|c$d..e", rollback=False)
