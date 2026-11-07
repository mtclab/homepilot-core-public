"""Prove the Python gate is testing THIS tree's source, not another checkout's (#653).

`homepilot` is installed into the shared `.venv` as an editable install, and
the `.pth` file that install writes points at whichever checkout ran
`pip install -e .`. A worktree lane that borrows that venv therefore imports
the OTHER checkout's `src/` - and a full suite run reports green over code that
does not contain a single one of the lane's changes. It was only noticed
because a new test imported a symbol that did not exist on main.

`make gate-py` puts this tree's `src/` first on the path and then runs this
check, so a future change that quietly undoes the path fix turns a silent wrong
answer into a loud failure instead.

Usage: python scripts/check_gate_source.py <path-to-this-tree's-src>
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_gate_source.py <src-dir>", file=sys.stderr)
        return 2
    src = Path(argv[1]).resolve()
    import homepilot

    imported = Path(homepilot.__file__).resolve()
    if src not in imported.parents:
        print(
            f"gate-py: `homepilot` is imported from {imported},\n"
            f"         not from this tree's {src}.\n"
            "         The gate would be testing another checkout's source (#653).\n"
            "         Run it through `make gate-py` (which sets PYTHONPATH), or put\n"
            "         this tree's src/ first on PYTHONPATH.",
            file=sys.stderr,
        )
        return 1
    print(f"gate-py: testing the source at {imported.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
