"""Gate: the README CLI reference must match the real Typer command tree (#432).

The README documented `hp artifacts drift <id>`, which has never existed - drift
is a top-level `hp drift`. It also omitted several real commands (`hp artifacts
replay`, `hp token list`, `hp token revoke`, `hp agent revoke`, and `hp init` in
the reference block).

A documented-but-absent command is the worse half: an operator (or an LLM
reading the README) plans around a command that exits with a usage error.

Commands are discovered by parsing ``cli/main.py`` for ``@<app>.command(...)``
decorators and mapping each Typer sub-app to its ``add_typer(name=...)`` group,
rather than importing and walking Typer internals - the parse is stable across
Typer versions and needs no app instantiation.

Teeth: add a command to ``cli/main.py`` without documenting it, or document one
that does not exist, and this fails naming it.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLI_SOURCE = _REPO_ROOT / "src" / "homepilot" / "cli" / "main.py"
_README = _REPO_ROOT / "README.md"

# Commands intentionally absent from the README reference block, each with a
# reason. Keep empty unless something genuinely warrants it.
_UNDOCUMENTED_ALLOWLIST: frozenset[str] = frozenset()


def _strip_args(command: str) -> str:
    """`hp artifacts show <id>` -> `hp artifacts show`."""
    return re.sub(r"\s*<[^>]+>", "", command).strip()


def _registered_commands() -> set[str]:
    """Every `hp ...` command registered in cli/main.py."""
    if not _CLI_SOURCE.exists():
        raise AssertionError(f"CLI source not found at {_CLI_SOURCE}")
    src = _CLI_SOURCE.read_text(encoding="utf-8")

    groups: dict[str, str] = dict(re.findall(r'app\.add_typer\((\w+),\s*name="([^"]+)"\)', src))
    groups["app"] = ""  # the root Typer app contributes no group prefix

    commands: set[str] = set()
    for match in re.finditer(r'@(\w+)\.command\(\s*(?:"([^"]+)")?[^)]*\)\s*\ndef (\w+)', src):
        app_var, explicit_name, func_name = match.groups()
        if app_var not in groups:
            continue
        name = explicit_name or func_name.replace("_", "-")
        commands.add(f"hp {groups[app_var]} {name}".replace("  ", " ").strip())
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
