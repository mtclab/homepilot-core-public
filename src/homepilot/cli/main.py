from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.tokens import generate_api_token
from homepilot.config import get_settings
from homepilot.vault import VaultManager

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
) -> None:
    """Interactive setup: configure PVE, vault, and create ~/.hp/ structure."""
    import secrets as _secrets

    data_dir = Path(os.environ.get("HP_DATA_DIR", str(Path.home() / ".hp")))
    data_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = Path(os.environ.get("HP_ARTIFACTS_DIR", str(data_dir / "artifacts")))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    console.print("[bold]HomePilot Init[/bold]\n")

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

    secret_key = _secrets.token_urlsafe(32)

    env_path = data_dir / ".env"
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

    ssh_dir = data_dir / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)

    full_token, token_prefix, token_hash = generate_api_token()
    token_path = data_dir / "api-token"
    token_path.write_text(full_token + "\n")
    os.chmod(str(token_path), 0o600)

    async def _register_token() -> None:
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db_path = data_dir / "homepilot.db"
        database = Database(str(db_path))
        await database.connect()
        await run_migrations(database)
        repo = Repository(database)
        user_id = await repo.create_user(display_name="admin", auth_source="api_token")
        await repo.create_api_token(
            user_id=str(user_id),
            token_type="personal",
            prefix=token_prefix,
            hash=token_hash,
            scope="read,write",
            expires_at=None,
        )
        await database.conn.commit()
        await database.close()

        vault = VaultManager(data_dir, passphrase)
        await vault.ensure_master_identity()
        await vault.store_secret("secret-key", {"value": secret_key})
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
    console.print("  secret-key   → vault (used for JWT signing)")
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
    table.add_row("Vault", "unlocked" if settings.vault_passphrase else "locked")

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
        console.print(f"[green]Approved: {id}[/green]")
    except Exception as e:
        err_console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


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


@artifacts_app.command("apply")
def artifacts_apply(
    id: str = typer.Argument(..., help="Artifact ID"),
    approve: bool = typer.Option(False, "--approve", help="Approve first, then apply"),
) -> None:
    """Apply an approved artifact (or approve+apply together)."""
    from homepilot.mcp.executor_tools import apply_artifact

    lifecycle = _get_lifecycle()

    if approve:
        try:
            asyncio.run(lifecycle.approve(id, user="cli"))
            console.print(f"[green]Approved: {id}[/green]")
        except Exception as e:
            err_console.print(f"[red]Approve failed: {e}[/red]")
            raise typer.Exit(1) from e

    try:
        result = asyncio.run(apply_artifact(lifecycle, id, approved_by="cli"))
        if result.get("success"):
            console.print(f"[green]Applied: {id}[/green]")
        else:
            err_console.print(f"[red]Apply failed: {result.get('error', 'unknown')}[/red]")
            raise typer.Exit(1)
    except NotImplementedError as e:
        err_console.print(f"[red]Executor not implemented: {e}[/red]")
        err_console.print(
            "[dim]Per-kind executors need proxmox/ssh/vault injection "
            "— use lifecycle.mark_applied() manually[/dim]"
        )
        raise typer.Exit(1) from e
    except Exception as e:
        err_console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


@artifacts_app.command("replay")
def artifacts_replay(
    id: str = typer.Argument(..., help="Artifact ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Re-apply an already-applied artifact."""
    from homepilot.mcp.executor_tools import apply_artifact

    if not yes:
        typer.confirm(f"Replay artifact {id}? This will re-execute the spec.", abort=True)

    lifecycle = _get_lifecycle()
    try:
        result = asyncio.run(apply_artifact(lifecycle, id, approved_by="cli"))
        if result.get("success"):
            console.print(f"[green]Replayed: {id}[/green]")
        else:
            err_console.print(f"[red]Replay failed: {result.get('error', 'unknown')}[/red]")
            raise typer.Exit(1)
    except NotImplementedError as e:
        err_console.print(f"[red]Executor not implemented: {e}[/red]")
        raise typer.Exit(1) from e
    except Exception as e:
        err_console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


@artifacts_app.command("revoke")
def artifacts_revoke(
    id: str = typer.Argument(..., help="Artifact ID"),
    reason: list[str] | None = typer.Option(None, "--reason", help="Revocation reason"),  # noqa: B008
) -> None:
    """Revoke an applied artifact."""
    from homepilot.mcp.executor_tools import revoke_artifact

    lifecycle = _get_lifecycle()
    reason_str = " ".join(reason) if reason else None
    try:
        result = asyncio.run(revoke_artifact(lifecycle, id, user="cli", reason=reason_str))
        console.print(f"[yellow]Revoked: {id}[/yellow]")
        if result.get("has_rollback"):
            console.print("[dim]Rollback spec exists in artifact body[/dim]")
    except Exception as e:
        err_console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e


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


@app.command()
def export() -> None:
    """Produce tarball (artifacts repo + DB + README)."""
    settings = _get_settings()
    data_dir = Path(settings.data_dir)
    artifacts_dir = Path(settings.artifacts_dir)
    db_path = data_dir / "homepilot.db"

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    tarball_name = f"homepilot-export-{ts}.tar.gz"
    tarball_path = Path.cwd() / tarball_name

    with tarfile.open(str(tarball_path), "w:gz") as tar:
        if artifacts_dir.exists():
            tar.add(str(artifacts_dir), arcname="artifacts")
        if db_path.exists():
            tar.add(str(db_path), arcname="homepilot.db")

        readme = (
            "# HomePilot Export\n\n"
            f"Generated: {datetime.now(UTC).isoformat()}\n\n"
            "## Contents\n\n"
            "- `artifacts/` — Git repo of artifact Markdown files\n"
            "- `homepilot.db` — SQLite database (inventory, KB, journal)\n\n"
            "## How to read without HomePilot\n\n"
            "- Artifacts: Markdown files in `artifacts/YYYY/MM/<id>.md`\n"
            '- Database: `sqlite3 homepilot.db "SELECT * FROM hosts;"`\n'
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, prefix="hp-readme-"
        ) as _rf:
            _rf.write(readme)
            _readme_path = _rf.name
        try:
            tar.add(_readme_path, arcname="README.md")
        finally:
            Path(_readme_path).unlink(missing_ok=True)

    console.print(f"[green]Exported to {tarball_path}[/green]")


@app.command("import")
def import_backup(
    path: Path = typer.Argument(..., help="Path to homepilot-export-*.tar.gz"),  # noqa: B008
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Restore artifacts repo + DB from a tarball. Backs up current DB first."""
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

    if not yes:
        typer.confirm(
            f"This will overwrite {artifacts_dir} and {db_path}. "
            "Current DB will be backed up. Continue?",
            abort=True,
        )

    # Back up current DB before any overwrite
    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup_dest = backups_dir / f"homepilot-pre-import-{ts}.db"
        import shutil

        shutil.copy2(str(db_path), str(backup_dest))
        console.print(f"[dim]Current DB backed up to {backup_dest}[/dim]")

    try:
        with tarfile.open(str(path), "r:gz") as tar:
            # Validate all members before extracting — prevent path traversal
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    err_console.print(f"[red]Unsafe path in archive: {member.name}[/red]")
                    raise typer.Exit(1)

            # Extract artifacts/
            artifact_members = [m for m in tar.getmembers() if m.name.startswith("artifacts")]
            if artifact_members:
                artifacts_dir.mkdir(parents=True, exist_ok=True)
                tar.extractall(str(data_dir), members=artifact_members, filter="data")
                console.print(f"[green]Artifacts restored to {artifacts_dir}[/green]")

            # Extract DB
            try:
                db_member = tar.getmember("homepilot.db")
                tar.extractall(str(data_dir), members=[db_member], filter="data")
                console.print(f"[green]Database restored to {db_path}[/green]")
            except KeyError:
                console.print("[yellow]No homepilot.db found in archive — skipped[/yellow]")

    except tarfile.TarError as e:
        err_console.print(f"[red]Failed to read archive: {e}[/red]")
        raise typer.Exit(1) from e

    console.print("[bold green]Import complete.[/bold green]")


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
        ("Jump server host", "jump_server"),
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
        try:
            import httpx

            resp = httpx.get("http://localhost:11434/api/version", timeout=2.0)
            resp.raise_for_status()
        except Exception:  # intentional broad catch — prints user-friendly message and exits
            console.print(
                "[yellow]Warning: Ollama not reachable at localhost:11434. "
                "Reindex will fall back to keyword-only search (no embeddings).[/yellow]"
            )
            no_embeddings = True

    async def _kb_reindex_via_api(no_embs: bool) -> dict[str, Any] | None:
        settings = _get_settings()
        base_url = f"http://127.0.0.1:{settings.daemon_port}"
        headers: dict[str, str] = {}
        admin_secret = settings.admin_secret
        if admin_secret:
            headers["X-HP-Admin-Secret"] = admin_secret
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
                pass
            except Exception:  # intentional broad catch — silently falls back to direct DB
                pass
        return None

    async def _do(skip_embeddings: bool = False) -> dict[str, Any]:
        via_api = await _kb_reindex_via_api(skip_embeddings)
        if via_api is not None:
            return via_api

        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
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
            await run_migrations(database)
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


async def _mint_token_via_api(
    settings: Any, label: str, scope: str
) -> tuple[str | None, str | None]:
    """Create an API token through the running backend's admin endpoint.

    Avoids sqlite write-lock contention with a live backend (the backend owns
    the DB). Returns ``(token, error)``:
      * ``(token, None)`` — created via the API.
      * ``(None, None)``  — backend not reachable; caller should fall back to
        the direct-DB path.
      * ``(None, msg)``   — backend reached but refused (e.g. admin-secret not
        configured / mismatched); caller should surface ``msg`` and NOT touch
        the DB (it is locked).

    The admin secret is resolved exactly like the backend's
    ``resolve_admin_secret`` (settings/env, then vault) — no passphrase
    fallback, which previously produced confusing 403s."""
    import httpx

    admin_secret = getattr(settings, "admin_secret", "") or ""
    if not admin_secret and hasattr(settings, "_try_vault_secret"):
        admin_secret = settings._try_vault_secret("admin-secret") or ""
    port = getattr(settings, "daemon_port", 8000)

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/auth/tokens",
                json={"label": label, "scope": scope},
                headers={"x-hp-admin-secret": admin_secret},
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
        " Bootstrap the admin secret with `hp init --non-interactive` "
        "(stores it in the vault), or stop the backend to create a token via "
        "the local DB."
    )
    return None, f"Backend refused token creation ({resp.status_code}): {detail}.{hint}"


@token_app.command("create")
def token_create(
    label: str = typer.Option("admin", "--label", "-l", help="Display name for the token owner"),
    scope: str = typer.Option(
        "read,write", "--scope", "-s", help="Comma-separated scopes (e.g. read,write)"
    ),
    output: str = typer.Option("plain", "--output", "-o", help="Output format: plain or json"),
) -> None:
    """Create an API token non-interactively (safe for Docker / CI environments)."""

    async def _create() -> str:
        from aiosqlite import OperationalError

        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        settings = _get_settings()

        token, err = await _mint_token_via_api(settings, label, scope)
        if token:
            return token
        if err:
            # Backend is up but refused — do not fall back to the DB path
            # (it would only hit the write lock). Surface the real reason.
            err_console.print(f"[red]{err}[/red]")
            raise typer.Exit(1)

        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        db_path = data_dir / "homepilot.db"
        try:
            database = Database(str(db_path))
            await database.connect()
            await run_migrations(database)
            repo = Repository(database)

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
                expires_at=None,
            )
            await database.conn.commit()
            await database.close()
        except OperationalError as exc:
            if "database is locked" in str(exc).lower():
                err_console.print(
                    "[red]Database is locked — the backend may be running. "
                    "Try: hp token create (uses API when backend is live), "
                    "or stop the backend first.[/red]"
                )
                raise typer.Exit(1) from exc
            raise
        return full_token

    token = asyncio.run(_create())

    if output == "json":
        console.print(json.dumps({"token": token, "scope": scope}))
    else:
        console.print(token)


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
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        settings = _get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            return []
        db = Database(str(db_path))
        await db.connect()
        await run_migrations(db)
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
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        settings = _get_settings()
        db_path = Path(settings.data_dir) / "homepilot.db"
        if not db_path.exists():
            return None
        db = Database(str(db_path))
        await db.connect()
        await run_migrations(db)
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
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository
        from homepilot.inventory.service import InventoryService

        db_path = Path(settings.data_dir) / "homepilot.db"
        db = Database(str(db_path))
        await db.connect()
        await run_migrations(db)
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
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository

    settings = _get_settings()
    db_path = Path(settings.data_dir) / "homepilot.db"
    db = Database(str(db_path))
    await db.connect()
    await run_migrations(db)
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
        console.print(json.dumps(configs, default=str))
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
        await deliver_with_retry(
            url=config["url"],
            payload=test_payload,
            secret=config.get("secret"),
            max_retries=config.get("max_retries", 3),
        )
        await db.close()
        console.print("[green]Test event sent[/green]")

    asyncio.run(_test())


# ── Agent management ──────────────────────────────────────────────────────────

agent_app = typer.Typer(help="Agent hub management")
app.add_typer(agent_app, name="agent")


@agent_app.command("bootstrap")
def agent_bootstrap(
    hub_host: str = typer.Option("localhost", help="Agent hub host"),
    hub_port: int = typer.Option(8443, help="Agent hub port"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Generate a bootstrap token for agent enrollment."""
    from homepilot.agent_hub.tokens import generate_bootstrap_token

    token = generate_bootstrap_token()
    get_settings()

    if json_output:
        console.print_json(
            data={
                "token": token,
                "hub_host": hub_host,
                "hub_port": hub_port,
                "command": (
                    f"HP_AGENT_HUB_HOST={hub_host} "
                    f"HP_AGENT_HUB_PORT={hub_port} "
                    f"HP_AGENT_AUTH_TOKEN={token} "
                    f"agent-host"
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
            f"agent-host"
        )
        console.print(Panel(cmd, title="Agent command", subtitle="Run on managed host"))
        console.print(
            "\n[dim]Store the token hash in HP_AGENT_HUB_AUTH_TOKEN for hub verification.[/dim]"
        )
        console.print(f"[dim]Token hash: {token[:8]}...[/dim]")


@agent_app.command("list")
def agent_list() -> None:
    """List connected agents."""
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None:
        err_console.print("[yellow]Agent hub not enabled[/yellow]")
        raise typer.Exit(1)
    agents = registry.list_connected()
    if not agents:
        console.print("[dim]No agents connected[/dim]")
        return
    table = Table(title="Connected Agents")
    table.add_column("Agent ID", style="cyan")
    table.add_column("Hostname", style="green")
    table.add_column("OS", style="blue")
    table.add_column("Last Heartbeat", style="dim")
    table.add_column("Stale (s)", style="yellow")
    for a in agents:
        sys_info = a.get("system_info", {})
        table.add_row(
            a["agent_id"],
            a["hostname"],
            f"{sys_info.get('os', '?')} {sys_info.get('arch', '?')}",
            a["last_heartbeat"],
            str(a["stale_seconds"]),
        )
    console.print(table)


@agent_app.command("token")
def agent_token(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show the agent hub auth token for configuring agents."""
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None or registry.hub_server is None:
        err_console.print("[yellow]Agent hub not enabled[/yellow]")
        raise typer.Exit(1)
    hub = registry.hub_server
    token = hub.auth_token or ""
    if json_output:
        console.print_json(
            data={
                "auth_token": token,
                "hub_host": hub.host,
                "hub_port": hub.port,
            }
        )
    else:
        if token:
            console.print(
                Panel(token, title="Agent Hub Auth Token", subtitle="Set HP_AGENT_HUB_AUTH_TOKEN")
            )
        else:
            console.print("[yellow]No auth token configured — set HP_AGENT_HUB_AUTH_TOKEN[/yellow]")
        console.print(f"[dim]Hub endpoint: {hub.host}:{hub.port}[/dim]")


@agent_app.command("bootstrap")
def agent_bootstrap_cmd(
    hub_host: str = typer.Option("localhost", help="Agent hub host"),
    hub_port: int = typer.Option(8443, help="Agent hub port"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Generate a one-time bootstrap token for agent enrollment."""
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None or registry.hub_server is None:
        err_console.print("[yellow]Agent hub not enabled[/yellow]")
        raise typer.Exit(1)

    hub = registry.hub_server
    token = asyncio.run(hub._token_store.create())

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
                    f"agent-host"
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
            f"agent-host"
        )
        console.print(Panel(cmd, title="Agent command", subtitle="Run on managed host"))


@agent_app.command("zabbix-push")
def agent_zabbix_push(
    agent_id: str = typer.Argument(..., help="Agent ID or hostname"),
) -> None:
    """Trigger Zabbix metrics push for a connected agent."""
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None or registry.hub_server is None:
        err_console.print("[yellow]Agent hub not enabled[/yellow]")
        raise typer.Exit(1)

    agent = registry.get(agent_id) or registry.get_by_hostname(agent_id)
    if not agent:
        err_console.print(f"[red]Agent '{agent_id}' not connected[/red]")
        raise typer.Exit(1)

    hub = registry.hub_server

    async def _push() -> dict[str, Any]:
        return await hub.send_zabbix_push(agent.agent_id)

    try:
        result = asyncio.run(_push())
        metrics_sent = result.get("metrics_sent", 0)
        zabbix_resp = result.get("zabbix_response", {})
        console.print(f"[green]Pushed {metrics_sent} metrics to Zabbix[/green]")
        if zabbix_resp:
            info = zabbix_resp.get("info", "")
            console.print(f"[dim]Zabbix response: {info}[/dim]")
    except ConnectionError as exc:
        err_console.print(f"[red]Agent not connected: {exc}[/red]")
        raise typer.Exit(1) from None
    except TimeoutError:
        err_console.print("[red]Zabbix push timed out[/red]")
        raise typer.Exit(1) from None
