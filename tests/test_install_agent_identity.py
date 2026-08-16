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


def _identity_block() -> str:
    text = SCRIPT.read_text()
    start = text.index(START)
    end = text.index(END)
    block = text[text.index("\n", start) + 1 : text.rindex("\n", start, end)]
    assert "AGENT_ID" in block, "identity block markers no longer wrap the id logic"
    return block


def _run(conf_dir: Path) -> dict[str, str]:
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
            'PRIVILEGED="false"',
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
