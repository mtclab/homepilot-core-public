"""Parity gate between the two remaining command allowlists (#381).

The canonical allowlist is the Go agent's ``agent/go/allowlist.go``
``safeCommands`` (what a host agent will actually execute in read-only mode).
The Python adapter (``src/homepilot/adapters/agent.py``) keeps a second copy used
by ``exec_readonly``. The two had drifted (the Python ``systemctl`` permitted
only ``status`` where the agent allows ``status|is-active|is-enabled|
daemon-reload``; the Python ``cat`` prefix set omitted ``/etc/systemd/`` and
``/opt/``).

This test parses the Go safe set and asserts the Python read-only allowlist is
consistent with it:

* every command the Python adapter allows read-only is present in the Go
  ``safeCommands`` (no Python-only command that the agent would reject);
* the Python ``cat`` prefix set matches the Go one;
* the Python ``systemctl`` read-only subcommands are a subset of (and match) the
  Go safe ``systemctl`` subcommands.

Teeth: re-add a command to the Python adapter that is absent from the Go safe
set, or re-narrow the reconciled ``systemctl`` / ``cat`` sets, and this fails.
"""

from __future__ import annotations

import re
from pathlib import Path

from homepilot.adapters.agent import (
    _CAT_ALLOWED_PREFIXES,
    _SAFE_READONLY_COMMANDS,
    AgentAdapter,
)

_GO_ALLOWLIST = Path(__file__).resolve().parent.parent / "agent" / "go" / "allowlist.go"

# Commands the Go agent allows read-only that the Python adapter deliberately
# does NOT expose via exec_readonly. `docker` (ps/images/inspect/logs/…) is host
# fleet-management, kept out of the adapter's read-only command path on purpose.
_INTENTIONAL_NARROWING = {"docker"}


def _go_text() -> str:
    return _GO_ALLOWLIST.read_text(encoding="utf-8")


def _go_safe_block() -> str:
    m = re.search(
        r"var safeCommands = map\[string\]\*regexp\.Regexp\{(.*?)\n\}",
        _go_text(),
        re.DOTALL,
    )
    assert m, "could not locate safeCommands map in allowlist.go"
    return m.group(1)


def _go_safe_keys() -> set[str]:
    return set(re.findall(r'"([\w-]+)":', _go_safe_block()))


def _go_cat_prefixes() -> set[str]:
    m = re.search(
        r"var catAllowedPrefixes = \[\]string\{(.*?)\}",
        _go_text(),
        re.DOTALL,
    )
    assert m, "could not locate catAllowedPrefixes in allowlist.go"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _go_systemctl_subcommands() -> set[str]:
    # The first `(...)` group of the safe systemctl pattern is the subcommand
    # alternation, e.g. (status|is-active|is-enabled|daemon-reload).
    m = re.search(r"\^systemctl\\s\+\(([a-z|\-]+)\)", _go_safe_block())
    assert m, "could not parse safe systemctl subcommands from allowlist.go"
    return set(m.group(1).split("|"))


def _py_systemctl_subcommands() -> set[str]:
    pattern = _SAFE_READONLY_COMMANDS["systemctl"].pattern
    m = re.search(r"\(([a-z|\-]+)\)", pattern)
    assert m, "could not parse systemctl subcommands from the Python adapter"
    return set(m.group(1).split("|"))


def test_python_readonly_commands_are_subset_of_go_safe() -> None:
    py_keys = set(_SAFE_READONLY_COMMANDS)
    go_keys = _go_safe_keys()

    extra = py_keys - go_keys
    assert not extra, (
        "Python adapter permits read-only commands absent from the Go safe "
        f"allowlist (the agent would reject them): {sorted(extra)}"
    )
    # Lock the intentional narrowing so a new Go safe command forces a conscious
    # decision here rather than silently drifting.
    assert go_keys - py_keys == _INTENTIONAL_NARROWING, (
        "Go/Python read-only allowlists drifted; reconcile the adapter or update "
        f"_INTENTIONAL_NARROWING. Go-only commands: {sorted(go_keys - py_keys)}"
    )


def test_cat_prefixes_match_go() -> None:
    py = set(_CAT_ALLOWED_PREFIXES)
    go = _go_cat_prefixes()
    assert py <= go, f"Python cat prefixes permit paths the agent rejects: {sorted(py - go)}"
    assert py == go, (
        "Python cat prefixes do not match the Go safe set (the adapter would "
        f"reject a read path the agent allows): missing {sorted(go - py)}"
    )


def test_systemctl_subcommands_match_go() -> None:
    py = _py_systemctl_subcommands()
    go = _go_systemctl_subcommands()
    assert py <= go, f"Python systemctl subcommands the agent rejects: {sorted(py - go)}"
    assert py == go, (
        "Python systemctl subcommands do not match the Go safe set (the adapter "
        f"would reject a subcommand the agent allows): missing {sorted(go - py)}"
    )


def test_reconciled_readonly_commands_validate() -> None:
    """Behavioral proof the adapter now accepts the reconciled read-only forms
    the Go agent allows, and still rejects a command outside the safe set."""
    allowed = [
        "systemctl status nginx",
        "systemctl is-active nginx",
        "systemctl is-enabled nginx.service",
        "systemctl daemon-reload",
        "cat /etc/systemd/system/foo.service",
        "cat /opt/homepilot/bootstrap.sh",
    ]
    for cmd in allowed:
        assert AgentAdapter._validate_readonly_command(cmd), f"should allow: {cmd}"

    rejected = [
        "rm -rf /tmp/x",  # not in any allowlist
        "systemctl restart nginx",  # a state change, not a safe subcommand
        "cat /root/.ssh/id_rsa",  # path outside the allowed prefixes
    ]
    for cmd in rejected:
        assert not AgentAdapter._validate_readonly_command(cmd), f"should reject: {cmd}"
