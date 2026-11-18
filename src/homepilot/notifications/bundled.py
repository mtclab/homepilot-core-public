"""Phone alerts from the product (#822): the bundled ntfy server and its phones.

Before this, an operator who wanted alerts on a phone built an ntfy server by
hand next to HomePilot - its own compose folder, hand-made accounts, and a
token carried by hand from one place to another. Here HomePilot provisions the
server itself:

* At enable time it generates two ntfy access tokens - a PUBLISHER (write-only
  on HomePilot's topic) and an ADMIN (manages phone logins) - stores both in the
  vault, and writes ntfy's declarative config (``auth-users`` / ``auth-access``
  / ``auth-tokens``) into ``<data_dir>/ntfy/server.yml``, the directory the
  opt-in ``ntfy`` compose service mounts. Anonymous access is ``deny-all``,
  signup and the web app are off, attachments are never configured.
* It creates its own notification channel (the publisher token, the internal
  compose address) through the same Notifier every other channel uses.
* "Add a phone" creates a regular ntfy user with READ-ONLY access to the topic
  through ntfy's admin API with the admin token - never ``docker exec``. The
  phone's password is shown once, in the answer to that request, and kept
  nowhere: the phone is the only place it lives.

Neither token is ever returned, logged or put in an environment. Both accounts
carry a well-formed bcrypt hash of NO known password (``_unknown_password_hash``),
so they authenticate with their token only.

Every ntfy fact relied on here was verified against the pinned release
(``NTFY_VERSION``) - source lines and a run of the real binary - in
docs/spikes/ntfy-bundle.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import yaml

from ..common import redact_endpoint
from .service import ChannelStoreError, Notifier

logger = logging.getLogger(__name__)

# The release the compose files pin (tag AND digest) and the spike verified.
NTFY_VERSION = "2.37.2"

# The two accounts HomePilot provisions. "hp-" is reserved: a phone may not
# take a name that collides with them or with a future one.
ADMIN_USER = "hp-admin"
PUBLISHER_USER = "hp-publisher"
RESERVED_PREFIX = "hp-"
TOPIC = "homepilot"

CONFIG_DIRNAME = "ntfy"
CONFIG_FILENAME = "server.yml"
# Where the compose service mounts that directory (both compose files).
CONTAINER_DIR = "/var/lib/ntfy"

VAULT_REF = "ntfy-bundled"
STATE_KEY = "phone_alerts"
CHANNEL_NAME = "Phone (bundled ntfy)"

# What the operator runs once on the control plane's host to start the opt-in
# service. HomePilot cannot start a container itself: that takes the Docker
# socket, which is root on the host.
COMPOSE_COMMAND = "docker compose --profile ntfy up -d"

HTTP_TIMEOUT_SECONDS = 5.0
# ntfy's own username rule is ^[-_.+@a-zA-Z0-9]+$ (user/util.go). A phone name
# is narrower: lower case, so "Pixel" and "pixel" cannot be two logins, and short
# enough to read on the ntfy app's account screen.
_PHONE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
# A phone password is typed on a phone keyboard: 16 characters of [a-z0-9]
# (~82 bits) in four groups, no symbols to hunt for.
_PASSWORD_GROUPS = 4
_PASSWORD_GROUP_LEN = 4
# bcrypt's own base64 alphabet (not RFC 4648's): ./A-Za-z0-9.
_BCRYPT_B64 = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

LIMIT_NOTE = (
    "This ntfy server runs on the same host as HomePilot. If that host goes down, "
    "nothing is left to push 'this host is down' - the alert and the server that "
    "would carry it stop together. Add a second channel that does not depend on "
    "this host (ntfy.sh, or an ntfy server elsewhere) if that matters to you."
)
NTFY_SH_WARNING = (
    "ntfy.sh is a public server run by someone else. Alert text - host names, rule "
    "names, values - leaves your network, and anyone who learns the topic name can "
    "read it. The topic name is random and long, and it is the only thing guarding it."
)


class PhoneAlertsError(RuntimeError):
    """A phone-alert action could not be done; the text says why and what to do."""


class PhoneAlertsConflictError(PhoneAlertsError):
    """The action conflicts with what exists (a phone name taken, not enabled)."""


# ── Credentials ──────────────────────────────────────────────────────────────


def generate_token() -> str:
    """An ntfy access token: ``tk_`` + 29 of [a-z0-9] (ntfy: user/util.go
    ``allowedTokenRegex``, ``GenerateToken`` lower-cases on purpose)."""
    return "tk_" + "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(29))


def generate_password() -> str:
    groups = (
        "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_PASSWORD_GROUP_LEN))
        for _ in range(_PASSWORD_GROUPS)
    )
    return "-".join(groups)


def _bcrypt_b64(data: bytes) -> str:
    """bcrypt's unpadded base64 (OpenBSD alphabet)."""
    out: list[str] = []
    i = 0
    while i < len(data):
        c1 = data[i]
        i += 1
        out.append(_BCRYPT_B64[c1 >> 2])
        c1 = (c1 & 0x03) << 4
        if i >= len(data):
            out.append(_BCRYPT_B64[c1])
            break
        c2 = data[i]
        i += 1
        out.append(_BCRYPT_B64[c1 | (c2 >> 4)])
        c1 = (c2 & 0x0F) << 2
        if i >= len(data):
            out.append(_BCRYPT_B64[c1])
            break
        c2 = data[i]
        i += 1
        out.append(_BCRYPT_B64[c1 | (c2 >> 6)])
        out.append(_BCRYPT_B64[c2 & 0x3F])
    return "".join(out)


def _unknown_password_hash() -> str:
    """A well-formed bcrypt hash (cost 10) whose password nobody knows.

    ntfy's ``auth-users`` insists on a bcrypt hash of cost >= 10 for every
    provisioned account (cmd/serve.go parseUsers -> user.ValidPasswordHash). The
    two accounts HomePilot provisions log in with their TOKEN only, so their
    hash is a random 16-byte salt and a random 23-byte digest: the exact shape
    bcrypt produces, with no password behind it. Password login to them is
    therefore impossible, which is the point; the spike and the journey test
    prove the real server accepts it and refuses a guessed password."""
    salt = _bcrypt_b64(secrets.token_bytes(16))  # 22 characters
    digest = _bcrypt_b64(secrets.token_bytes(23))  # 31 characters
    return f"$2a$10${salt}{digest}"


def render_server_config(*, admin_token: str, publisher_token: str, topic: str = TOPIC) -> str:
    """ntfy's server.yml for the bundled service. Keys and value formats:
    docs/spikes/ntfy-bundle.md. Paths are the CONTAINER's (``CONTAINER_DIR``)."""
    config: dict[str, Any] = {
        "listen-http": ":80",
        # Behind the reference vhost every phone shares the proxy's address;
        # without this ntfy rate-limits (and bans on failed logins) all of them
        # as one visitor. A direct LAN client without the header falls back to
        # its own address (server/util.go extractIPAddress).
        "behind-proxy": True,
        "cache-file": f"{CONTAINER_DIR}/cache.db",
        "cache-duration": "24h",
        "auth-file": f"{CONTAINER_DIR}/user.db",
        "auth-default-access": "deny-all",
        "enable-signup": False,
        "enable-login": False,
        "enable-reservations": False,
        "web-root": "disable",
        "auth-users": [
            f"{ADMIN_USER}:{_unknown_password_hash()}:admin",
            f"{PUBLISHER_USER}:{_unknown_password_hash()}:user",
        ],
        "auth-access": [f"{PUBLISHER_USER}:{topic}:write-only"],
        "auth-tokens": [
            f"{ADMIN_USER}:{admin_token}:HomePilot phone logins",
            f"{PUBLISHER_USER}:{publisher_token}:HomePilot alerts",
        ],
    }
    header = (
        "# Written by HomePilot (phone alerts, #822). Do not edit: HomePilot rewrites\n"
        "# it, and the ntfy service restarts itself when it changes.\n"
    )
    return header + yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, readable by its owner only."""
    fd, tmp = tempfile.mkstemp(prefix=".server.yml.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── Subscribe links ──────────────────────────────────────────────────────────


def subscribe_link(server_url: str, topic: str, display: str = "HomePilot") -> str:
    """The ntfy app's deep link for a topic (docs/subscribe/phone.md, "ntfy://
    links"): ``ntfy://<host>[/prefix]/<topic>``, ``secure=false`` for http."""
    parts = urlsplit(server_url.strip())
    link = f"ntfy://{parts.netloc}{parts.path.rstrip('/')}/{topic}?display={quote(display)}"
    if parts.scheme == "http":
        link += "&secure=false"
    return link


def validate_public_url(raw: str) -> str:
    """The address a phone will reach the bundled server at, normalised (no
    trailing slash). ValueError naming the problem."""
    value = (raw or "").strip().rstrip("/")
    if not value or len(value) > 512 or any(ch.isspace() for ch in value):
        raise ValueError("the address must be one URL of at most 512 characters")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the address must start with http:// or https:// and name a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("the address must be just scheme://host[:port][/path]")
    return value


# ── The service ──────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class PhoneAlerts:
    """Enable / inspect / disable the bundled ntfy server and manage its phones.

    ``transport`` is an httpx transport for the calls to ntfy, injectable for a
    test; the journey test runs the REAL ntfy binary instead. One instance per
    app (the router keeps it on ``app.state``), so its lock serialises enable,
    disable and the address check."""

    def __init__(
        self,
        *,
        notifier: Notifier,
        repo: Any,
        data_dir: str | Path,
        internal_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._notifier = notifier
        self._vault = notifier.vault
        self._repo = repo
        self.config_dir = Path(data_dir) / CONFIG_DIRNAME
        self.config_path = self.config_dir / CONFIG_FILENAME
        self.internal_url = internal_url.rstrip("/")
        self._transport = transport
        self._lock = asyncio.Lock()

    # ── State ────────────────────────────────────────────────────────────────
    async def _state(self) -> dict[str, Any]:
        row = await self._repo.get_setting(STATE_KEY)
        if not row:
            return {}
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    async def _save_state(self, state: dict[str, Any]) -> None:
        await self._repo.set_setting(STATE_KEY, json.dumps(state, sort_keys=True))

    async def _secrets(self) -> dict[str, Any]:
        if self._vault is None:
            raise PhoneAlertsError("There is no vault on this install.")
        try:
            value = await self._vault.get_secret(VAULT_REF)
        except Exception as exc:  # any vault failure (locked, missing, corrupt) reads the same
            raise PhoneAlertsError(
                f"The bundled ntfy server's tokens could not be read from the vault "
                f"({type(exc).__name__}). If the vault is locked, unlock it."
            ) from None
        if not isinstance(value, dict) or not value.get("admin_token"):
            raise PhoneAlertsError("The vault holds no tokens for the bundled ntfy server.")
        return value

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, transport=self._transport)

    # ── Enable / disable ─────────────────────────────────────────────────────
    async def enable(self) -> dict[str, Any]:
        """Provision the bundled server. Idempotent: a second call rewrites the
        config from the tokens already in the vault instead of minting new ones,
        so a phone or a channel set up earlier keeps working."""
        if self._vault is None:
            raise PhoneAlertsError(
                "There is no vault on this install, so the ntfy tokens cannot be stored "
                "safely. Nothing was written."
            )
        async with self._lock:
            state = await self._state()
            if state.get("mode") == "bundled":
                stored = await self._secrets()
                admin_token, publisher_token = stored["admin_token"], stored["publisher_token"]
            else:
                admin_token, publisher_token = generate_token(), generate_token()
                try:
                    await self._vault.store_secret(
                        VAULT_REF,
                        {"admin_token": admin_token, "publisher_token": publisher_token},
                    )
                except Exception as exc:  # any vault failure: nothing may be written
                    raise PhoneAlertsError(
                        f"The vault refused the ntfy tokens ({type(exc).__name__}); nothing "
                        "was written. If the vault is locked, unlock it and try again."
                    ) from None
            try:
                self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_private(
                    self.config_path,
                    render_server_config(admin_token=admin_token, publisher_token=publisher_token),
                )
            except OSError as exc:
                if state.get("mode") != "bundled":
                    await self._forget_secrets()
                raise PhoneAlertsError(
                    f"HomePilot could not write {self.config_path} ({exc.strerror or exc}). "
                    "The directory must be writable by HomePilot; on the shipped compose, "
                    "`docker compose up -d` hands it over (hp-data-init)."
                ) from None
            channel_id = state.get("channel_id")
            if not (channel_id and await self._notifier.store.get(int(channel_id))):
                try:
                    view = await self._notifier.create_channel(
                        kind="ntfy",
                        name=CHANNEL_NAME,
                        url=f"{self.internal_url}/{TOPIC}",
                        token=publisher_token,
                    )
                except (ValueError, ChannelStoreError) as exc:
                    raise PhoneAlertsError(
                        f"The alert channel could not be created: {exc}"
                    ) from None
                channel_id = view["id"]
            state = {
                "mode": "bundled",
                "channel_id": int(channel_id),
                "enabled_at": state.get("enabled_at") or _now(),
                "public_url": state.get("public_url"),
                "public_url_ok_at": state.get("public_url_ok_at"),
            }
            await self._save_state(state)
            logger.info(
                "phone alerts: bundled ntfy provisioned (config %s, channel %s)",
                self.config_path,
                channel_id,
            )
        return await self.status()

    async def _forget_secrets(self) -> None:
        if self._vault is None:
            return
        try:
            await self._vault.delete_secret(VAULT_REF)
        except Exception as exc:  # best effort: the config and channel are already gone
            logger.warning(
                "phone alerts: the vault entry %s could not be removed (%s)",
                VAULT_REF,
                type(exc).__name__,
            )

    async def disable(self) -> dict[str, Any]:
        """Remove the channel, the config (the service stops serving within
        seconds) and the tokens. ntfy's own database - phone logins - stays in
        the data directory, so enabling again keeps the phones."""
        async with self._lock:
            state = await self._state()
            if state.get("mode") != "bundled":
                raise PhoneAlertsConflictError("The bundled ntfy server is not enabled.")
            if state.get("channel_id"):
                await self._notifier.delete_channel(int(state["channel_id"]))
            self.config_path.unlink(missing_ok=True)
            await self._forget_secrets()
            await self._save_state({})
        return await self.status()

    # ── Status ───────────────────────────────────────────────────────────────
    async def _server_state(self) -> dict[str, Any]:
        """Is the bundled server up, and is it running OUR config?

        ``/v1/health`` answers for any ntfy; ``/v1/version`` answers only an
        admin - so a 200 there with HomePilot's admin token proves the server
        loaded the config HomePilot wrote, not an older one."""
        try:
            stored = await self._secrets()
        except PhoneAlertsError as exc:
            return {"running": False, "configured": False, "version": None, "detail": str(exc)}
        try:
            async with self._client() as client:
                health = await client.get(f"{self.internal_url}/v1/health")
                if health.status_code != 200:
                    return {
                        "running": False,
                        "configured": False,
                        "version": None,
                        "detail": f"ntfy answered HTTP {health.status_code} to its health check",
                    }
                version = await client.get(
                    f"{self.internal_url}/v1/version",
                    headers={"Authorization": f"Bearer {stored['admin_token']}"},
                )
        except (httpx.HTTPError, OSError) as exc:
            return {
                "running": False,
                "configured": False,
                "version": None,
                "detail": f"the ntfy service does not answer ({type(exc).__name__})",
            }
        if version.status_code != 200:
            return {
                "running": True,
                "configured": False,
                "version": None,
                "detail": (
                    "ntfy is running but has not loaded HomePilot's config yet; it "
                    "restarts itself within seconds of a config change"
                ),
            }
        try:
            found = str((version.json() or {}).get("version") or "")
        except ValueError:
            found = ""
        return {"running": True, "configured": True, "version": found or None, "detail": None}

    async def status(self) -> dict[str, Any]:
        state = await self._state()
        if state.get("mode") != "bundled":
            return {
                "bundled": None,
                "compose_command": COMPOSE_COMMAND,
                "ntfy_version": NTFY_VERSION,
            }
        server = await self._server_state()
        phones: list[dict[str, Any]] = []
        if server["configured"]:
            try:
                phones = await self.list_phones()
            except PhoneAlertsError:
                phones = []
        public_url = state.get("public_url")
        return {
            "bundled": {
                "enabled_at": state.get("enabled_at"),
                "channel_id": state.get("channel_id"),
                "topic": TOPIC,
                "server": server,
                "public_url": public_url,
                "public_url_ok_at": state.get("public_url_ok_at"),
                "subscribe_link": subscribe_link(public_url, TOPIC) if public_url else None,
                "phones": phones,
                "limit": LIMIT_NOTE,
            },
            "compose_command": COMPOSE_COMMAND,
            "ntfy_version": NTFY_VERSION,
        }

    # ── The address a phone uses ─────────────────────────────────────────────
    async def set_public_url(self, raw: str) -> dict[str, Any]:
        """Store the address phones will use and CHECK it answers as this server.

        The check never sends a token over that address - if it points at the
        wrong server, a token would leak there. It proves two things anonymously:
        an ntfy answers ``/v1/health``, and it refuses an anonymous read of
        HomePilot's topic (``deny-all``). A public server that lets anyone read
        the topic is not the bundled one."""
        url = validate_public_url(raw)
        async with self._lock:
            state = await self._state()
            if state.get("mode") != "bundled":
                raise PhoneAlertsConflictError("Enable the bundled ntfy server first.")
            ok, detail = await self._check_public(url)
            state["public_url"] = url
            state["public_url_ok_at"] = _now() if ok else None
            await self._save_state(state)
        return {
            "public_url": url,
            "ok": ok,
            "detail": detail,
            "subscribe_link": subscribe_link(url, TOPIC),
        }

    async def _check_public(self, url: str) -> tuple[bool, str]:
        shown = redact_endpoint(url)
        try:
            async with self._client() as client:
                health = await client.get(f"{url}/v1/health")
                if health.status_code != 200 or not (health.json() or {}).get("healthy"):
                    return False, (
                        f"{shown} answered HTTP {health.status_code} to /v1/health - that is not "
                        "an ntfy server, or the proxy in front of it does not pass the path."
                    )
                anonymous = await client.get(f"{url}/{TOPIC}/json", params={"poll": "1"})
        except httpx.HTTPError as exc:
            text = str(exc).lower()
            if "certificate" in text or "ssl" in text:
                return False, (
                    f"{shown} answered, but its TLS certificate is not trusted. The ntfy app "
                    "will refuse it too: use a certificate the phone trusts."
                )
            if isinstance(exc, httpx.ConnectError):
                return False, f"Nothing answers at {shown} from the control plane."
            return False, f"{shown} did not answer ({type(exc).__name__})."
        except ValueError:
            return False, f"{shown} answered /v1/health with something that is not ntfy's JSON."
        if anonymous.status_code in (401, 403):
            return True, f"{shown} answers, and refuses anyone without a login."
        return False, (
            f"An ntfy server answers at {shown}, but it lets an anonymous client read the "
            f"'{TOPIC}' topic (HTTP {anonymous.status_code}). The bundled server refuses that, "
            "so this address leads to a different server."
        )

    # ── Phones ───────────────────────────────────────────────────────────────
    async def _admin(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        stored = await self._secrets()
        try:
            async with self._client() as client:
                return await client.request(
                    method,
                    f"{self.internal_url}{path}",
                    content=json.dumps(body).encode() if body is not None else None,
                    headers={"Authorization": f"Bearer {stored['admin_token']}"},
                )
        except (httpx.HTTPError, OSError) as exc:
            raise PhoneAlertsError(
                f"The bundled ntfy server does not answer ({type(exc).__name__}). Is the "
                f"service running? Start it once with: {COMPOSE_COMMAND}"
            ) from None

    async def _require_bundled(self) -> None:
        if (await self._state()).get("mode") != "bundled":
            raise PhoneAlertsConflictError("Enable the bundled ntfy server first.")

    async def list_phones(self) -> list[dict[str, Any]]:
        """Every regular ntfy login HomePilot did not provision, and whether it
        may read HomePilot's topic. Read from ntfy itself, so a login added
        before HomePilot adopted the server is listed too (issue point 6)."""
        resp = await self._admin("GET", "/v1/users")
        if resp.status_code != 200:
            raise PhoneAlertsError(f"ntfy refused to list its users (HTTP {resp.status_code})")
        phones = []
        for entry in resp.json() or []:
            name = str(entry.get("username") or "")
            if entry.get("role") != "user" or entry.get("provisioned") or name in ("", "*"):
                continue
            grants = entry.get("grants") or []
            can_read = any(
                g.get("topic") in (TOPIC, "*") and "read" in str(g.get("permission", ""))
                for g in grants
            )
            phones.append({"name": name, "can_read": can_read})
        return sorted(phones, key=lambda p: p["name"])

    @staticmethod
    def _check_phone_name(raw: str) -> str:
        name = (raw or "").strip()
        if not _PHONE_NAME_RE.match(name):
            raise ValueError(
                "a phone's name is 1-32 of lower-case letters, digits, '.', '_' or '-', "
                "starting with a letter or digit"
            )
        if name.startswith(RESERVED_PREFIX):
            raise ValueError(f"names starting with '{RESERVED_PREFIX}' are HomePilot's own")
        return name

    async def add_phone(self, raw_name: str) -> dict[str, Any]:
        """Create a phone login with read-only access to HomePilot's topic.

        The password is in this answer and nowhere else - not the database, not
        the vault, not a log. Losing it means removing the phone and adding it
        again."""
        name = self._check_phone_name(raw_name)
        await self._require_bundled()
        password = generate_password()
        created = await self._admin("POST", "/v1/users", {"username": name, "password": password})
        if created.status_code == 409:
            raise PhoneAlertsConflictError(
                f"A login named '{name}' already exists on the ntfy server."
            )
        if created.status_code != 200:
            raise PhoneAlertsError(f"ntfy refused to create the login (HTTP {created.status_code})")
        granted = await self._allow(name)
        if not granted:
            # A login that cannot read the topic is a phone that never buzzes.
            await self._admin("DELETE", "/v1/users", {"username": name})
            raise PhoneAlertsError("ntfy created the login but refused it read access; removed.")
        state = await self._state()
        public_url = state.get("public_url")
        logger.info("phone alerts: phone login %s added", name)
        return {
            "name": name,
            "username": name,
            "password": password,
            "topic": TOPIC,
            "server_url": public_url,
            "subscribe_link": subscribe_link(public_url, TOPIC) if public_url else None,
        }

    async def _allow(self, name: str) -> bool:
        resp = await self._admin(
            "PUT",
            "/v1/users/access",
            {"username": name, "topic": TOPIC, "permission": "read-only"},
        )
        return resp.status_code == 200

    async def allow_phone(self, raw_name: str) -> dict[str, Any]:
        """Give an EXISTING login (one made before HomePilot adopted the server)
        read access to HomePilot's topic."""
        name = self._check_phone_name(raw_name)
        await self._require_bundled()
        if not any(p["name"] == name for p in await self.list_phones()):
            raise LookupError(name)
        if not await self._allow(name):
            raise PhoneAlertsError(f"ntfy refused to give '{name}' read access.")
        return {"name": name, "can_read": True}

    async def remove_phone(self, raw_name: str) -> dict[str, Any]:
        name = self._check_phone_name(raw_name)
        await self._require_bundled()
        resp = await self._admin("DELETE", "/v1/users", {"username": name})
        if resp.status_code == 404:
            raise LookupError(name)
        if resp.status_code != 200:
            raise PhoneAlertsError(f"ntfy refused to remove the login (HTTP {resp.status_code})")
        logger.info("phone alerts: phone login %s removed", name)
        return {"name": name, "removed": True}


# ── ntfy.sh ──────────────────────────────────────────────────────────────────


NTFY_SH_URL = "https://ntfy.sh"


async def create_ntfy_sh_channel(notifier: Notifier) -> dict[str, Any]:
    """A channel on the public ntfy.sh with a random topic HomePilot picks.

    The topic is the only credential there, so it is long (24 random
    characters) and HomePilot chooses it - an operator-typed "alerts" would be
    everyone's. It is in this answer once (the phone has to subscribe to it)
    and in the vault; never again."""
    topic = "hp-" + "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(24))
    url = f"{NTFY_SH_URL}/{topic}"
    view = await notifier.create_channel(kind="ntfy", name="Phone (ntfy.sh)", url=url, token=None)
    return {
        "channel": view,
        "topic_url": url,
        "subscribe_link": subscribe_link(NTFY_SH_URL, topic),
        "warning": NTFY_SH_WARNING,
    }
