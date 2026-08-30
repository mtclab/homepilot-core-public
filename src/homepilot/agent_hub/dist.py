"""The agent payload HomePilot serves to guests itself (#464).

Both install paths used to send the GUEST to GitHub: the one-liner fetched
`install-agent.sh` from `releases/latest/download`, and the script then resolved
a release through `api.github.com` and pulled `hp-agent-linux-$GOARCH`. Two
consequences, both against ADR-004:

* **An isolated guest could not enrol at all.** Not hypothetical - the friend
  portal (#442) specifies an egress-limited guest VLAN, and plenty of homelab
  guests have no outbound route. "Installs automatically" failed exactly where
  the network is tightest.
* **Nothing was verified beyond TLS.** A script piped to bash and a binary run as
  root, with no checksum. #381 flagged the installer checksum; the UI enrolment
  path inherited the same hole.

HomePilot has both artifacts in its own image, so it serves them. The guest
fetches from the control plane it is being enrolled into - no internet, and the
agent an install ENROLS matches the hub that enrolled it.

That is enrolment only, and this file used to claim more: "the agent version
always matches the hub that manages it, which kills a class of version skew". It
does not. Nothing upgrades an already-enrolled agent, and nothing reports the
gap - dev ran a v3.6.6 agent against a 3.6.15 hub for weeks with every surface
green, so a fix that lived in the binary shipped and changed nothing on any
managed host. `system_info.agent_version` is recorded on every register and is
in the fleet list; comparing it to the hub is #648's tranche-1 follow-up.

The checksum is computed from the bytes on disk at request time rather than
baked at build: there is then no way for a recorded hash and a served file to
disagree, which is the only failure this check could otherwise introduce.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Where the image puts the payload (see the Dockerfile). Overridable so tests -
# and a source checkout, which has no built binaries - can point somewhere real.
DEFAULT_DIST_DIR = "/app/agent-dist"

INSTALLER_NAME = "install-agent.sh"

# uname -m -> the GOARCH we ship. Deliberately a fixed table: an unknown machine
# gets a clear refusal rather than a 404 on a guessed filename.
ARCH_ALIASES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


class DistUnavailableError(Exception):
    """The requested artifact is not in this image, with a reason to show."""


def dist_dir() -> Path:
    return Path(os.environ.get("HP_AGENT_DIST_DIR", DEFAULT_DIST_DIR))


def normalise_arch(arch: str) -> str:
    key = (arch or "").strip().lower()
    if key not in ARCH_ALIASES:
        raise DistUnavailableError(
            f"Unsupported architecture {arch!r}. HomePilot ships "
            f"{sorted(set(ARCH_ALIASES.values()))}."
        )
    return ARCH_ALIASES[key]


def _resolve(name: str) -> Path:
    # Names come from a fixed table or a validated arch, never straight from a
    # request, but resolve and re-check anyway: this function hands back a path
    # that gets read and served, and that is worth being boring about.
    root = dist_dir().resolve()
    path = (root / name).resolve()
    if root not in path.parents and path != root:
        raise DistUnavailableError(f"Refusing to serve {name!r} from outside the payload directory")
    if not path.is_file():
        raise DistUnavailableError(
            f"{name} is not in this image. The agent payload is built into the "
            "image (see the Dockerfile); a source checkout has none until it is built."
        )
    return path


def agent_binary(arch: str) -> Path:
    return _resolve(f"hp-agent-linux-{normalise_arch(arch)}")


def installer() -> Path:
    return _resolve(INSTALLER_NAME)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest() -> dict[str, dict[str, object]]:
    """What this image can serve, with sizes and digests.

    Reported even when an entry is missing, so an operator debugging an enrolment
    can see WHICH artifact the image lacks rather than inferring it from a failed
    download.
    """
    entries: dict[str, dict[str, object]] = {}
    for name in (INSTALLER_NAME, "hp-agent-linux-amd64", "hp-agent-linux-arm64"):
        try:
            path = _resolve(name)
        except DistUnavailableError as exc:
            entries[name] = {"available": False, "reason": str(exc)}
        else:
            entries[name] = {
                "available": True,
                "sha256": sha256(path),
                "size_bytes": path.stat().st_size,
            }
    return entries
