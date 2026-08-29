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

    def test_the_agents_own_credential_files_are_denied(self):
        """Review #648, found live on dev at 3.6.14.

        `/etc/homepilot/agent.env` is what scripts/install-agent.sh writes, and
        it carries HP_AGENT_AUTH_TOKEN - the SHARED FLEET ENROLMENT TOKEN - plus
        the hub's address and certificate pin. The MCP surface refuses to serve
        that token by name (GET /agents/token and the installer one-liner are
        both in EXCLUDED_GET_ROUTES: "a credential that provisions machines must
        not appear in an MCP transcript") - and then read_file_on_guest, which
        sits at the READ tier, read it straight off a managed host instead.
        `/etc/` is an allowed prefix and nothing on the denylist matched it.

        TEETH: remove either path from _DENIED_READ_PATHS and this fails.
        """
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/etc/homepilot/agent.env")
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path("/etc/homepilot/agent.token")

    def test_ordinary_files_in_the_agent_config_dir_still_read(self):
        """Guard the guard: /etc/homepilot is a granted WRITE prefix, so
        artifacts put files there. Denying the whole directory would look like a
        stronger fix and would break reading back what HomePilot itself wrote."""
        _check_guest_read_path("/etc/homepilot/some-artifact.conf")


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
