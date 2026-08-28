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

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from homepilot import __version__
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.tokens import generate_api_token
from homepilot.config import get_settings
from homepilot.vault import VaultManager

if TYPE_CHECKING:  # imports kept out of CLI startup, present for type checking only
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


async def _migrate_or_refuse(database: Any, what: str) -> None:
    """Run migrations from the CLI, unless a backend holds this data directory.

    `hp init`, `hp token create|revoke`, `hp agent revoke`, `hp kb reindex` and
    `hp import` each migrate the same file a running server is using, so a CLI
    could change the schema under it (#431). One helper rather than a guard per
    call site: five copies is five chances to forget the fifth.
    """
    from homepilot.db.migrations import run_migrations

    _refuse_if_server_running(_get_settings(), what)
    await run_migrations(database)


def _refuse_if_server_running(settings: Any, what: str) -> None:
    """Refuse a schema migration while a backend holds this data directory (#431).

    `hp init`, `hp token create` and `hp agent revoke` each run `run_migrations()`
    on the same file a running server is using, so a CLI could migrate the schema
    out from under it. The backend takes an advisory lock for its lifetime; this
    asks whether anyone holds it.
    """
    from homepilot.instance_lock import another_instance_is_running

    if another_instance_is_running(settings.data_dir):
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


def _get_lifecycle() -> ArtifactLifecycle:
    store = _get_artifact_store()
    return ArtifactLifecycle(store)


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

    env_path.write_text("\n".join(env_lines) + "\n")
    os.chmod(str(env_path), 0o600)

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
    token_path.write_text(full_token + "\n")
    os.chmod(str(token_path), 0o600)

    async def _register_token() -> None:
        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository

        db_path = data_dir / "homepilot.db"
        database = Database(str(db_path))
        await database.connect()
        await _migrate_or_refuse(database, "hp init")
        repo = Repository(database)
        user_id = await repo.create_user(display_name="admin", auth_source="api_token")
        await repo.create_api_token(
            user_id=str(user_id),
            token_type="personal",
            prefix=token_prefix,
            hash=token_hash,
            # "all" normalizes to "*" - the admin scope the browser claim hands
            # its first credential. It used to be "read,write", which could not
            # open Settings -> Tokens (admin) and, now that minting requires an
            # admin, could not mint either: the box's only token was locked out
            # of managing tokens.
            scope="all",
            label="admin",
            expires_at=None,
        )
        await database.conn.commit()
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
    console.print("[dim]Add to your MCP config:[/dim]")
    console.print(
        f'[dim]  {{"mcpServers": {{"homepilot": '
        f'{{"command": "hp", "args": ["mcp-serve"], '
        f'"env": {{"HP_MCP_TOKEN": "{full_token}"}}}}}}}}[/dim]'
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
    """Show daemon info, PVE connectivity, artifact counts, and vault state."""
    settings = _get_settings()
    data_dir = Path(settings.data_dir)

    table = Table(title="HomePilot Status", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    table.add_row("Data dir", str(data_dir))
    table.add_row("Artifacts dir", str(Path(settings.artifacts_dir)))
    table.add_row("PVE host", settings.proxmox_host or "(not configured)")
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
    """Start the MCP server (stdio or http transport)."""
    if transport == "http":
        from homepilot.mcp.server import run_server_http

        asyncio.run(run_server_http(host=host, port=port))
    else:
        from homepilot.mcp.server import main as mcp_main

        mcp_main()


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
    lifecycle = _get_lifecycle()
    reason_str = " ".join(reason) if reason else None
    try:
        asyncio.run(lifecycle.approve(id, user="cli", reason=reason_str))
        # The CLI lifecycle carries no repo, so clear the spent code directly.
        with contextlib.suppress(Exception):
            asyncio.run(_with_repo(lambda repo: repo.clear_approval_code(id)))
        console.print(f"[green]Approved: {id}[/green]")
    except Exception as e:
        err_console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


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
    lifecycle = _get_lifecycle()
    reason_str = " ".join(reason) if reason else None
    try:
        asyncio.run(lifecycle.reject(id, user="cli", reason=reason_str))
        with contextlib.suppress(Exception):
            asyncio.run(_with_repo(lambda repo: repo.clear_approval_code(id)))
        console.print(f"[yellow]Rejected: {id}[/yellow]")
    except Exception as e:
        err_console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


@artifacts_app.command("edit")
def artifacts_edit(
    id: str = typer.Argument(..., help="Artifact ID"),
) -> None:
    """Open artifact in $EDITOR."""
    store = _get_artifact_store()
    try:
        path = store.resolve_path(id)
    except ValueError:
        err_console.print(f"[red]Invalid artifact ID: {id}[/red]")
        raise typer.Exit(1) from None

    if not path.exists():
        err_console.print(f"[red]Artifact not found: {id}[/red]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(path)], check=False)

    lifecycle = _get_lifecycle()
    try:
        asyncio.run(lifecycle.edit(id))
        console.print(f"[green]Edit recorded for: {id}[/green]")
    except Exception as e:
        console.print(f"[dim]Edit sync: {e}[/dim]")


def _run_artifact_task(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    past_tense: str,
    artifact_id: str,
) -> None:
    """Drive an apply/replay/revoke through the backend and report the OUTCOME.

    `sync=true` so the command does not exit while the change is still happening:
    a CLI that returns before the host has been touched is the same lie as a
    rollback that prints and does nothing.
    """
    try:
        result = asyncio.run(_backend_api(method, f"{path}?sync=true", body, scope="write"))
    except RuntimeError as exc:
        err_console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None

    if not isinstance(result, dict):
        err_console.print(f"[red]Unexpected response from {path}[/red]")
        raise typer.Exit(1)

    # A finished task reports its own status; a successful sync apply returns the
    # artifact instead. Anything that is not an explicit failure is a success.
    status = result.get("status")
    if status in ("failed", "cancelled"):
        error = result.get("error") or "no reason recorded"
        err_console.print(f"[red]{past_tense} failed for {artifact_id}: {error}[/red]")
        log = (result.get("result") or {}).get("execution_log") if result.get("result") else None
        if log:
            console.print(f"[dim]{log}[/dim]")
        raise typer.Exit(1)

    console.print(f"[green]{past_tense}: {artifact_id}[/green]")


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
    lifecycle = _get_lifecycle()

    if approve:
        try:
            asyncio.run(lifecycle.approve(id, user="cli"))
            console.print(f"[green]Approved: {id}[/green]")
        except Exception as e:
            err_console.print(f"[red]Approve failed: {e}[/red]")
            raise typer.Exit(1) from e

    _run_artifact_task("POST", f"/artifacts/{id}/apply", {"approved_by": "cli"}, "Applied", id)


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
    body: dict[str, Any] = {"user": "cli"}
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


@app.command()
def drift() -> None:
    """Show artifact pile vs current inventory."""
    store = _get_artifact_store()

    active_artifacts = [a for a in store.list() if a.get("status") in ("applied", "approved")]

    if not active_artifacts:
        console.print("[dim]No active artifacts to compare against.[/dim]")
        return

    table = Table(title="Drift: Artifact Pile vs Inventory")
    table.add_column("Artifact ID", style="cyan")
    table.add_column("Kind")
    table.add_column("Intent", max_width=40)
    table.add_column("Status")
    table.add_column("Target", max_width=20)

    for a in active_artifacts:
        target = a.get("target", {})
        target_str = target.get("host", "") or target.get("node", "") or "(global)"
        table.add_row(
            a.get("id", ""),
            a.get("kind", ""),
            a.get("intent", ""),
            a.get("status", ""),
            target_str,
        )

    console.print(table)
    console.print(
        "[dim]Run 'hp status' for live inventory counts. "
        "Compare targets above against inventory.[/dim]"
    )


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
SECRET_PATHS: tuple[str, ...] = (
    ".env",
    ".vault_passphrase",
    "api-token",
    "vault/identities",
    "vault/secrets",
    "ssh",
)


def _present_secret_paths(data_dir: Path) -> list[str]:
    return [rel for rel in SECRET_PATHS if (data_dir / rel).exists()]


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


def _print_secrets_banner(tarball_path: Path) -> None:
    err_console.print("[bold red]DANGER: this tarball CONTAINS SECRETS.[/bold red]")
    err_console.print("[red]It holds the vault identity and passphrase,[/red]")
    err_console.print("[red]so anyone who reads it can decrypt every[/red]")
    err_console.print("[red]secret HomePilot holds.[/red]")
    err_console.print(f"[red]Treat {tarball_path.name} as a credential:[/red]")
    err_console.print("[red]encrypt it at rest, never commit it,[/red]")
    err_console.print("[red]delete it once restored.[/red]")


def _export_readme(includes_secrets: bool) -> str:
    secrets_section = (
        "- `secrets/` - vault identity, vault secrets and key material\n"
        if includes_secrets
        else "- (no `secrets/` - this archive cannot restore a working host)\n"
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
        help="Include vault identity, vault secrets and key material. "
        "The tarball can then decrypt everything - guard it accordingly.",
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

        contents = [MANIFEST_NAME, "README.md"]
        if db_snapshot.exists():
            contents.append("homepilot.db")
        if artifacts_dir.exists():
            contents.append("artifacts")
        if secret_rels:
            contents.append("secrets")

        manifest = {
            "manifest_schema_version": EXPORT_MANIFEST_SCHEMA_VERSION,
            "homepilot_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "includes_secrets": bool(secret_rels),
            "secret_paths": secret_rels,
            "db_schema_version": db_schema_version,
            "build_supports_db_schema_version": max(MIGRATIONS.keys()),
            "contents": contents,
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        readme_path = staging / "README.md"
        readme_path.write_text(_export_readme(bool(secret_rels)), encoding="utf-8")

        with tarfile.open(str(tarball_path), "w:gz") as tar:
            tar.add(str(manifest_path), arcname=MANIFEST_NAME)
            tar.add(str(readme_path), arcname="README.md")
            if db_snapshot.exists():
                # Only the VACUUM INTO snapshot: -wal/-shm are never archived,
                # they would replay onto the restored file (#421).
                tar.add(str(db_snapshot), arcname="homepilot.db")
            if artifacts_dir.exists():
                tar.add(str(artifacts_dir), arcname="artifacts")
            for rel in secret_rels:
                tar.add(str(data_dir / rel), arcname=f"secrets/{rel}")

    if secret_rels:
        # The archive is now a credential; keep it off other users' reach.
        os.chmod(str(tarball_path), 0o600)

    console.print(f"[green]Exported to {tarball_path}[/green]")
    if secret_rels:
        _print_secrets_banner(tarball_path)
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
    _restore_tree(src, dest)
    if dest.is_dir():
        os.chmod(str(dest), 0o700)
        for child in dest.rglob("*"):
            os.chmod(str(child), 0o700 if child.is_dir() else 0o600)
    else:
        os.chmod(str(dest), 0o600)


@app.command("import")
def import_backup(
    path: Path = typer.Argument(..., help="Path to homepilot-export-*.tar.gz"),  # noqa: B008
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    restore_secrets: bool = typer.Option(
        False,
        "--restore-secrets",
        help="Also restore the vault identity, vault secrets and key material. "
        "Overwrites the live keys on this host (the old ones are backed up).",
    ),
) -> None:
    """Restore a HomePilot data dir from a tarball. Backs up current state first."""
    from homepilot.db.backup import DatabaseLockedError, SnapshotError, ensure_not_locked
    from homepilot.db.connection import Database

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
            _restore_tree(staged_db, db_path)
            # A stale -wal/-shm left beside a replaced database file is replayed
            # onto it by SQLite - the restored data would be corrupted by the
            # journal of the database it just replaced (#421).
            for sidecar in (
                db_path.with_name(db_path.name + "-wal"),
                db_path.with_name(db_path.name + "-shm"),
            ):
                if sidecar.exists():
                    sidecar.unlink()
                    console.print(f"[dim]Removed stale {sidecar.name}[/dim]")
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
            database = Database(str(db_path))
            await database.connect()
            try:
                await _migrate_or_refuse(database, "hp import")
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
    if not archive_has_secrets:
        err_console.print("[bold yellow]No secrets in this archive.[/bold yellow]")
        err_console.print("[yellow]The vault on this host is unchanged; if[/yellow]")
        err_console.print("[yellow]this is a fresh host the restored data[/yellow]")
        err_console.print("[yellow]references secrets it cannot decrypt.[/yellow]")
    elif not restore_secrets:
        err_console.print("[yellow]Archive contains secrets; they were NOT[/yellow]")
        err_console.print("[yellow]restored. Pass --restore-secrets to[/yellow]")
        err_console.print("[yellow]replace the vault on this host.[/yellow]")


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

    _get_artifact_store()
    lifecycle = _get_lifecycle()

    for question_text, key in questions:
        answer = typer.prompt(question_text, default="")
        if not answer:
            continue

        date_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
        fact_id = f"{date_prefix}-kb-policy-{key}"
        spec = {
            "id": fact_id,
            "kind": "kb-note",
            "intent": f"policy: {key}",
            "body": f"# Policy: {key}\n\n{answer}",
            "note_kind": "policy",
            "target": {"kind": "global"},
            "produced_by": {"session": "policy-init", "agent": "cli", "user": "cli"},
        }
        with contextlib.suppress(Exception):
            asyncio.run(lifecycle.propose(spec))

    console.print("\n[green]Policy initialization complete.[/green]")
    console.print(
        "[dim]These policies are now in the KB — the agent will find them via search_kb.[/dim]"
    )


@kb_app.command("reindex")
def kb_reindex(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    no_embeddings: bool = typer.Option(
        False, "--no-embeddings", help="Skip embedding computation; rebuild keyword metadata only"
    ),
) -> None:
    """Rewalk all applied kb-note artifacts, recompute embeddings, rebuild SQLite index."""
    if not yes:
        typer.confirm("Delete and rebuild all KB index entries?", abort=True)

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

    async def _kb_reindex_via_api(no_embs: bool) -> dict[str, Any] | None:
        settings = _get_settings()
        base_url = f"http://127.0.0.1:{settings.daemon_port}"
        # `/kb/reindex` requires the ADMIN SCOPE, i.e. a bearer token. This sent
        # only the admin-secret header, so every invocation 401'd and fell
        # silently through to the offline path - which is the path that wiped the
        # index (#388). Mint a short-lived admin token the same way the other
        # CLI-to-backend commands do.
        token, mint_error = await _mint_token_via_api(settings, "cli-kb-reindex", "admin")
        if token is None:
            if mint_error:
                err_console.print(f"[yellow]{mint_error}[/yellow]")
            return None
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        import httpx as _httpx

        async with _httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    f"{base_url}/kb/reindex",
                    params={"no_embeddings": no_embs},
                    headers=headers,
                )
                if resp.status_code == 200:
                    result: dict[str, Any] | None = resp.json()
                    return result
            except _httpx.ConnectError:
                pass  # backend down → fall back to direct DB below
            except Exception:
                # any other API failure: fall back to the direct-DB path, but
                # leave a breadcrumb instead of swallowing it silently.
                import logging as _logging

                _logging.getLogger(__name__).debug("token API mint failed, using DB", exc_info=True)
        return None

    async def _do(skip_embeddings: bool = False) -> dict[str, Any]:
        via_api = await _kb_reindex_via_api(skip_embeddings)
        if via_api is not None:
            return via_api

        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository
        from homepilot.executor import kb_note

        settings = _get_settings()
        data_dir = Path(settings.data_dir)
        db_path = data_dir / "homepilot.db"
        if not db_path.exists():
            return {"deleted": 0, "reindexed": 0, "errors": 0}

        database = Database(str(db_path))
        await database.connect()
        try:
            await _migrate_or_refuse(database, "hp kb reindex")
            repo = Repository(database)
            store = _get_artifact_store()

            rows = await database.fetchall(
                "SELECT id FROM doc_metadata WHERE source LIKE 'artifact:%'"
            )
            artifact_doc_ids = [r["id"] for r in rows]
            for doc_id in artifact_doc_ids:
                await database.execute("DELETE FROM vec_docs WHERE id = ?", (doc_id,))
            await database.execute("DELETE FROM doc_metadata WHERE source LIKE 'artifact:%'")
            await database.conn.commit()

            deleted = len(artifact_doc_ids)
            reindexed = 0
            errors = 0

            for artifact_meta in store.list(kind="kb-note", status="applied"):
                artifact_id = artifact_meta.get("id", "")
                try:
                    fm, body = store.read(artifact_id)
                    result = await kb_note.execute(fm, body, repo, no_embeddings=skip_embeddings)
                    if result.get("success"):
                        reindexed += 1
                    else:
                        errors += 1
                except Exception:  # broad catch: increments error counter
                    errors += 1
        finally:
            await database.close()

        return {"deleted": deleted, "reindexed": reindexed, "errors": errors}

    result = asyncio.run(_do(skip_embeddings=no_embeddings))
    console.print(
        f"[green]Reindex complete:[/green] "
        f"{result['deleted']} removed, "
        f"{result['reindexed']} reindexed, "
        f"{result['errors']} errors"
    )


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


async def _mint_token_via_api(
    settings: Any, label: str, scope: str
) -> tuple[str | None, str | None]:
    """Create an API token through the running backend, as an admin.

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

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/auth/tokens",
                json={"label": label, "scope": scope},
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
    hint = (
        " Pass an admin token in HP_ADMIN_TOKEN (the one the claim or "
        "Settings -> Tokens gave you), or bootstrap the admin secret with "
        "`hp init --non-interactive` (stores it in the vault)."
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

        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository

        settings = _get_settings()

        token, err = await _mint_token_via_api(settings, label, scope)
        if token:
            return token

        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        db_path = data_dir / "homepilot.db"
        try:
            database = Database(str(db_path))
            await database.connect()
            await _migrate_or_refuse(database, "hp token create")
            repo = Repository(database)

            # THE RULE. An unauthenticated mint is allowed only while there is no
            # admin to mint through: zero live tokens on the instance. The moment
            # one exists, this path refuses and names the way in - otherwise the
            # authenticated path above is decoration anyone can walk around.
            live = await repo.count_live_api_tokens()
            if live > 0:
                await database.close()
                err_console.print(
                    f"[red]Refusing to mint: {live} live token(s) exist - {_TOKEN_RULE}[/red]"
                )
                if err:
                    err_console.print(f"[yellow]{err}[/yellow]")
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
            await database.close()
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
        return None, (
            "No admin credential on this box: set HP_ADMIN_TOKEN to an admin "
            "token, or run `hp init` to store an admin secret in the vault."
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

    Invites are minted on the box by the operator and there is no admin API for
    them by design (the portal's whole point is a small surface), so the CLI
    talks to SQLite directly - WAL plus busy_timeout lets it write while the
    backend is running.
    """
    from homepilot.db.connection import Database
    from homepilot.portal.repository import InviteRepository

    settings = _get_settings()
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(str(data_dir / "homepilot.db"))
    await database.connect()
    await _migrate_or_refuse(database, "hp token revoke")
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
        err_console.print(f"[red]Invalid caps: {exc.errors()[0]['msg']}[/red]")
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

    rows = asyncio.run(_list())
    if output == "json":
        console.print(json.dumps([{**r, "state": invite_state(r)} for r in rows]))
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
            invite_state(row),
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
    """List hosts in the local inventory DB."""
    from homepilot.inventory.service import InventoryService

    async def _list() -> list[dict[str, Any]]:
        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository

        settings = _get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            return []
        db = Database(str(db_path))
        await db.connect()
        await _migrate_or_refuse(db, "hp inventory list")
        repo = Repository(db)
        svc = InventoryService(repo=repo)
        filt: dict[str, Any] = {}
        if role:
            filt["role"] = role
        if status:
            filt["status"] = status
        hosts = await svc.query_inventory(filter=filt or None)
        await db.close()
        return hosts

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


@inventory_app.command("show")
def inventory_show(
    hostname: str = typer.Argument(..., help="Hostname to look up"),
    output: str = typer.Option("text", "--output", "-o", help="Output format: text or json"),
) -> None:
    """Show details for a specific host."""

    async def _show() -> dict[str, Any] | None:
        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository

        settings = _get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            return None
        db = Database(str(db_path))
        await db.connect()
        await _migrate_or_refuse(db, "hp inventory show")
        repo = Repository(db)
        host = await repo.get_host_by_hostname(hostname)
        await db.close()
        return dict(host) if host else None

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
        token, err = await _mint_token_via_api(settings, "cli-inventory-refresh", "write")
        if token:
            import httpx

            port = getattr(settings, "daemon_port", 8000)
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/inventory/refresh",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"scope": scope} if scope else {},
                )
                resp.raise_for_status()
                return dict(resp.json())
        if err:
            err_console.print(f"[yellow]{err}[/yellow]")
            err_console.print(
                "[yellow]The backend refreshes inventory automatically; you can "
                "also use the UI 'Sync from Proxmox' button.[/yellow]"
            )
            raise typer.Exit(1)

        # Backend not running → refresh directly against the DB.
        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository
        from homepilot.inventory.service import InventoryService

        db_path = Path(settings.data_dir) / "homepilot.db"
        db = Database(str(db_path))
        await db.connect()
        await _migrate_or_refuse(db, "hp inventory refresh")
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
        result = await svc.refresh_inventory(scope=scope or None)
        await db.close()
        return result

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


@vault_app.command("set")
def vault_set(
    name: str = typer.Argument(..., help="Secret name (e.g. pve-token)"),
    value: str = typer.Option("", "--value", "-v", help="JSON value. Omit to enter interactively."),
) -> None:
    """Store a secret in the vault. Value must be valid JSON (e.g. '{\"token\": \"abc\"}')."""
    import json as _json

    vault = _get_vault()

    if not value:
        value = typer.prompt("Value (JSON)", hide_input=False)

    try:
        parsed = _json.loads(value)
    except _json.JSONDecodeError as exc:
        console.print(f"[red]Invalid JSON: {exc}[/red]")
        raise typer.Exit(1) from exc

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

    async def _delete() -> None:
        await vault.delete_secret(name)

    asyncio.run(_delete())
    console.print(f"[green]Secret '{name}' deleted[/green]")


async def _get_repo_for_webhook() -> tuple[Any, Any]:
    from homepilot.db.connection import Database
    from homepilot.db.repository import Repository

    settings = _get_settings()
    db_path = Path(settings.data_dir) / "homepilot.db"
    db = Database(str(db_path))
    await db.connect()
    await _migrate_or_refuse(db, "hp vault delete")
    return db, Repository(db)


@webhook_app.command("add")
def webhook_add(
    url: str = typer.Option(..., "--url", help="Webhook endpoint URL"),
    events: str = typer.Option(..., "--events", help="Comma-separated event types (use * for all)"),
    secret: str = typer.Option("", "--secret", help="HMAC signing key (optional)"),
    max_retries: int = typer.Option(3, "--max-retries", help="Max retry attempts"),
) -> None:
    """Register a new webhook endpoint."""
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    secret_val = secret or None

    async def _add() -> int:
        db, repo = await _get_repo_for_webhook()
        config_id: int = await repo.create_webhook_config(
            url=url, event_types=event_list, secret=secret_val, max_retries=max_retries
        )
        await db.conn.commit()
        await db.close()
        return config_id

    config_id = asyncio.run(_add())
    console.print(f"[green]Webhook #{config_id} registered[/green] — {url} → {events}")


@webhook_app.command("list")
def webhook_list(
    output: str = typer.Option("table", "--output", "-o", help="Output format: table or json"),
) -> None:
    """List all webhook configurations."""

    async def _list() -> list[dict[str, Any]]:
        db, repo = await _get_repo_for_webhook()
        configs: list[dict[str, Any]] = await repo.list_webhook_configs()
        await db.close()
        return configs

    configs = asyncio.run(_list())

    if output == "json":
        # The HMAC signing key is redacted, exactly as the table branch omits it
        # (#388). `--output json` is what gets piped into CI logs.
        # nosec B105 - "***" is the redaction MARKER that replaces the signing
        # key, not a credential. Flagged as a hardcoded password by bandit,
        # which cannot tell the two apart.
        redacted = [
            {**c, "secret": "***"} if c.get("secret") else dict(c)  # nosec B105
            for c in configs
        ]
        console.print(json.dumps(redacted, default=str))
        return

    if not configs:
        console.print("[dim]No webhooks configured[/dim]")
        return

    table = Table(title="Webhooks", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("URL")
    table.add_column("Events")
    table.add_column("Enabled")
    table.add_column("Max Retries")
    for c in configs:
        table.add_row(
            str(c["id"]),
            c["url"],
            c["event_types"],
            str(c["enabled"]),
            str(c["max_retries"]),
        )
    console.print(table)


@webhook_app.command("delete")
def webhook_delete(
    id: int = typer.Argument(..., help="Webhook config ID to delete"),
) -> None:
    """Delete a webhook configuration."""

    async def _delete() -> bool:
        db, repo = await _get_repo_for_webhook()
        deleted: bool = await repo.delete_webhook_config(id)
        await db.conn.commit()
        await db.close()
        return deleted

    deleted = asyncio.run(_delete())
    if deleted:
        console.print(f"[green]Webhook #{id} deleted[/green]")
    else:
        err_console.print(f"[red]Webhook #{id} not found[/red]")
        raise typer.Exit(1)


@webhook_app.command("test")
def webhook_test(
    id: int = typer.Argument(..., help="Webhook config ID to test"),
) -> None:
    """Send a test event to a webhook endpoint."""

    async def _test() -> None:
        from homepilot.events import deliver_with_retry

        db, repo = await _get_repo_for_webhook()
        config = await repo.get_webhook_config(id)
        if not config:
            err_console.print(f"[red]Webhook #{id} not found[/red]")
            raise typer.Exit(1)

        test_payload = {
            "event": "webhook_test",
            "message": f"Test event from webhook #{id}",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        console.print(f"[dim]Sending test event to {config['url']}...[/dim]")
        delivered = await deliver_with_retry(
            url=config["url"],
            payload=test_payload,
            secret=config.get("secret"),
            max_retries=config.get("max_retries", 3),
        )
        await db.close()
        if not delivered:
            # This command exists to answer "does delivery work". Printing
            # success for an endpoint that never answered is the one thing it
            # must not do (#388).
            err_console.print(
                f"[red]Test event was NOT accepted by {config['url']} "
                f"after {config.get('max_retries', 3) + 1} attempt(s)[/red]"
            )
            raise typer.Exit(1)
        console.print("[green]Test event delivered[/green]")

    asyncio.run(_test())


# ── Agent management ──────────────────────────────────────────────────────────

agent_app = typer.Typer(help="Agent hub management")
app.add_typer(agent_app, name="agent")


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
    import httpx

    settings = _get_settings()
    token, err = await _mint_token_via_api(settings, f"cli-{scope}", scope)
    if token is None:
        raise RuntimeError(
            err
            or (
                "The HomePilot backend is not reachable on "
                f"127.0.0.1:{getattr(settings, 'daemon_port', 8000)}. The agent hub runs "
                "inside it, so it has to be running to answer this."
            )
        )
    port = getattr(settings, "daemon_port", 8000)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method,
                f"http://127.0.0.1:{port}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"{resp.status_code} from {path}: {resp.text}")
            body: dict[str, Any] | list[Any] = resp.json()
            return body
    finally:
        # Best-effort cleanup: the prefix is the token's own first 16 chars.
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(
                    f"http://127.0.0.1:{port}/auth/tokens/{token[:16]}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).debug("could not revoke the CLI token", exc_info=True)


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
        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository

        settings = get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            err_console.print(f"[red]Database not found at {db_path}[/red]")
            raise typer.Exit(1)
        db = Database(str(db_path))
        await db.connect()
        try:
            await _migrate_or_refuse(db, "hp agent revoke")
            repo = Repository(db)
            return await repo.revoke_agent_credential(agent_id)
        finally:
            await db.close()

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

    This CLI path talks to the database directly, so it does NOT know whether the
    agent is currently connected (the API refuses that case). Removing a live
    agent's record pulls the credential out from under an open connection; stop
    the agent first.
    """

    async def _remove() -> bool:
        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository

        settings = get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            err_console.print(f"[red]Database not found at {db_path}[/red]")
            raise typer.Exit(1)
        db = Database(str(db_path))
        await db.connect()
        try:
            await _migrate_or_refuse(db, "hp agent remove")
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
