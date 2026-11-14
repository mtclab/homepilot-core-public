"""Remote mode for `hp`: talk to a HomePilot server from any machine.

On the control-plane host `hp` works as it always has - it reaches the backend
on loopback with this box's own admin credential, and some commands open the
data directory directly. From an operator workstation none of that exists, so
the CLI also knows *remote targets*: a server URL and a token, kept in
``~/.config/homepilot/client.toml`` (XDG) as named profiles.

Which target a run uses, first match wins:

1. ``--url`` / ``HP_URL`` - a one-off target for scripts. The token comes from
   ``HP_TOKEN``, or from the selected profile's token file.
2. ``--profile`` / ``HP_PROFILE`` - a named profile.
3. ``default_profile`` in client.toml (``hp login`` sets it for the first
   profile it writes).
4. Nothing: local mode, exactly the behaviour `hp` had before remote mode.

The token is a credential to the whole estate, so it is handled as one:

* it is never accepted in argv (``hp login`` reads stdin or a no-echo prompt),
  so it never lands in shell history or a process listing;
* the token file is created 0600 inside a 0700 directory, and a token file
  other users can read is refused with a plain message rather than used;
* ``Target.__repr__`` leaves it out, and ``redact`` scrubs it from any text the
  CLI prints, so a traceback or an echoed request cannot carry it;
* TLS is verified by default; a self-signed server is trusted by naming its CA
  in the profile (``ca_file``), never by turning verification off.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import ssl
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from homepilot import __version__
from homepilot.common import write_private

# The public mirror is where releases are cut and tagged (vX.Y.Z); a tag is
# installable as-is with `uv tool install git+<mirror>@vX.Y.Z` or pipx. No
# wheel is attached to releases today, which is why the git form is used.
RELEASE_SOURCE = "https://github.com/mtclab/homepilot-core-public"

CONFIG_NAME = "client.toml"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RemoteConfigError(Exception):
    """A client configuration problem the operator must fix. ``exit_code`` is
    2 for a usage mistake (an unknown profile) and 1 for a refusal (an unsafe
    token file)."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ── Paths ────────────────────────────────────────────────────────────────────


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "homepilot"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "homepilot"


def config_path() -> Path:
    return config_dir() / CONFIG_NAME


def _ensure_private_dir(path: Path) -> None:
    """Create ``path`` 0700, and tighten it if it already exists looser."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)


# ── Profiles ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Profile:
    name: str
    url: str
    token_file: str
    ca_file: str = ""
    allow_http: bool = False


@dataclass(frozen=True)
class Target:
    """Where this run's API calls go, and with what. The token is kept out of
    ``repr`` so no debug print or traceback frame can show it."""

    url: str
    token: str = field(repr=False)
    profile: str = ""
    ca_file: str = ""

    def base(self) -> str:
        return self.url.rstrip("/")


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RemoteConfigError(f"Cannot read {path}: {exc}") from None


def profiles(config: dict[str, Any] | None = None) -> dict[str, Profile]:
    config = load_config() if config is None else config
    out: dict[str, Profile] = {}
    for name, raw in (config.get("profiles") or {}).items():
        if not isinstance(raw, dict):
            continue
        out[name] = Profile(
            name=name,
            url=str(raw.get("url", "")),
            token_file=str(raw.get("token_file", "")),
            ca_file=str(raw.get("ca_file", "")),
            allow_http=bool(raw.get("allow_http", False)),
        )
    return out


def _toml_str(value: str) -> str:
    # A TOML basic string accepts the JSON escapes json.dumps emits
    # (\" \\ \n \t \uXXXX), so this is a correct quoting, not an approximation.
    return json.dumps(value)


def render_config(default_profile: str, all_profiles: dict[str, Profile]) -> str:
    lines = [
        "# HomePilot CLI client configuration (written by `hp login`).",
        "# Tokens are not stored here: each profile names a 0600 token file.",
    ]
    if default_profile:
        lines.append(f"default_profile = {_toml_str(default_profile)}")
    for name in sorted(all_profiles):
        p = all_profiles[name]
        lines += ["", f"[profiles.{name}]", f"url = {_toml_str(p.url)}"]
        lines.append(f"token_file = {_toml_str(p.token_file)}")
        if p.ca_file:
            lines.append(f"ca_file = {_toml_str(p.ca_file)}")
        if p.allow_http:
            lines.append("allow_http = true")
    return "\n".join(lines) + "\n"


def default_token_file(profile: str) -> Path:
    return config_dir() / "tokens" / f"{profile}.token"


def save_login(
    profile: str,
    url: str,
    token: str,
    *,
    ca_file: str = "",
    token_file: str = "",
    allow_http: bool = False,
    make_default: bool = False,
) -> tuple[Path, Path, bool]:
    """Write the token file (0600) and the profile. Returns
    ``(config path, token file, became default)``."""
    check_profile_name(profile)
    config = load_config()
    existing = profiles(config)
    _ensure_private_dir(config_dir())
    token_path = Path(token_file).expanduser() if token_file else default_token_file(profile)
    _ensure_private_dir(token_path.parent)
    write_private(token_path, token.strip() + "\n")
    existing[profile] = Profile(
        name=profile,
        url=url.rstrip("/"),
        token_file=str(token_path),
        ca_file=str(Path(ca_file).expanduser()) if ca_file else "",
        allow_http=allow_http,
    )
    default = str(config.get("default_profile") or "")
    became_default = make_default or not default or default not in existing
    if became_default:
        default = profile
    write_private(config_path(), render_config(default, existing))
    return config_path(), token_path, became_default


def check_profile_name(name: str) -> None:
    if not _PROFILE_NAME.match(name):
        raise RemoteConfigError(
            f"Profile names are letters, digits, '-' and '_' (got {name!r}).", exit_code=2
        )


def read_token_file(path: str) -> str:
    """The token in ``path``, refusing a file other users can read or write."""
    p = Path(path).expanduser()
    try:
        st = p.stat()
    except OSError as exc:
        raise RemoteConfigError(
            f"Cannot read the token file {p}: {exc.strerror}. Run `hp login` again."
        ) from None
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise RemoteConfigError(
            f"Refusing the token file {p}: other users can read or change it "
            f"(mode {mode:04o}). Run `chmod 600 {p}` and try again."
        )
    token = p.read_text(encoding="utf-8").strip()
    if not token:
        raise RemoteConfigError(f"The token file {p} is empty. Run `hp login` again.")
    return token


# ── URL safety ───────────────────────────────────────────────────────────────


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, *, allow_http: bool = False) -> str:
    """A server URL the CLI may send a token to. Plain http is accepted only to
    loopback (or a profile that opted in): anywhere else the token would cross
    the network in clear."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteConfigError(
            f"Not a server URL: {url!r} (expected https://host[:port]).", exit_code=2
        )
    if parts.scheme == "http" and not allow_http and not _is_loopback(parts.hostname):
        raise RemoteConfigError(
            f"{url} is plain http, so the token would travel in clear. Use https (a "
            "self-signed server is trusted with `hp login --ca-file`), or opt in "
            "for this profile with `hp login --allow-http`.",
            exit_code=2,
        )
    return url.rstrip("/")


# ── Which target this run uses ───────────────────────────────────────────────

_active: Target | None = None


def active_target() -> Target | None:
    return _active


def set_active_target(target: Target | None) -> None:
    global _active, _server_version, _version_checked
    _active = target
    _server_version = None
    _version_checked = False


def resolve_target(profile: str | None, url: str | None) -> Target | None:
    """Pick this run's target from flags, environment and client.toml.

    Returns ``None`` for local mode. Raises ``RemoteConfigError`` for a target
    that was asked for and cannot be used - never silently falls back to local
    mode, which would run the command against a different server than the one
    the operator named.
    """
    profile = profile or os.environ.get("HP_PROFILE") or None
    url = url or os.environ.get("HP_URL") or None
    config = load_config()
    known = profiles(config)

    chosen: Profile | None = None
    if profile:
        check_profile_name(profile)
        chosen = known.get(profile)
        if chosen is None:
            raise RemoteConfigError(
                f"No profile {profile!r} in {config_path()}. Create it with "
                f"`hp login --profile {profile} --url https://homepilot.example.com`.",
                exit_code=2,
            )
    elif not url:
        default = str(config.get("default_profile") or "")
        if default:
            chosen = known.get(default)
            if chosen is None:
                raise RemoteConfigError(
                    f"default_profile {default!r} in {config_path()} names no profile.",
                    exit_code=2,
                )

    if url:
        allow_http = bool(chosen and chosen.allow_http)
        env_token = os.environ.get("HP_TOKEN", "").strip()
        if env_token:
            token = env_token
        elif chosen is not None:
            token = read_token_file(chosen.token_file)
        else:
            raise RemoteConfigError(
                "--url/HP_URL needs a token: set HP_TOKEN, or add --profile NAME to "
                "use that profile's token file.",
                exit_code=2,
            )
        return Target(
            url=check_url(url, allow_http=allow_http),
            token=token,
            profile=chosen.name if chosen else "",
            ca_file=chosen.ca_file if chosen else "",
        )
    if chosen is None:
        return None
    env_token = os.environ.get("HP_TOKEN", "").strip()
    return Target(
        url=check_url(chosen.url, allow_http=chosen.allow_http),
        token=env_token or read_token_file(chosen.token_file),
        profile=chosen.name,
        ca_file=chosen.ca_file,
    )


# ── HTTP ─────────────────────────────────────────────────────────────────────


def tls_verify(target: Target) -> ssl.SSLContext:
    """Verification is always on. ``ca_file`` ADDS a trust anchor (for a
    self-signed server); there is no switch that turns checking off."""
    context = ssl.create_default_context()
    if target.ca_file:
        try:
            context.load_verify_locations(cafile=str(Path(target.ca_file).expanduser()))
        except (OSError, ssl.SSLError) as exc:
            raise RemoteConfigError(
                f"Cannot load the profile's ca_file {target.ca_file}: {exc}"
            ) from None
    return context


def http_client(target: Target, timeout: float) -> Any:
    import httpx

    return httpx.AsyncClient(timeout=timeout, verify=tls_verify(target), follow_redirects=False)


def auth_headers(target: Target) -> dict[str, str]:
    return {"Authorization": f"Bearer {target.token}"}


def redact(text: str, target: Target | None = None) -> str:
    """``text`` with the active token replaced. Applied to everything the CLI
    prints from an exception or a server answer: the token is in the request
    headers, and an error that echoes a request must not carry it out."""
    target = target or _active
    if target is not None and target.token and target.token in text:
        text = text.replace(target.token, "[token]")
    return text


# ── Keeping up with the server ───────────────────────────────────────────────

_server_version: str | None = None
_version_checked = False


def parse_version(value: str) -> tuple[int, ...] | None:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(int(p) for p in match.groups()) if match else None


def release_version(value: str) -> str | None:
    """``X.Y.Z`` when ``value`` is exactly a release version (an optional ``v``
    in front, nothing after), else None. Self-update builds the install source
    from this, and the value can come from the server's /health: anything
    after the version (a ref, a space, an option) is refused, not passed on."""
    match = re.fullmatch(r"v?(\d{1,4})\.(\d{1,5})\.(\d{1,6})", value.strip())
    return ".".join(match.groups()) if match else None


def is_older(mine: str, theirs: str) -> bool:
    a, b = parse_version(mine), parse_version(theirs)
    return a is not None and b is not None and a < b


async def server_version(target: Target) -> str | None:
    """The server's version, read from /health once per run."""
    global _server_version, _version_checked
    if _version_checked:
        return _server_version
    _version_checked = True
    try:
        async with http_client(target, 10.0) as client:
            resp = await client.get(f"{target.base()}/health")
        value = resp.json().get("version") if resp.content else None
        _server_version = str(value) if value else None
    except Exception:  # an unreadable /health just means no version notice
        _server_version = None
    return _server_version


def older_notice(server: str | None) -> str | None:
    if server and is_older(__version__, server):
        return f"hp {__version__} is older than the server ({server}): run `hp self-update`"
    return None


async def check_version_once(target: Target) -> None:
    """Read the server version (once per run) and say so on STDERR when this
    CLI is older. Never blocks the command and never touches stdout, so a
    `--json` consumer sees only its JSON."""
    first = not _version_checked
    notice = older_notice(await server_version(target))
    if first and notice:
        print(notice, file=sys.stderr)


# ── Self-update ──────────────────────────────────────────────────────────────


def install_kind(prefix: str | None = None) -> str | None:
    """How this `hp` was installed: ``"uv"`` (uv tool install), ``"pipx"``, or
    ``None`` for anything else (the server image, a dev checkout, a venv)."""
    root = Path(prefix or sys.prefix)
    if (root / "uv-receipt.toml").exists():
        return "uv"
    if (root / "pipx_metadata.json").exists():
        return "pipx"
    return None


def install_spec(version: str) -> str:
    exact = release_version(version)
    if exact is None:
        raise RemoteConfigError(
            f"Not a release version: {version!r} (expected X.Y.Z).", exit_code=2
        )
    return f"git+{RELEASE_SOURCE}@v{exact}"


def install_command(kind: str, version: str, executable: str) -> list[str]:
    spec = install_spec(version)
    if kind == "uv":
        return [executable, "tool", "install", "--force", spec]
    return [executable, "install", "--force", spec]


def scrubbed_env() -> dict[str, str]:
    """The environment for a child process: without any HomePilot credential."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("HP_TOKEN", "HP_ADMIN_TOKEN", "HP_ADMIN_SECRET", "HP_VAULT_PASSPHRASE")
    }
