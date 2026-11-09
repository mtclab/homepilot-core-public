"""Refuse to gate on a venv that does not match uv.lock (3.10.0).

The 3.9.0 release gate passed in a venv that had drifted from the lock to newer
fastapi / pydantic / starlette. On the locked versions, the ones the image
installs, the first request after every start blocked the event loop ~300 ms,
and nothing in `make gate` could see it: the gate had tested other code than
the code that ships. So `make gate` starts here.

The check is uv's own: `uv sync --locked --all-extras --check --offline` against
the venv `make` was handed. `--locked` also fails a uv.lock that no longer
matches pyproject.toml; `--check` changes nothing; `--offline` needs no network
(the lock and the venv are both on disk). Every installed distribution must be
exactly the locked one - a different version, a missing one, and an extra one
all fail.

One difference is tolerated: the project itself (`homepilot`). A worktree lane
may borrow another checkout's venv, whose editable install points at that
checkout; that is a question of which SOURCE is tested, which gate-py answers
with PYTHONPATH and scripts/check_gate_source.py (#653). Dependencies are never
tolerated.

Usage: python scripts/check_venv_lock.py <venv-bin-dir>
Environment: UV (the uv executable; default: `uv` on PATH).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = "Rebuild it from the lock:  uv sync --frozen --all-extras"
# "Would install/uninstall" lines of `uv sync --check`, e.g. " - fastapi==0.162.2"
# or " + homepilot==3.10.0 (from file:///...)".
_CHANGE = re.compile(r"^\s*([-+~])\s+([A-Za-z0-9][A-Za-z0-9._-]*)(?:==|\s|$)")


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def drifted(output: str, project: str) -> tuple[list[str], list[str]]:
    """(dependency changes, project-only changes) named by `uv sync --check`."""
    deps: list[str] = []
    own: list[str] = []
    for line in output.splitlines():
        m = _CHANGE.match(line)
        if not m:
            continue
        (own if _canon(m.group(2)) == _canon(project) else deps).append(line.strip())
    return deps, own


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_venv_lock.py <venv-bin-dir>", file=sys.stderr)
        return 2
    venv = Path(argv[1]).resolve().parent
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        print(
            "gate-lock: FAIL - `uv` is not installed, so the venv cannot be checked "
            f"against uv.lock. Install uv (https://docs.astral.sh/uv/), then: {FIX}",
            file=sys.stderr,
        )
        return 1
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "name"
    ]
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}
    proc = subprocess.run(
        [uv, "sync", "--locked", "--all-extras", "--check", "--offline"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 0:
        print(f"gate-lock: {venv} matches uv.lock exactly")
        return 0
    deps, own = drifted(output, project)
    if deps:
        print(
            f"gate-lock: FAIL - {venv} does not match uv.lock; the gate would test other "
            "dependencies than the image ships:",
            file=sys.stderr,
        )
        for line in deps:
            print(f"    {line}", file=sys.stderr)
        print(f"  {FIX}", file=sys.stderr)
        return 1
    if own:
        print(
            f"gate-lock: {venv} matches uv.lock; only `{project}` itself differs "
            f"({'; '.join(own)}) - gate-py checks which source is tested"
        )
        return 0
    print(
        "gate-lock: FAIL - `uv sync --locked --check` refused, without naming a package:\n"
        + output.rstrip()
        + f"\n  If uv.lock is out of date with pyproject.toml, run `uv lock`. {FIX}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
