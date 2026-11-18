"""install-agent.sh must give every host a STABLE agent identity.

The 2.6.0 per-agent-credential feature binds the token the hub mints to the
agent_id that enrolled. The installer never wrote an id, so the agent generated a
fresh UUID on every start, presented its persisted token under an id the hub had
no credential for, failed auth, and was banned after MAX_AUTH_FAILURES — fleet
wide and permanent.

These tests execute the installer's real identity block (sliced verbatim between
its markers) in a sandbox, so they assert the SHIPPED script's behaviour, not a
copy of it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-agent.sh"
START = ">>> agent-identity block"
END = "<<< end agent-identity block"


def _between(start_marker: str, end_marker: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(start_marker)
    end = text.index(end_marker)
    return text[text.index("\n", start) + 1 : text.rindex("\n", start, end)]


def _identity_block() -> str:
    """The id resolution (run early in the installer, before the binary swap)
    followed by the identity + env block, both verbatim: together they are what
    decides and writes the host's agent id."""
    resolve = _between(">>> agent-id resolve block", "<<< end agent-id resolve block")
    block = _between(START, END)
    assert "resolve_agent_id" in resolve and "resolve_agent_id" in block, (
        "the identity block markers no longer wrap the id logic"
    )
    return resolve + "\n" + block


def _run(conf_dir: Path, tls_pin: str = "") -> dict[str, str]:
    """Run the installer's identity block against conf_dir and return agent.env."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "SUDO=''",
            f'CONF_DIR="{conf_dir}"',
            'HUB_HOST="hub.example"',
            "HUB_PORT=8443",
            'TOKEN="enrollment-token"',
            'USE_TLS="false"',
            f'TLS_PIN="{tls_pin}"',
            'PRIVILEGED="false"',
            # Grant settings the env block records alongside the identity (#422).
            'ALLOW_PACKAGE_INSTALL="false"',
            'WRITE_PREFIXES_ENV="/etc/homepilot"',
            'mkdir -p "$CONF_DIR"',
            _identity_block(),
        ]
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"identity block failed: {proc.stderr}"
    env = {}
    for line in (conf_dir / "agent.env").read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
    return env


class TestInstallerAgentIdentity:
    def test_fresh_install_writes_a_stable_id(self, tmp_path):
        """A fresh install pins HP_AGENT_ID (and the id file) in agent.env, so the
        agent is stable by construction instead of inventing an id per start.

        Revert-check: drop the HP_AGENT_ID/HP_AGENT_ID_FILE lines from the
        installer's agent.env heredoc and this fails."""
        env = _run(tmp_path)
        assert re.fullmatch(r"[0-9a-fA-F-]{16,}", env["HP_AGENT_ID"]), env["HP_AGENT_ID"]
        assert env["HP_AGENT_ID_FILE"] == f"{tmp_path}/agent.id"
        assert (tmp_path / "agent.id").read_text().strip() == env["HP_AGENT_ID"]
        assert (tmp_path / "agent.id").stat().st_mode & 0o777 == 0o600

    def test_reinstall_keeps_the_existing_id(self, tmp_path):
        """Re-running the installer must NOT change an existing agent's id —
        changing it would orphan the credential the hub issued to that id.

        Revert-check: make the installer always generate a new id and this
        fails."""
        first = _run(tmp_path)["HP_AGENT_ID"]
        second = _run(tmp_path)["HP_AGENT_ID"]
        assert second == first, "reinstall changed the agent id"

    def test_id_recovered_from_the_id_file_alone(self, tmp_path):
        """An agent.env lost/rewritten without HP_AGENT_ID still recovers the id
        from the id file the agent itself persists."""
        first = _run(tmp_path)["HP_AGENT_ID"]
        (tmp_path / "agent.env").unlink()
        assert _run(tmp_path)["HP_AGENT_ID"] == first

    def test_distinct_hosts_get_distinct_ids(self, tmp_path):
        """Two fresh hosts must not collide on one id."""
        a = _run(tmp_path / "host-a")["HP_AGENT_ID"]
        b = _run(tmp_path / "host-b")["HP_AGENT_ID"]
        assert a != b

    @pytest.mark.parametrize("key", ["HP_AGENT_TOKEN_FILE", "HP_AGENT_AUTH_TOKEN"])
    def test_existing_env_keys_preserved(self, tmp_path, key):
        """The identity change must not drop the connection settings."""
        assert _run(tmp_path)[key]


class TestInstallerHubPin:
    """The hub's certificate is self-signed, so the agent can only verify it
    against the fingerprint the installer was given. That fingerprint has to
    reach agent.env or the agent has nothing to verify against."""

    def test_pin_reaches_the_agent_environment(self, tmp_path):
        pin = "sha256:" + "ab" * 32
        assert _run(tmp_path, tls_pin=pin)["HP_AGENT_TLS_PIN"] == pin

    def test_no_pin_leaves_the_variable_empty(self, tmp_path):
        assert _run(tmp_path)["HP_AGENT_TLS_PIN"] == ""

    def test_installer_refuses_a_non_fingerprint_pin(self):
        """A pin is written verbatim into agent.env, so anything that is not a
        sha256 hex fingerprint is rejected before it gets there."""
        script = SCRIPT.read_text()
        proc = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            env={
                "PATH": "/usr/bin:/bin",
                "HUB": "hub.example:8443",
                "TOKEN": "t",
                "TLS_PIN": "not-a-fingerprint\nHP_AGENT_PRIVILEGED=true",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode != 0
        assert "--tls-pin must be a sha256 fingerprint" in proc.stderr

    @pytest.mark.parametrize("prefix", ["sha256:", ""])
    def test_installer_refuses_a_valid_pin_with_a_line_smuggled_after_it(
        self, tmp_path: Path, prefix: str
    ):
        """#842: the check matched line by line (grep), so 64 hex characters
        followed by a newline and VAR=... passed and the whole value was written
        into agent.env - a second variable line. Matched on the whole value now.

        Revert-check: put the grep check back and this test gets past the pin
        check (it then stops at the download, which nothing serves)."""
        shim = tmp_path / "shim"
        shim.mkdir()
        (shim / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
        (shim / "sudo").chmod(0o755)
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            env={
                "PATH": f"{shim}:/usr/bin:/bin",
                "HUB": "hub.example:8443",
                "TOKEN": "t",
                "TLS_PIN": prefix + "ab" * 32 + "\nHP_AGENT_PRIVILEGED=true",
                # Sandbox everything the script touches past the pin check.
                "CONF_DIR": str(tmp_path / "conf"),
                "INSTALL_DIR": str(tmp_path / "bin"),
                "SNAPSHOT_DIR": str(tmp_path / "snapshot"),
                "UNIT_PATH": str(tmp_path / "unit"),
                "HP_API": "http://127.0.0.1:9",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode != 0
        assert "--tls-pin must be a sha256 fingerprint" in proc.stderr, proc.stderr
        assert not (tmp_path / "conf" / "agent.env").exists()
