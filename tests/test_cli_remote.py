"""Remote mode for `hp`: the real entry point against the real app (#remote-cli).

Every journey here runs the installed ``hp`` console script as a subprocess, on
an isolated "workstation" (its own HOME and XDG dirs, no server data dir),
against ``homepilot.main:app`` served by tests/e2e_fleet_server.py on loopback.
The assertions are on what the operator sees - the files `hp login` leaves
behind and their modes, what `hp whoami` prints, which hosts a generated call
returns, whether a DELETE happened - not on function returns.

The secret gate: the fleet token is random per run, so it is its own sentinel.
``_assert_no_token`` checks it is absent from every captured stdout and stderr,
including the failure paths (a refused token, a 404, an unreachable server, a
world-readable token file).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import ssl
import stat
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from .cli_live_server import Fleet, fleet_server, free_port, run_hp

pytestmark = pytest.mark.timeout(600)


@pytest.fixture(scope="module")
def fleet() -> Iterator[Fleet]:
    with fleet_server() as f:
        yield f


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "workstation"
    h.mkdir()
    return h


def _assert_no_token(result: subprocess.CompletedProcess[str], token: str) -> None:
    assert token not in result.stdout, "the token leaked into stdout"
    assert token not in result.stderr, "the token leaked into stderr"


def _login(fleet: Fleet, home: Path, profile: str = "lab") -> subprocess.CompletedProcess[str]:
    result = run_hp(
        "login", "--profile", profile, "--url", fleet.base, home=home, stdin=fleet.token
    )
    assert result.returncode == 0, result.stderr
    _assert_no_token(result, fleet.token)
    return result


# ── login / profiles / whoami ────────────────────────────────────────────────


def test_login_writes_a_private_profile_and_whoami_reads_it(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    cfg_dir = home / ".config" / "homepilot"
    config = cfg_dir / "client.toml"
    token_file = cfg_dir / "tokens" / "lab.token"
    assert stat.S_IMODE(cfg_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert token_file.read_text().strip() == fleet.token
    text = config.read_text()
    assert fleet.token not in text, "the profile file must name the token file, not hold the token"
    assert 'default_profile = "lab"' in text and f'url = "{fleet.base}"' in text

    who = run_hp("whoami", "--json", home=home)
    assert who.returncode == 0, who.stderr
    _assert_no_token(who, fleet.token)
    info = json.loads(who.stdout)
    from homepilot import __version__

    assert info["mode"] == "remote" and info["server"] == fleet.base and info["profile"] == "lab"
    assert info["server_version"] == __version__
    assert info["token_label"] == "cli-journey"
    assert set(info["capabilities"]) == {"read", "write", "admin"}


def test_login_refuses_a_bad_token_and_saves_nothing(fleet: Fleet, home: Path) -> None:
    bad = "hp_" + "0" * 13 + "_not-a-real-token-for-this-test"
    result = run_hp("login", "--url", fleet.base, home=home, stdin=bad)
    assert result.returncode == 1
    assert "refused this token (401)" in result.stderr
    assert bad not in result.stdout + result.stderr
    assert not (home / ".config" / "homepilot" / "client.toml").exists()


def test_the_token_is_never_taken_from_argv(fleet: Fleet, home: Path) -> None:
    result = run_hp("login", "--url", fleet.base, "--token", fleet.token, home=home)
    assert result.returncode == 2, "a --token option would put the token in shell history"
    assert not (home / ".config" / "homepilot").exists()


def test_a_token_file_other_users_can_read_is_refused(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    token_file = home / ".config" / "homepilot" / "tokens" / "lab.token"
    token_file.chmod(0o644)
    result = run_hp("whoami", home=home)
    assert result.returncode == 1
    assert "other users can read" in result.stderr and "chmod 600" in result.stderr
    _assert_no_token(result, fleet.token)


def test_an_unknown_profile_is_a_usage_error_not_local_mode(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    result = run_hp("-p", "nope", "whoami", home=home)
    assert result.returncode == 2
    assert "No profile 'nope'" in result.stderr


def test_plain_http_to_a_remote_host_is_refused_before_any_request(home: Path) -> None:
    result = run_hp(
        "--url", "http://homepilot.example.com", "whoami", home=home, HP_TOKEN="hp_sentinel_x"
    )
    assert result.returncode == 2
    assert "plain http" in result.stderr
    assert "hp_sentinel_x" not in result.stdout + result.stderr


def test_url_and_token_from_the_environment_need_no_config(fleet: Fleet, home: Path) -> None:
    result = run_hp("whoami", "--json", home=home, HP_URL=fleet.base, HP_TOKEN=fleet.token)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["server"] == fleet.base
    _assert_no_token(result, fleet.token)
    assert not (home / ".config" / "homepilot").exists()


def test_no_profile_and_no_url_keeps_local_mode(home: Path) -> None:
    """With nothing configured, `hp status` is today's on-box status - not a
    remote call and not a refusal."""
    result = run_hp("status", home=home)
    assert result.returncode == 0, result.stderr
    assert "Data dir" in result.stdout and "no-server-here" in result.stdout


# ── Local-only commands say so ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "words"),
    [
        (["vault", "list"], "opens this host's vault files"),
        (["changes", "edit", "x"], "hp api artifacts edit-artifact"),
        (["hosts", "refresh"], "hp api inventory refresh-inventory"),
        (["db", "check"], "this host's database file"),
        (["token", "list"], "hp api auth list-tokens"),
    ],
)
def test_local_only_commands_refuse_remotely_and_say_why(
    fleet: Fleet, home: Path, args: list[str], words: str
) -> None:
    _login(fleet, home)
    result = run_hp(*args, home=home)
    assert result.returncode == 2
    assert "runs on the control-plane host only" in result.stderr and words in result.stderr
    # Named as typed: `hp hosts refresh`, never the alias it is registered under.
    assert f"`hp {args[0]}" in result.stderr, "name the command as it was typed"
    assert fleet.base in result.stderr


def test_every_api_command_a_local_only_reason_names_exists() -> None:
    """A hint naming a command the generated layer does not have is worse than
    no hint. Checked against the real app's OpenAPI document."""
    import re

    from homepilot.cli.api_layer import operations
    from homepilot.cli.main import LOCAL_ONLY
    from homepilot.main import app

    offered = {f"hp api {op.group} {op.name}" for op in operations(app.openapi())}
    named = {
        m.group(0)
        for reason in LOCAL_ONLY.values()
        for m in re.finditer(r"hp api [a-z-]+ [a-z-]+", reason)
    }
    assert named, "the LOCAL_ONLY hints name no `hp api` command at all"
    assert not (named - offered), f"hints name commands that do not exist: {named - offered}"


def test_existing_api_backed_commands_work_remotely(fleet: Fleet, home: Path) -> None:
    """The seams: commands written for loopback reach the profile's server."""
    _login(fleet, home)
    hosts = run_hp("inventory", "list", "--output", "json", home=home)
    assert hosts.returncode == 0, hosts.stderr
    names = {h["hostname"] for h in json.loads(hosts.stdout)}
    assert {"atlas", "relay", "dns"} <= names
    hooks = run_hp("webhook", "list", home=home)
    assert hooks.returncode == 0, hooks.stderr
    _assert_no_token(hosts, fleet.token)


# ── Keeping up with the server ───────────────────────────────────────────────


def test_an_older_cli_says_so_once_on_stderr_and_never_on_stdout(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from homepilot.cli import main as cli
    from homepilot.cli import remote

    monkeypatch.setattr(remote, "__version__", "1.0.0")
    monkeypatch.setenv("HP_URL", fleet.base)
    monkeypatch.setenv("HP_TOKEN", fleet.token)
    result = CliRunner().invoke(cli.app, ["whoami", "--json"])
    assert result.exit_code == 0, result.stderr
    json.loads(result.stdout)  # stdout is ONLY the JSON
    notice = "hp 1.0.0 is older than the server"
    assert result.stderr.count(notice) == 1, result.stderr
    assert "run `hp self-update`" in result.stderr

    from homepilot import __version__

    monkeypatch.setattr(remote, "__version__", __version__)
    same = CliRunner().invoke(cli.app, ["whoami", "--json"])
    assert "older than the server" not in same.stderr


# ── Self-update ──────────────────────────────────────────────────────────────


def _fake_installer(tmp_path: Path, name: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    tool = bindir / name
    record = tmp_path / f"{name}.called"
    tool.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{record}"\n'
        f'env | grep -c "^HP_TOKEN=" >> "{record}" || true\n'
    )
    tool.chmod(0o755)
    return record


def test_self_update_reinstalls_a_uv_tool_install_at_the_servers_release(
    fleet: Fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from homepilot.cli import main as cli
    from homepilot.cli import remote

    prefix = tmp_path / "tool-env"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_text("[tool]\n")
    record = _fake_installer(tmp_path, "uv")
    monkeypatch.setattr(remote.sys, "prefix", str(prefix))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("HP_URL", fleet.base)
    monkeypatch.setenv("HP_TOKEN", fleet.token)
    monkeypatch.setattr(remote, "__version__", "1.0.0")
    monkeypatch.setattr(cli, "__version__", "1.0.0")
    from homepilot import __version__ as server

    monkeypatch.setattr(cli, "_installed_version", lambda: server)
    result = CliRunner().invoke(cli.app, ["self-update"])
    assert result.exit_code == 0, result.stderr
    assert f"hp is now {server}" in result.stdout
    argv = record.read_text().split("\n")
    assert argv[:4] == [
        "tool",
        "install",
        "--force",
        f"git+https://github.com/mtclab/homepilot-core-public@v{server}",
    ]
    assert argv[4] == "0", "the installer must not inherit HP_TOKEN"
    assert fleet.token not in result.stdout + result.stderr


def test_self_update_refuses_an_install_it_cannot_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from homepilot.cli import main as cli
    from homepilot.cli import remote

    monkeypatch.setattr(remote.sys, "prefix", str(tmp_path))
    result = CliRunner().invoke(cli.app, ["self-update", "--version", "9.9.9"])
    assert result.exit_code == 1
    assert "not a `uv tool` or pipx install" in result.stderr
    assert "uv tool install git+https://github.com/mtclab/homepilot-core-public@v9.9.9" in (
        result.stderr.replace("\n", "")
    )


def test_self_update_does_not_downgrade_past_the_server(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from homepilot.cli import main as cli
    from homepilot.cli import remote

    monkeypatch.setenv("HP_URL", fleet.base)
    monkeypatch.setenv("HP_TOKEN", fleet.token)
    monkeypatch.setattr(remote, "__version__", "99.0.0")
    monkeypatch.setattr(cli, "__version__", "99.0.0")
    result = CliRunner().invoke(cli.app, ["self-update"])
    assert result.exit_code == 0
    assert "newer than the server" in result.stdout


# ── TLS: verified by default, a self-signed server trusted via ca_file ───────


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "homepilot test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "server.pem", tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[str, Path, str]]:
    """A tiny https stand-in answering /health and /auth/me, on a self-signed cert."""
    from homepilot import __version__

    token = "hp_tlsprobe00000_tls-sentinel-token"
    cert, key = _self_signed(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                body: dict[str, Any] = {"status": "ok", "version": __version__}
            elif self.path == "/auth/me" and self.headers.get("Authorization") == f"Bearer {token}":
                body = {"token_label": "tls", "capabilities": ["read"], "scope": "read"}
            else:
                self.send_response(401)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", cert, token
    finally:
        server.shutdown()
        server.server_close()


def test_tls_is_verified_and_a_self_signed_server_needs_its_ca(
    tls_server: tuple[str, Path, str], home: Path
) -> None:
    url, cert, token = tls_server
    untrusted = run_hp("login", "--url", url, home=home, stdin=token)
    assert untrusted.returncode == 1
    assert "does not trust" in untrusted.stderr and "--ca-file" in untrusted.stderr
    assert not (home / ".config" / "homepilot" / "client.toml").exists()

    trusted = run_hp("login", "--url", url, "--ca-file", str(cert), home=home, stdin=token)
    assert trusted.returncode == 0, trusted.stderr
    assert f'ca_file = "{cert}"' in (home / ".config" / "homepilot" / "client.toml").read_text()
    who = run_hp("whoami", "--json", home=home)
    assert who.returncode == 0, who.stderr
    assert json.loads(who.stdout)["token_label"] == "tls"
    for r in (untrusted, trusted, who):
        _assert_no_token(r, token)


# ── The generated `hp api` layer ─────────────────────────────────────────────


def _host_id(fleet: Fleet, home: Path, name: str) -> str:
    page = run_hp("api", "inventory", "list-inventory", "--q", name, home=home)
    assert page.returncode == 0, page.stderr
    return next(str(h["id"]) for h in json.loads(page.stdout)["items"] if h["hostname"] == name)


def test_api_lists_every_operation(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    result = run_hp("api", "--list", home=home)
    assert result.returncode == 0, result.stderr
    assert "GET     /inventory/{host_id}" in result.stdout
    assert "hp api inventory get-host" in result.stdout
    assert "! Forget Host" in result.stdout, "a DELETE must be flagged destructive"
    assert "/guest/vms" not in result.stdout, "the mTLS guest portal is not offered"
    cached = list((home / ".cache" / "homepilot" / "openapi").glob("*.json"))
    from homepilot import __version__

    assert len(cached) == 1 and cached[0].name.endswith(f"-{__version__}.json")


def test_api_get_with_a_path_parameter(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    host = run_hp("api", "inventory", "get-host", _host_id(fleet, home, "atlas"), home=home)
    assert host.returncode == 0, host.stderr
    assert json.loads(host.stdout)["hostname"] == "atlas"
    missing = run_hp("api", "inventory", "get-host", "no-such-host", home=home)
    assert missing.returncode == 1 and "refused (404)" in missing.stderr
    _assert_no_token(missing, fleet.token)


def test_api_post_with_a_body_then_a_delete_that_asks_first(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    created = run_hp(
        "api", "monitoring", "create-rule",
        "--name", "cli journey rule", "--metric", "cpu.percent",
        "--comparison", "gt", "--threshold", "95", "--for-seconds", "0",
        home=home,
    )  # fmt: skip
    assert created.returncode == 0, created.stderr
    rule_id = str(json.loads(created.stdout)["id"])

    def rule_names() -> set[str]:
        listed = run_hp("api", "monitoring", "list-rules", home=home)
        assert listed.returncode == 0, listed.stderr
        data = json.loads(listed.stdout)
        rows = data["items"] if isinstance(data, dict) else data
        return {str(r["id"]) for r in rows}

    assert rule_id in rule_names()

    refused = run_hp("api", "monitoring", "delete-rule", rule_id, home=home)
    assert refused.returncode == 1
    assert f"Refusing: DELETE /monitoring/rules/{rule_id} is destructive" in refused.stderr
    assert rule_id in rule_names(), "a refused DELETE must not have happened"

    done = run_hp("api", "monitoring", "delete-rule", rule_id, "--yes", home=home)
    assert done.returncode == 0, done.stderr
    assert rule_id not in rule_names()


def test_a_destructive_call_names_what_it_would_affect(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    gone = _host_id(fleet, home, "old-web")
    refused = run_hp("api", "inventory", "forget-host", gone, home=home)
    assert refused.returncode == 1
    assert f"DELETE /inventory/{gone} (hostname old-web)" in refused.stderr
    still = run_hp("api", "inventory", "get-host", gone, home=home)
    assert still.returncode == 0, "the refused forget removed the host anyway"


def test_a_server_marked_destructive_post_asks_first(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    refused = run_hp("api", "tasks", "cancel-task", "some-task", home=home)
    assert refused.returncode == 1
    assert "Refusing: POST /tasks/some-task/cancel is destructive" in refused.stderr


def test_api_raw_and_its_guard_against_other_hosts(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    raw = run_hp("api", "raw", "GET", "/inventory?q=dns", home=home)
    assert raw.returncode == 0, raw.stderr
    assert any(h["hostname"] == "dns" for h in json.loads(raw.stdout)["items"])
    elsewhere = run_hp("api", "raw", "GET", "https://example.com/steal", home=home)
    assert elsewhere.returncode == 2
    deleting = run_hp("api", "raw", "DELETE", "/kb/123", home=home)
    assert deleting.returncode == 1 and "Refusing: DELETE /kb/123" in deleting.stderr


def test_api_table_output(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    table = run_hp("api", "notifications", "list-channels", "--table", home=home)
    assert table.returncode == 0, table.stderr
    tasks = run_hp("api", "tasks", "list-tasks", "--table", home=home)
    assert tasks.returncode == 0, tasks.stderr


def test_the_token_never_appears_on_failure_paths(fleet: Fleet, home: Path) -> None:
    _login(fleet, home)
    closed = f"http://127.0.0.1:{free_port()}"
    runs = [
        run_hp("api", "raw", "GET", "/nope", home=home),
        run_hp("--url", closed, "whoami", home=home, HP_TOKEN=fleet.token),
        run_hp("--url", closed, "api", "--list", home=home, HP_TOKEN=fleet.token),
        run_hp("--url", fleet.base, "whoami", home=home, HP_TOKEN=fleet.token + "x"),
        run_hp("inventory", "show", "no-such-host", home=home),
    ]
    for r in runs:
        assert r.returncode != 0
        _assert_no_token(r, fleet.token)


def test_a_server_that_echoes_the_credential_cannot_make_hp_print_it(home: Path) -> None:
    """Defence in depth: whatever a server (or a proxy in front of it) echoes back
    in a refusal - here the Authorization header itself - the CLI scrubs the
    token before printing, on the generated layer and on the curated path."""
    token = "hp_echoprobe0000_echo-sentinel-token"

    class Echo(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                status, body = 200, {"status": "ok", "version": "0.0.1"}
            elif self.path == "/openapi.json":
                status, body = 200, {"openapi": "3.1.0", "paths": {}}
            else:
                status = 400
                body = {"detail": f"bad request with {self.headers.get('Authorization')}"}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", free_port()), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        generated = run_hp("--url", url, "api", "raw", "GET", "/echo", home=home, HP_TOKEN=token)
        curated = run_hp("--url", url, "inventory", "list", home=home, HP_TOKEN=token)
    finally:
        server.shutdown()
        server.server_close()
    for result in (generated, curated):
        assert result.returncode == 1
        assert "Bearer [token]" in result.stderr.replace("\n", ""), result.stderr
        _assert_no_token(result, token)


def test_a_union_body_field_is_offered_as_its_primitive_branch(fleet: Fleet, home: Path) -> None:
    """`target: str | dict | None` on a KB note is `--target` (found walking the
    layer: it was missing, so a note could not be aimed at a host by option)."""
    _login(fleet, home)
    helped = run_hp("api", "kb", "create-note", "--help", home=home)
    assert helped.returncode == 0, helped.stderr
    assert "--target" in helped.stdout and "--content" in helped.stdout


@pytest.mark.parametrize(
    "value",
    [
        "3.12.0 --index-url https://evil.example.com",
        "3.12.0@main",
        "3.12.0;rm",
        "3.12",
        "v3.12.0-rc1",
        "",
    ],
)
def test_self_update_installs_only_an_exact_release_version(value: str) -> None:
    """The version self-update installs can come from the server's /health. Only
    an exact X.Y.Z reaches the install source; anything after it is refused,
    never passed on to the installer."""
    from homepilot.cli import remote

    assert remote.release_version(value) is None
    with pytest.raises(remote.RemoteConfigError):
        remote.install_spec(value)


def test_an_exact_release_version_builds_the_tag_spec() -> None:
    from homepilot.cli import remote

    assert remote.release_version("v3.12.0") == "3.12.0"
    assert remote.install_spec("3.12.0").endswith("@v3.12.0")


def test_remote_mode_touches_nothing_of_this_machines_homepilot(
    fleet: Fleet, tmp_path: Path
) -> None:
    """A workstation is not a server (3.13.0 dev walk).

    Remote commands used to build the SERVER's Settings: that looked up the
    admin secret and hub token in the local vault (a "vault lookup ... failed"
    line on every command) and, finding no hub token, generated one and wrote
    it with a vault passphrase into ~/.hp - server secrets minted on a client.
    Here HOME and XDG point at an EMPTY directory and HP_DATA_DIR is unset, so
    any such write lands in that HOME and is seen.

    Also: off a terminal (no COLUMNS, stdout captured), tables print in full -
    they used to be cut to 80 columns ("2026-11-14-destroy-pro…").
    """
    import os

    from .cli_live_server import HP, REPO

    home = tmp_path / "empty-home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(REPO / "src"),
        "PROXMOX_SECRETS": "/nonexistent",
    }

    def hp(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(HP), *args],
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(home),
        )
        assert result.returncode == 0, f"{args}: {result.stderr}"
        assert "vault" not in result.stderr.lower(), f"{args} reached a vault: {result.stderr}"
        _assert_no_token(result, fleet.token)
        return result

    hp("login", "--url", fleet.base, stdin=fleet.token)
    hp("status")
    hosts = hp("hosts", "list", "--gone").stdout
    changes = hp("changes", "list").stdout
    hp("api", "raw", "GET", "/health")
    hp("whoami")

    created = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    allowed = (".config", ".config/homepilot", ".cache", ".cache/homepilot")
    stray = [
        p
        for p in created
        if p not in allowed
        and not p.startswith(".config/homepilot/")
        and not p.startswith(".cache/homepilot/")
    ]
    assert not stray, f"remote mode wrote outside the client's own dirs: {stray}"

    assert "…" not in changes and "…" not in hosts, "a table was truncated off a terminal"
    assert "destroy-probe-1-e2e813" in changes
    assert "old-cache" in hosts and "Gone since" in hosts
