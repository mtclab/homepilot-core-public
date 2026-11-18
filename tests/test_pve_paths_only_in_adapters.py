"""PVE endpoint paths live ONLY in the adapter modules (#717).

The rule, owner decision 2026-11-18 (amending the 2026-08-26 mandate that
ProxmoxClient "gains no new endpoint knowledge"): HomePilot may call Proxmox
endpoints directly, but a PVE API path is written down in exactly one family
of modules - ``ADAPTER_MODULES`` below. Everything else asks an adapter by name
(``pve_reads.cluster_guests(proxmox)``, ``PveSdnGateway.list_vnets()``).

The audit that filed #717 found the old mandate followed by a dozen new
endpoints in ProxmoxClient AND by reads hand-rolled far from any adapter:
``provision/probes.py`` read ``/cluster/sdn/vnets`` and ``/zones`` although
``pve_sdn`` called itself "the ONE place HomePilot names a PVE SDN endpoint",
and ``inventory/service.py`` built its own node, guest and network paths.
Nothing noticed, because nothing checked.

This does: every string literal in ``src/homepilot`` (f-strings included,
docstrings excluded) that IS a PVE API path - it starts with one of PVE's
top-level collections and holds no whitespace - fails the build with
file:line unless it is in an adapter module.

Teeth: put ``"/cluster/sdn/vnets"`` back into ``provision/probes.py`` (or any
``proxmox.read("/nodes")`` back into ``inventory/service.py``) and
``test_no_pve_path_literal_outside_the_adapters`` fails naming the line.
``test_the_gate_bites_on_the_code_it_replaced`` holds that on a copy of the
pre-#717 lines, so the detector cannot quietly go blind.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "homepilot"

# Where a PVE path may be written. Relative to src/homepilot.
ADAPTER_MODULES = frozenset(
    {
        "adapters/proxmox.py",
        "adapters/pve_reads.py",
        "adapters/pve_sdn.py",
        "adapters/pve_access.py",
        "adapters/pve_firewall_guard.py",
    }
)

# Known exceptions, each with the reason it is not moved yet. Empty since
# 3.14: main.py's node read and proxmox_api_read's prefixes moved into
# adapters/pve_reads.py. Keyed by file, then the exact literal.
EXCEPTIONS: dict[str, dict[str, str]] = {}

# PVE's top-level API collections. A path is one of these, optionally slashed,
# followed by nothing, a sub-path or a query - and never whitespace (prose that
# mentions "/cluster/nextid, which ..." is not a path).
_COLLECTIONS = ("nodes", "cluster", "storage", "access", "pools", "version")
_PATH = re.compile(r"^/?(?:" + "|".join(_COLLECTIONS) + r")(?:[/?]\S*)?$")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _literal_text(node: ast.AST) -> str | None:
    """A string literal's text; an f-string's with each `{...}` as `{}`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    return None


def pve_path_literals(source: str) -> list[tuple[int, str]]:
    """Every (line, literal) in `source` that is a PVE API path."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    # The constant pieces of an f-string are reported as the whole f-string.
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _literal_text(node)
        # A bare collection name ("nodes", "version") is a dict key far more
        # often than a path; a path names its slash.
        if text is not None and "/" in text and _PATH.match(text):
            found.append((getattr(node, "lineno", 0), text))
    return sorted(found)


def violations(root: Path = SRC) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in ADAPTER_MODULES:
            continue
        allowed = EXCEPTIONS.get(rel, {})
        for line, text in pve_path_literals(path.read_text(encoding="utf-8")):
            if text not in allowed:
                out.append(f"src/homepilot/{rel}:{line}: {text!r}")
    return out


def test_no_pve_path_literal_outside_the_adapters() -> None:
    found = violations()
    assert not found, (
        "PVE API paths outside the adapter modules ("
        + ", ".join(sorted(ADAPTER_MODULES))
        + "). Add a named read to adapters/pve_reads.py (or use pve_sdn / "
        "ProxmoxClient) and call it instead:\n  " + "\n  ".join(found)
    )


def test_every_exception_is_still_needed() -> None:
    """An exception whose literal is gone is a stale excuse: remove it."""
    stale: list[str] = []
    for rel, literals in EXCEPTIONS.items():
        present = {text for _, text in pve_path_literals((SRC / rel).read_text(encoding="utf-8"))}
        stale.extend(f"{rel}: {lit!r}" for lit in literals if lit not in present)
    assert not stale, "exceptions no longer needed - delete them:\n  " + "\n  ".join(stale)


def test_the_gate_bites_on_the_code_it_replaced() -> None:
    """The exact lines #717 moved into the adapters, as they stood before."""
    pre_717 = """
async def probe(ctx):
    vnet_rows = _rows(await _read(ctx, "/cluster/sdn/vnets"))
    rows = _rows(await _read(ctx, "/cluster/sdn/zones"))

async def sweep(self, node_name, guest_type, vmid):
    nodes = await self.proxmox.read("/nodes")
    guests = await self.proxmox.read(f"/nodes/{node_name}/{guest_type}")
    cfg = await self.proxmox.read(f"/nodes/{node_name}/{guest_type}/{vmid}/config")
    listing = await self.proxmox.read("/cluster/resources", {"type": "vm"})
    await client.read("/version")
"""
    found = [text for _, text in pve_path_literals(pre_717)]
    assert found == [
        "/cluster/sdn/vnets",
        "/cluster/sdn/zones",
        "/nodes",
        "/nodes/{}/{}",
        "/nodes/{}/{}/{}/config",
        "/cluster/resources",
        "/version",
    ]


def test_prose_and_keys_are_not_paths() -> None:
    sample = '''
"""Reads /version and /nodes - a docstring is documentation, not a call."""
HELP = "/cluster/nextid, which hands back the LOWEST free id"
row = {"version": 1, "nodes": []}
'''
    assert pve_path_literals(sample) == []
