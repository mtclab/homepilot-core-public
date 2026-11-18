"""Say plainly when the box is too busy for the gate's timing checks (#810).

The loop-block detector (tests/loop_block_detector.py) holds every event-loop
callback to a wall-clock budget. On a box shared with other heavy jobs the OS
can leave the loop thread unscheduled for longer than that budget; the detector
now names such a window "INCONCLUSIVE: starved" instead of failing on it, but a
loaded box still means fewer windows are judged. This runs first in `make gate`
and WARNS - it never refuses and never changes a budget - so whoever reads the
gate's output knows the timing evidence came from a loaded machine.

Exit status is always 0. Thresholds: HP_GATE_PSI_WARN (CPU PSI `some` avg10,
percent, default 20) and HP_GATE_LOAD_WARN (1-minute load per core, default 0.7).
"""

from __future__ import annotations

import os
import sys

PSI_WARN = float(os.environ.get("HP_GATE_PSI_WARN", "20"))
LOAD_WARN = float(os.environ.get("HP_GATE_LOAD_WARN", "0.7"))


def read_psi_avg10(path: str = "/proc/pressure/cpu") -> float | None:
    try:
        with open(path, encoding="ascii") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == "some":
                    fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
                    return float(fields["avg10"])
    except (OSError, KeyError, ValueError):
        return None
    return None


def verdict(
    psi_avg10: float | None,
    load1: float | None,
    cores: int,
    psi_warn: float = PSI_WARN,
    load_warn: float = LOAD_WARN,
) -> str | None:
    """The warning to print, or None when the box is quiet enough."""
    reasons = []
    if psi_avg10 is not None and psi_avg10 > psi_warn:
        reasons.append(f"CPU pressure {psi_avg10:.1f}% (PSI some avg10; warn above {psi_warn:g}%)")
    per_core = load1 / max(cores, 1) if load1 is not None else None
    if per_core is not None and per_core > load_warn:
        reasons.append(
            f"load {load1:.1f} on {cores} cores = {per_core:.2f} per core "
            f"(warn above {load_warn:g})"
        )
    if not reasons:
        return None
    return (
        "gate: WARNING - this box is loaded: "
        + "; ".join(reasons)
        + ". The loop-block detector's timing evidence is weaker here: windows where the "
        "loop thread waited for a CPU are reported as INCONCLUSIVE: starved, not judged. "
        "Prefer a quiet box for a release gate."
    )


def main() -> int:
    try:
        load1: float | None = os.getloadavg()[0]
    except OSError:
        load1 = None
    message = verdict(read_psi_avg10(), load1, os.cpu_count() or 1)
    if message:
        print(message, file=sys.stderr)
    else:
        print("gate: box load is fine for the timing checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
