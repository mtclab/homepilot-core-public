"""Unit tests for adoption-time read-only host introspection (#397).

Covers the pure parsers and the ``introspect_host`` orchestration: probes are
parsed into a structured result; a probe that exits non-zero or raises is marked
``unavailable`` without aborting the run; and a missing/unconnected agent yields
``skipped``.
"""

from __future__ import annotations

from typing import Any

from homepilot.inventory.introspect import (
    PACKAGE_SAMPLE_CAP,
    PROBE_OK,
    PROBE_UNAVAILABLE,
    introspect_host,
    parse_docker_ps,
    parse_dpkg,
    parse_os_release,
    parse_ss,
    parse_uname,
)

# ── canned probe outputs ────────────────────────────────────────────────────────

_OS_RELEASE = (
    'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
    'NAME="Debian GNU/Linux"\n'
    "ID=debian\n"
    'VERSION_ID="12"\n'
)
_UNAME = "Linux vm1 6.1.0-13-amd64 #1 SMP PREEMPT_DYNAMIC x86_64 GNU/Linux"
_DPKG = (
    "Desired=Unknown/Install/Remove/Purge/Hold\n"
    "| Status=Not/Inst/Conf-files/Unpacked\n"
    "||/ Name           Version      Architecture Description\n"
    "+++-==============-============-============-=================\n"
    "ii  bash           5.2.15-2     amd64        GNU Bourne Again SHell\n"
    "ii  curl           7.88.1-10    amd64        command line tool\n"
    "ii  libc6:amd64    2.36-9       amd64        GNU C Library\n"
    "rc  oldpkg         1.0          amd64        removed, config remains\n"
)
_SS = (
    "State   Recv-Q  Send-Q   Local Address:Port   Peer Address:Port\n"
    "LISTEN  0       128      0.0.0.0:22           0.0.0.0:*\n"
    "LISTEN  0       128      [::]:80              [::]:*\n"
)
_DOCKER = (
    "CONTAINER ID   IMAGE          COMMAND      CREATED       STATUS       PORTS     NAMES\n"
    'abc123def456   nginx:latest   "/docker"    2 hours ago   Up 2 hours   80/tcp    web\n'
    '789ghi012jkl   redis:7        "redis"      3 days ago    Up 3 days    6379/tcp  cache\n'
)
_HOSTNAME = "vm1\n"


def _canned_adapter(overrides: dict[str, tuple[int, str]] | None = None) -> Any:
    """A fake agent adapter whose exec_readonly returns canned probe outputs.

    ``overrides`` maps a command prefix to a ``(exit_code, stdout)`` pair,
    replacing the default healthy output for that probe.
    """
    defaults: dict[str, tuple[int, str]] = {
        "uname": (0, _UNAME),
        "cat /etc/os-release": (0, _OS_RELEASE),
        "dpkg -l": (0, _DPKG),
        "ss -tln": (0, _SS),
        "docker ps": (0, _DOCKER),
        "cat /etc/hostname": (0, _HOSTNAME),
    }
    if overrides:
        defaults.update(overrides)

    class _Adapter:
        async def test_connection(self, host: str) -> bool:
            return True

        async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]:
            for prefix, (rc, out) in defaults.items():
                if command.startswith(prefix):
                    return rc, out, ""
            return 127, "", "not found"

    return _Adapter()


# ── parsers ─────────────────────────────────────────────────────────────────────


def test_parse_os_release() -> None:
    pretty, distro = parse_os_release(_OS_RELEASE)
    assert pretty == "Debian GNU/Linux 12 (bookworm)"
    assert distro == "debian"


def test_parse_os_release_empty() -> None:
    assert parse_os_release("") == (None, None)


def test_parse_uname_extracts_kernel_release() -> None:
    assert parse_uname(_UNAME) == "6.1.0-13-amd64"


def test_parse_dpkg_counts_installed_and_samples() -> None:
    count, sample = parse_dpkg(_DPKG)
    # Only the three ``ii`` rows are installed; the ``rc`` row is excluded.
    assert count == 3
    # Arch-qualified name is reduced to the bare package name.
    assert sample == ["bash", "curl", "libc6"]


def test_parse_dpkg_sample_is_capped() -> None:
    rows = "".join(f"ii  pkg{i}  1.0  amd64  desc\n" for i in range(PACKAGE_SAMPLE_CAP + 10))
    count, sample = parse_dpkg(rows)
    assert count == PACKAGE_SAMPLE_CAP + 10
    assert len(sample) == PACKAGE_SAMPLE_CAP


def test_parse_ss_skips_header_and_extracts_ports() -> None:
    ports = parse_ss(_SS)
    assert ports == [
        {"proto": "tcp", "local": "0.0.0.0:22", "port": "22"},
        {"proto": "tcp", "local": "[::]:80", "port": "80"},
    ]


def test_parse_docker_ps() -> None:
    containers = parse_docker_ps(_DOCKER)
    assert containers == [
        {"image": "nginx:latest", "name": "web"},
        {"image": "redis:7", "name": "cache"},
    ]


def test_parse_docker_ps_unexpected_format_is_empty() -> None:
    assert parse_docker_ps("some other command output\n") == []


# ── introspect_host ─────────────────────────────────────────────────────────────


async def test_introspect_host_parses_all_probes() -> None:
    result = await introspect_host("vm1", _canned_adapter())
    assert result.skipped is None
    assert result.os == "Debian GNU/Linux 12 (bookworm)"
    assert result.distro_id == "debian"
    assert result.kernel == "6.1.0-13-amd64"
    assert result.reported_hostname == "vm1"
    assert result.package_count == 3
    assert result.package_sample == ["bash", "curl", "libc6"]
    assert len(result.listening_ports) == 2
    assert len(result.docker_containers) == 2
    assert all(status == PROBE_OK for status in result.probes.values())


async def test_introspect_host_nonzero_rc_is_unavailable_without_aborting() -> None:
    # dpkg exits non-zero on a non-dpkg system: that probe is unavailable, its
    # data stays unset, and every OTHER probe still runs and parses.
    adapter = _canned_adapter(overrides={"dpkg -l": (1, "dpkg: not found")})
    result = await introspect_host("vm1", adapter)
    assert result.probes["packages"] == PROBE_UNAVAILABLE
    assert result.package_count is None
    assert result.package_sample == []
    # Downstream probes were not aborted by the failed one.
    assert result.probes["listening_ports"] == PROBE_OK
    assert result.probes["hostname"] == PROBE_OK
    assert result.reported_hostname == "vm1"


async def test_introspect_host_probe_exception_is_unavailable() -> None:
    class _Boom:
        async def test_connection(self, host: str) -> bool:
            return True

        async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]:
            if command.startswith("docker"):
                raise RuntimeError("command not in read-only whitelist")
            return 0, _HOSTNAME if "hostname" in command else _UNAME, ""

    result = await introspect_host("vm1", _Boom())
    # A raising probe (mirrors the adapter rejecting `docker ps`) is unavailable,
    # and the run continues past it.
    assert result.probes["docker"] == PROBE_UNAVAILABLE
    assert result.docker_containers == []
    assert result.probes["hostname"] == PROBE_OK


async def test_introspect_host_no_adapter_is_skipped() -> None:
    result = await introspect_host("vm1", None)
    assert result.skipped == "no agent connected"
    assert result.os is None
    assert result.probes == {}


async def test_introspect_host_disconnected_agent_is_skipped() -> None:
    class _Disconnected:
        async def test_connection(self, host: str) -> bool:
            return False

        async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]:
            raise AssertionError("must not probe when no agent is connected")

    result = await introspect_host("vm1", _Disconnected())
    assert result.skipped == "no agent connected"


async def test_as_found_note_is_marked_observed() -> None:
    result = await introspect_host("vm1", _canned_adapter())
    note = result.as_found_note()
    assert "OBSERVED" in note
    assert "As-found observation" in note
    assert "never re-apply" in note.lower()
