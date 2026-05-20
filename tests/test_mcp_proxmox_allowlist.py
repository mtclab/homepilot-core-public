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
