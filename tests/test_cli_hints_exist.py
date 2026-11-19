"""Every `hp ...` command the product names to a person is a real, visible command.

3.14 walk: ``propose_artifact`` answered "Review with: hp artifacts show <id>".
`hp artifacts` became `hp changes` in 3.13 and survives only as a hidden alias,
so the hint named a command `hp --help` does not list; the same stale group
names (`hp artifacts`, `hp inventory`, `hp agent`) were in 40-odd other
messages, help texts and refusals.

The gate walks the REAL Typer command tree (as tests/test_readme_cli_parity.py
does) and resolves every `hp <word> <word>...` in the product's text - every
string literal under src/homepilot (messages, help, docstrings - typer's help
is a docstring) and the console's source under web/src - down to a visible
command. Words after a leaf command are its arguments and are not checked.

Teeth: put "Review with: hp artifacts show" back in artifact_tools.py and the
source gate names the line; the propose journey fails on the message itself.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .test_mcp_input_fuzz_gate import real_ctx  # noqa: F401 - the shared fixture

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "homepilot"
WEB = ROOT / "web" / "src"

# `hp` followed by words, not part of a longer name (`hp_...`, `hp-agent`, a path).
HP = re.compile(r"(?<![\w./-])hp((?: +[a-z][a-z0-9-]*)+)")

# Where "hp" is the subject of an English sentence about the installed client
# (`hp self-update`: "hp is now 3.14.0"), not a command. Each word with its use.
PROSE = {
    "is": "hp is already / now / unchanged <version>",
    "was": "This hp was installed with <tool>",
    "runs": "This hp runs from <prefix>",
    "reports": "hp reports <version>",
}


def _tree() -> Any:
    import typer

    from homepilot.cli.main import app

    return typer.main.get_command(app)


def _problem(root: Any, words: list[str]) -> str | None:
    """Why `hp <words>` is not a visible command, or None when it is."""
    if words[0] in PROSE:
        return None
    cmd, said = root, "hp"
    for word in words:
        subs = getattr(cmd, "commands", None) or {}
        if not subs:
            return None  # a leaf: the rest are its arguments
        sub = subs.get(word)
        if sub is None:
            if cmd is root:
                return f"`{said} {word}` is not a command"
            return f"`{said}` has no `{word}`"
        if getattr(sub, "hidden", False):
            return f"`{said} {word}` is a hidden alias - name the command `hp --help` lists"
        cmd, said = sub, f"{said} {word}"
    return None


def _python_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _web_lines(path: Path) -> list[tuple[int, str]]:
    return list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1))


def _offenders(texts: list[tuple[str, int, str]]) -> list[str]:
    root = _tree()
    out = []
    for where, line, text in texts:
        for match in HP.finditer(text):
            problem = _problem(root, match.group(1).split())
            if problem:
                out.append(f"{where}:{line}: {problem}")
    return out


def _product_texts() -> list[tuple[str, int, str]]:
    texts = [
        (str(p.relative_to(ROOT)), line, s)
        for p in sorted(SRC.rglob("*.py"))
        for line, s in _python_strings(p)
    ]
    for p in sorted([*WEB.rglob("*.svelte"), *WEB.rglob("*.ts")]):
        if p.name.endswith(".test.ts"):
            continue
        texts += [(str(p.relative_to(ROOT)), line, s) for line, s in _web_lines(p)]
    return texts


def test_every_hp_command_the_product_names_is_a_visible_command() -> None:
    offenders = _offenders(_product_texts())
    assert not offenders, "a hint names a command the CLI does not list:\n" + "\n".join(offenders)


def test_the_gate_has_teeth() -> None:
    """The walk itself: the walk's stale hint is caught, a real one passes."""
    assert _offenders([("x", 1, "Review with: hp artifacts show 2026-01-01-x")]) == [
        "x:1: `hp artifacts` is a hidden alias - name the command `hp --help` lists"
    ]
    assert _offenders([("x", 1, "run `hp chnages show`")]) == ["x:1: `hp chnages` is not a command"]
    assert _offenders([("x", 1, "`hp changes shw 1`")]) == ["x:1: `hp changes` has no `shw`"]
    assert _offenders([("x", 1, "Review with: hp changes show 2026-01-01-x")]) == []
    # The product does name commands - the scan is not vacuously empty.
    assert sum(len(HP.findall(t)) for _, _, t in _product_texts()) > 50


async def test_propose_artifact_names_the_real_review_command(real_ctx) -> None:  # noqa: F811
    """The walk's journey: propose over the MCP dispatcher, read the message the
    assistant relays, and resolve the command it names in the real CLI."""
    import json
    from types import SimpleNamespace

    from homepilot.mcp import server as mcp_server

    spec = {
        "id": "2026-11-18-review-hint",
        "kind": "proxmox-api-sequence",
        "intent": "read the node status",
        "body": (
            "# s\n\n```yaml proxmox-api-spec\nsteps:\n"
            "  - id: read\n    method: GET\n    path: /nodes/pve/status\n```\n"
        ),
        "target": {"kind": "cluster"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s", "agent": "a", "user": "u"},
    }
    scope = mcp_server._mcp_token_scope_var.set("admin")
    try:
        result = await mcp_server._on_call_tool(
            None, SimpleNamespace(name="propose_artifact", arguments={"spec": json.dumps(spec)})
        )
    finally:
        mcp_server._mcp_token_scope_var.reset(scope)
    text = result.content[0].text if result.content else ""
    assert not result.is_error, text
    message = json.loads(text)["message"]
    assert "hp changes show 2026-11-18-review-hint" in message, message
    assert _offenders([("propose_artifact", 1, message)]) == []
