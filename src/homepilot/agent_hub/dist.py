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
import shlex
from pathlib import Path
from typing import Any

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


_installer_digest: tuple[tuple[str, int, int], str] | None = None


def installer_sha256() -> str | None:
    """The sha256 of the installer this control plane serves, or None without one.

    The one-liner and the zero-touch install carry it and refuse to run a
    downloaded installer that does not match (#843): the installer is fetched
    over plain http when the hub runs without TLS and runs as root, and until
    this nothing checked it (only the binary it fetches was checked). Cached by
    path, size and mtime, so a mint does not re-read the file every time.
    """
    global _installer_digest
    try:
        path = installer()
    except DistUnavailableError:
        return None
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if _installer_digest is None or _installer_digest[0] != key:
        _installer_digest = (key, sha256(path))
    return _installer_digest[1]


# Said when the downloaded installer is not the one the control plane vouched for.
INSTALLER_MISMATCH_MESSAGE = (
    "the downloaded install-agent.sh does not match the sha256 HomePilot gave for it - "
    "refusing to run it (something changed it on the way)"
)


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


# ── The by-hand install one-liner (lane A3, review 05/1) ─────────────────────
#
# The Hosts page used to render a one-liner that fetched the installer from
# GitHub `releases/latest`, and the installer then pulled the binary from there
# too: `--hp-api` existed but the UI never passed it. So a host installed by
# hand ran "whatever GitHub's latest release is", not the version of the hub
# that enrolled it - the two matched only by accident of release timing. The
# command is built HERE, on the server that knows whether it can serve the
# payload, and the UI shows it verbatim.

# The public mirror the fallback names. Used ONLY when this control plane cannot
# serve the payload itself (a source checkout with no built binaries), and then
# pinned to THIS control plane's release, never `latest`.
PUBLIC_RELEASES = "https://github.com/mtclab/homepilot-core-public/releases"


def api_base_for(hub_host: str) -> str:
    """The HomePilot API address a host being enrolled reaches the control plane
    on: the advertised hub host on the API port.

    One answer for both install paths (the zero-touch install stages the same
    value), so a by-hand install and an automatic one fetch from one place.
    """
    from ..config import get_settings

    host = hub_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # a bare IPv6 literal needs brackets inside a URL
    return f"http://{host}:{get_settings().daemon_port}"


def control_plane_can_serve() -> tuple[bool, str]:
    """Whether a host can install entirely from this control plane: the
    installer plus at least one agent binary. Returns ``(ok, why_not)``.

    Presence only: this runs inside a request on the event loop, and hashing
    multi-megabyte binaries (``manifest()``) to answer "are they here" froze it
    for no reason."""
    try:
        _resolve(INSTALLER_NAME)
    except DistUnavailableError as exc:
        return False, str(exc)
    for arch in sorted(set(ARCH_ALIASES.values())):
        try:
            _resolve(f"hp-agent-linux-{arch}")
        except DistUnavailableError:
            continue
        return True, ""
    return False, "no hp-agent binary is in this image"


# Printed when the one-liner fails, after whatever curl or the installer said.
INSTALL_FAILED_MESSAGE = "HomePilot agent install FAILED - see the error above"


def install_command(
    *,
    hub_host: str,
    hub_port: int,
    token: str,
    tls: bool,
    pin: str,
    control_version: str,
) -> dict[str, Any]:
    """The one-liner an operator pastes on a host, and where it installs from.

    ``source`` is ``"control_plane"`` (the normal case: installer and binary
    both come from this HomePilot, so the agent IS this hub's version and the
    host needs no internet) or ``"github"`` (the documented fallback, with
    ``fallback_reason`` saying why, pinned to ``control_version``).

    Every value is shell-quoted: the shared token is operator-settable and this
    string is pasted into a root shell.
    """
    hub = f"{hub_host}:{hub_port}"
    flags = ["--hub", hub, "--token", token]
    if tls:
        flags += ["--tls-pin", f"sha256:{pin}"] if pin else ["--tls"]
    ok, why_not = control_plane_can_serve()
    if ok:
        api = api_base_for(hub_host)
        fetch = [
            "curl",
            "-fsSL",
            "-H",
            f"x-hp-agent-token: {token}",
            f"{api}/agents/dist/{INSTALLER_NAME}",
        ]
        run = ["--hp-api", api, *flags]
        source, reason, version_note = "control_plane", None, control_version
        digest = installer_sha256()
    else:
        tag = f"v{control_version.lstrip('v')}"
        fetch = ["curl", "-fsSL", f"{PUBLIC_RELEASES}/download/{tag}/{INSTALLER_NAME}"]
        run = ["--version", tag, *flags]
        source, reason, version_note = "github", why_not, tag
        # Fetched over https from the public mirror, whose TLS vouches for it;
        # this control plane has no copy whose digest it could state.
        digest = None
    # Downloaded to a file and run from it, never `curl ... | bash`: in a pipe
    # the shell reports bash's status, and bash given nothing (curl refused,
    # 401) exits 0 - the 3.7.0-rc2 dev walk printed one curl error, returned
    # success and left the old agent in place. Each step is chained with `&&`,
    # NOT `set -e`: bash ignores `set -e` inside a subshell that is the left
    # side of `||`, so a refused fetch would run the empty file and "succeed"
    # all the same. A failed step ends the subshell with its own status, the
    # line after `||` says so in words, and a root shell the operator pasted
    # into stays open. The token is in curl's and the installer's argv exactly
    # as before.
    #
    # The installer's sha256 is checked BEFORE bash runs it (#843): a file that
    # does not match is refused with a plain sentence and nothing runs.
    verify = ""
    if digest:
        verify = (
            f"{{ printf '%s  %s\\n' {digest} \"$f\" | sha256sum -c - >/dev/null 2>&1 "
            f"|| {{ echo {shlex.quote(INSTALLER_MISMATCH_MESSAGE)} >&2; false; }}; }} && "
        )
    command = (
        f'( f="$(mktemp)" && trap \'rm -f "$f"\' EXIT && '
        f'{shlex.join(fetch)} -o "$f" && {verify}bash "$f" {shlex.join(run)} ) '
        f"|| {{ echo {shlex.quote(INSTALL_FAILED_MESSAGE)} >&2; false; }}"
    )
    return {
        "install_command": command,
        "install_source": source,
        "install_version": version_note,
        "install_fallback_reason": reason,
        # The digest the command checks the installer against (None: no check,
        # the GitHub fallback). Shown so an operator can compare it by hand.
        "installer_sha256": digest,
    }
