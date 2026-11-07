"""Secret files are 0600 from the moment they exist (#690).

The shape the audit found in five places - `path.write_text(secret)` followed by
`path.chmod(0o600)` - creates the file under the process umask (typically
0644), so the local CLI admin token, the auto-generated vault passphrase and
hub token, and the vault's protected identity were readable by other local
users until the chmod landed, and stayed that way if the process died in
between. Every secret write now goes through `common.write_private` (a 0600
temp file renamed into place).

Teeth: write any secret file the old way anywhere in src/ and the AST gate
below fails, naming the function; break `write_private` and the behavioural
tests fail.
"""

from __future__ import annotations

import ast
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from homepilot.common import write_private

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"

# (file relative to src/homepilot, enclosing function) -> why a chmod 0o600 is
# right there. Anything else that chmods to 0o600 is the write-then-chmod shape.
ALLOWED_CHMODS = {
    # Normalises modes on the STAGED copy inside the private staging directory,
    # before it is moved into the data dir under a 0o077 umask.
    ("cli/main.py", "_restore_secret_entry"),
}


def _mentions_0600(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and n.value == 0o600 for n in ast.walk(node))


def _chmod_0600_sites() -> Iterator[tuple[str, str, int]]:
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "chmod" or not any(_mentions_0600(a) for a in node.args):
                continue
            scope: ast.AST | None = node
            while scope is not None and not isinstance(
                scope, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                scope = parents.get(scope)
            func_name = scope.name if scope is not None else "<module>"
            yield rel, func_name, node.lineno


def test_no_secret_file_is_written_then_chmodded() -> None:
    offenders = [
        f"{rel}:{line} in {func}()"
        for rel, func, line in _chmod_0600_sites()
        if (rel, func) not in ALLOWED_CHMODS
    ]
    assert not offenders, (
        "chmod(0o600) after the fact leaves the file readable until it lands - "
        "use homepilot.common.write_private:\n" + "\n".join(offenders)
    )


def test_the_allowlist_has_no_dead_entries() -> None:
    live = {(rel, func) for rel, func, _ in _chmod_0600_sites()}
    assert live >= ALLOWED_CHMODS, f"stale allowlist entries: {ALLOWED_CHMODS - live}"


@contextmanager
def _umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_private_is_0600_even_under_a_permissive_umask(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    with _umask(0o000):
        write_private(target, "s3cret")
    assert target.read_text() == "s3cret"
    assert _mode(target) == 0o600


def test_write_private_replaces_a_loose_file_with_a_private_one(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_text("old")
    target.chmod(0o644)
    write_private(target, b"new")
    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["secret"], "a temp file was left behind"


def test_generated_passphrase_and_hub_token_are_private(tmp_path: Path) -> None:
    from homepilot.config import Settings

    data_dir = tmp_path / "hp"
    with _umask(0o000):
        settings = Settings(data_dir=str(data_dir), artifacts_dir=str(data_dir / "artifacts"))
        settings._auto_generate_passphrase()
        settings._auto_generate_hub_token()
    for name in (".vault_passphrase", ".agent_hub_token"):
        assert _mode(data_dir / name) == 0o600, name


async def test_the_vault_identity_is_private(tmp_path: Path) -> None:
    from homepilot.vault import VaultManager

    with _umask(0o000):
        await VaultManager(tmp_path, "a-test-passphrase").ensure_master_identity()
    protected = next(tmp_path.rglob("master.protected"))
    assert _mode(protected) == 0o600
