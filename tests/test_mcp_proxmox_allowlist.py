"""Tests for _proxmox_path_allowed boundary check."""

from __future__ import annotations

from homepilot.mcp.tools.system_tools import _proxmox_path_allowed


class TestExactMatch:
    def test_version_exact(self):
        assert _proxmox_path_allowed("/version") is True

    def test_api2_json_version_exact(self):
        assert _proxmox_path_allowed("/api2/json/version") is True

    def test_no_leading_slash_still_works(self):
        assert _proxmox_path_allowed("version") is True


class TestBoundedPrefix:
    def test_nodes_subpath(self):
        assert _proxmox_path_allowed("/nodes/pve1/status") is True

    def test_storage_subpath(self):
        assert _proxmox_path_allowed("/storage/local/content") is True

    def test_cluster_subpath(self):
        assert _proxmox_path_allowed("/cluster/resources") is True


class TestBoundaryRejection:
    """Without the boundary fix, plain startswith() would accept these."""

    def test_versionfoo_rejected(self):
        assert _proxmox_path_allowed("/versionfoo") is False

    def test_version_destroy_rejected(self):
        assert _proxmox_path_allowed("/version-destroy") is False

    def test_api2_json_versionfoo_rejected(self):
        assert _proxmox_path_allowed("/api2/json/versionfoo") is False

    def test_nodesfoo_rejected(self):
        # /nodes/ has trailing slash so already bounded, but verify anyway
        assert _proxmox_path_allowed("/nodesfoo") is False

    def test_storagex_rejected(self):
        assert _proxmox_path_allowed("/storagex") is False


class TestUnknownPrefixes:
    def test_random_path_rejected(self):
        assert _proxmox_path_allowed("/qemu/100/status") is False

    def test_empty_rejected(self):
        assert _proxmox_path_allowed("") is False

    def test_root_rejected(self):
        assert _proxmox_path_allowed("/") is False


class TestGuestAgentNamespaceDenied:
    """#716: the QEMU guest-agent API is not a read of the hypervisor.

    `GET /nodes/<n>/qemu/<vmid>/agent/file-read?file=...` reads a file INSIDE the
    guest - including /etc/homepilot/agent.env, the fleet enrolment token that
    read_file_on_guest refuses by name. `agent/exec-status` returns another
    caller's command output. The read-tier passthrough sat under the allowed
    /nodes/ prefix and only blocked execute/vnc/term/upload, so every spelling
    below used to be accepted. The whole guest-agent namespace is denied, and the
    check runs on the DECODED, segment-split path so an encoding or a doubled or
    dotted segment cannot walk around it.
    """

    SPELLINGS = (
        "/nodes/pve1/qemu/100/agent/file-read",
        "/nodes/pve1/qemu/100/agent/exec-status",
        "/nodes/pve1/qemu/100/agent/get-users",
        "/nodes/pve1/qemu/100/agent",
        "nodes/pve1/qemu/100/agent/file-read",
        "/nodes/pve1/qemu/100//agent/file-read",
        "/nodes/pve1/qemu/100/./agent/file-read",
        "/nodes/pve1/qemu/100/status/../agent/file-read",
        "/nodes/pve1/qemu/100/%61gent/file-read",
        "/nodes/pve1/qemu/100/agent%2Ffile-read",
        "/nodes/pve1/qemu/100%2Fagent%2Ffile-read",
        "/nodes/pve1/qemu/100/%2561gent/file-read",
        "/nodes/pve1/qemu/100/AGENT/file-read",
        "/nodes/pve1/qemu/100/agent/file-read?file=/etc/homepilot/agent.env",
    )

    def test_every_spelling_is_refused(self):
        accepted = [p for p in self.SPELLINGS if _proxmox_path_allowed(p)]
        assert not accepted, f"guest-agent paths still accepted: {accepted}"

    def test_hypervisor_reads_are_still_allowed(self):
        for path in (
            "/nodes/pve1/qemu/100/status/current",
            "/nodes/pve1/qemu/100/config",
            "/nodes/pve1/qemu",
            "/cluster/resources",
            # A segment that merely CONTAINS the word is not the namespace.
            "/nodes/agent-host/status",
        ):
            assert _proxmox_path_allowed(path), path
