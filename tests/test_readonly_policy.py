"""The READ tier's exec and file-read gates, held to one shared policy (#682, #691).

Found in the 2026-09-24 audit, on both layers: ``exec_on_guest_readonly`` accepted
``ip link set eth0 down``, ``ip route del default``, ``ip addr flush``, ``ss -K``,
``systemctl daemon-reload`` and ``cat /etc/homepilot/agent.env`` (the fleet
enrolment token), and ``read_file_on_guest`` served ``/etc/ssh/ssh_host_*_key``,
``/etc/pve/priv/*``, ``/etc/wireguard/*``, ceph keyrings, ``~/.docker`` and
``~/.kube`` credentials and ``/opt/homepilot/.env``. Both tools are READ tier.

The corpus in ``agent/go/testdata/readonly_policy_corpus.json`` is run here
against the control plane's gates AND by ``agent/go/readonly_policy_test.go``
against the agent, so the two layers cannot disagree about a verb or a path the
corpus names. Add every new finding to the corpus.

Teeth: revert ``AgentAdapter._validate_readonly_command`` or
``_check_guest_read_path`` to the old regex/prefix tables and the corpus tests
fail on the mutating verbs and secret paths above. Edit either copy of
``readonly_policy.json`` alone and the byte-identity test fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from homepilot.adapters import readonly_policy
from homepilot.adapters.agent import AgentAdapter
from homepilot.mcp.tools.system_tools import _check_guest_read_path

_ROOT = Path(__file__).resolve().parent.parent
_GO_POLICY = _ROOT / "agent" / "go" / "readonly_policy.json"
_PY_POLICY = _ROOT / "src" / "homepilot" / "adapters" / "readonly_policy.json"
_CORPUS = json.loads(
    (_ROOT / "agent" / "go" / "testdata" / "readonly_policy_corpus.json").read_text("utf-8")
)
_COMMANDS = _CORPUS["commands"]
_PATHS = _CORPUS["read_paths"]


def test_one_policy_file_byte_identical_on_both_layers() -> None:
    assert _GO_POLICY.read_bytes() == _PY_POLICY.read_bytes(), (
        "the agent's and the control plane's read-only policy differ. Edit "
        "agent/go/readonly_policy.json and copy it verbatim to "
        "src/homepilot/adapters/readonly_policy.json"
    )
    # And the package loads the copy it ships, not some other file.
    assert readonly_policy.policy_text().encode("utf-8") == _PY_POLICY.read_bytes()


@pytest.mark.parametrize("command", _COMMANDS["allow"])
def test_read_tier_runs_the_read_commands(command: str) -> None:
    assert AgentAdapter._validate_readonly_command(command), command


@pytest.mark.parametrize(
    "command",
    _COMMANDS["refuse_everywhere"] + _COMMANDS["refuse_read_tier"] + _COMMANDS["agent_only"],
)
def test_read_tier_refuses_mutating_and_secret_commands(command: str) -> None:
    assert not AgentAdapter._validate_readonly_command(command), (
        f"the READ tier accepts {command!r}"
    )


@pytest.mark.parametrize("path", _PATHS["allow"])
def test_read_file_serves_the_narrow_allowlist(path: str) -> None:
    _check_guest_read_path(path)


@pytest.mark.parametrize("path", _PATHS["refuse"])
def test_read_file_refuses_secrets_and_everything_unlisted(path: str) -> None:
    with pytest.raises(ValueError):
        _check_guest_read_path(path)


@pytest.mark.parametrize(
    "path",
    ["/etc/ssh/ssh_host_ed25519_key", "/etc/pve/priv/token.cfg", "/etc/homepilot/agent.env"],
)
def test_cat_goes_through_the_same_path_check_as_read_file(path: str) -> None:
    """``cat`` is not a side door: the path a read_file refuses, cat refuses."""
    with pytest.raises(ValueError):
        _check_guest_read_path(path)
    assert not AgentAdapter._validate_readonly_command(f"cat {path}")


def test_a_denied_path_stays_denied_under_an_allowed_prefix() -> None:
    """Deny wins over allow: these sit under prefixes the read tier serves."""
    for path in (
        "/opt/homepilot/.env",
        "/etc/homepilot/agent.token",
        "/etc/nginx/.htpasswd",
        "/etc/docker/key.json",
        "/opt/homepilot/data/vault/master.protected",
    ):
        with pytest.raises(ValueError, match="denied"):
            _check_guest_read_path(path)


def test_the_read_tier_command_set_is_a_conscious_list() -> None:
    """A command joins the READ tier by an edit someone has to look at. docker
    stays agent-only: ``docker inspect`` prints container environments."""
    assert readonly_policy.read_tier_commands() == {
        "hostname",
        "uptime",
        "uname",
        "free",
        "df",
        "ls",
        "ps",
        "ip",
        "ss",
        "systemctl",
        "journalctl",
        "dpkg",
        "cat",
    }


def test_the_readonly_exec_description_names_every_read_tier_command() -> None:
    """The tool description is what an assistant reads to decide what to ask
    for; it once omitted `cat`, so a permitted read looked forbidden (#690).
    Held to the shared policy, so a command added there must be named here."""
    import re

    from homepilot.mcp.tools.system_tools import TOOL_DEFINITIONS

    description = next(
        t["description"] for t in TOOL_DEFINITIONS if t["name"] == "exec_on_guest_readonly"
    )
    missing = sorted(
        c for c in readonly_policy.read_tier_commands() if not re.search(rf"\b{c}\b", description)
    )
    assert not missing, f"exec_on_guest_readonly description omits: {missing}"
