"""Phone alerts (#822): the parts that need no ntfy server.

The real-server journey is tests/test_phone_alerts_journey_822.py. These pin
what it cannot reach cheaply:

* the config HomePilot writes is one ntfy v2.37.2 accepts - token and hash
  formats exactly as ntfy validates them (cmd/serve.go parseUsers/parseTokens,
  user/util.go), deny-all, signup/web app/attachments off;
* enabling with no vault (or a vault that refuses) writes NOTHING - no config
  file holding tokens nobody can retrieve;
* enabling twice keeps the same tokens (a phone set up earlier keeps working);
* the ntfy.sh choice picks a long random topic itself and says plainly that
  alert text leaves the estate;
* a phone name cannot take HomePilot's own accounts;
* both compose files carry the same opt-in, pinned, address-bound service with
  its data under the HomePilot data dir;
* the phone-alert routes are admin-only and in the MCP exclusion map with a
  reason (the parity gate holds that).

Teeth: noted per test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.notifications import Notifier, bundled
from homepilot.notifications.bundled import (
    PhoneAlerts,
    PhoneAlertsError,
    create_ntfy_sh_channel,
    render_server_config,
    subscribe_link,
    validate_public_url,
)

REPO = Path(__file__).resolve().parents[1]
ROOT_COMPOSE = REPO / "docker-compose.yml"
DEPLOY_COMPOSE = REPO / "deploy" / "control-plane" / "docker-compose.yml"

# ntfy v2.37.2 user/util.go: allowedTokenRegex, allowedUsernameRegex,
# allowedTopicPatternRegex; ValidPasswordHash wants $2a$/$2b$/$2y$ and cost >= 10.
NTFY_TOKEN = re.compile(r"^tk_[-_A-Za-z0-9]{29}$")
NTFY_USERNAME = re.compile(r"^[-_.+@a-zA-Z0-9]+$")
NTFY_TOPIC_PATTERN = re.compile(r"^[-_*A-Za-z0-9]{1,64}$")
BCRYPT = re.compile(r"^\$2[aby]\$(\d\d)\$[./A-Za-z0-9]{53}$")


class MemVault:
    def __init__(self, refuse: bool = False) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}
        self.refuse = refuse

    async def store_secret(self, name: str, value: dict[str, Any]) -> None:
        if self.refuse:
            raise RuntimeError("vault locked")
        self.secrets[name] = dict(value)

    async def get_secret(self, name: str) -> dict[str, Any]:
        if name not in self.secrets:
            raise KeyError(name)
        return dict(self.secrets[name])

    async def delete_secret(self, name: str) -> bool:
        return self.secrets.pop(name, None) is not None


@pytest.fixture
async def parts(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    yield db, Repository(db)
    await db.close()


def _phone(parts: tuple[Any, Any], tmp_path: Path, vault: Any) -> PhoneAlerts:
    db, repo = parts
    notifier = Notifier(db, vault, retry_delays=())
    # An address nothing listens on: these tests never reach a server.
    return PhoneAlerts(
        notifier=notifier, repo=repo, data_dir=tmp_path / "data", internal_url="http://127.0.0.1:9"
    )


class TestTheConfigIsOneNtfyAccepts:
    """Teeth: emit a token with upper case or 28 characters, a cost-4 hash, or
    read-write default access, and these fail."""

    def test_formats(self) -> None:
        admin, publisher = bundled.generate_token(), bundled.generate_token()
        config = yaml.safe_load(render_server_config(admin_token=admin, publisher_token=publisher))
        assert NTFY_TOKEN.match(admin) and NTFY_TOKEN.match(publisher)
        assert admin.lower() == admin, "ntfy generates lower-case tokens (e-mail topics)"
        for line in config["auth-users"]:
            name, hash_, role = line.split(":")
            assert NTFY_USERNAME.match(name)
            match = BCRYPT.match(hash_)
            assert match and int(match.group(1)) >= 10, hash_
            assert role in ("admin", "user")
        for line in config["auth-access"]:
            name, topic, permission = line.split(":")
            assert NTFY_TOPIC_PATTERN.match(topic)
            assert permission == "write-only", "the publisher must not read what it sends"
        tokens = {line.split(":")[1] for line in config["auth-tokens"]}
        assert tokens == {admin, publisher}

    def test_private_by_default(self) -> None:
        config = yaml.safe_load(
            render_server_config(
                admin_token=bundled.generate_token(), publisher_token=bundled.generate_token()
            )
        )
        assert config["auth-default-access"] == "deny-all"
        assert config["enable-signup"] is False
        assert config["enable-login"] is False
        assert config["web-root"] == "disable"
        assert "attachment-cache-dir" not in config
        assert config["auth-file"].startswith(bundled.CONTAINER_DIR + "/")

    def test_no_password_behind_the_provisioned_accounts(self) -> None:
        """Every call is a fresh random hash: nothing to reuse, nothing known."""
        hashes = {bundled._unknown_password_hash() for _ in range(20)}
        assert len(hashes) == 20
        assert all(BCRYPT.match(h) for h in hashes)

    def test_phone_passwords_are_long_and_typeable(self) -> None:
        password = bundled.generate_password()
        assert re.fullmatch(r"[a-z0-9]{4}(-[a-z0-9]{4}){3}", password)


class TestLinks:
    def test_https_link(self) -> None:
        assert (
            subscribe_link("https://ntfy.example.com", "homepilot")
            == "ntfy://ntfy.example.com/homepilot?display=HomePilot"
        )

    def test_http_link_says_insecure_and_keeps_a_path_prefix(self) -> None:
        assert (
            subscribe_link("http://example.com:8080/ntfy/", "homepilot")
            == "ntfy://example.com:8080/ntfy/homepilot?display=HomePilot&secure=false"
        )

    @pytest.mark.parametrize(
        "bad",
        ["", "ntfy.example.com", "ftp://example.com", "https://u:p@example.com", "https://x/?a"],
    )
    def test_public_url_refusals(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_public_url(bad)

    def test_public_url_normalised(self) -> None:
        assert validate_public_url(" https://ntfy.example.com/ ") == "https://ntfy.example.com"


@pytest.mark.asyncio
class TestEnable:
    async def test_no_vault_writes_nothing(self, parts, tmp_path: Path) -> None:
        """Teeth: write the config before asking the vault and a file holding
        tokens nobody can retrieve is left behind."""
        alerts = _phone(parts, tmp_path, None)
        with pytest.raises(PhoneAlertsError, match="no vault"):
            await alerts.enable()
        assert not alerts.config_path.exists()

    async def test_a_refusing_vault_writes_nothing(self, parts, tmp_path: Path) -> None:
        alerts = _phone(parts, tmp_path, MemVault(refuse=True))
        with pytest.raises(PhoneAlertsError, match="vault refused"):
            await alerts.enable()
        assert not alerts.config_path.exists()
        assert (await alerts.status())["bundled"] is None

    async def test_enabling_twice_keeps_the_tokens_and_one_channel(
        self, parts, tmp_path: Path
    ) -> None:
        """Teeth: mint fresh tokens on every enable and a phone set up earlier,
        and the alert channel, silently stop working."""
        vault = MemVault()
        alerts = _phone(parts, tmp_path, vault)
        first = await alerts.enable()
        tokens = dict(vault.secrets[bundled.VAULT_REF])
        second = await alerts.enable()
        assert vault.secrets[bundled.VAULT_REF] == tokens
        assert first["bundled"]["channel_id"] == second["bundled"]["channel_id"]
        assert len(await alerts._notifier.store.list()) == 1
        assert tokens["admin_token"] in alerts.config_path.read_text()

    async def test_status_reports_a_server_that_is_not_running(self, parts, tmp_path: Path) -> None:
        alerts = _phone(parts, tmp_path, MemVault())
        status = await alerts.enable()
        server = status["bundled"]["server"]
        assert server["running"] is False and server["configured"] is False
        assert "does not answer" in server["detail"]
        assert status["compose_command"] == "docker compose --profile ntfy up -d"
        assert "same host" in status["bundled"]["limit"]

    async def test_a_phone_name_cannot_take_homepilots_accounts(
        self, parts, tmp_path: Path
    ) -> None:
        alerts = _phone(parts, tmp_path, MemVault())
        await alerts.enable()
        for name in ("hp-admin", "hp-publisher", "hp-anything", "Pixel", "a b", "*"):
            with pytest.raises(ValueError):
                await alerts.add_phone(name)

    async def test_disable_removes_config_channel_and_tokens(self, parts, tmp_path: Path) -> None:
        vault = MemVault()
        alerts = _phone(parts, tmp_path, vault)
        await alerts.enable()
        await alerts.disable()
        assert not alerts.config_path.exists()
        assert bundled.VAULT_REF not in vault.secrets
        assert await alerts._notifier.store.list() == []


@pytest.mark.asyncio
async def test_ntfy_sh_picks_a_long_random_topic_and_warns(parts, tmp_path: Path) -> None:
    """Teeth: a short or fixed topic is everyone's topic on a public server."""
    db, _repo = parts
    notifier = Notifier(db, MemVault(), retry_delays=())
    a = await create_ntfy_sh_channel(notifier)
    b = await create_ntfy_sh_channel(notifier)
    topic = a["topic_url"].rsplit("/", 1)[1]
    assert a["topic_url"].startswith("https://ntfy.sh/")
    assert len(topic) >= 24 and a["topic_url"] != b["topic_url"]
    assert "leaves your network" in a["warning"]
    assert a["channel"]["endpoint"] == "https://ntfy.sh", "the topic is never in the listing"


class TestTheComposeService:
    """Teeth: drop the profile (it runs on every install), unpin the digest,
    bind 0.0.0.0, or mount anything but the HomePilot data dir's ntfy/."""

    @pytest.mark.parametrize("path", [ROOT_COMPOSE, DEPLOY_COMPOSE], ids=["root", "control-plane"])
    def test_opt_in_pinned_and_bound(self, path: Path) -> None:
        service = yaml.safe_load(path.read_text())["services"]["ntfy"]
        assert service["profiles"] == ["ntfy"], "the ntfy service must be opt-in"
        assert re.fullmatch(
            rf"binwiederhier/ntfy:v{re.escape(bundled.NTFY_VERSION)}@sha256:[0-9a-f]{{64}}",
            service["image"],
        ), service["image"]
        (port,) = service["ports"]
        assert port.startswith("${HP_NTFY_BIND:-127.0.0.1}:"), "bound to one address only"

    def test_root_data_is_under_the_homepilot_data_dir(self) -> None:
        compose = yaml.safe_load(ROOT_COMPOSE.read_text())["services"]
        assert compose["ntfy"]["volumes"] == ["./data/hp/ntfy:/var/lib/ntfy"]
        assert "mkdir -p /home/homepilot/.hp/ntfy" in " ".join(
            compose["hp-data-init"]["entrypoint"]
        )
        assert compose["ntfy"]["depends_on"]["hp-data-init"]["condition"] == (
            "service_completed_successfully"
        )

    def test_control_plane_data_is_a_subpath_of_hp_data(self) -> None:
        (volume,) = yaml.safe_load(DEPLOY_COMPOSE.read_text())["services"]["ntfy"]["volumes"]
        assert volume["source"] == "hp-data"
        assert volume["volume"]["subpath"] == bundled.CONFIG_DIRNAME
        assert volume["target"] == bundled.CONTAINER_DIR

    def test_both_files_run_the_same_service_script(self) -> None:
        def script(path: Path) -> str:
            return yaml.safe_load(path.read_text())["services"]["ntfy"]["command"][0]

        assert script(ROOT_COMPOSE) == script(DEPLOY_COMPOSE)
        assert f"{bundled.CONTAINER_DIR}/{bundled.CONFIG_FILENAME}" in script(ROOT_COMPOSE)


def test_the_phone_routes_are_admin_only() -> None:
    """Every phone-alert route sits on the notifications router, whose routes
    each carry require_scope('admin')."""
    from homepilot.notifications.router import router

    phone = [r for r in router.routes if "/phone" in getattr(r, "path", "")]
    assert len(phone) == 9
    for route in phone:
        names = [getattr(d.dependency, "__qualname__", "") for d in route.dependencies]
        assert any("require_scope" in n for n in names), route.path
