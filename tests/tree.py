"""Which tree the suite is running in: the private repository or its public mirror.

scripts/scrub-for-public.sh writes PROVENANCE into every export and removes the
agent overlay (docker-compose.agent.yml, .env.example.agent) on purpose; the
private tree never carries a PROVENANCE. Tests that read those overlay files
must therefore require them here and expect them gone in the mirror - never
skip quietly when a file is merely missing.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IS_PUBLIC_MIRROR = (REPO_ROOT / "PROVENANCE").is_file()

# Exactly what the scrub deletes (scripts/scrub-for-public.sh, "agent overlay").
MIRROR_DROPPED = frozenset({"docker-compose.agent.yml", ".env.example.agent"})


def shipped(*relative: str) -> tuple[Path, ...]:
    """The given repo-relative files that this tree is supposed to ship."""
    return tuple(
        REPO_ROOT / rel for rel in relative if not (IS_PUBLIC_MIRROR and rel in MIRROR_DROPPED)
    )
