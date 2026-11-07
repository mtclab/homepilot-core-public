from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from homepilot import __version__
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.principal import Principal
from homepilot.auth.tokens import generate_api_token
from homepilot.common import format_validation_error, write_private
from homepilot.config import get_settings
from homepilot.tasks.repository import TERMINAL_TASK_STATUSES
from homepilot.vault import VaultManager

if TYPE_CHECKING:  # imports kept out of CLI startup, present for type checking only
    from collections.abc import AsyncIterator

    from homepilot.db.connection import Database
    from homepilot.portal.repository import InviteRepository

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="hp",
    help="HomePilot — AI-first, artifact-backed, MCP-native homelab platform",
    no_args_is_help=True,
)

artifacts_app = typer.Typer(help="Artifact management")
app.add_typer(artifacts_app, name="artifacts")

policy_app = typer.Typer(help="Policy management")
app.add_typer(policy_app, name="policy")

kb_app = typer.Typer(help="KB management")
app.add_typer(kb_app, name="kb")

token_app = typer.Typer(help="API token management")
app.add_typer(token_app, name="token")

vault_app = typer.Typer(help="Vault secret management (requires HP_VAULT_PASSPHRASE)")
app.add_typer(vault_app, name="vault")

inventory_app = typer.Typer(help="Inventory management")
app.add_typer(inventory_app, name="inventory")

webhook_app = typer.Typer(help="Webhook management")
app.add_typer(webhook_app, name="webhook")

db_app = typer.Typer(help="Database file operations (restore a backup, check integrity)")
app.add_typer(db_app, name="db")

invite_app = typer.Typer(help="Self-service provisioning invites (#442 portal)")
app.add_typer(invite_app, name="invite")
quota_app = typer.Typer(
    help="Per-guest resource budgets (#442): totals across ALL a guest's machines"
)
app.add_typer(quota_app, name="quota")


def _vault_state(settings: Any) -> str:
    """Whether the vault can actually be OPENED (#431).

    This reported "unlocked" from a non-empty passphrase alone, so a wrong
    passphrase, a missing identity or a corrupt one all read as unlocked - `hp
    status` said the thing was fine and every secret lookup then failed
    elsewhere. Unwrap the identity and report what happens.
    """
    if not settings.vault_passphrase:
        return "locked"
    try:
        from homepilot.vault import VaultManager

        vault = VaultManager(Path(settings.data_dir), settings.vault_passphrase)
        asyncio.run(vault.ensure_master_identity())
    except Exception as exc:
        return f"locked ({type(exc).__name__})"
    return "unlocked"


def _pve_host(settings: Any) -> str:
    """The PVE address in force, for display.

    Reading settings.proxmox_host alone printed "(not configured)" on an
    install whose hypervisor is stored in the vault - the same wrong claim the
    self-check made (found live on dev 3.6.9). Resolve it the way the running
    backend does.
    """
    from homepilot.proxmox_config import resolve_proxmox_config

    vault: Any = None
    if settings.vault_passphrase:
        try:
            from homepilot.vault import VaultManager

            vault = VaultManager(Path(settings.data_dir), settings.vault_passphrase)
        except Exception:
            vault = None
    try:
        config = asyncio.run(resolve_proxmox_config(settings, vault))
    except Exception:
        config = None
    host = config.host if config else (settings.proxmox_host or "")
    return host or "(not configured)"


async def _open_cli_db(what: str, db_path: Path | None = None) -> Any:
    """Open the control-plane database for a CLI command, migrating it first.

    Two rules live here, and both are about a command that must NOT proceed.

    **The refusal is decided BEFORE anything is opened.** `hp init`,
    `hp token create|revoke`, `hp invite *`, `hp inventory *`, `hp webhook *`,
    `hp agent revoke|remove`, `hp kb reindex` and `hp import` all migrate the
    same file a running server is using, so a CLI could change the schema under
    it (#431). The refusal used to be raised while the caller already held an
    open aiosqlite connection - and a CLI that raises while holding one NEVER
    EXITS: aiosqlite runs the real sqlite3 handle on a NON-DAEMON worker thread
    parked on a queue nobody will feed again, and CPython joins every non-daemon
    thread at interpreter exit. The command printed its refusal and then hung
    forever. `docker compose exec` does not forward the caller's signals either,
    so giving up left an orphaned python process in the container that only a
    SIGKILL from inside it cleared (#623; reproduced on dev at 3.6.14 with
    `hp token create`, which `docs/deployment.md` calls the way to mint a token).

    **A failure during the migration closes what it opened**, for the same
    reason: an abandoned handle is a process that will not exit.

    Callers still own the returned connection and must close it in a `finally`.
    """
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    _refuse_if_server_running(_get_settings(), what)
    database = Database(str(db_path if db_path is not None else _db_path()))
    await database.connect()
    try:
        await run_migrations(database)
    except BaseException:
        await database.close()
        raise
    return database


def _server_is_running(settings: Any) -> bool:
    """Whether a backend holds this data directory (it takes the instance lock)."""
    from homepilot.instance_lock import another_instance_is_running

    return bool(another_instance_is_running(settings.data_dir))


def _refuse_if_server_running(settings: Any, what: str) -> None:
    """Refuse a schema migration while a backend holds this data directory (#431).

    `hp init`, `hp token create` and `hp agent revoke` each run `run_migrations()`
    on the same file a running server is using, so a CLI could migrate the schema
    out from under it. The backend takes an advisory lock for its lifetime; this
    asks whether anyone holds it.
    """
    if _server_is_running(settings):
        err_console.print(
            f"[red]The HomePilot backend is running against {settings.data_dir}.[/red]"
        )
        err_console.print(
            f"[yellow]{what} would migrate the schema under it. Stop the backend "
            "first, or use the API.[/yellow]"
        )
        raise typer.Exit(1)


def _get_settings() -> Any:
    return get_settings()


def _get_artifact_store() -> ArtifactStore:
    settings = _get_settings()
    artifacts_dir = Path(settings.artifacts_dir)
    return ArtifactStore(
        artifacts_dir,
        remote=getattr(settings, "artifacts_remote", ""),
        ssh_key=getattr(settings, "artifacts_ssh_key", ""),
    )


@contextlib.asynccontextmanager
async def _lifecycle_with_repo(what: str) -> AsyncIterator[ArtifactLifecycle]:
    """The artifact lifecycle WITH its repository, for a stopped backend (#693).

    Without the repository every transition is file-only: the DB row, the audit
    entry and the per-config webhooks silently never happen. Refused (by
    `_open_cli_db`) while a backend holds the data directory - then the CLI
    goes through the API instead.
    """
    from homepilot.db.repository import Repository

    database = await _open_cli_db(what)
    try:
        yield ArtifactLifecycle(_get_artifact_store(), repository=Repository(database))
    finally:
        await database.close()


# The on-box CLI approves as an operator (#681): it opens the control plane's
# database and artifact store directly, so whoever runs it already holds
# everything an operator credential would grant.
_CLI_OPERATOR = Principal.operator("cli")


def _lifecycle_transition(action: str, id: str, reason: str | None) -> None:
    """Approve or reject through whoever owns the lifecycle right now (#693).

    With the backend running it is a request to its API - the same route the UI
    uses, so its DB, audit log, SSE stream and webhooks all see it and its
    policy applies. With the backend stopped the CLI runs the lifecycle itself,
    with the repository. Any failure exits non-zero.
    """

    async def _direct() -> None:
        async with _lifecycle_with_repo(f"hp artifacts {action}") as lifecycle:
            if action == "approve":
                await lifecycle.approve(id, _CLI_OPERATOR, reason=reason)
            else:
                await lifecycle.reject(id, _CLI_OPERATOR.name, reason=reason)

    try:
        if _server_is_running(_get_settings()):
            # No identity in the body: the API takes it from the credential
            # (#681). Approving needs an OPERATOR credential - a write-tier
            # token is an assistant there and would need a relayed code - so
            # approve mints the admin capability; reject needs only write.
            body: dict[str, Any] = {}
            if reason:
                body["reason"] = reason
            scope = "admin" if action == "approve" else "write"
            asyncio.run(_backend_api("POST", f"/artifacts/{id}/{action}", body, scope=scope))
        else:
            asyncio.run(_direct())
    except typer.Exit:
        raise
    except Exception as e:
        err_console.print(f"[red]{action.capitalize()} failed for {id}: {e}[/red]")
        raise typer.Exit(1) from None


def _db_path() -> Path:
    return Path(_get_settings().data_dir) / "homepilot.db"


async def _with_repo(fn: Any) -> Any:
    """Open the main DB, run `fn(repo)`, and close. For the approval-code surface
    the CLI reaches directly (its ArtifactLifecycle carries no repo)."""
    from homepilot.db.connection import Database
    from homepilot.db.repository import Repository

    database = Database(str(_db_path()))
    await database.connect()
    try:
        return await fn(Repository(database))
    finally:
        await database.close()


def _artifact_approval_code(artifact_id: str, status: str) -> tuple[str | None, bool]:
    """(display code, locked) for a PROPOSED artifact, or (None, False).

    Reads the code from the DB, backfilling one lazily if the artifact was
    proposed before this feature shipped. Any DB/table trouble degrades to
    (None, False) so `show` still renders."""
    if status != "proposed":
        return None, False

    async def _work(repo: Any) -> tuple[str | None, bool]:
        from homepilot.artifacts.approval_code import ensure_approval_code, format_for_display

        code = await ensure_approval_code(repo, artifact_id)
        row = await repo.get_approval_code_row(artifact_id)
        return format_for_display(code), bool(row and int(row["locked"]))

    try:
        result: tuple[str | None, bool] = asyncio.run(_with_repo(_work))
        return result
    except Exception:
        return None, False


async def _audit_cli_mint(repo: Any, prefix: str, label: str, scope: str) -> None:
    """The audit row for a token the CLI minted straight into the database (#686).

    `hp init` and the zero-token bootstrap of `hp token create` are the two
    mints with no API in between, so they write the row the API would."""
    from homepilot.auth.audit import ACTION_MINTED, audit_credential_event

    await audit_credential_event(
        repo,
        action=ACTION_MINTED,
        actor="cli",
        source="cli",
        prefix=prefix,
        label=label,
        scope=scope,
    )


@app.command()
def init(
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        "-y",
        help=(
            "Skip prompts (Docker/scripted bootstrap): take PVE_API_TOKEN / "
            "HP_ADMIN_SECRET / HP_VAULT_PASSPHRASE from the environment and "
            "auto-generate any that are blank. Proxmox host is left to .env."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Reinitialize even if HomePilot is already set up. The existing .env "
            "and master vault identity are backed up to timestamped .bak files "
            "first, but a fresh vault passphrase orphans the OLD vault secrets."
        ),
    ),
) -> None:
    """Interactive setup: configure PVE, vault, and create ~/.hp/ structure."""
    import secrets as _secrets

    data_dir = Path(os.environ.get("HP_DATA_DIR", str(Path.home() / ".hp")))
    data_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = Path(os.environ.get("HP_ARTIFACTS_DIR", str(data_dir / "artifacts")))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    console.print("[bold]HomePilot Init[/bold]\n")

    # Refuse a live install BEFORE the first byte is written. `hp init` rewrites
    # .env, the master vault identity and the box's api-token, and it used to do
    # all of that and only then discover that a backend holds the data directory
    # (#431) - so a refused init had already replaced the operator's .env.
    _refuse_if_server_running(_get_settings(), "hp init")

    # Re-run guard (#384): `hp init` generates a fresh vault passphrase and
    # rewrites .env — the ONLY place the old passphrase lives. Running it a
    # second time would orphan every existing vault secret
    # (vault/identities/master.protected). Refuse unless --force, and even then
    # back up the .env + master identity before overwriting so the data is
    # recoverable.
    env_path = data_dir / ".env"
    master_identity_path = data_dir / "vault" / "identities" / "master.protected"
    already_initialized = env_path.exists() or master_identity_path.exists()
    if already_initialized and not force:
        err_console.print(
            f"[red]HomePilot is already initialized at {data_dir}.[/red]\n"
            "[yellow]Your existing configuration and vault are intact — nothing "
            "was changed.[/yellow]\n"
            "Re-running `hp init` would generate a new vault passphrase and "
            "overwrite .env, orphaning the current vault secrets.\n"
            "Pass [bold]--force[/bold] to reinitialize (the old .env and master "
            "identity are backed up first)."
        )
        raise typer.Exit(1)
    if already_initialized and force:
        import shutil

        backup_ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        if env_path.exists():
            env_backup = env_path.with_name(f".env.{backup_ts}.bak")
            shutil.copy2(str(env_path), str(env_backup))
            console.print(f"[dim]Backed up existing .env to {env_backup}[/dim]")
        if master_identity_path.exists():
            identity_backup = master_identity_path.with_name(f"master.protected.{backup_ts}.bak")
            shutil.copy2(str(master_identity_path), str(identity_backup))
            # The old identity is wrapped with the OLD passphrase; the fresh
            # passphrase generated below cannot unwrap it. Remove it (the .bak
            # preserves it) so a new identity is generated under the new
            # passphrase instead of failing to decrypt.
            master_identity_path.unlink()
            console.print(f"[dim]Backed up existing master identity to {identity_backup}[/dim]")

    if non_interactive:
        # Headless bootstrap: secrets come from the environment (the same .env
        # the backend reads), blanks are auto-generated below. Proxmox host is
        # already configured via .env, so we don't rewrite it here.
        pve_url = ""
        pve_token = os.environ.get("PVE_API_TOKEN", "")
        admin_secret = os.environ.get("HP_ADMIN_SECRET", "")
        passphrase = os.environ.get("HP_VAULT_PASSPHRASE", "")
        console.print(
            "[dim]non-interactive: reading secrets from env, auto-generating blanks[/dim]"
        )
    else:
        pve_url = typer.prompt("Proxmox VE URL (e.g. https://pve1.lan:8006)", default="")
        pve_token = typer.prompt("Proxmox API Token (PVEAPIToken=...)", default="", hide_input=True)
        admin_secret = typer.prompt(
            "Admin secret for API (leave blank to auto-generate)",
            default="",
            hide_input=True,
        )
        passphrase = typer.prompt(
            "Master passphrase for vault (leave blank to auto-generate)",
            default="",
            hide_input=True,
            confirmation_prompt=True,
        )

    if not passphrase:
        passphrase = _secrets.token_urlsafe(24)
        console.print("[dim]Generated a random vault passphrase[/dim]")

    if not admin_secret:
        admin_secret = _secrets.token_urlsafe(32)

    env_lines = [
        f"HP_VAULT_PASSPHRASE={passphrase}",
        f"HP_DATA_DIR={data_dir}",
        f"HP_ARTIFACTS_DIR={artifacts_dir}",
    ]

    if pve_url:
        base = pve_url.replace("https://", "").replace("http://", "").split(":")[0]
        env_lines.append(f"HP_PROXMOX_HOST={base}")
        port_part = pve_url.split(":")[-1] if pve_url.count(":") >= 2 else "8006"
        with contextlib.suppress(ValueError):
            env_lines.append(f"HP_PROXMOX_PORT={int(port_part)}")

    write_private(env_path, "\n".join(env_lines) + "\n")

    vault_dir = data_dir / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    # A fixed, operator-managed location, NOT a configurable one: the
    # `ssh_key_dir` setting lost its last reader with the jumpserver removal
    # (#327) and was deleted in #394. Keys an operator puts here are archived
    # by `hp export --include-secrets` (#421).
    ssh_dir = data_dir / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)

    full_token, token_prefix, token_hash = generate_api_token()
    token_path = data_dir / "api-token"
    write_private(token_path, full_token + "\n")

    async def _register_token() -> None:
        from homepilot.db.repository import Repository

        database = await _open_cli_db("hp init", data_dir / "homepilot.db")
        try:
            repo = Repository(database)
            user_id = await repo.create_user(display_name="admin", auth_source="api_token")
            await repo.create_api_token(
                user_id=str(user_id),
                token_type="personal",
                prefix=token_prefix,
                hash=token_hash,
                # "all" normalizes to "*" - the admin scope the browser claim
                # hands its first credential. It used to be "read,write", which
                # could not open Settings -> Tokens (admin) and, now that
                # minting requires an admin, could not mint either: the box's
                # only token was locked out of managing tokens.
                scope="all",
                label="admin",
                expires_at=None,
            )
            await database.conn.commit()
            await _audit_cli_mint(repo, token_prefix, "admin", "all")
        finally:
            await database.close()

        vault = VaultManager(data_dir, passphrase)
        await vault.ensure_master_identity()
        await vault.store_secret("admin-secret", {"value": admin_secret})
        if pve_token:
            await vault.store_secret("pve-token", {"token": pve_token})
            console.print("[green]PVE API token stored in vault[/green]")

    asyncio.run(_register_token())

    subprocess.run(["git", "init"], cwd=str(artifacts_dir), capture_output=True, check=False)
    subprocess.run(
        ["git", "config", "user.email", "homepilot@localhost"],
        cwd=str(artifacts_dir),
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "HomePilot"],
        cwd=str(artifacts_dir),
        capture_output=True,
        check=False,
    )

    console.print(f"\n[green]HomePilot initialized at {data_dir}[/green]")
    console.print("[green]Vault secrets stored:[/green]")
    console.print("  admin-secret → vault (used for admin API auth)")
    if pve_token:
        console.print("  pve-token    → vault (used for Proxmox API)")
    console.print("[dim].env contains only HP_VAULT_PASSPHRASE (bootstrap secret)[/dim]")
    console.print(
        f"[dim]API token (save this — it won't be shown again):[/dim] [bold]{full_token}[/bold]"
    )
    console.print("[dim]Add to your MCP config (the backend serves MCP at /mcp):[/dim]")
    console.print(
        f'[dim]  {{"mcpServers": {{"homepilot": '
        f'{{"url": "http://<backend-host>:{_get_settings().daemon_port}/mcp", '
        f'"headers": {{"Authorization": "Bearer {full_token}"}}}}}}}}[/dim]'
    )
    console.print(
        "[dim]  (`hp mcp-serve` over stdio is for running WITHOUT a backend; it "
        "refuses while one is running.)[/dim]"
    )


@app.command("claim-code")
def claim_code() -> None:
    """Print this instance's claim code (only needed when it is not claimed from
    its own network).

    The normal first run needs nothing from this command: a browser on the same
    network claims the instance directly. The code is what an instance reached
    from OUTSIDE its network asks for, and this is how to read it without
    scrolling container logs.
    """
    from homepilot.claim.repository import ClaimRepository
    from homepilot.claim.startup import claim_code_path, read_claim_code
    from homepilot.db.connection import Database

    settings = _get_settings()
    data_dir = Path(settings.data_dir)

    async def _state() -> tuple[bool, bool]:
        """(the instance is claimed, a pending claim row exists)."""
        database = Database(str(data_dir / "homepilot.db"))
        await database.connect()
        try:
            claims = ClaimRepository(database)
            return await claims.is_claimed(), (await claims.get()) is not None
        finally:
            await database.close()

    if not (data_dir / "homepilot.db").exists():
        err_console.print(
            f"[red]No HomePilot database at {data_dir}.[/red] "
            "Start the backend once, then run this again."
        )
        raise typer.Exit(1)

    claimed, has_row = asyncio.run(_state())
    if claimed:
        console.print("[green]This instance is already claimed.[/green] No claim code is in use.")
        return
    if not has_row:
        err_console.print(
            "[red]No claim code has been issued yet.[/red] "
            "Start the backend once - it issues one on first boot."
        )
        raise typer.Exit(1)

    code = read_claim_code(data_dir)
    if not code:
        err_console.print(
            f"[red]The claim code file {claim_code_path(data_dir)} is missing.[/red]\n"
            "Restart the backend: it issues a fresh code when the saved copy is gone."
        )
        raise typer.Exit(1)

    console.print(f"[bold]{code}[/bold]")
    console.print(
        "[dim]Only needed from outside this network - a browser on the local "
        "network claims the instance with no code at all.[/dim]"
    )


@app.command()
def status() -> None:
    """Show whether a backend holds this data dir, the configured PVE address,
    whether the vault opens, and artifact counts.

    The PVE row is the CONFIGURED address; nothing here connects to it (#704:
    the docstring promised connectivity it never checked). Settings -> Proxmox
    in the web UI runs a real connection test.
    """
    settings = _get_settings()
    data_dir = Path(settings.data_dir)

    table = Table(title="HomePilot Status", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    table.add_row("Data dir", str(data_dir))
    table.add_row("Artifacts dir", str(Path(settings.artifacts_dir)))
    # Established from the instance lock the backend holds for its lifetime -
    # not from a port answering.
    table.add_row(
        "Backend",
        "running (holds the data dir)" if _server_is_running(settings) else "not running",
    )
    table.add_row("PVE host (configured, not probed)", _pve_host(settings))
    table.add_row("Vault", _vault_state(settings))

    store = _get_artifact_store()
    all_artifacts = store.list()
    counts: dict[str, int] = {}
    for a in all_artifacts:
        s = a.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    for status_val in [
        "proposed",
        "approved",
        "applied",
        "failed",
        "superseded",
        "revoked",
        "rejected",
    ]:
        table.add_row(f"  Artifacts/{status_val}", str(counts.get(status_val, 0)))

    console.print(table)


@app.command()
def mcp_serve(
    transport: str = typer.Option("stdio", "--transport", help="Transport: stdio or http"),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host (http only)"),
    port: int = typer.Option(8000, "--port", help="Bind port (http only)"),
) -> None:
    """Start a STANDALONE MCP server (stdio or http transport).

    Standalone means no backend is running on this data directory: this process
    then is the control plane. With a backend running it refuses (#698) - point
    the client at the backend's own /mcp endpoint instead.
    """
    from homepilot.instance_lock import InstanceLockError

    try:
        if transport == "http":
            from homepilot.mcp.server import run_server_http

            asyncio.run(run_server_http(host=host, port=port))
        else:
            from homepilot.mcp.server import main as mcp_main

            mcp_main()
    except InstanceLockError as exc:
        err_console.print(f"[red]hp mcp-serve refused: {exc}[/red]")
        raise typer.Exit(1) from None


@artifacts_app.command("list")
def artifacts_list(
    status: str | None = typer.Option(None, "--status", help="Filter by status"),
    kind: str | None = typer.Option(None, "--kind", help="Filter by kind"),
) -> None:
    """List artifacts in a table."""
    store = _get_artifact_store()
    results = store.list(status=status, kind=kind)

    table = Table(title="Artifacts")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Intent", max_width=50)
    table.add_column("Mutating")

    for a in results:
        table.add_row(
            a.get("id", ""),
            a.get("kind", ""),
            a.get("status", ""),
            a.get("intent", ""),
            str(a.get("mutating", "")),
        )

    console.print(table)


@artifacts_app.command("show")
def artifacts_show(
    id: str = typer.Argument(..., help="Artifact ID"),
    diff: bool = typer.Option(False, "--diff", help="Show git diff for last change"),
) -> None:
    """Render full artifact with rich formatting."""
    store = _get_artifact_store()
    try:
        fm, body = store.read(id)
    except FileNotFoundError:
        err_console.print(f"[red]Artifact not found: {id}[/red]")
        raise typer.Exit(1) from None

    header = (
        f"[bold]{fm.get('id', id)}[/bold]  [{fm.get('status', '?')}]  kind={fm.get('kind', '?')}"
    )
    console.print(Panel(header, style="bold blue"))

    meta_lines = []
    for k in (
        "intent",
        "mutating",
        "idempotence",
        "target",
        "produced_by",
        "approved_by",
        "applied_at",
        "supersedes",
        "superseded_by",
        "tags",
        "rollback",
        "replay_safe",
        "requires_snapshot",
    ):
        v = fm.get(k)
        if v is not None:
            meta_lines.append(
                f"  [dim]{k}:[/dim] "
                f"{json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}"
            )
    if meta_lines:
        console.print("\n".join(meta_lines))

    # Approval code (human-relay MCP approval): shown ONLY while proposed, so a
    # present operator can read it and relay it to the assistant, which cannot
    # see it over MCP. Approving here at the CLI needs no code (you are the human).
    code, locked = _artifact_approval_code(id, str(fm.get("status", "")))
    if code is not None:
        console.print()
        console.print(
            Panel(
                f"[bold]{code}[/bold]\n"
                "[dim]Relay this to the assistant to approve over MCP, or run "
                "`hp artifacts approve` here.[/dim]"
                + (
                    "\n[red]LOCKED — too many wrong codes. "
                    "Run `hp artifacts reset-approval` to unlock.[/red]"
                    if locked
                    else ""
                ),
                title="Approval code",
                style="bold green",
            )
        )

    artifact_path = store.resolve_path(id)
    rel_path = str(artifact_path.relative_to(store.root))
    artifacts_dir = str(store.root)

    _print_git_history(artifacts_dir, rel_path, lines=3)

    console.print()
    console.print(Markdown(body))

    if diff:
        _print_git_diff(artifacts_dir, rel_path)


def _print_git_history(artifacts_dir: str, rel_path: str, lines: int = 3) -> None:
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"-{lines}", "--", rel_path],
            cwd=artifacts_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return
    if result.returncode != 0 or not result.stdout.strip():
        return
    console.print()
    console.print(Panel("History", style="dim"))
    for line in result.stdout.strip().split("\n"):
        console.print(f"  [dim]{line}[/dim]")


def _print_git_diff(artifacts_dir: str, rel_path: str) -> None:
    try:
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-5", "--", rel_path],
            cwd=artifacts_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return
    if log_result.returncode != 0 or not log_result.stdout.strip():
        console.print("\n[dim]No git history available for diff.[/dim]")
        return
    console.print()
    console.print(Panel("Diff (HEAD~1)", style="bold yellow"))
    try:
        diff_result = subprocess.run(
            ["git", "diff", "HEAD~1", "--", rel_path],
            cwd=artifacts_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return
    if diff_result.returncode != 0 or not diff_result.stdout.strip():
        console.print("[dim]No diff available (single commit or no change).[/dim]")
        return
    console.print(diff_result.stdout)


@artifacts_app.command("approve")
def artifacts_approve(
    id: str = typer.Argument(..., help="Artifact ID"),
    reason: list[str] | None = typer.Option(None, "--reason", help="Approval reason"),  # noqa: B008
) -> None:
    """Approve a proposed artifact."""
    reason_str = " ".join(reason) if reason else None
    _lifecycle_transition("approve", id, reason_str)
    console.print(f"[green]Approved: {id}[/green]")


@artifacts_app.command("reset-approval")
def artifacts_reset_approval(
    id: str = typer.Argument(..., help="Artifact ID"),
) -> None:
    """Clear a brute-force approval LOCK on an artifact.

    After too many wrong codes relayed over MCP, coded approval locks for the
    artifact. This clears the lock; the code itself is unchanged, so you can
    re-read it with `hp artifacts show` and relay it again."""

    async def _reset(repo: Any) -> bool:
        return bool(await repo.reset_approval_lock(id))

    try:
        existed = asyncio.run(_with_repo(_reset))
    except Exception as e:
        err_console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e
    if not existed:
        err_console.print(f"[yellow]No active approval code for {id}.[/yellow]")
        raise typer.Exit(1)
    console.print(f"[green]Approval lock cleared: {id}[/green]")


@artifacts_app.command("reject")
def artifacts_reject(
    id: str = typer.Argument(..., help="Artifact ID"),
    reason: list[str] | None = typer.Option(None, "--reason", help="Rejection reason"),  # noqa: B008
) -> None:
    """Reject a proposed artifact."""
    reason_str = " ".join(reason) if reason else None
    _lifecycle_transition("reject", id, reason_str)
    console.print(f"[yellow]Rejected: {id}[/yellow]")


@artifacts_app.command("edit")
def artifacts_edit(
    id: str = typer.Argument(..., help="Artifact ID"),
) -> None:
    """Edit an artifact in $EDITOR, then submit the edit to its lifecycle.

    The editor works on a COPY. The artifact's own file changes only when the
    lifecycle accepts the edit - through the running backend's API, or directly
    with the backend stopped - so a refused edit leaves it exactly as it was
    (#693: editing an applied artifact used to save the change into its file,
    print a dim line and exit 0). A terminal artifact is refused before the
    editor opens.
    """
    from homepilot.artifacts.models import ArtifactStatus

    store = _get_artifact_store()
    try:
        path = store.resolve_path(id)
    except ValueError:
        err_console.print(f"[red]Invalid artifact ID: {id}[/red]")
        raise typer.Exit(1) from None

    if not path.exists():
        err_console.print(f"[red]Artifact not found: {id}[/red]")
        raise typer.Exit(1)

    fm, _body = store.read(id)
    terminal = {
        ArtifactStatus.APPLIED.value,
        ArtifactStatus.SUPERSEDED.value,
        ArtifactStatus.REVOKED.value,
        ArtifactStatus.REJECTED.value,
    }
    if fm.get("status") in terminal:
        err_console.print(
            f"[red]{id} is {fm.get('status')} and can no longer be edited. "
            "Propose a new artifact (it can supersede this one) instead.[/red]"
        )
        raise typer.Exit(1)

    original = path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="hp-edit-") as tmp:
        draft = Path(tmp) / f"{id}.md"
        draft.write_text(original, encoding="utf-8")
        editor = os.environ.get("EDITOR", "vi")
        subprocess.run([editor, str(draft)], check=False)
        edited = draft.read_text(encoding="utf-8")

    if edited == original:
        console.print(f"[dim]No changes to {id}.[/dim]")
        return

    try:
        if _server_is_running(_get_settings()):
            result = asyncio.run(
                _backend_api("POST", f"/artifacts/{id}/edit", {"content": edited}, scope="write")
            )
        else:
            result = asyncio.run(_edit_directly(id, edited))
    except Exception as e:
        err_console.print(f"[red]Edit refused, {id} is unchanged: {e}[/red]")
        raise typer.Exit(1) from None
    status = result.get("status") if isinstance(result, dict) else None
    console.print(
        f"[green]Edit recorded for: {id}[/green]" + (f" (now {status})" if status else "")
    )


async def _edit_directly(id: str, edited: str) -> dict[str, Any]:
    from homepilot.artifacts.edit_router import submit_edit

    async with _lifecycle_with_repo("hp artifacts edit") as lifecycle:
        return await submit_edit(lifecycle, id, edited, "cli")


# How long `hp artifacts apply|replay|revoke` follow a task before saying its
# outcome is unknown. Generous on purpose: an apply that provisions a guest or
# runs a long playbook legitimately takes many minutes, and giving up early is
# only honest if it is reported as "unknown", which it is.
_TASK_WAIT_SECONDS = 1800.0
_TASK_POLL_INTERVAL = 2.0
# Consecutive poll failures tolerated before the backend counts as gone.
_TASK_POLL_MAX_FAILURES = 5
# Exit status when the command cannot say whether the change happened. Distinct
# from 1 (it failed) so a script can tell "go and look" from "it did not work".
EXIT_OUTCOME_UNKNOWN = 3


async def _follow_task(call: Any, path: str, method: str, body: dict[str, Any] | None) -> Any:
    """Start the task, then follow ``GET /tasks/{id}`` to a terminal status.

    Returns the last task record seen, or ``None`` if the task could not be
    started. A task still pending/running when this returns is one whose outcome
    is unknown - the caller must not report it as done.
    """
    import httpx

    started = await call(method, path, body)
    task_id = started.get("task_id") if isinstance(started, dict) else None
    if not task_id:
        raise RuntimeError(f"{path} did not start a task: {started!r}")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TASK_WAIT_SECONDS
    task: dict[str, Any] = {"id": task_id, "status": started.get("status", "pending")}
    failures = 0
    while True:
        try:
            polled = await call("GET", f"/tasks/{task_id}")
            failures = 0
            if isinstance(polled, dict):
                task = polled
        except (httpx.HTTPError, RuntimeError) as exc:
            failures += 1
            task = {**task, "_poll_error": str(exc) or type(exc).__name__}
            if failures >= _TASK_POLL_MAX_FAILURES:
                return task
        else:
            if task.get("status") in TERMINAL_TASK_STATUSES:
                return task
        if loop.time() >= deadline:
            return task
        await asyncio.sleep(_TASK_POLL_INTERVAL)


def _run_artifact_task(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    past_tense: str,
    artifact_id: str,
) -> None:
    """Drive an apply/replay/revoke through the backend and report the OUTCOME.

    The command follows the task until it finishes: a CLI that returns before the
    host has been touched is the same lie as a rollback that prints and does
    nothing. It used to hold ONE `?sync=true` request open for the whole apply
    under a 15 s client timeout, so a long apply crashed the CLI with a
    ReadTimeout traceback while the apply carried on - and a task the server
    stopped waiting for came back `running` and was printed as done (#694).

    Only ``succeeded`` is success. ``failed``/``cancelled`` exit 1; a task that
    is still going at the deadline, or a backend that stops answering, exits
    ``EXIT_OUTCOME_UNKNOWN`` and says how to find out.
    """
    import httpx

    async def _go() -> Any:
        # read AND write: starting the task is a write, but following it
        # (GET /tasks/{id}) is a read, and a "write" token does not include it.
        async with _backend_session("read,write") as call:
            return await _follow_task(call, path, method, body)

    try:
        task = asyncio.run(_go())
    except RuntimeError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        # The START request did not get an answer: the backend may or may not
        # have queued the change.
        err_console.print(
            f"[yellow]No answer from the backend while starting the task for "
            f"{artifact_id} ({type(exc).__name__}). The outcome is unknown: check "
            f"`hp artifacts show {artifact_id}` or the Tasks page.[/yellow]"
        )
        raise typer.Exit(EXIT_OUTCOME_UNKNOWN) from None

    status = task.get("status")
    task_id = task.get("id", "?")
    if status == "succeeded":
        console.print(f"[green]{past_tense}: {artifact_id}[/green]")
        return
    if status in ("failed", "cancelled"):
        default = "cancelled before it finished" if status == "cancelled" else "no reason recorded"
        error = task.get("error") or default
        err_console.print(f"[red]{past_tense} {status} for {artifact_id}: {error}[/red]")
        raw = task.get("result_json")
        log = None
        if isinstance(raw, str) and raw:
            with contextlib.suppress(ValueError):
                log = (json.loads(raw) or {}).get("execution_log")
        if log:
            console.print(f"[dim]{log}[/dim]")
        raise typer.Exit(1)

    reason = task.get("_poll_error")
    why = (
        f"the backend stopped answering ({reason})"
        if reason
        else f"task {task_id} is still {status or 'in an unknown state'}"
    )
    err_console.print(
        f"[yellow]The outcome of task {task_id} for {artifact_id} is unknown: {why}. "
        f"It may still complete - check `hp artifacts show {artifact_id}` or the "
        "Tasks page.[/yellow]"
    )
    raise typer.Exit(EXIT_OUTCOME_UNKNOWN)


@artifacts_app.command("apply")
def artifacts_apply(
    id: str = typer.Argument(..., help="Artifact ID"),
    approve: bool = typer.Option(False, "--approve", help="Approve first, then apply"),
) -> None:
    """Apply an approved artifact (or approve+apply together).

    Runs through the backend's executor - the same engine the UI and MCP use.
    It used to call a second, weaker one (#423) that had no pre-apply snapshot,
    no approved-body tamper check, no task row, no host-provision support and no
    rollback: on revoke it printed "Rollback spec exists in artifact body" and
    executed nothing, telling the operator a rollback had happened while the host
    was untouched.
    """
    if approve:
        _lifecycle_transition("approve", id, None)
        console.print(f"[green]Approved: {id}[/green]")

    _run_artifact_task("POST", f"/artifacts/{id}/apply", {}, "Applied", id)


@artifacts_app.command("replay")
def artifacts_replay(
    id: str = typer.Argument(..., help="Artifact ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Re-apply an already-applied artifact.

    This is the command that made the shadow engine dangerous: it bypassed the
    `replay_safe: false` and replay-only guards the executor enforces and
    ARTIFACT_SPEC promises, because the engine it called had never heard of them
    (#423).
    """
    if not yes:
        typer.confirm(f"Replay artifact {id}? This will re-execute the spec.", abort=True)

    _run_artifact_task("POST", f"/artifacts/{id}/replay", None, "Replayed", id)


@artifacts_app.command("revoke")
def artifacts_revoke(
    id: str = typer.Argument(..., help="Artifact ID"),
    reason: list[str] | None = typer.Option(None, "--reason", help="Revocation reason"),  # noqa: B008
) -> None:
    """Revoke an applied artifact, running its rollback.

    The old CLI path never rolled anything back - it looked for a `## Rollback`
    heading and printed that one existed (#423).
    """
    reason_str = " ".join(reason) if reason else None
    # No `user`: the backend records the authenticated credential (#681).
    body: dict[str, Any] = {}
    if reason_str:
        body["reason"] = reason_str
    _run_artifact_task("DELETE", f"/artifacts/{id}", body, "Revoked", id)


@artifacts_app.command("push")
def artifacts_push(
    remote: str = typer.Option("origin", "--remote", "-r", help="Git remote name"),
) -> None:
    """Push artifacts to remote git repository."""
    from homepilot.artifacts.store import GitOperationError

    store = _get_artifact_store()
    try:
        output = store.push(remote=remote)
        console.print(f"[green]Pushed to {remote}[/green]")
        if output.strip():
            console.print(output.strip())
    except GitOperationError as e:
        err_console.print(f"[red]Push failed: {e}[/red]")
        raise typer.Exit(1) from e


@artifacts_app.command("pull")
def artifacts_pull(
    remote: str = typer.Option("origin", "--remote", "-r", help="Git remote name"),
) -> None:
    """Pull artifacts from remote git repository (fast-forward only)."""
    from homepilot.artifacts.store import GitOperationError

    store = _get_artifact_store()
    try:
        output = store.pull(remote=remote)
        console.print(f"[green]Pulled from {remote}[/green]")
        if output.strip():
            console.print(output.strip())
    except GitOperationError as e:
        err_console.print(f"[red]Pull failed: {e}[/red]")
        raise typer.Exit(1) from e


@artifacts_app.command("sync-status")
def artifacts_sync_status() -> None:
    """Show git status and recent log for the artifacts repository."""
    from homepilot.artifacts.store import GitOperationError

    store = _get_artifact_store()
    try:
        info = store.sync_status()
    except GitOperationError as e:
        err_console.print(f"[red]Status failed: {e}[/red]")
        raise typer.Exit(1) from e

    if info["status"].strip():
        console.print("[bold]Uncommitted changes:[/bold]")
        console.print(info["status"])
    else:
        console.print("[green]Working tree clean[/green]")

    if info["log"].strip():
        console.print("[bold]Recent commits:[/bold]")
        console.print(info["log"])


def _drift_rows(
    artifacts: list[dict[str, Any]], checks: list[dict[str, Any]]
) -> list[tuple[str, str, str, str, str]]:
    """(id, kind, target, state, checked) per active artifact, from the stored checks.

    The state is what the last check ESTABLISHED (#425/#712): `drifted`,
    `in spec`, `unknown` (the check ran and could not tell, with its reason) or
    `never checked`. Nothing here is inferred from the artifact alone.
    """
    by_id = {str(c.get("artifact_id")): c for c in checks}
    rows: list[tuple[str, str, str, str, str]] = []
    for a in artifacts:
        target = a.get("target") or {}
        target_str = str(
            target.get("host") or target.get("service") or target.get("node") or "(global)"
        )
        check = by_id.get(str(a.get("id", "")))
        if check is None:
            state, checked = "never checked", "-"
        else:
            raw = check.get("state")
            checked = str(check.get("checked_at") or "-")
            if raw == "drifted":
                state = "drifted"
            elif raw == "in_spec":
                state = "in spec"
            else:
                reason = ""
                with contextlib.suppress(TypeError, ValueError, AttributeError):
                    reason = str(
                        (json.loads(check.get("details_json") or "{}") or {}).get("reason") or ""
                    )
                state = f"unknown ({reason.replace('_', ' ')})" if reason else "unknown"
        rows.append((str(a.get("id", "")), str(a.get("kind", "")), target_str, state, checked))
    return rows


@app.command()
def drift(
    refresh: bool = typer.Option(
        False, "--refresh", help="Run a drift check of every active artifact first"
    ),
) -> None:
    """Show what the last drift check established for each active artifact.

    It used to print the applied/approved artifacts under the title "Drift:
    Artifact Pile vs Inventory" while comparing nothing (#704). The checks live
    in the backend (the drift reconciler and its results), so this asks it: the
    state per artifact is the stored verdict - drifted, in spec, unknown (with
    why), or never checked - and `--refresh` runs a check first.
    """

    unread = 0

    async def _read() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nonlocal unread
        async with _backend_session("read") as call:
            artifacts: list[dict[str, Any]] = []
            for status in ("applied", "approved"):
                page = await call("GET", f"/artifacts?status={status}&limit=1000") or {}
                items = page.get("items", [])
                artifacts.extend(items)
                unread += max(0, int(page.get("total", len(items))) - len(items))
            query = "refresh=true&limit=1000" if refresh else "limit=1000"
            checks = await call("GET", f"/artifacts/drift?{query}", timeout=600.0)
            return artifacts, (checks or {}).get("items", [])

    try:
        artifacts, checks = asyncio.run(_read())
    except Exception as exc:
        err_console.print(f"[red]Could not read drift from the backend: {exc}[/red]")
        raise typer.Exit(1) from None

    if not artifacts:
        console.print("[dim]No applied or approved artifacts, so there is nothing to check.[/dim]")
        return

    rows = _drift_rows(artifacts, checks)
    table = Table(title="Drift: last check per active artifact")
    table.add_column("Artifact ID", style="cyan")
    table.add_column("Kind")
    table.add_column("Target", max_width=20)
    table.add_column("State")
    table.add_column("Checked")
    for row in rows:
        table.add_row(*row)
    console.print(table)
    drifted = sum(1 for r in rows if r[3] == "drifted")
    unchecked = sum(1 for r in rows if r[3] == "never checked")
    console.print(
        f"{drifted} drifted, {unchecked} never checked, of {len(rows)} active artifact(s)."
    )
    if unread:
        console.print(f"[yellow]{unread} more active artifact(s) were not read.[/yellow]")
    if drifted:
        raise typer.Exit(2)


@app.command()
def doc(
    target: str = typer.Argument(..., help="Host, service, or network name"),
) -> None:
    """Render environment doc in terminal."""
    from homepilot.mcp.server import _bootstrap, _handle_tool

    async def _render() -> Any:
        ctx = await _bootstrap()
        result = await _handle_tool("get_environment_doc", {"target": target}, ctx)
        if ctx.get("proxmox"):
            await ctx["proxmox"].close()
        if ctx.get("database"):
            await ctx["database"].close()
        return result

    results = asyncio.run(_render())
    for r in results:
        console.print(r.text)


# ── Backup / restore (#421) ────────────────────────────────────────────────
# Bumped only when the tarball LAYOUT changes such that an older build can no
# longer read it. Import refuses anything newer than this.
EXPORT_MANIFEST_SCHEMA_VERSION = 1

MANIFEST_NAME = "manifest.json"

# Secret material, relative to the data dir, archived under `secrets/` in the
# tarball. Without every one of these a restored host can decrypt nothing it
# used to own: master.protected is the vault identity, the passphrase sources
# unwrap it, and the rest are the credentials the backend hands out.
#
# `hub/` (the agent hub's certificate and key) and `.agent_hub_token` are the
# FLEET's identity, and were missing (#722). Every agent pins the hub
# certificate by fingerprint (HP_AGENT_TLS_PIN, the installer default) and
# authenticates with the hub token; a restore without them mints a new cert and
# token on first boot, every agent refuses the new hub, and an env-pinned agent
# cannot be re-pinned remotely - the whole fleet needs re-enrolling by hand.
SECRET_PATHS: tuple[str, ...] = (
    ".env",
    ".vault_passphrase",
    "api-token",
    "vault/identities",
    "vault/secrets",
    "ssh",
    "hub",
    ".agent_hub_token",
)

# The archive entry a passphrase harvested from the ENVIRONMENT is written to.
# It is the same name the auto-generated shape uses, so a restored host loads it
# with no further configuration.
PASSPHRASE_REL = ".vault_passphrase"  # nosec B105 - a FILE NAME, not a credential

# The vault identity. An archive holding this and no passphrase source is the
# trap this pair of constants exists to detect: it looks like a full backup,
# `includes_secrets` is true, and nothing in it can be decrypted.
IDENTITY_REL = "vault/identities"


def _present_secret_paths(data_dir: Path) -> list[str]:
    return [rel for rel in SECRET_PATHS if (data_dir / rel).exists()]


def _resolved_passphrase(settings: Any) -> str:
    """The passphrase actually in force, wherever it came from.

    `.vault_passphrase` under the data dir is only ONE of the three shapes
    `docs/vault.md` documents. `hp init` writes `HP_VAULT_PASSPHRASE` into a
    `.env` beside docker-compose.yml, and `HP_VAULT_PASSPHRASE_FILE` points
    wherever the operator chose - neither is inside the data dir, so neither was
    ever archived. On the compose deployment `docs/deployment.md` describes,
    that made `hp export --include-secrets` produce an archive of encrypted
    material with no key: the restore then boots into
    `Failed to decrypt identity (wrong passphrase?)`, and every vault secret is
    gone for good. Verified live on dev 3.6.15, where the archive's manifest
    listed exactly `vault/identities` and `vault/secrets`.
    """
    return str(getattr(settings, "vault_passphrase", "") or "")


def _passphrase_opens_identity(data_dir: Path, passphrase: str) -> bool:
    """Whether `passphrase` actually unwraps this data dir's vault identity.

    ESTABLISHED, not assumed - the whole point. `Settings` auto-generates a
    passphrase when none is configured, so "there is a passphrase" is not the
    same claim as "this passphrase opens this vault": an export that archived
    the auto-generated one beside somebody else's identity would ship a key that
    fits nothing and say `includes_vault_passphrase: true` about it.

    Only ever called when `master.protected` already exists, because
    `ensure_master_identity()` CREATES one when it does not - a check that
    quietly writes a new vault identity would be worse than no check.
    """
    if not passphrase:
        return False
    from homepilot.vault import VaultManager

    try:
        asyncio.run(VaultManager(data_dir, passphrase).ensure_master_identity())
    except Exception:
        return False
    return True


def _print_no_secrets_warning(missing: list[str]) -> None:
    """Loud, unmissable: a default tarball is not a restorable host backup."""
    err_console.print("[bold yellow]WARNING: no secrets in this backup.[/bold yellow]")
    err_console.print("[yellow]This tarball CANNOT restore a working host.[/yellow]")
    err_console.print("[yellow]Not included:[/yellow]")
    for rel in missing:
        err_console.print(f"[yellow]  - {rel}[/yellow]")
    err_console.print("[yellow]Without them every vault secret stays[/yellow]")
    err_console.print("[yellow]undecryptable: pve-token, admin-secret,[/yellow]")
    err_console.print("[yellow]webhook secrets.[/yellow]")
    err_console.print("[yellow]Re-run with --include-secrets for a[/yellow]")
    err_console.print("[yellow]restorable backup.[/yellow]")


def _print_secrets_banner(tarball_path: Path, has_passphrase: bool) -> None:
    """Say what this archive HOLDS, not what the flag was called.

    The wording used to assert "the vault identity and passphrase" from the flag
    alone. On any install whose passphrase lives outside the data dir that was
    simply false, and it was the reassuring half of a two-part trap: a red
    banner claiming a full backup, over an archive that cannot decrypt itself.
    """
    err_console.print("[bold red]DANGER: this tarball CONTAINS SECRETS.[/bold red]")
    if has_passphrase:
        err_console.print("[red]It holds the vault identity AND the[/red]")
        err_console.print("[red]passphrase that unwraps it, so anyone who[/red]")
        err_console.print("[red]reads it can decrypt every secret[/red]")
        err_console.print("[red]HomePilot holds.[/red]")
    else:
        err_console.print("[red]It holds the vault identity and the[/red]")
        err_console.print("[red]encrypted secrets.[/red]")
    err_console.print(f"[red]Treat {tarball_path.name} as a credential:[/red]")
    err_console.print("[red]encrypt it at rest, never commit it,[/red]")
    err_console.print("[red]delete it once restored.[/red]")


def _print_no_passphrase_warning(mismatch: bool) -> None:
    """The archive has the vault but not a key that opens it. Say so, now.

    Silence here is how a "full" backup becomes unrecoverable: the operator
    finds out at restore time, on a host whose original passphrase is gone.
    """
    err_console.print("[bold yellow]WARNING: no usable vault passphrase in[/bold yellow]")
    err_console.print("[bold yellow]this backup.[/bold yellow]")
    if mismatch:
        err_console.print("[yellow]The passphrase this host is configured[/yellow]")
        err_console.print("[yellow]with does NOT open[/yellow]")
        err_console.print("[yellow]vault/identities/master.protected, so it[/yellow]")
        err_console.print("[yellow]was not archived as one.[/yellow]")
    else:
        err_console.print("[yellow]It holds vault/identities and[/yellow]")
        err_console.print("[yellow]vault/secrets but nothing that can unwrap[/yellow]")
        err_console.print("[yellow]them: neither HP_VAULT_PASSPHRASE,[/yellow]")
        err_console.print("[yellow]HP_VAULT_PASSPHRASE_FILE nor[/yellow]")
        err_console.print("[yellow]<data_dir>/.vault_passphrase is set.[/yellow]")
    err_console.print("[yellow]Restored elsewhere, every secret in it stays[/yellow]")
    err_console.print("[yellow]undecryptable FOREVER - pve-token,[/yellow]")
    err_console.print("[yellow]admin-secret, webhook secrets - and the[/yellow]")
    err_console.print("[yellow]backend starts with the vault locked.[/yellow]")
    err_console.print("[yellow]Back the passphrase up separately.[/yellow]")


def _export_readme(includes_secrets: bool, has_passphrase: bool = False) -> str:
    if not includes_secrets:
        secrets_section = "- (no `secrets/` - this archive cannot restore a working host)\n"
    elif has_passphrase:
        secrets_section = (
            "- `secrets/` - vault identity, vault secrets, key material AND the\n"
            "  vault passphrase (`secrets/.vault_passphrase`)\n"
        )
    else:
        secrets_section = (
            "- `secrets/` - vault identity and vault secrets, but NO passphrase:\n"
            "  restored on another host nothing in the vault can be decrypted\n"
        )
    return (
        "# HomePilot Export\n\n"
        f"Generated: {datetime.now(UTC).isoformat()}\n\n"
        "## Contents\n\n"
        "- `manifest.json` - archive schema, versions, whether secrets are present\n"
        "- `artifacts/` - Git repo of artifact Markdown files\n"
        "- `homepilot.db` - SQLite snapshot taken with VACUUM INTO (no -wal/-shm)\n"
        f"{secrets_section}"
        "\n## Restore\n\n"
        "Stop the backend, then `hp import <tarball>`\n"
        "(add `--restore-secrets` to put the vault back).\n\n"
        "## How to read without HomePilot\n\n"
        "- Artifacts: Markdown files in `artifacts/YYYY/MM/<id>.md`\n"
        '- Database: `sqlite3 homepilot.db "SELECT * FROM hosts;"`\n'
        "- Embeddings are not exported: run `hp kb reindex` after a restore\n"
    )


@app.command()
def export(
    include_secrets: bool = typer.Option(
        False,
        "--include-secrets",
        help="Include vault identity, vault secrets, key material and the agent "
        "hub's certificate, key and token. The tarball can then decrypt "
        "everything - guard it accordingly.",
    ),
    output: Path | None = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Destination file, or a directory to write the tarball into.",
    ),
) -> None:
    """Produce a restorable tarball (DB snapshot + artifacts + manifest)."""
    from homepilot.db.backup import SnapshotError, read_schema_version, snapshot_database
    from homepilot.db.migrations import MIGRATIONS

    settings = _get_settings()
    data_dir = Path(settings.data_dir)
    artifacts_dir = Path(settings.artifacts_dir)
    db_path = data_dir / "homepilot.db"

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    tarball_name = f"homepilot-export-{ts}.tar.gz"
    if output is None:
        tarball_path = Path.cwd() / tarball_name
    elif output.is_dir():
        tarball_path = output / tarball_name
    else:
        tarball_path = output
    tarball_path.parent.mkdir(parents=True, exist_ok=True)

    secret_rels = _present_secret_paths(data_dir) if include_secrets else []
    if include_secrets and not secret_rels:
        err_console.print("[yellow]--include-secrets: no secret files found in[/yellow]")
        err_console.print(f"[yellow]{data_dir}[/yellow]")

    # Does this archive carry something that OPENS the vault it carries? Not
    # "was --include-secrets passed", which is what the old banner answered.
    # The passphrase is harvested from wherever the operator put it, so
    # `--include-secrets` means what `docs/deployment.md` says on all three
    # documented shapes and not only the auto-generated one - and it is
    # VERIFIED against the identity before the archive claims to hold it.
    harvested_passphrase = ""
    passphrase_ok = True
    identity_present = include_secrets and (data_dir / IDENTITY_REL / "master.protected").exists()
    if identity_present:
        # The IN-FORCE passphrase first, in `config.py`'s own resolution order
        # (env, passphrase file, then `<data_dir>/.vault_passphrase`), because
        # that is the one the running backend opens the vault with. Preferring
        # the data-dir FILE would archive a stale auto-generated passphrase over
        # a working environment one - seen on a dev instance, where the export
        # then correctly but uselessly reported "no usable vault passphrase"
        # about a vault it was, at that moment, decrypting.
        candidates = [_resolved_passphrase(settings)]
        if PASSPHRASE_REL in secret_rels:
            candidates.append((data_dir / PASSPHRASE_REL).read_text(encoding="utf-8").strip())
        working = next(
            (c for c in candidates if c and _passphrase_opens_identity(data_dir, c)),
            "",
        )
        passphrase_ok = bool(working)
        if passphrase_ok:
            # Always staged, even when a `.vault_passphrase` exists: the archive
            # must carry the one that WORKS, not whichever file is lying there.
            harvested_passphrase = working
            if PASSPHRASE_REL not in secret_rels:
                secret_rels = [*secret_rels, PASSPHRASE_REL]
        elif PASSPHRASE_REL in secret_rels:
            # Archiving a key that fits nothing, under a manifest flag saying it
            # does, is worse than shipping no key at all.
            secret_rels = [rel for rel in secret_rels if rel != PASSPHRASE_REL]

    with tempfile.TemporaryDirectory(prefix="hp-export-") as staging_str:
        staging = Path(staging_str)

        db_schema_version: int | None = None
        db_snapshot = staging / "homepilot.db"
        if db_path.exists():
            try:
                snapshot_database(db_path, db_snapshot)
            except SnapshotError as exc:
                err_console.print(f"[red]Database snapshot failed: {exc}[/red]", soft_wrap=True)
                raise typer.Exit(1) from exc
            db_schema_version = read_schema_version(db_snapshot)

        staged_passphrase: Path | None = None
        if harvested_passphrase:
            staged_passphrase = staging / "vault_passphrase"
            write_private(staged_passphrase, harvested_passphrase)

        contents = [MANIFEST_NAME, "README.md"]
        if db_snapshot.exists():
            contents.append("homepilot.db")
        if artifacts_dir.exists():
            contents.append("artifacts")
        if secret_rels:
            contents.append("secrets")

        # Recorded, not inferred: `hp import` refuses to call an archive
        # restorable on the strength of `includes_secrets` alone, and an
        # operator auditing old tarballs can tell which of them are keys and
        # which are ballast.
        has_passphrase = PASSPHRASE_REL in secret_rels and passphrase_ok
        manifest = {
            "manifest_schema_version": EXPORT_MANIFEST_SCHEMA_VERSION,
            "homepilot_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "includes_secrets": bool(secret_rels),
            "includes_vault_passphrase": has_passphrase,
            "secret_paths": secret_rels,
            "db_schema_version": db_schema_version,
            "build_supports_db_schema_version": max(MIGRATIONS.keys()),
            "contents": contents,
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        readme_path = staging / "README.md"
        readme_path.write_text(_export_readme(bool(secret_rels), has_passphrase), encoding="utf-8")

        # An archive with secrets in it is a credential from its first byte: it
        # is created 0600 rather than chmodded once complete (#690). fchmod also
        # tightens a pre-existing file before anything is written into it.
        if secret_rels:
            fd = os.open(str(tarball_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            tar_target = os.fdopen(fd, "wb")
        else:
            tar_target = open(str(tarball_path), "wb")  # noqa: SIM115 - closed below
        with tar_target, tarfile.open(fileobj=tar_target, mode="w:gz") as tar:
            tar.add(str(manifest_path), arcname=MANIFEST_NAME)
            tar.add(str(readme_path), arcname="README.md")
            if db_snapshot.exists():
                # Only the VACUUM INTO snapshot: -wal/-shm are never archived,
                # they would replay onto the restored file (#421).
                tar.add(str(db_snapshot), arcname="homepilot.db")
            if artifacts_dir.exists():
                tar.add(str(artifacts_dir), arcname="artifacts")
            for rel in secret_rels:
                source = (
                    staged_passphrase
                    if rel == PASSPHRASE_REL and staged_passphrase is not None
                    else data_dir / rel
                )
                tar.add(str(source), arcname=f"secrets/{rel}")

    console.print(f"[green]Exported to {tarball_path}[/green]")
    if secret_rels:
        _print_secrets_banner(tarball_path, has_passphrase)
        if identity_present and not has_passphrase:
            _print_no_passphrase_warning(mismatch=bool(_resolved_passphrase(settings)))
    else:
        # Name what this host actually holds and the archive left behind, so the
        # warning is a fact about this backup, not a generic notice.
        _print_no_secrets_warning(_present_secret_paths(data_dir) or list(SECRET_PATHS))


def _read_manifest(tar: tarfile.TarFile) -> dict[str, Any]:
    try:
        member = tar.getmember(MANIFEST_NAME)
    except KeyError as exc:
        raise ValueError(
            "no manifest.json. Tarballs produced before the backup fix hold a "
            "raw copy of a live WAL database and may be torn; refusing to "
            "restore one. Re-export from the source host."
        ) from exc
    handle = tar.extractfile(member)
    if handle is None:
        raise ValueError("manifest.json is not a regular file")
    try:
        parsed = json.loads(handle.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest.json is unreadable: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("manifest.json is not an object")
    return parsed


def _check_manifest_versions(manifest: dict[str, Any]) -> None:
    """Refuse anything this build cannot restore. No down-migrations exist."""
    from homepilot.db.migrations import MIGRATIONS

    archive_version = int(manifest.get("manifest_schema_version", 0))
    if archive_version > EXPORT_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"archive manifest schema version {archive_version} is newer than this "
            f"build supports (version {EXPORT_MANIFEST_SCHEMA_VERSION}). Restore it "
            "with the HomePilot version that produced it."
        )
    db_version = manifest.get("db_schema_version")
    supported = max(MIGRATIONS.keys())
    if db_version is not None and int(db_version) > supported:
        raise ValueError(
            f"archive database schema version {int(db_version)} is newer than this "
            f"build supports (version {supported}). No down-migrations exist: run "
            "the image that produced the archive."
        )


def _vault_opens_after_import(data_dir: Path) -> bool:
    """Whether the vault just restored can actually be OPENED on this host.

    Tried, not inferred from the manifest - the same rule that stops `/health`
    calling a directory listing an unlocked vault. An archive written before
    3.6.16 can carry a `.vault_passphrase` belonging to a different identity,
    and this host may hold an auto-generated one that opens nothing; both look
    fine in a flag and fail at boot.

    Sources in the order `config.py` resolves them: the environment, the
    passphrase file it points at, then `<data_dir>/.vault_passphrase`.
    """
    if not (data_dir / IDENTITY_REL / "master.protected").exists():
        return True
    passphrase = os.environ.get("HP_VAULT_PASSPHRASE", "").strip()
    if not passphrase:
        pass_file = os.environ.get("HP_VAULT_PASSPHRASE_FILE", "").strip()
        if pass_file and Path(pass_file).exists():
            passphrase = Path(pass_file).read_text(encoding="utf-8").strip()
    if not passphrase and (data_dir / PASSPHRASE_REL).exists():
        passphrase = (data_dir / PASSPHRASE_REL).read_text(encoding="utf-8").strip()
    return _passphrase_opens_identity(data_dir, passphrase)


def _backup_dir_for_import(data_dir: Path, ts: str) -> Path:
    return data_dir / "backups" / f"pre-import-{ts}"


def _backup_current_state(data_dir: Path, artifacts_dir: Path, db_path: Path, ts: str) -> Path:
    """Snapshot the live DB and artifacts tree before anything is overwritten.

    Fail closed: without a restorable copy the import must not start, because a
    half-restored data dir has no way back (same rule as the pre-migration
    backup).
    """
    from homepilot.db.backup import snapshot_database

    backup_dir = _backup_dir_for_import(data_dir, ts)
    backup_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        snapshot_database(db_path, backup_dir / "homepilot.db")
    if artifacts_dir.exists():
        shutil.copytree(str(artifacts_dir), str(backup_dir / "artifacts"), symlinks=True)
    return backup_dir


def _restore_tree(src: Path, dest: Path) -> None:
    """Replace `dest` (file or directory) with the staged `src`, wholesale."""
    if dest.is_dir() and not dest.is_symlink():
        shutil.rmtree(str(dest))
    elif dest.exists() or dest.is_symlink():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def _restore_secret_entry(src: Path, dest: Path, backup_dir: Path, rel: str) -> None:
    if dest.exists():
        # Never destroy live key material without a copy: an operator who
        # restores the wrong tarball still has the previous vault.
        stashed = backup_dir / "secrets" / rel
        stashed.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_dir():
            shutil.copytree(str(dest), str(stashed), symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(str(dest), str(stashed))
    # Modes are tightened on the STAGED copy, inside the private (0700) staging
    # dir, and only then moved into place (#690). Moving first and chmodding
    # after left restored keys at whatever mode the archive carried, inside the
    # data dir, until the chmod landed. A rename keeps the mode; when staging and
    # the data dir are different filesystems the move is a COPY, which creates
    # each file under the umask before copying the mode across - so the umask is
    # tightened for the move as well. (The CLI is single-threaded here.)
    if src.is_dir():
        for child in src.rglob("*"):
            os.chmod(str(child), 0o700 if child.is_dir() else 0o600)
        os.chmod(str(src), 0o700)
    else:
        os.chmod(str(src), 0o600)
    previous_umask = os.umask(0o077)
    try:
        _restore_tree(src, dest)
    finally:
        os.umask(previous_umask)


@app.command("import")
def import_backup(
    path: Path = typer.Argument(..., help="Path to homepilot-export-*.tar.gz"),  # noqa: B008
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    restore_secrets: bool = typer.Option(
        False,
        "--restore-secrets",
        help="Also restore the vault identity, vault secrets, key material and "
        "the agent hub's identity. Overwrites the live keys on this host (the old "
        "ones are backed up).",
    ),
) -> None:
    """Restore a HomePilot data dir from a tarball. Backs up current state first."""
    from homepilot.db.backup import (
        DatabaseLockedError,
        SnapshotError,
        ensure_not_locked,
        remove_wal_sidecars,
    )

    if not path.exists():
        err_console.print(f"[red]File not found: {path}[/red]")
        raise typer.Exit(1)
    if path.suffix not in (".gz", ".tgz") and not str(path).endswith(".tar.gz"):
        err_console.print("[red]Expected a .tar.gz file[/red]")
        raise typer.Exit(1)

    settings = _get_settings()
    data_dir = Path(settings.data_dir)
    artifacts_dir = Path(settings.artifacts_dir)
    db_path = data_dir / "homepilot.db"

    try:
        with tarfile.open(str(path), "r:gz") as tar:
            members = tar.getmembers()
            # Validate every member before extracting - prevent path traversal
            for member in members:
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    err_console.print(f"[red]Unsafe path in archive: {member.name}[/red]")
                    raise typer.Exit(1)
            manifest = _read_manifest(tar)
            _check_manifest_versions(manifest)
    except tarfile.TarError as exc:
        err_console.print(f"[red]Failed to read archive: {exc}[/red]")
        raise typer.Exit(1) from exc
    except ValueError as exc:
        err_console.print(f"[red]Refusing to import: {exc}[/red]", soft_wrap=True)
        raise typer.Exit(1) from exc

    archive_has_secrets = bool(manifest.get("includes_secrets"))
    if restore_secrets and not archive_has_secrets:
        err_console.print("[red]--restore-secrets: this archive has no secrets.[/red]")
        err_console.print("[red]It was exported without --include-secrets.[/red]")
        raise typer.Exit(1)

    # Fail closed BEFORE anything is touched: a live backend still writing to
    # the database would race the restore and lose whichever half it wrote.
    try:
        ensure_not_locked(db_path)
    except DatabaseLockedError as exc:
        err_console.print("[red]Refusing to import: the database is in use.[/red]")
        err_console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(1) from exc

    if not yes:
        typer.confirm(
            f"This will overwrite {artifacts_dir} and {db_path}. "
            "Current DB and artifacts will be backed up. Continue?",
            abort=True,
        )

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    try:
        backup_dir = _backup_current_state(data_dir, artifacts_dir, db_path, ts)
    except (SnapshotError, OSError) as exc:
        err_console.print(f"[red]Pre-import backup failed: {exc}[/red]", soft_wrap=True)
        err_console.print("[red]Nothing was changed.[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[dim]Current state backed up to {backup_dir}[/dim]")

    with tempfile.TemporaryDirectory(prefix="hp-import-") as staging_str:
        staging = Path(staging_str)
        try:
            with tarfile.open(str(path), "r:gz") as tar:
                tar.extractall(str(staging), filter="data")
        except tarfile.TarError as exc:
            err_console.print(f"[red]Failed to read archive: {exc}[/red]")
            raise typer.Exit(1) from exc

        staged_db = staging / "homepilot.db"
        if staged_db.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            # Sidecars go FIRST, before the file is replaced. A stale -wal/-shm
            # beside a replaced database is replayed onto it by SQLite - the
            # restored data corrupted by the journal of the database it just
            # replaced (#421). Clearing them afterwards leaves a window in which
            # a crash, an OOM kill or a Ctrl-C lands exactly in that state; the
            # old database has already been snapshotted into `backup_dir` above,
            # so nothing is lost by dropping its journal first.
            for sidecar in remove_wal_sidecars(db_path):
                console.print(f"[dim]Removed stale {sidecar.name}[/dim]")
            _restore_tree(staged_db, db_path)
            console.print(f"[green]Database restored to {db_path}[/green]")
        else:
            console.print("[yellow]No homepilot.db in archive - skipped[/yellow]")

        staged_artifacts = staging / "artifacts"
        if staged_artifacts.exists():
            _restore_tree(staged_artifacts, artifacts_dir)
            console.print(f"[green]Artifacts restored to {artifacts_dir}[/green]")

        staged_secrets = staging / "secrets"
        if restore_secrets and staged_secrets.exists():
            for rel in manifest.get("secret_paths", SECRET_PATHS):
                src = staged_secrets / rel
                if src.exists():
                    _restore_secret_entry(src, data_dir / rel, backup_dir, rel)
            console.print("[green]Vault and key material restored[/green]")

    if db_path.exists():

        async def _migrate() -> int:
            database = await _open_cli_db("hp import", db_path)
            try:
                row = await database.fetchone(
                    "SELECT value FROM settings WHERE key = 'schema_version'"
                )
                return int(row["value"]) if row else 0
            finally:
                await database.close()

        try:
            version = asyncio.run(_migrate())
        except Exception as exc:
            err_console.print(f"[red]Migrations after restore failed: {exc}[/red]", soft_wrap=True)
            err_console.print(f"[red]Pre-import backup is at {backup_dir}[/red]")
            raise typer.Exit(1) from exc
        console.print(f"[dim]Schema migrated to version {version}[/dim]")

    console.print("[bold green]Import complete.[/bold green]")
    if restore_secrets and not _vault_opens_after_import(data_dir):
        # The one outcome an operator must never learn from a crash loop. A
        # restore that puts the identity back without a key leaves a host that
        # cannot start: `create_app_state` fails on
        # `Failed to decrypt identity (wrong passphrase?)`. Reproduced end to
        # end on 3.6.15 - export --include-secrets, import --restore-secrets,
        # container exits 3 - with "Import complete." as the only thing said
        # about it.
        err_console.print("[bold red]The restored vault CANNOT BE OPENED.[/bold red]")
        err_console.print("[red]The identity is back, but no passphrase on[/red]")
        err_console.print("[red]this host unwraps it - not the environment,[/red]")
        err_console.print("[red]not the archive, not .vault_passphrase.[/red]")
        err_console.print("[red]Set HP_VAULT_PASSPHRASE (or[/red]")
        err_console.print("[red]HP_VAULT_PASSPHRASE_FILE) to the passphrase[/red]")
        err_console.print("[red]from the SOURCE host before starting the[/red]")
        err_console.print("[red]backend. Until then every vault secret -[/red]")
        err_console.print("[red]pve-token, admin-secret, webhook secrets -[/red]")
        err_console.print("[red]stays undecryptable.[/red]")
    if not archive_has_secrets:
        err_console.print("[bold yellow]No secrets in this archive.[/bold yellow]")
        err_console.print("[yellow]The vault on this host is unchanged; if[/yellow]")
        err_console.print("[yellow]this is a fresh host the restored data[/yellow]")
        err_console.print("[yellow]references secrets it cannot decrypt.[/yellow]")
    elif not restore_secrets:
        err_console.print("[yellow]Archive contains secrets; they were NOT[/yellow]")
        err_console.print("[yellow]restored. Pass --restore-secrets to[/yellow]")
        err_console.print("[yellow]replace the vault on this host.[/yellow]")


@db_app.command("restore")
def db_restore(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="A .db file written by HomePilot (backups/pre-migration-v<N>.db)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Put a single database backup back in place, safely.

    The operation `run_migrations` recommends when a database is newer than the
    build, and the one that had no supported form. The product WRITES
    `backups/pre-migration-v<N>.db`, its own refusal names that file, and
    `docs/deployment.md` documented restoring only tarballs - so the remaining
    step was a `cp`, which destroys the database: SQLite replays the previous
    database's `-wal` onto the file that just replaced it and the result is
    `database disk image is malformed`. That happened on dev on 2026-08-29, to
    the live database and the backup at once.

    Four things `cp` cannot do, and all four are the point:
      * refuse while a backend holds the database (the same exclusive-lock
        check `hp import` makes);
      * verify the SOURCE before installing it - a corrupt backup laid over a
        working database loses both copies;
      * clear the `-wal`/`-shm` that would otherwise be replayed;
      * snapshot what it is about to overwrite, and migrate afterwards.
    """
    from homepilot.db.backup import (
        DatabaseLockedError,
        SnapshotError,
        ensure_not_locked,
        integrity_problems,
        read_schema_version,
        remove_wal_sidecars,
        snapshot_database,
        wal_sidecars,
    )
    from homepilot.db.migrations import MIGRATIONS

    settings = _get_settings()
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "homepilot.db"

    if not path.exists() or not path.is_file():
        err_console.print(f"[red]No such database file: {path}[/red]")
        raise typer.Exit(1)
    if path.resolve() == db_path.resolve():
        err_console.print("[red]That is the live database, not a backup.[/red]")
        raise typer.Exit(1)

    try:
        ensure_not_locked(db_path)
    except DatabaseLockedError as exc:
        err_console.print("[red]Refusing to restore: the database is in use.[/red]")
        err_console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(1) from exc

    # Fail closed on the SOURCE first: everything after this point overwrites
    # the only other copy of the data.
    problems = integrity_problems(path)
    if problems:
        err_console.print(f"[red]Refusing to restore: {path} is not a sound[/red]")
        err_console.print("[red]SQLite database.[/red]")
        for problem in problems[:5]:
            err_console.print(f"[red]  {problem}[/red]", soft_wrap=True)
        err_console.print("[red]Nothing was changed.[/red]")
        raise typer.Exit(1)

    source_version = read_schema_version(path)
    supported = max(MIGRATIONS.keys())
    if source_version > supported:
        err_console.print(
            f"[red]Refusing to restore: {path} is at schema version {source_version}, "
            f"newer than this build supports ({supported}). No down-migrations exist - "
            "run the image that wrote it.[/red]",
            soft_wrap=True,
        )
        raise typer.Exit(1)

    live_version = read_schema_version(db_path) if db_path.exists() else 0
    if not yes:
        typer.confirm(
            f"Replace {db_path} (schema v{live_version}) with {path} (schema "
            f"v{source_version})? Everything written since that snapshot is lost. "
            "The current database is backed up first. Continue?",
            abort=True,
        )

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / "backups" / f"pre-restore-{ts}"
    if db_path.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_database(db_path, backup_dir / "homepilot.db")
            console.print(f"[dim]Current database backed up to {backup_dir}[/dim]")
        except (SnapshotError, OSError) as exc:
            # The database being replaced is usually the REASON for the restore,
            # and a corrupt one cannot be snapshotted at all: VACUUM INTO reads
            # every page. Refusing here made this command useless in the case it
            # exists for - found by running it against a dev database that was
            # genuinely `database disk image is malformed`.
            #
            # So keep the bytes instead. A raw copy of a corrupt database is
            # evidence, not a restorable backup, and it is saved under names
            # SQLite will never pick up as a journal so that the directory
            # cannot repeat the very accident being recovered from.
            try:
                shutil.copyfile(str(db_path), str(backup_dir / "homepilot.db.corrupt"))
                for sidecar in wal_sidecars(db_path):
                    if sidecar.exists():
                        shutil.copyfile(str(sidecar), str(backup_dir / f"{sidecar.name}.saved"))
            except OSError as copy_exc:
                err_console.print(f"[red]Pre-restore backup failed: {exc}[/red]", soft_wrap=True)
                err_console.print(
                    f"[red]and the raw copy failed too: {copy_exc}[/red]", soft_wrap=True
                )
                err_console.print("[red]Nothing was changed.[/red]")
                raise typer.Exit(1) from copy_exc
            err_console.print("[yellow]The current database could not be snapshotted[/yellow]")
            err_console.print(f"[yellow]({exc}).[/yellow]", soft_wrap=True)
            err_console.print("[yellow]Its raw bytes were kept for forensics as[/yellow]")
            err_console.print(f"[yellow]{backup_dir}/homepilot.db.corrupt[/yellow]")
            err_console.print("[yellow]That copy is EVIDENCE, not a backup: it[/yellow]")
            err_console.print("[yellow]cannot be restored. Continuing.[/yellow]")

    # Sidecars first, then the file: never leave a new database beside an old
    # journal, not even for the length of one copy.
    for sidecar in remove_wal_sidecars(db_path):
        console.print(f"[dim]Removed stale {sidecar.name}[/dim]")
    shutil.copyfile(str(path), str(db_path))
    console.print(f"[green]Restored {path} to {db_path}[/green]")

    async def _migrate() -> int:
        database = await _open_cli_db("hp db restore", db_path)
        try:
            row = await database.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
            return int(row["value"]) if row else 0
        finally:
            await database.close()

    try:
        version = asyncio.run(_migrate())
    except Exception as exc:
        err_console.print(f"[red]Migrations after restore failed: {exc}[/red]", soft_wrap=True)
        err_console.print(f"[red]The previous database is at {backup_dir}[/red]")
        raise typer.Exit(1) from exc

    remaining = integrity_problems(db_path)
    if remaining:
        # The restored file passed its check and the migration ran, so this
        # should be unreachable - which is exactly why it is worth saying out
        # loud rather than assuming.
        err_console.print("[red]The restored database does not pass its[/red]")
        err_console.print("[red]integrity check:[/red]")
        for problem in remaining[:5]:
            err_console.print(f"[red]  {problem}[/red]", soft_wrap=True)
        err_console.print(f"[red]The previous database is at {backup_dir}[/red]")
        raise typer.Exit(1)

    console.print(f"[dim]Schema migrated to version {version}[/dim]")
    console.print("[bold green]Restore complete.[/bold green]")


@db_app.command("check")
def db_check() -> None:
    """Verify the database file is readable and sound.

    The product had no way to ask this. `/health` answered `database: ok` from
    `SELECT 1`, which never touches a page, so a database corrupted under a
    running backend reported healthy while real reads returned 500 (#642).
    """
    from homepilot.db.backup import integrity_problems, read_schema_version

    settings = _get_settings()
    db_path = Path(settings.data_dir) / "homepilot.db"
    if not db_path.exists():
        err_console.print(f"[red]No database at {db_path}[/red]")
        raise typer.Exit(1)

    problems = integrity_problems(db_path, limit=20)
    if problems:
        err_console.print(f"[bold red]{db_path} is CORRUPT.[/bold red]")
        for problem in problems:
            err_console.print(f"[red]  {problem}[/red]", soft_wrap=True)
        err_console.print("[red]Restore a backup: hp db restore <file>[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{db_path} passes PRAGMA quick_check[/green]")
    console.print(f"[dim]Schema version {read_schema_version(db_path)}[/dim]")


async def _index_proposed_notes(artifact_ids: list[str]) -> tuple[int, list[str]]:
    """Put freshly proposed kb-notes into the knowledge base, from the CLI.

    The CLI's lifecycle has no KB service attached, so a note proposed here was
    written to disk and to the artifacts table and left OUT of `doc_metadata`
    until a running backend happened to rebuild. This closes that gap for the
    one command whose entire purpose is to seed the KB.
    """
    if not artifact_ids:
        return 0, []
    from homepilot.db.repository import Repository
    from homepilot.executor import kb_note

    settings = _get_settings()
    db_path = Path(settings.data_dir) / "homepilot.db"
    if not db_path.exists():
        return 0, [f"no database at {db_path}"]
    store = _get_artifact_store()
    database = await _open_cli_db("hp policy init", db_path)
    indexed = 0
    problems: list[str] = []
    try:
        repo = Repository(database)
        for artifact_id in artifact_ids:
            try:
                fm, body = store.read(artifact_id)
                result = await kb_note.execute(fm, body, repo)
                if result.get("success"):
                    indexed += 1
                else:
                    problems.append(f"{artifact_id}: {result.get('failure_reason', 'failed')}")
            except Exception as exc:
                problems.append(f"{artifact_id}: {exc}")
    finally:
        await database.close()
    return indexed, problems


@policy_app.command("init")
def policy_init() -> None:
    """Interactive ~20 question onboarding to seed policy KB."""
    console.print("[bold]HomePilot Policy Onboarding[/bold]\n")

    questions = [
        ("Preferred VM template (e.g. debian-13-standard)", "template"),
        ("Preferred LXC template", "lxc_template"),
        ("Default storage pool (e.g. nvme-pool)", "storage_pool"),
        ("Default memory for VMs (MB)", "vm_memory"),
        ("Default memory for LXCs (MB)", "lxc_memory"),
        ("Default CPU cores for VMs", "vm_cores"),
        ("Default CPU cores for LXCs", "lxc_cores"),
        ("Default network bridge (e.g. vmbr0)", "network_bridge"),
        ("Reverse proxy (caddy / traefik / nginx)", "reverse_proxy"),
        ("Identity provider (authentik / keycloak / none)", "idp"),
        ("SSO group naming convention", "sso_group_naming"),
        ("Snapshot policy (always / on_mutate / never)", "snapshot_policy"),
        ("Default VLAN for services", "default_vlan"),
        ("Prefer LXC over VM for stateless services? (y/n)", "prefer_lxc"),
        ("DNS provider (e.g. cloudflare, pi-hole)", "dns_provider"),
        ("Backup solution (e.g. proxmox-backup-client)", "backup_solution"),
        ("Automatic updates for guests? (y/n)", "auto_updates"),
        ("Notification method (e.g. smtp, gotify, none)", "notifications"),
        ("Naming convention for hosts (e.g. service-lxc, service.service)", "naming_convention"),
    ]

    specs: list[tuple[str, dict[str, Any]]] = []
    for question_text, key in questions:
        answer = typer.prompt(question_text, default="")
        if not answer:
            continue

        date_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
        fact_id = f"{date_prefix}-kb-policy-{key}"
        specs.append(
            (
                key,
                {
                    "id": fact_id,
                    "kind": "kb-note",
                    "intent": f"policy: {key}",
                    "body": f"# Policy: {key}\n\n{answer}",
                    "note_kind": "policy",
                    "target": {"kind": "global"},
                    "produced_by": {"session": "policy-init", "agent": "cli", "user": "cli"},
                },
            )
        )

    written = 0
    failed: list[str] = []
    proposed_ids: list[str] = []
    backend_running = _server_is_running(_get_settings())

    async def _propose_via_api() -> None:
        # The backend proposes AND indexes each note (its lifecycle carries the
        # KB service), with the DB row and audit entry a file-only propose
        # skipped. The direct path wrote the file behind a running backend and
        # was then refused the index (#700).
        nonlocal written
        async with _backend_session("read,write") as call:
            for key, spec in specs:
                try:
                    await call("POST", "/artifacts", spec)
                    written += 1
                except BackendRefusedError as exc:
                    failed.append(f"{key}: {exc.detail}")

    async def _propose_directly() -> None:
        nonlocal written
        async with _lifecycle_with_repo("hp policy init") as lifecycle:
            for key, spec in specs:
                try:
                    await lifecycle.propose(spec)
                    written += 1
                    proposed_ids.append(spec["id"])
                except Exception as exc:
                    failed.append(f"{key}: {exc}")

    if specs:
        try:
            asyncio.run(_propose_via_api() if backend_running else _propose_directly())
        except typer.Exit:
            raise
        except Exception as exc:
            err_console.print(f"[red]Could not record policies: {exc}[/red]")
            raise typer.Exit(code=1) from None

    # Every propose failure used to be swallowed by `contextlib.suppress` and
    # the command printed the same green success line, so an operator who
    # answered nineteen questions into a broken store was told their policies
    # were in the KB (#648 tranche 6).
    if failed:
        err_console.print(f"\n[red]{len(failed)} policy note(s) could not be written:[/red]")
        for line in failed:
            err_console.print(f"  [red]{line}[/red]")
    if not written:
        err_console.print("[red]No policies were recorded.[/red]")
        raise typer.Exit(code=1)
    console.print(f"\n[green]Recorded {written} policy note(s).[/green]")
    if backend_running:
        console.print("[dim]Indexed by the backend - the agent will find them via search_kb.[/dim]")
        return

    # Index them HERE rather than promising someone else will. Writing the
    # artifact is not the same as putting it in the knowledge base, and this
    # command's closing line asserted the latter having only done the former
    # (ADR-004 corollary 4: "Automating a step beats documenting it").
    indexed, index_errors = asyncio.run(_index_proposed_notes(proposed_ids))
    if indexed:
        console.print(f"[dim]{indexed} indexed — the agent will find them via search_kb.[/dim]")
    for line in index_errors:
        err_console.print(f"[yellow]not indexed — {line}[/yellow]")
    if index_errors:
        err_console.print(
            "[yellow]Run `hp kb reindex` once the cause is fixed; until then these "
            "policies are not searchable.[/yellow]"
        )


@kb_app.command("reindex")
def kb_reindex(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    no_embeddings: bool = typer.Option(
        False, "--no-embeddings", help="Skip embedding computation; rebuild keyword metadata only"
    ),
    force_embeddings: bool = typer.Option(
        False,
        "--force-embeddings",
        help="Drop every stored vector and recompute — run this after changing the embedding model",
    ),
) -> None:
    """Re-index every applied kb-note: upsert each, drop the ones no longer applied.

    Row ids are stable across a reindex, and an unchanged note keeps the
    embedding it has. After changing the embedding MODEL that is the wrong
    answer - the stored vectors are from a different space - so pass
    `--force-embeddings` then.
    """
    if not yes:
        typer.confirm("Rebuild the KB search index from the applied notes?", abort=True)

    if not no_embeddings:
        # Judge the CONFIGURED service, not a hardcoded Ollama on localhost: with
        # no embedding service set (the default), reindexing keyword-only is the
        # correct outcome and "Ollama not reachable" would name a service this
        # install never pointed at.
        from homepilot.common import redact_endpoint

        _settings_for_embed = _get_settings()
        _embed_url = (
            _settings_for_embed.embedding_service_url or _settings_for_embed.embedding_fallback_url
        )
        if not _embed_url:
            console.print(
                "[yellow]No embedding service configured "
                "(HP_EMBEDDING_SERVICE_URL / HP_EMBEDDING_FALLBACK_URL are empty). "
                "Reindexing keyword-only.[/yellow]"
            )
            no_embeddings = True
        else:
            # Ask the service for an actual embedding: a GET liveness ping on a
            # POST-only endpoint proves nothing about whether reindexing will
            # produce vectors.
            import asyncio as _asyncio

            from homepilot.kb.service import _call_embed_service

            _embed_model = (
                _settings_for_embed.embedding_model
                if _settings_for_embed.embedding_service_url
                else _settings_for_embed.embedding_fallback_model
            )
            _probe = _asyncio.run(
                _call_embed_service(_embed_url, _embed_model, "test", timeout=5.0)
            )
            if _probe is None:
                console.print(
                    f"[yellow]Warning: embedding service at {redact_endpoint(_embed_url)} is not "
                    "answering. Reindex will fall back to keyword-only search "
                    "(no embeddings).[/yellow]"
                )
                no_embeddings = True

    async def _kb_reindex_via_api(no_embs: bool, force: bool) -> dict[str, Any] | None:
        settings = _get_settings()
        base_url = f"http://127.0.0.1:{settings.daemon_port}"
        # `/kb/reindex` requires the ADMIN SCOPE, i.e. a bearer token. This sent
        # only the admin-secret header, so every invocation 401'd and fell
        # silently through to the offline path - which is the path that wiped the
        # index (#388). Mint a short-lived admin token the same way the other
        # CLI-to-backend commands do.
        async with _ephemeral_token(settings, "cli-kb-reindex", "admin") as (token, mint_error):
            if token is None:
                if mint_error:
                    err_console.print(f"[yellow]{mint_error}[/yellow]")
                return None
            headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
            import httpx as _httpx

            # A reindex with embeddings is minutes of work on a real KB.
            async with _backend_http_client(600.0) as client:
                try:
                    resp = await client.post(
                        f"{base_url}/kb/reindex",
                        params={"no_embeddings": no_embs, "force_embeddings": force},
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        result: dict[str, Any] | None = resp.json()
                        return result
                    # Answered and refused: say so. This used to drop through
                    # to the direct path, which a running backend refuses (#700).
                    err_console.print(
                        f"[red]The backend refused the reindex ({resp.status_code}): "
                        f"{_api_error_text(resp)}[/red]"
                    )
                except _httpx.ConnectError:
                    pass  # backend down → fall back to direct DB below
                except Exception as exc:
                    # Any other failure of the call: say what it was. The
                    # caller decides whether a direct rebuild is allowed.
                    err_console.print(
                        f"[yellow]The reindex request failed: {type(exc).__name__}[/yellow]"
                    )
        return None

    async def _do(skip_embeddings: bool = False, force: bool = False) -> dict[str, Any]:
        via_api = await _kb_reindex_via_api(skip_embeddings, force)
        if via_api is not None:
            return via_api
        if _server_is_running(_get_settings()):
            # The backend holds the index; rebuilding it behind its back is
            # exactly what the direct path below would refuse to do (#700).
            err_console.print(
                "[red]The backend is running and the reindex did not go through its "
                "API (see above). Nothing was reindexed.[/red]"
            )
            raise typer.Exit(1)

        from homepilot.db.repository import Repository
        from homepilot.executor import kb_note

        settings = _get_settings()
        data_dir = Path(settings.data_dir)
        db_path = data_dir / "homepilot.db"
        if not db_path.exists():
            return {"removed": 0, "reindexed": 0, "errors": 0, "errors_detail": []}

        database = await _open_cli_db("hp kb reindex", db_path)
        try:
            repo = Repository(database)
            store = _get_artifact_store()

            # Upsert, never delete-then-rebuild. The old shape deleted every
            # artifact-backed document up front, so an artifact that could not
            # be read afterwards was simply GONE from the knowledge base - and
            # the counters said `0 errors` because `store.list` never offered
            # it (#648 tranche 6). It also reassigned every doc id.
            reindexed = 0
            errors = 0
            error_lines: list[str] = []
            seen_sources: set[str] = set()

            if force and not skip_embeddings:
                # After a model change every stored vector is from a different
                # space, and an unchanged note would otherwise keep it.
                await database.execute("DELETE FROM vec_docs")
                await database.conn.commit()

            for artifact_meta in store.list(kind="kb-note", status="applied"):
                artifact_id = artifact_meta.get("id", "")
                seen_sources.add(f"artifact:{artifact_id}")
                try:
                    fm, body = store.read(artifact_id)
                    result = await kb_note.execute(fm, body, repo, no_embeddings=skip_embeddings)
                    if result.get("success"):
                        reindexed += 1
                    else:
                        errors += 1
                        error_lines.append(
                            f"{artifact_id}: {result.get('failure_reason', 'failed')}"
                        )
                except Exception as exc:
                    errors += 1
                    error_lines.append(f"{artifact_id}: {exc}")

            # Retired, never merely unseen: `store.list` skips any file it
            # cannot open, so pruning on its absence deletes a note because we
            # could not look at it. The artifacts table is the authority.
            rows = await database.fetchall(
                "SELECT id, source FROM doc_metadata WHERE source LIKE 'artifact:%'"
            )
            removed = 0
            unverified: list[str] = []
            for row in rows:
                source = str(row["source"])
                if source in seen_sources:
                    continue
                artifact_id = source[len("artifact:") :]
                if store.exists(artifact_id):
                    try:
                        fm_check, _ = store.read(artifact_id)
                    except Exception:  # "could not look" is the answer, not a failure
                        unverified.append(artifact_id)
                        continue
                    if str(fm_check.get("status")) == "applied":
                        unverified.append(artifact_id)
                        continue
                if await repo.delete_doc_metadata(int(row["id"])):
                    removed += 1
        finally:
            await database.close()

        return {
            "removed": removed,
            "reindexed": reindexed,
            "errors": errors,
            "errors_detail": error_lines,
            "unverified": unverified,
        }

    result = asyncio.run(_do(skip_embeddings=no_embeddings, force=force_embeddings))
    errors = int(result.get("errors", 0))
    unverified_ids = list(result.get("unverified") or [])
    # The backend's verdict counts embedding failures too (#777): every insert
    # of a model whose vectors do not fit the index failed under a green line.
    failed = bool(errors) or result.get("status") == "completed_with_errors"
    headline = "[green]Reindex complete:[/green]" if not failed else "[red]Reindex FAILED for"
    console.print(
        f"{headline} "
        f"{result.get('reindexed', 0)} indexed, "
        f"{result.get('removed', 0)} removed (no longer applied), "
        f"{errors} error(s)"
    )
    # A green "complete" over a rebuild that lost documents is the line an
    # operator reads and then does not go looking.
    for line in result.get("errors_detail") or []:
        err_console.print(f"  [red]{line}[/red]")
    rebuilt = result.get("vector_index_rebuilt")
    if isinstance(rebuilt, dict):
        console.print(
            f"[yellow]Vector index rebuilt for the embedding model: {rebuilt.get('from')} -> "
            f"{rebuilt.get('to')} dimensions; {rebuilt.get('vectors_dropped')} vector(s) of "
            "the previous model dropped and re-embedded.[/yellow]"
        )
    embedding_failures = int(result.get("embedding_failures") or 0)
    if embedding_failures and result.get("embedding_first_failure"):
        err_console.print(
            f"[red]{embedding_failures} document(s) not embedded; first failure: "
            f"{result.get('embedding_first_failure')}[/red]"
        )
    if unverified_ids:
        err_console.print(
            f"[yellow]{len(unverified_ids)} document(s) kept without being able to read "
            "their artifact — still searchable, but not confirmed current:[/yellow]"
        )
        for artifact_id in unverified_ids[:10]:
            err_console.print(f"  [yellow]{artifact_id}[/yellow]")
    if failed:
        raise typer.Exit(code=1)


_TOKEN_RULE = (  # nosec B105 - a refusal message about tokens, not a credential
    "Tokens are minted by admins: create through the API with an admin token, "
    "or from Settings -> Tokens."
)


def _stored_admin_token(settings: Any) -> str:
    """The admin token this box keeps for its own CLI, if there is one.

    Autocreated and persisted, never something a human has to manage: `hp init`
    writes it, and the browser claim writes one too (the operator's own login
    token is never put on disk). File permissions are the gate, exactly as for
    the vault passphrase in .env.
    """
    try:
        path = Path(settings.data_dir) / "api-token"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return ""


def _admin_credentials(settings: Any) -> tuple[str, str]:
    """(admin_token, admin_secret) - whatever admin credential this box holds.

    Precedence for the token: HP_ADMIN_TOKEN (the operator pasting the token they
    hold), then the stored one in the data dir. The admin secret is resolved
    exactly like the backend's ``resolve_admin_secret`` (settings/env, then
    vault) - no passphrase fallback, which previously produced confusing 403s.
    An instance installed through the browser claim has NO admin secret at all,
    which is why the token is the first-class credential now.
    """
    admin_token = os.environ.get("HP_ADMIN_TOKEN", "").strip() or _stored_admin_token(settings)
    admin_secret = getattr(settings, "admin_secret", "") or ""
    if not admin_secret and hasattr(settings, "_try_vault_secret"):
        admin_secret = settings._try_vault_secret("admin-secret") or ""
    return admin_token, admin_secret


def _admin_headers(settings: Any) -> dict[str, str]:
    admin_token, admin_secret = _admin_credentials(settings)
    headers: dict[str, str] = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    if admin_secret:
        headers["x-hp-admin-secret"] = admin_secret
    return headers


# Lifetime of the credential a CLI command mints for its own calls. Long
# enough to follow the longest apply (`_TASK_WAIT_SECONDS`), short enough that
# a token the cleanup could not delete stops working on its own (#699).
_CLI_TOKEN_TTL_SECONDS = 3600


@contextlib.asynccontextmanager
async def _ephemeral_token(
    settings: Any, label: str, scope: str
) -> AsyncIterator[tuple[str | None, str | None]]:
    """Mint a short-lived token for one command and DELETE it afterwards.

    Yields ``_mint_token_via_api``'s ``(token, error)``. The delete is made with
    the ADMIN credential that minted the token, not with the token itself: a
    ``write`` token is not allowed to delete tokens, so self-deletion answered
    403 and every `hp artifacts apply` left a live write credential behind
    (#699). A delete that still fails is logged as a warning - the expiry
    bounds the damage, but the operator should know.
    """
    import httpx

    token, err = await _mint_token_via_api(
        settings, label, scope, expires_in_seconds=_CLI_TOKEN_TTL_SECONDS
    )
    try:
        yield token, err
    finally:
        if token:
            port = getattr(settings, "daemon_port", 8000)
            failure = ""
            try:
                async with _backend_http_client(5.0) as client:
                    resp = await client.delete(
                        f"http://127.0.0.1:{port}/auth/tokens/{token[:16]}",
                        headers=_admin_headers(settings),
                    )
                if resp.status_code not in (204, 404):
                    failure = f"{resp.status_code} {resp.text[:200]}"
            except httpx.HTTPError as exc:
                failure = type(exc).__name__
            if failure:
                err_console.print(
                    f"[yellow]Could not delete the temporary CLI token {token[:16]} "
                    f"({failure}); it expires on its own within "
                    f"{_CLI_TOKEN_TTL_SECONDS // 60} minutes.[/yellow]"
                )


async def _mint_token_via_api(
    settings: Any, label: str, scope: str, expires_in_seconds: int | None = None
) -> tuple[str | None, str | None]:
    """Create an API token through the running backend, as an admin.

    ``expires_in_seconds`` makes the backend store an expiry; the per-command
    credentials the CLI mints for itself always pass one (#699).

    The backend owns the DB, so this is also what avoids sqlite write-lock
    contention. Returns ``(token, error)``:
      * ``(token, None)`` — created via the API.
      * ``(None, None)``  - backend not reachable, or this box holds no admin
        credential at all; the caller decides what that means.
      * ``(None, msg)``   - backend reached and refused; caller should surface
        ``msg`` and NOT touch the DB (it is locked).
    """
    import httpx

    headers = _admin_headers(settings)
    if not headers:
        return None, None  # nothing to authenticate with - caller decides
    port = getattr(settings, "daemon_port", 8000)

    payload: dict[str, Any] = {"label": label, "scope": scope}
    if expires_in_seconds is not None:
        payload["expires_in_seconds"] = expires_in_seconds
    try:
        async with _backend_http_client(3.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/auth/tokens",
                json=payload,
                headers=headers,
            )
    except httpx.ConnectError:
        return None, None  # backend down → caller falls back to direct DB
    except Exception:
        return None, None

    if resp.status_code == 201:
        return str(resp.json()["token"]), None

    detail = ""
    try:
        detail = str(resp.json().get("detail", ""))
    except Exception:
        detail = resp.text
    # `hp init` is deliberately not offered here either: on an instance that is
    # already set up it refuses, and forcing it generates a fresh vault
    # passphrase that orphans every stored secret.
    hint = (
        " Pass an admin token in HP_ADMIN_TOKEN (the one the claim or Settings -> Tokens gave you)."
    )
    return None, f"Backend refused token creation ({resp.status_code}): {detail}.{hint}"


@token_app.command("create")
def token_create(
    label: str = typer.Option("admin", "--label", "-l", help="Display name for the token owner"),
    scope: str = typer.Option(
        "read,write",
        "--scope",
        "-s",
        help=(
            "Comma-separated scopes (e.g. read,write), or 'all' for the "
            "superuser scope '*'. ('full' is a legacy alias for 'all' - the "
            "write-tier API scope is 'write', not 'full'.)"
        ),
    ),
    output: str = typer.Option("plain", "--output", "-o", help="Output format: plain or json"),
) -> None:
    """Create an API token as an admin (the direct-DB mint is bootstrap only).

    Owner rule (2026-08-26): "it should be ok to create tokens if one is logged
    in with admin token". Minting used to be an unauthenticated direct write to
    the local DB - anyone who could run `hp` on the box could mint a fleet-root
    credential, and nothing was recorded about who did. Now the token is minted
    through POST /auth/tokens with this box's admin credential, exactly as
    `hp agent` does its work. The old path survives for the one case that cannot
    be authenticated - an instance with no live token to authenticate WITH.
    """
    # Refuse a scope the instance cannot honour BEFORE anything is minted: a
    # typo used to produce a real token that authenticates and is refused by
    # every scoped route, discovered at first use rather than here.
    from homepilot.auth.tokens import validate_scope

    try:
        validate_scope(scope)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None

    if scope.strip() in ("all", "full", "*"):
        legacy = " ('full' is the legacy name for 'all')" if scope.strip() == "full" else ""
        typer.secho(
            f"note: this mints a SUPERUSER token (scope '*', everything){legacy}. "
            "Pass --scope read,write if you meant a write-capable token. (#579)",
            fg=typer.colors.YELLOW,
            err=True,
        )

    bootstrapped = False

    async def _create() -> str:
        nonlocal bootstrapped
        from aiosqlite import OperationalError

        from homepilot.db.repository import Repository

        settings = _get_settings()

        token, err = await _mint_token_via_api(settings, label, scope)
        if token:
            return token
        if err:
            # The backend was REACHED and refused. Falling through to the
            # direct-DB path would only produce a second, less relevant refusal
            # (the schema guard) on top of the real one.
            err_console.print(f"[red]{err}[/red]")
            raise typer.Exit(1)

        # "I could not ask" and "the backend said no" are different answers, and
        # so is "there is nothing here to ask WITH". Reaching the direct-DB path
        # on a LIVE install produced the schema-guard refusal ("stop the backend
        # first"), which named neither the real obstacle nor the real fix: on an
        # instance whose first admin token came from the console rather than from
        # `hp init` or the browser claim, <data dir>/api-token does not exist and
        # this box holds no credential to mint through. Say that.
        if not _admin_headers(settings) and _server_is_running(settings):
            err_console.print(
                "[red]The backend is running, but this box holds no admin credential "
                "to mint through it.[/red]"
            )
            err_console.print(
                "[yellow]Set HP_ADMIN_TOKEN to an admin token (Settings -> Tokens "
                "shows the ones you have), or mint it there directly. The direct-DB "
                "bootstrap only runs with the backend stopped.[/yellow]"
            )
            raise typer.Exit(1)

        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        db_path = data_dir / "homepilot.db"
        database = await _open_cli_db("hp token create", db_path)
        try:
            repo = Repository(database)

            # THE RULE. An unauthenticated mint is allowed only while there is no
            # admin to mint through: zero live tokens on the instance. The moment
            # one exists, this path refuses and names the way in - otherwise the
            # authenticated path above is decoration anyone can walk around.
            live = await repo.count_live_api_tokens()
            if live > 0:
                err_console.print(
                    f"[red]Refusing to mint: {live} live token(s) exist - {_TOKEN_RULE}[/red]"
                )
                raise typer.Exit(1)

            full_token, prefix, token_hash = generate_api_token()

            existing_users = await database.fetchall("SELECT id FROM users LIMIT 1")
            if existing_users:
                user_id = existing_users[0]["id"]
            else:
                user_id = str(await repo.create_user(display_name=label, auth_source="api_token"))

            await repo.create_api_token(
                user_id=str(user_id),
                token_type="personal",
                prefix=prefix,
                hash=token_hash,
                scope=scope,
                label=label,
                expires_at=None,
            )
            await database.conn.commit()
            await _audit_cli_mint(repo, prefix, label, scope)
            bootstrapped = True
        except OperationalError as exc:
            if "database is locked" in str(exc).lower():
                err_console.print(
                    "[red]Database is locked - the backend owns it. Mint through "
                    "the running backend instead: set HP_ADMIN_TOKEN to an admin "
                    "token, or use Settings -> Tokens.[/red]"
                )
                raise typer.Exit(1) from exc
            raise
        finally:
            await database.close()
        return full_token

    token = asyncio.run(_create())

    # typer.echo, not the rich console: a token (and a JSON line) is machine
    # output and must never be line-wrapped to the terminal width.
    if output == "json":
        typer.echo(json.dumps({"token": token, "scope": scope, "bootstrap": bootstrapped}))
    else:
        typer.echo(token)
    if bootstrapped:
        err_console.print(
            f"[yellow]Bootstrap mint: this instance had no live token. {_TOKEN_RULE}[/yellow]"
        )


async def _admin_request(settings: Any, method: str, path: str) -> tuple[Any | None, str | None]:
    """Call a backend admin endpoint with whichever admin credential this box
    holds - an admin token first, the vault's admin secret otherwise. Returns
    (json_or_None, error_or_None) — backend-down and refusals both report a
    message so the CLI never fails silently."""
    import httpx

    headers = _admin_headers(settings)
    if not headers:
        # NOT "run `hp init`". On an instance that is already set up, `hp init`
        # refuses (it would generate a fresh vault passphrase and orphan every
        # stored secret), and with the backend running it now refuses earlier
        # still. Pointing the operator at it was advice that could not be
        # followed - and if forced, would have cost them the vault.
        return None, (
            "No admin credential on this box: mint one in Settings -> Tokens "
            "(scope 'admin') and export it as HP_ADMIN_TOKEN. `hp init` is the "
            "bootstrap for a NEW instance, not a way to add a credential to "
            "this one."
        )
    port = getattr(settings, "daemon_port", 8000)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.request(
                method,
                f"http://127.0.0.1:{port}{path}",
                headers=headers,
            )
    except httpx.ConnectError:
        return None, "Backend not reachable — start it first (token list/revoke need the live API)."
    except Exception as exc:
        return None, f"Request failed: {exc}"
    if resp.status_code in (200, 204):
        return (resp.json() if resp.content else None), None
    detail = ""
    try:
        detail = str(resp.json().get("detail", ""))
    except Exception:
        detail = resp.text
    return None, f"Backend refused ({resp.status_code}): {detail}"


@token_app.command("list")
def token_list(
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
) -> None:
    """List API tokens (prefix, label, scope, last-used) via the live backend."""
    settings = _get_settings()
    data, err = asyncio.run(_admin_request(settings, "GET", "/auth/tokens"))
    if err:
        err_console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)
    tokens = data if isinstance(data, list) else (data or {}).get("items", [])
    if output == "json":
        console.print(json.dumps(tokens))
        return
    if not tokens:
        console.print("No tokens.")
        return
    table = Table("Prefix", "Label", "Scope", "Last used", "Expires")
    for t in tokens:
        table.add_row(
            str(t.get("prefix", "")),
            str(t.get("label", t.get("display_name", ""))),
            str(t.get("scope", "")),
            str(t.get("last_used_at") or "never"),
            str(t.get("expires_at") or "—"),
        )
    console.print(table)


@token_app.command("revoke")
def token_revoke(
    prefix: str = typer.Argument(..., help="The token prefix (first 16 chars) to revoke"),
) -> None:
    """Revoke an API token by its prefix via the live backend."""
    settings = _get_settings()
    _, err = asyncio.run(_admin_request(settings, "DELETE", f"/auth/tokens/{prefix}"))
    if err:
        err_console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)
    console.print(f"Revoked token {prefix}.")


async def _open_invite_repo() -> tuple[Database, InviteRepository]:
    """Open the control-plane database for invite work. Returns (database, repo).

    Only for a STOPPED backend (#623, #700). While one runs, `hp invite` and
    `hp quota` go through its admin API (`_via_backend`): a second process
    writing the same SQLite file is the class `hp token create` was cured of,
    and `_open_cli_db` refuses it.
    """
    from homepilot.portal.repository import InviteRepository

    settings = _get_settings()
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # The command name is the operator's own: it used to say "hp token revoke"
    # here, so `hp invite create` refused with a message about a command the
    # operator had not run.
    database = await _open_cli_db("hp invite", data_dir / "homepilot.db")
    return database, InviteRepository(database)


@invite_app.command("create")
def invite_create(
    cn: str = typer.Option(..., "--cn", help="Client-certificate CN this invite is bound to"),
    template: int = typer.Option(..., "--template", help="Proxmox template VMID to clone"),
    node: str = typer.Option(..., "--node", help="Proxmox node to build on"),
    pool: str = typer.Option("", "--pool", help="Proxmox pool for the new guest"),
    storage: str = typer.Option(
        "", "--storage", help="Proxmox storage for the guest's disks (empty = the template's)"
    ),
    cores: int = typer.Option(0, "--cores", help="CPU cores (0 = template default)"),
    ram: int = typer.Option(0, "--ram", help="Memory in MB (0 = template default)"),
    disk: int = typer.Option(0, "--disk", help="Disk size in GB (0 = template default)"),
    disk_device: str = typer.Option("scsi0", "--disk-device", help="PVE disk to resize"),
    expires: str = typer.Option("7d", "--expires", help="Validity window, e.g. 30m / 48h / 7d"),
    base_url: str = typer.Option(
        "", "--base-url", help="Portal origin (defaults to HP_PORTAL_BASE_URL)"
    ),
) -> None:
    """Mint a one-time invite for one client certificate. Prints the URL ONCE."""
    from pydantic import ValidationError

    from homepilot.portal.models import InviteCaps
    from homepilot.portal.repository import parse_duration

    if not cn.strip():
        err_console.print("[red]--cn must not be empty[/red]")
        raise typer.Exit(1)
    try:
        ttl = parse_duration(expires)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    try:
        # Validated through the SAME model provisioning uses, so a mint can
        # never produce caps that redemption would later reject.
        caps = InviteCaps(
            template_vmid=template,
            node=node,
            pool=pool or None,
            storage=storage or None,
            cores=cores or None,
            memory_mb=ram or None,
            disk_gb=disk or None,
            disk=disk_device,
        )
    except ValidationError as exc:
        err_console.print(f"[red]Invalid caps: {format_validation_error(exc)}[/red]")
        raise typer.Exit(1) from exc

    async def _mint() -> tuple[str, str]:
        database, invites = await _open_invite_repo()
        try:
            _, full_token = await invites.create_invite(
                bound_cn=cn.strip(),
                caps=caps,
                created_by=os.environ.get("USER", "operator"),
                ttl=ttl,
            )
        finally:
            await database.close()
        return full_token, full_token[:16]

    if _server_is_running(_get_settings()):
        # The console's mint asks for an explicit size; "template default" is a
        # direct-write-only shape, and quietly inventing a size would mint a
        # different invite than the one asked for.
        sizes = (("--cores", cores), ("--ram", ram), ("--disk", disk))
        missing = [flag for flag, value in sizes if not value]
        if missing:
            err_console.print(
                "[red]The HomePilot backend is running, so `hp invite create` mints through "
                f"its API, which needs an explicit size: pass {', '.join(missing)}.[/red]"
            )
            raise typer.Exit(1)
        body: dict[str, Any] = {
            "cn": cn.strip(),
            "template_vmid": template,
            "node": node,
            "cores": cores,
            "memory_mb": ram,
            "disk_gb": disk,
            "disk": disk_device,
            "ttl_minutes": int(ttl.total_seconds() // 60),
        }
        if pool:
            body["pool"] = pool
        if storage:
            body["storage"] = storage
        minted = _via_backend("hp invite create", "POST", "/admin/guests/invites", body)
        token = str((minted or {}).get("token") or "")
        if not token:
            err_console.print("[red]The backend answered the mint without a token.[/red]")
            raise typer.Exit(1)
        prefix = token[:16]
    else:
        token, prefix = asyncio.run(_mint())
    origin = (base_url or getattr(_get_settings(), "portal_base_url", "")).rstrip("/")
    url = f"{origin}/invite/{token}" if origin else f"/invite/{token}"
    console.print(
        Panel(
            f"[bold]{url}[/bold]\n\n"
            f"Bound to CN: {cn}\nPrefix: {prefix}\nValid for: {expires}\n\n"
            "Shown once - HomePilot stores only a hash. Send it to the holder of that "
            "certificate; nobody else can redeem it.",
            title="Invite created",
        )
    )
    if not origin:
        err_console.print(
            "[yellow]No portal origin known: set HP_PORTAL_BASE_URL or pass --base-url "
            "to print a complete URL.[/yellow]"
        )


# The caps `hp invite list` shows, as both the DB row and the API name them.
_INVITE_CAP_KEYS = ("template_vmid", "node", "cores", "memory_mb", "disk_gb")


@invite_app.command("list")
def invite_list(
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
) -> None:
    """List invites. Never prints tokens - only prefixes, binding, caps and state."""
    from homepilot.portal.repository import invite_state

    async def _list() -> list[dict[str, Any]]:
        database, invites = await _open_invite_repo()
        try:
            return await invites.list_invites()
        finally:
            await database.close()

    if _server_is_running(_get_settings()):
        overview = _via_backend("hp invite list", "GET", "/admin/guests")
        rows = [
            {
                "id": i.get("id"),
                "token_prefix": i.get("prefix"),
                "bound_cn": i.get("cn"),
                **{k: (i.get("caps") or {}).get(k) for k in _INVITE_CAP_KEYS},
                "expires_at": i.get("expires_at"),
                "created_at": i.get("created_at"),
                "state": i.get("state"),
            }
            for i in (overview or {}).get("invites", [])
        ]
    else:
        rows = [{**r, "state": invite_state(r)} for r in asyncio.run(_list())]
    if output == "json":
        console.print(json.dumps(rows, default=str))
        return
    if not rows:
        console.print("No invites.")
        return
    table = Table("Prefix", "CN", "Template", "Node", "Caps", "Expires", "State")
    for row in rows:
        caps = f"{row['cores'] or '-'}c / {row['memory_mb'] or '-'}MB / {row['disk_gb'] or '-'}GB"
        table.add_row(
            str(row["token_prefix"]),
            str(row["bound_cn"]),
            str(row["template_vmid"]),
            str(row["node"]),
            caps,
            str(row["expires_at"]),
            str(row["state"]),
        )
    console.print(table)


@invite_app.command("revoke")
def invite_revoke(
    prefix: str = typer.Argument(..., help="The invite prefix (first 16 chars) to revoke"),
) -> None:
    """Revoke an unredeemed invite by its prefix."""

    async def _revoke() -> bool:
        database, invites = await _open_invite_repo()
        try:
            return await invites.revoke(prefix)
        finally:
            await database.close()

    if _server_is_running(_get_settings()):
        path = f"/admin/guests/invites/{quote(prefix, safe='')}/revoke"
        _via_backend(
            "hp invite revoke", "POST", path, not_found=f"No open invite with prefix {prefix}."
        )
        console.print(f"Revoked invite {prefix}.")
        return
    if not asyncio.run(_revoke()):
        err_console.print(f"[red]No open invite with prefix {prefix}.[/red]")
        raise typer.Exit(1)
    console.print(f"Revoked invite {prefix}.")


@inventory_app.command("list")
def inventory_list(
    role: str = typer.Option("", "--role", "-r", help="Filter by role"),
    status: str = typer.Option("", "--status", "-s", help="Filter by status"),
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
) -> None:
    """List hosts in inventory - from the running backend, or the database when it is stopped."""
    from urllib.parse import urlencode

    from homepilot.inventory.service import InventoryService

    async def _list() -> list[dict[str, Any]]:
        from homepilot.db.repository import Repository

        settings = _get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            return []
        db = await _open_cli_db("hp inventory list", db_path)
        try:
            repo = Repository(db)
            svc = InventoryService(repo=repo)
            filt: dict[str, Any] = {}
            if role:
                filt["role"] = role
            if status:
                filt["status"] = status
            hosts: list[dict[str, Any]] = await svc.query_inventory(filter=filt or None)
            return hosts
        finally:
            await db.close()

    truncated = 0
    if _server_is_running(_get_settings()):
        params = {
            "limit": 1000,
            **({"role": role} if role else {}),
            **({"status": status} if status else {}),
        }
        page = (
            _via_backend(
                "hp inventory list", "GET", f"/inventory?{urlencode(params)}", scope="read"
            )
            or {}
        )
        hosts = list(page.get("items", []))
        truncated = max(0, int(page.get("total", len(hosts))) - len(hosts))
    else:
        hosts = asyncio.run(_list())

    if output == "json":
        console.print(json.dumps(hosts, default=str))
        return

    if not hosts:
        console.print("[dim]No hosts in inventory.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Hostname")
    table.add_column("IP")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Node")
    for h in hosts:
        table.add_row(
            h.get("hostname", ""),
            h.get("ip_address", "") or "—",
            h.get("role", "") or "—",
            h.get("status", "") or "unknown",
            h.get("node", "") or "—",
        )
    console.print(table)
    if truncated:
        console.print(
            f"[yellow]{truncated} more host(s) not shown; filter with --role/--status.[/yellow]"
        )


@inventory_app.command("show")
def inventory_show(
    hostname: str = typer.Argument(..., help="Hostname to look up"),
    output: str = typer.Option("text", "--output", "-o", help="Output format: text or json"),
) -> None:
    """Show details for a specific host."""

    async def _show() -> dict[str, Any] | None:
        from homepilot.db.repository import Repository

        settings = _get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            return None
        db = await _open_cli_db("hp inventory show", db_path)
        try:
            repo = Repository(db)
            host = await repo.get_host_by_hostname(hostname)
            return dict(host) if host else None
        finally:
            await db.close()

    if _server_is_running(_get_settings()):
        page = (
            _via_backend(
                "hp inventory show",
                "GET",
                f"/inventory?q={quote(hostname)}&limit=1000",
                scope="read",
            )
            or {}
        )
        # `q` is free text; the command names ONE host, by its exact name.
        host = next((h for h in page.get("items", []) if h.get("hostname") == hostname), None)
    else:
        host = asyncio.run(_show())

    if host is None:
        err_console.print(f"[red]Host '{hostname}' not found in inventory[/red]")
        raise typer.Exit(1)

    if output == "json":
        console.print(json.dumps(host, default=str))
        return

    table = Table(show_header=False, box=None)
    table.add_column("Field", style="dim")
    table.add_column("Value")
    for k, v in host.items():
        if v is not None and v != "":
            table.add_row(k, str(v))
    console.print(Panel(table, title=f"[bold]{hostname}[/bold]", expand=False))


@inventory_app.command("refresh")
def inventory_refresh(
    scope: str = typer.Option("", "--scope", help="Limit sync to a specific PVE node name"),
) -> None:
    """Sync inventory from Proxmox (requires HP_PROXMOX_HOST configured)."""

    async def _refresh() -> dict[str, Any]:
        settings = _get_settings()

        # Prefer the API when the backend is running — it owns the sqlite write
        # lock, so a direct CLI write would hit "database is locked" (Bug 10).
        async with _ephemeral_token(settings, "cli-inventory-refresh", "write") as (token, err):
            if token:
                port = getattr(settings, "daemon_port", 8000)
                async with _backend_http_client(120.0) as client:
                    resp = await client.post(
                        f"http://127.0.0.1:{port}/inventory/refresh",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"scope": scope} if scope else {},
                    )
                    resp.raise_for_status()
                    return dict(resp.json())
        if err or _server_is_running(settings):
            # A backend holds the database: the direct path below would only be
            # refused (#700). Say what stopped the API call instead.
            why = err or (
                "The backend is running, but this box holds no admin credential "
                "to call it with (set HP_ADMIN_TOKEN)."
            )
            err_console.print(f"[yellow]{why}[/yellow]")
            err_console.print(
                "[yellow]The backend refreshes inventory automatically; you can "
                "also use the UI 'Sync from Proxmox' button.[/yellow]"
            )
            raise typer.Exit(1)

        # Backend not running → refresh directly against the DB.
        from homepilot.db.repository import Repository
        from homepilot.inventory.service import InventoryService

        db_path = Path(settings.data_dir) / "homepilot.db"
        db = await _open_cli_db("hp inventory refresh", db_path)
        try:
            repo = Repository(db)

            proxmox = None
            if settings.proxmox_host:
                try:
                    from homepilot.adapters.proxmox import ProxmoxClient

                    token = ""
                    import os as _os

                    token = _os.environ.get("PVE_API_TOKEN", "")
                    if token:
                        base_url = f"https://{settings.proxmox_host}:{settings.proxmox_port}"
                        proxmox = ProxmoxClient(
                            base_url=base_url, token=token, verify_ssl=settings.proxmox_verify_ssl
                        )
                except Exception as e:
                    err_console.print(f"[yellow]Proxmox adapter unavailable: {e}[/yellow]")

            svc = InventoryService(repo=repo, proxmox=proxmox, proxmox_host=settings.proxmox_host)
            result: dict[str, Any] = await svc.refresh_inventory(scope=scope or None)
            return result
        finally:
            await db.close()

    result = asyncio.run(_refresh())
    console.print(
        f"[green]Sync complete:[/green] {result['hosts']} hosts, {result['services']} services"
    )


def _get_vault() -> VaultManager:
    settings = _get_settings()
    if not settings.vault_passphrase:
        console.print("[red]HP_VAULT_PASSPHRASE not configured — vault unavailable[/red]")
        raise typer.Exit(1)
    data_dir = Path(settings.data_dir)
    return VaultManager(data_dir, settings.vault_passphrase)


def _read_secret_value() -> str:
    """The secret, never from argv (#704).

    `--value` put the secret in the process list and the shell history, and the
    interactive prompt echoed it to the screen. A terminal now gets a hidden,
    confirmed prompt; anything else (a pipe, `docker compose exec -T`) is read
    from stdin.
    """
    import sys

    if sys.stdin is not None and sys.stdin.isatty():
        return str(typer.prompt("Value (JSON)", hide_input=True, confirmation_prompt=True))
    return sys.stdin.read().strip() if sys.stdin is not None else ""


@vault_app.command("set")
def vault_set(
    name: str = typer.Argument(..., help="Secret name (e.g. pve-token)"),
) -> None:
    """Store a secret in the vault. The JSON value (e.g. '{\"token\": \"...\"}')
    is typed at a hidden prompt, or piped on stdin - never passed as an argument."""
    import json as _json

    vault = _get_vault()

    value = _read_secret_value()
    if not value:
        err_console.print("[red]No value given; nothing stored.[/red]")
        raise typer.Exit(1)

    try:
        parsed = _json.loads(value)
    except _json.JSONDecodeError as exc:
        # Position only: the message must not echo the secret back.
        err_console.print(f"[red]Invalid JSON (line {exc.lineno}, column {exc.colno}).[/red]")
        raise typer.Exit(1) from None

    async def _store() -> None:
        await vault.ensure_master_identity()
        await vault.store_secret(name, parsed)

    asyncio.run(_store())
    console.print(f"[green]Secret '{name}' stored in vault[/green]")


@vault_app.command("get")
def vault_get(
    name: str = typer.Argument(..., help="Secret name"),
    output: str = typer.Option("json", "--output", "-o", help="Output format: json or plain"),
) -> None:
    """Retrieve a secret from the vault."""
    import json as _json

    vault = _get_vault()

    async def _get() -> dict[str, Any]:
        return await vault.get_secret(name)

    try:
        secret = asyncio.run(_get())
    except Exception as exc:
        console.print(f"[red]Secret '{name}' not found: {exc}[/red]")
        raise typer.Exit(1) from exc

    if output == "json":
        console.print(_json.dumps(secret))
    else:
        for k, v in secret.items():
            console.print(f"{k}: {v}")


@vault_app.command("list")
def vault_list() -> None:
    """List secret names stored in the vault."""
    vault = _get_vault()

    async def _list() -> list[str]:
        return await vault.list_secrets()

    names = asyncio.run(_list())
    if not names:
        console.print("[dim]No secrets stored[/dim]")
    else:
        for name in names:
            console.print(name)


@vault_app.command("delete")
def vault_delete(
    name: str = typer.Argument(..., help="Secret name to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a secret from the vault."""
    vault = _get_vault()
    if not yes:
        typer.confirm(f"Delete secret '{name}'?", abort=True)

    async def _delete() -> bool:
        return await vault.delete_secret(name)

    if not asyncio.run(_delete()):
        # It used to print "deleted" for a name that was never there (#704).
        err_console.print(f"[red]No secret named '{name}'; nothing was deleted.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Secret '{name}' deleted[/green]")


@contextlib.asynccontextmanager
async def _webhook_repo() -> Any:
    """(database, Repository) for the webhook commands, always closed.

    A context manager rather than a bare opener because three of the four
    webhook commands raise `typer.Exit` on a not-found or an undelivered event -
    and a CLI that exits while holding an open aiosqlite connection never exits
    at all (see `_open_cli_db`). `hp webhook test <unknown id>` hung forever.
    """
    from homepilot.db.repository import Repository

    settings = _get_settings()
    db_path = Path(settings.data_dir) / "homepilot.db"
    # The command name is the operator's own: it used to say "hp vault delete"
    # here, so every `hp webhook` refusal talked about the vault.
    db = await _open_cli_db("hp webhook", db_path)
    try:
        yield db, Repository(db)
    finally:
        await db.close()


@webhook_app.command("add")
def webhook_add(
    url: str = typer.Option(..., "--url", help="Webhook endpoint URL (http or https)"),
    events: str = typer.Option(
        ..., "--events", help="Comma-separated event types, or * for every event"
    ),
    with_secret: bool = typer.Option(
        False,
        "--with-secret",
        help="Prompt (hidden) for an HMAC signing key; never passed as an argument",
    ),
    max_retries: int = typer.Option(3, "--max-retries", help="Retry attempts (0-10)"),
) -> None:
    """Register a webhook endpoint for the named events.

    Event names are checked against what the platform emits: a typo used to be
    stored and then never matched anything (#700). With the backend running
    this goes through `/admin/webhooks`; stopped, the CLI runs the same code.
    """
    from homepilot.webhooks import service
    from homepilot.webhooks.catalog import validate_event_types, validate_webhook_url

    event_list = [e.strip() for e in events.split(",") if e.strip()]
    try:
        validate_webhook_url(url)
        event_list = validate_event_types(event_list)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    secret_val = (
        str(typer.prompt("Signing key", hide_input=True, confirmation_prompt=True))
        if with_secret
        else None
    )

    if _server_is_running(_get_settings()):
        body: dict[str, Any] = {
            "url": url,
            "event_types": event_list,
            "max_retries": max_retries,
        }
        if secret_val:
            body["secret"] = secret_val
        view = _via_backend("hp webhook add", "POST", "/admin/webhooks", body) or {}
    else:

        async def _add() -> dict[str, Any]:
            async with _webhook_repo() as (_db, repo):
                return await service.create_webhook(
                    repo,
                    url=url,
                    event_types=event_list,
                    secret=secret_val,
                    max_retries=max_retries,
                    actor=_CLI_OPERATOR.name,
                    source="cli",
                )

        try:
            view = asyncio.run(_add())
        except ValueError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
    console.print(
        f"[green]Webhook #{view.get('id')} registered[/green] — "
        f"{view.get('endpoint')} → {', '.join(view.get('event_types') or event_list)}"
    )


@webhook_app.command("list")
def webhook_list(
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
) -> None:
    """List webhook configurations. Endpoints show scheme://host:port only."""
    from homepilot.webhooks import service

    if _server_is_running(_get_settings()):
        configs = list(
            (_via_backend("hp webhook list", "GET", "/admin/webhooks") or {}).get("items", [])
        )
    else:

        async def _list() -> list[dict[str, Any]]:
            async with _webhook_repo() as (_db, repo):
                return await service.list_webhooks(repo)

        configs = asyncio.run(_list())

    if output == "json":
        # Neither the signing key nor the URL's path ever leaves this command:
        # `--output json` is what gets piped into CI logs (#388), and endpoint
        # paths routinely carry their own credential (#700). typer.echo: machine
        # output must never be wrapped to the terminal width.
        typer.echo(json.dumps(configs, default=str))
        return

    if not configs:
        console.print("[dim]No webhooks configured[/dim]")
        return

    table = Table(title="Webhooks", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Endpoint")
    table.add_column("Events")
    table.add_column("Enabled")
    table.add_column("Signed")
    table.add_column("Max Retries")
    for c in configs:
        table.add_row(
            str(c["id"]),
            str(c.get("endpoint", "")),
            ", ".join(c.get("event_types") or []),
            "yes" if c.get("enabled") else "no",
            "yes" if c.get("has_secret") else "no",
            str(c.get("max_retries", "")),
        )
    console.print(table)


@webhook_app.command("delete")
def webhook_delete(
    id: int = typer.Argument(..., help="Webhook config ID to delete"),
) -> None:
    """Delete a webhook configuration."""
    from homepilot.webhooks import service

    if _server_is_running(_get_settings()):
        _via_backend(
            "hp webhook delete",
            "DELETE",
            f"/admin/webhooks/{id}",
            not_found=f"Webhook #{id} not found",
        )
        console.print(f"[green]Webhook #{id} deleted[/green]")
        return

    async def _delete() -> bool:
        async with _webhook_repo() as (_db, repo):
            return await service.delete_webhook(repo, id, actor=_CLI_OPERATOR.name, source="cli")

    if asyncio.run(_delete()):
        console.print(f"[green]Webhook #{id} deleted[/green]")
    else:
        err_console.print(f"[red]Webhook #{id} not found[/red]")
        raise typer.Exit(1)


@webhook_app.command("test")
def webhook_test(
    id: int = typer.Argument(..., help="Webhook config ID to test"),
) -> None:
    """Send one test event and say whether the endpoint accepted it."""
    from homepilot.webhooks import service

    if _server_is_running(_get_settings()):
        result = (
            _via_backend(
                "hp webhook test",
                "POST",
                f"/admin/webhooks/{id}/test",
                not_found=f"Webhook #{id} not found",
            )
            or {}
        )
    else:

        async def _test() -> dict[str, Any] | None:
            async with _webhook_repo() as (_db, repo):
                try:
                    return await service.send_test_event(
                        repo, id, actor=_CLI_OPERATOR.name, source="cli"
                    )
                except LookupError:
                    return None

        found = asyncio.run(_test())
        if found is None:
            err_console.print(f"[red]Webhook #{id} not found[/red]")
            raise typer.Exit(1)
        result = found

    if not result.get("delivered"):
        # This command exists to answer "does delivery work". Printing
        # success for an endpoint that never answered is the one thing it
        # must not do (#388).
        err_console.print(f"[red]Test event was NOT accepted by {result.get('endpoint')}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Test event delivered to {result.get('endpoint')}[/green]")


# ── Agent management ──────────────────────────────────────────────────────────

agent_app = typer.Typer(help="Agent hub management")
app.add_typer(agent_app, name="agent")


def _backend_http_client(timeout: float) -> Any:
    """The HTTP client every CLI-to-backend call goes through (a test seam)."""
    import httpx

    return httpx.AsyncClient(timeout=timeout)


class BackendRefusedError(RuntimeError):
    """The backend answered, and said no. `status` is the HTTP status."""

    def __init__(self, status: int, path: str, detail: str) -> None:
        self.status = status
        self.path = path
        self.detail = detail
        super().__init__(f"{status} from {path}: {detail}")


def _via_backend(
    what: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    scope: str = "admin",
    not_found: str | None = None,
) -> Any:
    """One call to the running backend for an `hp` command, or a clean exit 1.

    The commands that opened the database directly refused on every running
    install (the backend holds it), so on the shipped Docker deploy they were
    dead (#700). With the backend up they now go through its API - the same
    routes, validation and audit trail the console uses. ``not_found`` is the
    message for a 404, which is an answer, not a failure of the call.

    Every failure says that the backend is running and that the command went
    through its API, then what the API answered (#623) - an operator who
    expected a direct write should not have to guess where the "no" came from.
    Text from the backend is escaped so it cannot inject console markup.
    """
    from rich.markup import escape

    lead = f"The HomePilot backend is running, so `{what}` goes through its API - "
    try:
        return asyncio.run(_backend_api(method, path, body, scope=scope))
    except BackendRefusedError as exc:
        if exc.status == 404 and not_found:
            err_console.print(f"[yellow]{lead}and it answered: {escape(not_found)}[/yellow]")
        else:
            err_console.print(
                f"[red]{lead}and it refused ({exc.status}):[/red] {escape(exc.detail)}"
            )
        raise typer.Exit(1) from None
    except Exception as exc:
        err_console.print(f"[red]{lead}and that failed:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None


def _api_error_text(resp: Any) -> str:
    """The human sentence of an API refusal, not its raw JSON.

    A structured `detail` (the #710 busy refusal names the running task in
    `message`) collapses to that sentence, the way the web UI shows it; a body
    that is not JSON is passed through.
    """
    try:
        detail = resp.json().get("detail")
    except Exception:
        return str(resp.text)
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        for key in ("message", "reason", "error"):
            if isinstance(detail.get(key), str):
                return str(detail[key])
    return json.dumps(detail) if detail is not None else str(resp.text)


@contextlib.asynccontextmanager
async def _backend_session(scope: str = "admin") -> AsyncIterator[Any]:
    """A short-lived credential against the running backend, for several calls.

    Yields ``call(method, path, json_body=None, timeout=15.0)`` which returns the
    decoded JSON body and raises ``RuntimeError`` on an HTTP error status.
    Transport errors (``httpx.HTTPError``) propagate so a caller that follows a
    task can tell "the backend did not answer" from "the backend said no".
    The credential expires on its own and is deleted on the way out (#699).
    """
    settings = _get_settings()
    async with _ephemeral_token(settings, f"cli-{scope}", scope) as (token, err):
        if token is None:
            raise RuntimeError(
                err
                or (
                    "The HomePilot backend is not reachable on "
                    f"127.0.0.1:{getattr(settings, 'daemon_port', 8000)}, or this box holds "
                    "no admin credential to call it with (HP_ADMIN_TOKEN, or the data "
                    "dir's api-token). This command works through its API."
                )
            )
        port = getattr(settings, "daemon_port", 8000)
        auth = {"Authorization": f"Bearer {token}"}

        async def call(
            method: str,
            path: str,
            json_body: dict[str, Any] | None = None,
            timeout: float = 15.0,
        ) -> Any:
            async with _backend_http_client(timeout) as client:
                resp = await client.request(
                    method, f"http://127.0.0.1:{port}{path}", headers=auth, json=json_body
                )
            if resp.status_code >= 400:
                raise BackendRefusedError(resp.status_code, path, _api_error_text(resp))
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        yield call


async def _backend_api(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    scope: str = "admin",
) -> dict[str, Any] | list[Any]:
    """Call the running backend, as an operator would.

    Two families of command need this, for the same reason. `hp agent
    list|token|bootstrap` used to read `app_state.get_agent_registry()`, a global
    only ever set inside the FastAPI lifespan, so a standalone `hp` process
    ALWAYS printed "Agent hub not enabled" (#430). `hp artifacts
    apply|replay|revoke` ran a SECOND apply engine with weaker guarantees for the
    same reason - the real executor, its agent transport, its snapshots and its
    task rows all live in the backend process (#423).

    The engine and the hub live there; the only honest way for a separate process
    to use them is over the API.

    The short-lived admin token is minted through the same admin-secret endpoint
    the other CLI-to-backend commands use, and DELETED again afterwards - a
    read-only `hp agent list` must not leave a fleet-root credential behind on
    every invocation.
    """
    async with _backend_session(scope) as call:
        body: dict[str, Any] | list[Any] = await call(method, path, json_body)
        return body


@agent_app.command("list")
def agent_list() -> None:
    """List the fleet: connected agents, and the known ones that are not."""
    try:
        agents = asyncio.run(_backend_api("GET", "/agents/"))
    except RuntimeError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None
    if not isinstance(agents, list) or not agents:
        console.print("[dim]No agents known[/dim]")
        return
    table = Table(title="Agents")
    table.add_column("Agent ID", style="cyan")
    table.add_column("Hostname", style="green")
    table.add_column("OS", style="blue")
    table.add_column("Version", style="magenta")
    table.add_column("State", style="yellow")
    table.add_column("Last Heartbeat", style="dim")
    table.add_column("Last error", style="red")
    for a in agents:
        sys_info = a.get("system_info") or {}
        connected = bool(a.get("connected"))
        table.add_row(
            str(a.get("agent_id", "")),
            str(a.get("hostname", "")),
            f"{sys_info.get('os', '?')} {sys_info.get('arch', '?')}",
            # An agent built before the version stamp existed reports nothing;
            # say so rather than inventing a value (#430).
            str(sys_info.get("agent_version") or "unknown"),
            "connected" if connected else "disconnected",
            str(a.get("last_heartbeat") or "-"),
            str(a.get("last_error") or ""),
        )
    console.print(table)


@agent_app.command("token")
def agent_token(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show the agent hub auth token for configuring agents."""
    try:
        data = asyncio.run(_backend_api("GET", "/agents/token"))
    except RuntimeError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None
    if not isinstance(data, dict):
        err_console.print("[red]Unexpected response from /agents/token[/red]")
        raise typer.Exit(1)
    token = str(data.get("auth_token") or "")
    hub_host = str(data.get("hub_host") or "")
    hub_port = data.get("hub_port")
    if json_output:
        console.print_json(data={"auth_token": token, "hub_host": hub_host, "hub_port": hub_port})
    else:
        if token:
            console.print(
                Panel(token, title="Agent Hub Auth Token", subtitle="Set HP_AGENT_HUB_AUTH_TOKEN")
            )
        else:
            console.print("[yellow]No auth token configured - set HP_AGENT_HUB_AUTH_TOKEN[/yellow]")
        console.print(f"[dim]Hub endpoint: {hub_host}:{hub_port}[/dim]")


@agent_app.command("bootstrap")
def agent_bootstrap_cmd(
    hub_host: str = typer.Option("", help="Agent hub host (default: what the hub advertises)"),
    hub_port: int = typer.Option(0, help="Agent hub port (default: what the hub advertises)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Generate a one-time bootstrap token for agent enrollment."""
    try:
        data = asyncio.run(_backend_api("POST", "/agents/bootstrap"))
    except RuntimeError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None
    if not isinstance(data, dict) or not data.get("bootstrap_token"):
        err_console.print("[red]Unexpected response from /agents/bootstrap[/red]")
        raise typer.Exit(1)
    token = str(data["bootstrap_token"])
    # The hub knows its own reachable address; the flags exist to override it
    # from behind a NAT or a different name, not to be guessed at by default.
    hub_host = hub_host or str(data.get("hub_host") or "localhost")
    hub_port = hub_port or int(data.get("hub_port") or 8443)

    if json_output:
        console.print_json(
            data={
                "bootstrap_token": token,
                "hub_host": hub_host,
                "hub_port": hub_port,
                "command": (
                    f"HP_AGENT_HUB_HOST={hub_host} "
                    f"HP_AGENT_HUB_PORT={hub_port} "
                    f"HP_AGENT_AUTH_TOKEN={token} "
                    f"hp-agent"
                ),
            }
        )
    else:
        console.print(
            Panel(token, title="Bootstrap Token", subtitle="One-time use, expires in 24h")
        )
        cmd = (
            f"HP_AGENT_HUB_HOST={hub_host} "
            f"HP_AGENT_HUB_PORT={hub_port} "
            f"HP_AGENT_AUTH_TOKEN={token} "
            f"hp-agent"
        )
        console.print(Panel(cmd, title="Agent command", subtitle="Run on managed host"))


@agent_app.command("enrolment-window")
def agent_enrolment_window(
    action: str = typer.Argument(
        "status", help="open | close | status (default: status)", metavar="<action>"
    ),
    minutes: int = typer.Option(
        15, "--minutes", help="How long an opened window stays open (1-1440, default 15)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Open, close, or inspect the shared-token enrolment window.

    While the window is open, the shared fleet token may enrol a host this
    install has never seen. While it is closed, only already-known hosts and
    one-shot bootstrap tokens (`hp agent bootstrap`) can enrol - a leaked shared
    token cannot grow the fleet behind your back. A brand-new install with no
    agents at all is exempt, so the first zero-touch rollout still needs nothing.
    """
    verb = action.strip().lower()
    if verb not in {"open", "close", "status"}:
        err_console.print(f"[red]Unknown action '{action}' - use open, close or status[/red]")
        raise typer.Exit(2)
    method, path = {
        "open": ("POST", "/agents/enrolment-window"),
        "close": ("DELETE", "/agents/enrolment-window"),
        "status": ("GET", "/agents/enrolment-window"),
    }[verb]
    body = {"minutes": minutes} if verb == "open" else None
    try:
        data = asyncio.run(_backend_api(method, path, body))
    except RuntimeError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None
    if not isinstance(data, dict):
        err_console.print("[red]Unexpected response from /agents/enrolment-window[/red]")
        raise typer.Exit(1)

    if json_output:
        console.print_json(data=data)
        return
    if data.get("open"):
        console.print(f"[green]Enrolment window OPEN until {data.get('expires_at')}[/green]")
    else:
        console.print("[yellow]Enrolment window CLOSED[/yellow]")
    if data.get("fleet_empty"):
        console.print(
            "[dim]This install has no agents yet, so the first host enrols with the "
            "shared token either way.[/dim]"
        )


@agent_app.command("revoke")
def agent_revoke(
    agent_id: str = typer.Argument(..., help="Agent ID whose per-agent credential to revoke"),
) -> None:
    """Revoke an agent's per-agent credential.

    A revoked credential can no longer authenticate to the hub, so the agent
    cannot reconnect until it is re-enrolled with a fresh bootstrap/shared token.
    """

    async def _revoke() -> bool:
        from homepilot.db.repository import Repository

        settings = get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            err_console.print(f"[red]Database not found at {db_path}[/red]")
            raise typer.Exit(1)
        db = await _open_cli_db("hp agent revoke", db_path)
        try:
            repo = Repository(db)
            return await repo.revoke_agent_credential(agent_id)
        finally:
            await db.close()

    if _server_is_running(_get_settings()):
        # The route also closes the agent's live channel (#430), which a
        # database write from here never could (#700).
        answer = (
            _via_backend(
                "hp agent revoke",
                "POST",
                f"/agents/{agent_id}/revoke",
                not_found=f"No active credential to revoke for agent '{agent_id}'",
            )
            or {}
        )
        console.print(f"[green]Revoked per-agent credential for {agent_id}[/green]")
        if answer.get("channel_closed"):
            console.print("[dim]Its live connection was closed.[/dim]")
        console.print("[dim]The agent must re-enroll (bootstrap/shared token) to reconnect.[/dim]")
        return
    revoked = asyncio.run(_revoke())
    if revoked:
        console.print(f"[green]Revoked per-agent credential for {agent_id}[/green]")
        console.print("[dim]The agent must re-enroll (bootstrap/shared token) to reconnect.[/dim]")
    else:
        err_console.print(f"[yellow]No active credential to revoke for agent '{agent_id}'[/yellow]")
        raise typer.Exit(1)


@agent_app.command("remove")
def agent_remove(
    agent_id: str = typer.Argument(..., help="Agent ID to forget entirely"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Forget a decommissioned agent: delete its record AND revoke its credential.

    Unlike `agent revoke`, which leaves the row in place so the host can be
    re-enrolled, this removes the agent entirely. Use it when a box is gone for
    good - the `agents` table doubles as the per-agent credential store, so a
    record left behind is a credential a scrapped machine can still present.

    With the backend running this goes through its API, which refuses a
    connected agent. With it stopped no agent can be connected, and the CLI
    writes the database itself.
    """

    async def _remove() -> bool:
        from homepilot.db.repository import Repository

        settings = get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            err_console.print(f"[red]Database not found at {db_path}[/red]")
            raise typer.Exit(1)
        db = await _open_cli_db("hp agent remove", db_path)
        try:
            repo = Repository(db)
            # Revoke FIRST: if the delete then fails, the credential is already
            # dead rather than the other way round.
            await repo.revoke_agent_credential(agent_id)
            return await repo.delete_agent(agent_id)
        finally:
            await db.close()

    if not yes:
        confirmed = typer.confirm(
            f"Forget agent {agent_id} and revoke its credential? This cannot be undone"
        )
        if not confirmed:
            console.print("[dim]Left alone.[/dim]")
            raise typer.Exit(0)

    if _server_is_running(_get_settings()):
        # The route refuses a CONNECTED agent (409), which the direct path
        # below cannot know about (#700).
        _via_backend(
            "hp agent remove",
            "DELETE",
            f"/agents/{agent_id}",
            not_found=f"No agent '{agent_id}' to forget",
        )
        console.print(f"[green]Forgot agent {agent_id}; its credential is revoked[/green]")
        return
    removed = asyncio.run(_remove())
    if removed:
        console.print(f"[green]Forgot agent {agent_id}; its credential is revoked[/green]")
    else:
        err_console.print(f"[yellow]No agent '{agent_id}' to forget[/yellow]")
        raise typer.Exit(1)


@quota_app.command("set")
def quota_set(
    cn: str = typer.Option(..., "--cn", help="The guest's certificate CN"),
    max_vms: int | None = typer.Option(None, "--max-vms", help="Most machines held at once"),
    max_cores: int | None = typer.Option(
        None, "--max-cores", help="Total CPU cores across machines"
    ),
    max_memory_mb: int | None = typer.Option(None, "--max-memory-mb", help="Total memory (MB)"),
    max_disk_gb: int | None = typer.Option(None, "--max-disk-gb", help="Total disk (GB)"),
) -> None:
    """Set (or replace) a guest's resource budget. Unset axes are unlimited."""

    if _server_is_running(_get_settings()):
        result = (
            _via_backend(
                "hp quota set",
                "POST",
                "/admin/guests/quota",
                {
                    "cn": cn,
                    "max_vms": max_vms,
                    "max_cores": max_cores,
                    "max_memory_mb": max_memory_mb,
                    "max_disk_gb": max_disk_gb,
                },
            )
            or {}
        )
        used = result.get("usage") or {}
        console.print(f"Budget for [bold]{cn}[/bold]: {result.get('limits')}")
        console.print(
            f"Current use: {used.get('vms', 0)} machines, {used.get('cores', 0)} cores, "
            f"{used.get('memory_mb', 0)} MB memory, {used.get('disk_gb', 0)} GB disk"
        )
        return

    async def _run() -> None:
        from homepilot.db.repository import Repository
        from homepilot.guest.quota import get_quota, set_quota, usage_for

        database, _invites = await _open_invite_repo()
        try:
            repo = Repository(database)
            await set_quota(
                repo,
                cn,
                max_vms=max_vms,
                max_cores=max_cores,
                max_memory_mb=max_memory_mb,
                max_disk_gb=max_disk_gb,
            )
            quota = await get_quota(repo, cn)
            used = await usage_for(repo, cn)
            console.print(f"Budget for [bold]{cn}[/bold]: {quota}")
            console.print(
                f"Current use: {used.vms} machines, {used.cores} cores, "
                f"{used.memory_mb} MB memory, {used.disk_gb} GB disk"
            )
        finally:
            await database.close()

    asyncio.run(_run())


@quota_app.command("list")
def quota_list() -> None:
    """Every guest budget, next to what each guest actually uses."""

    if _server_is_running(_get_settings()):
        overview = _via_backend("hp quota list", "GET", "/admin/guests") or {}
        budgets = [g for g in overview.get("guests", []) if g.get("limits") is not None]
        if not budgets:
            console.print("[dim]No guest budgets set — invites alone cap provisioning.[/dim]")
            return
        for g in budgets:
            lim, used = g["limits"], g.get("usage") or {}
            console.print(
                f"[bold]{g['cn']}[/bold]: vms {used.get('vms', 0)}/{lim.get('vms') or '∞'} · "
                f"cores {used.get('cores', 0)}/{lim.get('cores') or '∞'} · "
                f"mem {used.get('memory_mb', 0)}/{lim.get('memory_mb') or '∞'} MB · "
                f"disk {used.get('disk_gb', 0)}/{lim.get('disk_gb') or '∞'} GB"
            )
        return

    async def _run() -> None:
        from homepilot.db.repository import Repository
        from homepilot.guest.quota import usage_for

        database, _invites = await _open_invite_repo()
        try:
            repo = Repository(database)
            rows = await database.fetchall("SELECT * FROM guest_quotas ORDER BY cn")
            if not rows:
                console.print("[dim]No guest budgets set — invites alone cap provisioning.[/dim]")
                return
            for r in rows:
                used = await usage_for(repo, r["cn"])
                console.print(
                    f"[bold]{r['cn']}[/bold]: vms {used.vms}/{r['max_vms'] or '∞'} · "
                    f"cores {used.cores}/{r['max_cores'] or '∞'} · "
                    f"mem {used.memory_mb}/{r['max_memory_mb'] or '∞'} MB · "
                    f"disk {used.disk_gb}/{r['max_disk_gb'] or '∞'} GB"
                )
        finally:
            await database.close()

    asyncio.run(_run())
