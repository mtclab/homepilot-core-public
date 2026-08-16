"""Best-effort, read-only host introspection run at adoption time (#397).

When an imported host is ADOPTED, HomePilot runs a small set of allowlisted,
read-only probes over the connected agent and records what it FINDS. This is a
descriptive snapshot of OBSERVED state — never authored intent. Nothing here is
ever "re-applied": the operator later authors real artifacts deliberately. The
observation is persisted as ``services`` rows (marked observed) plus an
"as-found" KB note (see ``InventoryService.introspect_and_record``).

Every probe is independent and best-effort: a probe that is blocked, errors, or
exits non-zero is recorded as ``unavailable`` and never aborts the run. If no
agent is connected the whole run is ``skipped``.

The probes use only commands the host agent permits read-only (kept in lockstep
with ``agent/go/allowlist.go`` ``safeCommands`` and the adapter's
``exec_readonly`` whitelist). ``docker ps`` is listed among the probes but is
intentionally narrowed OUT of the adapter's read-only path
(``_INTENTIONAL_NARROWING`` in ``adapters/agent.py``, parity-gated); when the
adapter rejects it the docker probe is simply recorded ``unavailable`` — the
parser is complete and ready should the read-only path ever include docker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Per-probe status vocabulary.
PROBE_OK = "ok"
PROBE_UNAVAILABLE = "unavailable"

# The allowlisted read-only probe commands, keyed by a stable probe name. Each is
# permitted by the agent's read-only allowlist (agent/go/allowlist.go). ``docker``
# is attempted best-effort even though the adapter narrows it out (see module
# docstring) — a rejection is tolerated as ``unavailable``.
PROBE_COMMANDS: dict[str, str] = {
    "kernel": "uname -a",
    "os": "cat /etc/os-release",
    "packages": "dpkg -l",
    "listening_ports": "ss -tln",
    "docker": "docker ps",
    "hostname": "cat /etc/hostname",
}

# Cap on how many package names are retained in the sample (the full count is
# still reported). Keeps the KB note and services rows bounded on large hosts.
PACKAGE_SAMPLE_CAP = 40


@dataclass
class IntrospectionResult:
    """Structured, descriptive result of a read-only introspection run.

    ``skipped`` is set (and every other field left empty) when no agent was
    connected. ``probes`` maps each probe name to ``ok``/``unavailable``.
    """

    host: str
    skipped: str | None = None
    observed_at: str = ""
    os: str | None = None
    distro_id: str | None = None
    kernel: str | None = None
    uname: str | None = None
    reported_hostname: str | None = None
    package_count: int | None = None
    package_sample: list[str] = field(default_factory=list)
    listening_ports: list[dict[str, str]] = field(default_factory=list)
    docker_containers: list[dict[str, str]] = field(default_factory=list)
    probes: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """A small, JSON-serializable summary safe to return in an API response."""
        if self.skipped:
            return {"host": self.host, "skipped": self.skipped}
        return {
            "host": self.host,
            "observed_at": self.observed_at,
            "os": self.os,
            "kernel": self.kernel,
            "reported_hostname": self.reported_hostname,
            "package_count": self.package_count,
            "listening_port_count": len(self.listening_ports),
            "docker_container_count": len(self.docker_containers),
            "probes": self.probes,
        }

    def as_found_note(self) -> str:
        """Render the observation as a Markdown "as-found" note body.

        The note is deliberately, repeatedly marked as OBSERVED / descriptive so
        it can never be mistaken for authored intent.
        """
        lines: list[str] = [
            f"# As-found observation: {self.reported_hostname or self.host}",
            "",
            "> **OBSERVED state captured at adoption.** This is a descriptive "
            "baseline of what was found on the host, NOT authored intent. "
            "HomePilot will never re-apply anything recorded here.",
            "",
            f"- Observed at: {self.observed_at}",
            f"- Target host: {self.host}",
        ]
        if self.skipped:
            lines.append(f"- Introspection skipped: {self.skipped}")
            return "\n".join(lines)
        lines.extend(
            [
                f"- OS: {self.os or 'unavailable'}",
                f"- Distro id: {self.distro_id or 'unavailable'}",
                f"- Kernel: {self.kernel or 'unavailable'}",
                f"- Reported hostname: {self.reported_hostname or 'unavailable'}",
                (
                    f"- Installed packages: {self.package_count}"
                    if self.package_count is not None
                    else "- Installed packages: unavailable"
                ),
            ]
        )
        if self.package_sample:
            sample = ", ".join(self.package_sample)
            lines.append(f"  - Sample ({len(self.package_sample)}): {sample}")
        if self.listening_ports:
            lines.append(f"- Listening ports ({len(self.listening_ports)}):")
            for p in self.listening_ports:
                lines.append(f"  - {p.get('proto', 'tcp')} {p.get('local', '')}")
        else:
            lines.append("- Listening ports: none observed / unavailable")
        if self.docker_containers:
            lines.append(f"- Docker containers ({len(self.docker_containers)}):")
            for c in self.docker_containers:
                lines.append(f"  - {c.get('name', '')} (image: {c.get('image', '')})")
        else:
            lines.append("- Docker containers: none observed / unavailable")
        lines.append("")
        lines.append("Per-probe status:")
        for name, status in self.probes.items():
            lines.append(f"- {name}: {status}")
        return "\n".join(lines)


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_os_release(text: str) -> tuple[str | None, str | None]:
    """Parse ``/etc/os-release`` into ``(pretty_name, distro_id)``.

    Tolerant of quotes and unknown keys; returns ``(None, None)`` on empty input.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    pretty = values.get("PRETTY_NAME") or values.get("NAME") or None
    distro_id = values.get("ID") or None
    return pretty, distro_id


def parse_uname(text: str) -> str | None:
    """Extract the kernel release from ``uname -a`` output.

    ``uname -a`` prints ``<sysname> <nodename> <release> <version...>``; the
    release is the third field. Falls back to the whole stripped line.
    """
    stripped = text.strip()
    if not stripped:
        return None
    fields = stripped.split()
    if len(fields) >= 3:
        return fields[2]
    return stripped


def parse_dpkg(text: str) -> tuple[int, list[str]]:
    """Parse ``dpkg -l`` output into ``(installed_count, sample_names)``.

    Only rows whose status field starts with ``ii`` (installed) are counted. The
    sample is capped at ``PACKAGE_SAMPLE_CAP`` package names.
    """
    count = 0
    sample: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        fields = line.split()
        if len(fields) < 2:
            continue
        if fields[0] != "ii":
            continue
        count += 1
        if len(sample) < PACKAGE_SAMPLE_CAP:
            # dpkg -l may print an arch-qualified name (e.g. ``libc6:amd64``);
            # keep the bare package name.
            sample.append(fields[1].split(":")[0])
    return count, sample


def parse_ss(text: str) -> list[dict[str, str]]:
    """Parse ``ss -tln`` output into a list of listening sockets.

    Each entry is ``{"proto": "tcp", "local": "<addr:port>", "port": "<port>"}``.
    The header row and malformed lines are skipped.
    """
    result: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = line.split()
        # Header row starts with "State"; data rows have the local address in
        # column index 3 (State Recv-Q Send-Q Local Peer).
        if fields[0].lower() in ("state", "netid"):
            continue
        if len(fields) < 4:
            continue
        local = fields[3]
        port = local.rsplit(":", 1)[-1] if ":" in local else ""
        result.append({"proto": "tcp", "local": local, "port": port})
    return result


def parse_docker_ps(text: str) -> list[dict[str, str]]:
    """Parse ``docker ps`` (default table format) into container dicts.

    Columns are separated by runs of two-or-more spaces. Returns
    ``{"image": ..., "name": ...}`` per container; the header row is skipped.
    """
    import re

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0]
    if not header.lower().startswith("container id"):
        # Unexpected format — do not guess.
        return []
    result: list[dict[str, str]] = []
    for line in lines[1:]:
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) < 2:
            continue
        image = cols[1] if len(cols) > 1 else ""
        # NAMES is the last column in the default `docker ps` layout.
        name = cols[-1]
        result.append({"image": image, "name": name})
    return result


async def _run_probe(agent_adapter: Any, host: str, command: str) -> tuple[bool, str]:
    """Run one read-only probe. Returns ``(ok, stdout)``.

    ``ok`` is False (and stdout empty) for any failure mode: a blocked command,
    a transport error, or a non-zero exit code. Never raises.
    """
    try:
        exit_code, stdout, _stderr = await agent_adapter.exec_readonly(host, command)
    except Exception as exc:  # best-effort: any failure -> unavailable
        logger.debug("Probe %r on %s unavailable: %s", command, host, exc)
        return False, ""
    if exit_code != 0:
        logger.debug("Probe %r on %s exited %s -> unavailable", command, host, exit_code)
        return False, ""
    return True, stdout


async def introspect_host(host: str, agent_adapter: Any) -> IntrospectionResult:
    """Run best-effort, read-only introspection of ``host`` via ``agent_adapter``.

    ``host`` is the hostname the agent adapter resolves. ``agent_adapter`` must
    expose ``async exec_readonly(host, command) -> (exit_code, stdout, stderr)``;
    an optional ``async test_connection(host) -> bool`` is used to short-circuit
    to ``skipped`` when no agent is connected. If ``agent_adapter`` is ``None``
    the run is skipped. Never raises.
    """
    result = IntrospectionResult(host=host, observed_at=_utcnow_iso())

    if agent_adapter is None:
        result.skipped = "no agent connected"
        return result

    test_connection = getattr(agent_adapter, "test_connection", None)
    if test_connection is not None:
        try:
            connected = await test_connection(host)
        except Exception as exc:  # a probe of connectivity must not abort
            logger.debug("Connectivity check for %s failed: %s", host, exc)
            connected = False
        if not connected:
            result.skipped = "no agent connected"
            return result

    for name, command in PROBE_COMMANDS.items():
        ok, stdout = await _run_probe(agent_adapter, host, command)
        result.probes[name] = PROBE_OK if ok else PROBE_UNAVAILABLE
        if not ok:
            continue
        if name == "kernel":
            result.uname = stdout.strip() or None
            result.kernel = parse_uname(stdout)
        elif name == "os":
            result.os, result.distro_id = parse_os_release(stdout)
        elif name == "packages":
            count, sample = parse_dpkg(stdout)
            result.package_count = count
            result.package_sample = sample
        elif name == "listening_ports":
            result.listening_ports = parse_ss(stdout)
        elif name == "docker":
            result.docker_containers = parse_docker_ps(stdout)
        elif name == "hostname":
            result.reported_hostname = stdout.strip() or None

    return result
