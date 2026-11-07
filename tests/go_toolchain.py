"""The one place the suite decides whether it has a Go toolchain (#726).

Fourteen journeys drive the REAL hp-agent binary, and each used to resolve Go on
its own (`HP_GO_BIN` or `go` on PATH) and SKIP when neither answered. `make gate`
documented `GO_BIN=...` but never handed it to pytest, so on a box whose Go lives
outside PATH the gate printed "3072 passed, 49 skipped" with every shipped-binary
journey among the skips - green, and silent about the part that matters most.

Now:
  * every Go-dependent test resolves the toolchain HERE (a gate test forbids a
    module resolving it on its own, which would bypass this);
  * under `make gate` (HP_GATE=1) a missing toolchain is a FAILURE before any
    test runs, unless the caller opts out by name with HP_GATE_ALLOW_NO_GO=1 -
    and then the run says, loudly, which journeys it did not exercise.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

GO_SKIP_REASON = "Go toolchain not available (set HP_GO_BIN)"

TESTS_DIR = Path(__file__).resolve().parent


def resolve_go(
    environ: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    return environ.get("HP_GO_BIN") or which("go") or ""


GO = resolve_go()


def go_dependent_modules() -> list[str]:
    """Test modules that need the toolchain, found by what they use - so the
    list a skip names cannot drift from what actually skips."""
    names = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if DEPENDENT_IMPORT in text or "hp_agent_binary" in text:
            names.append(path.name)
    return names


# The exact import a Go-dependent module uses; matched literally above.
DEPENDENT_IMPORT = "from .go_toolchain import GO as _GO"


class GoToolchainRequiredError(Exception):
    """`make gate` would have skipped the real-binary journeys."""


def check_gate_go(environ: Mapping[str, str], go: str) -> str:
    """Raise when a gate run has no Go toolchain and did not opt out.

    Returns a warning to print when the run continues WITHOUT Go (the explicit
    opt-out), or "" when there is nothing to say.
    """
    if go:
        if environ.get("HP_GATE") == "1" and not (os.path.isfile(go) and os.access(go, os.X_OK)):
            raise GoToolchainRequiredError(
                f"HP_GO_BIN={go!r} is not an executable file - the real-binary "
                "journeys cannot build the agent. Point GO_BIN at a Go toolchain."
            )
        return ""
    if environ.get("HP_GATE") != "1":
        return ""
    listing = "\n".join(f"  - {m}" for m in go_dependent_modules())
    if environ.get("HP_GATE_ALLOW_NO_GO") == "1":
        return (
            "WARNING: HP_GATE_ALLOW_NO_GO=1 - this gate run has NO Go toolchain, so the\n"
            "real hp-agent binary journeys in these modules are SKIPPED, not passed:\n"
            f"{listing}"
        )
    raise GoToolchainRequiredError(
        "make gate found no Go toolchain (HP_GO_BIN unset/empty and no `go` on PATH).\n"
        "These modules drive the REAL hp-agent binary and would silently skip:\n"
        f"{listing}\n"
        "Run `make gate GO_BIN=/path/to/go`, or set HP_GATE_ALLOW_NO_GO=1 to skip\n"
        "them knowingly (the run will then say what it did not test)."
    )
