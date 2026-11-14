"""Gate: the README CLI reference must match the real Typer command tree (#432).

The README documented `hp artifacts drift <id>`, which has never existed - drift
is a top-level `hp drift`. It also omitted several real commands (`hp artifacts
replay`, `hp token list`, `hp token revoke`, `hp agent revoke`, and `hp init` in
the reference block).

A documented-but-absent command is the worse half: an operator (or an LLM
reading the README) plans around a command that exits with a usage error.

Commands are discovered by walking the real Typer command tree (visible
commands only): since remote mode (#remote-cli) commands are registered from
more than one module and old group names stay as hidden aliases.

Teeth: add a command to ``cli/main.py`` without documenting it, or document one
that does not exist, and this fails naming it.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"

# Commands intentionally absent from the README reference block, each with a
# reason. Keep empty unless something genuinely warrants it.
_UNDOCUMENTED_ALLOWLIST: frozenset[str] = frozenset()


def _strip_args(command: str) -> str:
    """`hp artifacts show <id>` -> `hp artifacts show`."""
    return re.sub(r"\s*<[^>]+>", "", command).strip()


def _registered_commands() -> set[str]:
    """Every visible `hp ...` command, walked from the real command tree.

    The tree, not a parse of ``cli/main.py``: the operator commands are
    registered from ``cli/ops.py`` and the groups have pre-3.13 names kept as
    hidden aliases (`hp artifacts` = `hp changes`), which a regex over one file
    cannot see. Hidden commands and groups are not documented, by design.
    """
    import typer

    from homepilot.cli.main import app

    commands: set[str] = set()

    def walk(cmd: object, prefix: str) -> None:
        # Typer vendors its own click, so its groups are duck-typed here.
        subs = getattr(cmd, "commands", None) or {}
        if not subs:
            commands.add(prefix)
        for name, sub in subs.items():
            if not getattr(sub, "hidden", False):
                walk(sub, f"{prefix} {name}")

    walk(typer.main.get_command(app), "hp")
    return commands


def _documented_commands() -> set[str]:
    """Every `hp ...` line inside the README's fenced CLI reference block."""
    if not _README.exists():
        raise AssertionError(f"README not found at {_README}")
    text = _README.read_text(encoding="utf-8")

    # The reference block is the fenced block containing the `hp drift` line.
    # Accept ANY language tag: the README also has ```json / ```yaml fences, and
    # a pattern that only matched ```bash mis-paired the delimiters and shifted
    # every subsequent block boundary.
    documented: set[str] = set()
    for block in re.findall(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.DOTALL):
        if not re.search(r"^hp drift\b", block, re.MULTILINE):
            continue
        for line in block.splitlines():
            if not line.startswith("hp "):
                continue
            documented.add(_strip_args(line.split("#", 1)[0]))
    return documented


def test_cli_reference_block_was_found() -> None:
    """Guard the guard: an unmatched block would make both tests vacuous."""
    documented = _documented_commands()
    assert len(documented) >= 20, (
        f"could not locate the README CLI reference block (found {documented})"
    )


def test_every_registered_command_is_documented() -> None:
    missing = _registered_commands() - _documented_commands() - _UNDOCUMENTED_ALLOWLIST
    assert not missing, f"CLI commands missing from the README reference: {sorted(missing)}"


def test_readme_documents_no_nonexistent_command() -> None:
    invented = _documented_commands() - _registered_commands()
    assert not invented, (
        f"README documents command(s) that do not exist in cli/main.py: {sorted(invented)}"
    )
