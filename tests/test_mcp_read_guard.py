"""#378: read_file_on_guest enforces a read-prefix allowlist plus a secret
denylist (private keys, shadow files, process environments) as defense-in-depth
at the MCP boundary — mirroring the host agent's own protections."""

from __future__ import annotations

import asyncio

import pytest

from homepilot.mcp.tools.system_tools import (
    _check_guest_read_path,
    handle_read_file_on_guest,
)


class TestReadPathAllowlist:
    def test_allowed_prefix_passes(self):
        # Under an allowed prefix, no secret match -> permitted (no raise).
        _check_guest_read_path("/var/log/syslog")
        _check_guest_read_path("/etc/hostname")
        _check_guest_read_path("/opt/homepilot/config.yaml")

    def test_path_outside_allowlist_rejected(self):
        with pytest.raises(ValueError, match="not under an allowed read prefix"):
            _check_guest_read_path("/root/secret.txt")

    def test_parent_traversal_rejected(self):
        with pytest.raises(ValueError, match="parent traversal"):
            _check_guest_read_path("/var/log/../../root/.bashrc")


class TestReadSecretDenylist:
    def test_shadow_denied_even_under_allowed_prefix(self):
        # /etc/ is an allowed prefix, but /etc/shadow is denied regardless.
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/etc/shadow")

    def test_gshadow_denied(self):
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/etc/gshadow")

    def test_private_key_basenames_denied(self):
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/home/olli/.ssh/id_rsa")
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/home/olli/.ssh/id_ed25519")

    def test_key_and_pem_suffixes_denied(self):
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/etc/ssl/private/server.key")
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/etc/ssl/certs/server.pem")

    def test_proc_environ_denied(self):
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/proc/1/environ")


class TestHandlerEnforcesGuard:
    """The handler must reject a denied path BEFORE reaching the agent adapter."""

    def test_handler_blocks_denied_path_without_calling_adapter(self):
        class _RecordingAdapter:
            def __init__(self):
                self.called = False

            async def read_file(self, host, path):
                self.called = True
                return "SHOULD-NOT-BE-REACHED"

        adapter = _RecordingAdapter()
        ctx = {"agent_adapter": adapter}
        args = {"host": "web01", "path": "/etc/shadow"}
        with pytest.raises(ValueError, match="denied"):
            asyncio.run(handle_read_file_on_guest(args, ctx))
        assert adapter.called is False

    def test_handler_allows_permitted_path(self):
        class _OkAdapter:
            async def read_file(self, host, path):
                return "file-contents"

        ctx = {"agent_adapter": _OkAdapter()}
        args = {"host": "web01", "path": "/var/log/syslog"}
        result = asyncio.run(handle_read_file_on_guest(args, ctx))
        assert result[0].text == "file-contents"
