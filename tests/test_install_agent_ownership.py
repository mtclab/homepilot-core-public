"""The agent installer must never seize ownership of a tree it did not create.

FOUND LIVE ON PROD (2026-08-23, first boot after 3.0.0): install-agent.sh did
`chown -R hp-agent:hp-agent` over every managed write prefix - including
/opt/homepilot, which on the control-plane box is the BACKEND's deployment.
Re-running the installer handed the backend's database, vault, artifacts and
.env to hp-agent (uid 997) while the backend container runs as 999, and the
backend crash-looped on a PermissionError before serving a byte.

These gates parse the shipped script's prefix-setup block. A sandboxed
execution of the block is not feasible in this suite (the `case` paths and
`[ -e ]` are against absolute system paths), so the gates pin the two
properties whose loss recreates the incident - teeth checked by reverting the
fix: both fail on the old block.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-agent.sh"


def _prefix_block() -> str:
    text = SCRIPT.read_text()
    # The SETUP loop (the one with the case over managed paths) - not the
    # earlier validation loop over the same variable.
    blocks = re.findall(r"for p in \$WRITE_PREFIXES; do.*?\ndone", text, re.DOTALL)
    setup = [b for b in blocks if "/etc/homepilot|/opt/homepilot|/tmp/homepilot" in b]
    assert len(setup) == 1, (
        f"expected exactly one prefix SETUP loop, found {len(setup)} - update this gate"
    )
    return setup[0]


def test_the_script_still_parses() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_no_recursive_chown_inside_the_prefix_loop() -> None:
    block = _prefix_block()
    assert "chown -R" not in block, (
        "the prefix loop chowns recursively again - on a control-plane box that "
        "seizes the backend's database, vault and .env for hp-agent (prod outage "
        "2026-08-23)"
    )


def test_ownership_is_taken_only_of_directories_the_installer_creates() -> None:
    block = _prefix_block()
    assert re.search(r"if \[ ! -e \"\$p\" \]", block), (
        "the existence guard is gone: the installer will mkdir/chmod/chown "
        "prefixes that already exist and belong to someone else"
    )
    # And the chown must sit INSIDE that guard, not after it.
    guarded = re.search(r"if \[ ! -e \"\$p\" \];.*?fi", block, re.DOTALL)
    assert guarded and "chown" in guarded.group(0), "chown moved outside the created-by-us guard"
    after_guard = block[block.index(guarded.group(0)) + len(guarded.group(0)) :]
    assert "chown" not in after_guard, "an unguarded chown remains in the prefix loop"
