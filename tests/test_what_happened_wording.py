"""Every code the Overview can show has plain words (3.14).

THE DEFECT THIS FORBIDS: the Overview's "What happened" printed journal codes as
recorded - ``apply_refused``, ``rollback_refused``, ``guest_power_refused`` -
because the web's phrase table knew 50 codes and the producers write more. Its
"Needs you" said ``install_agent web-01 failed`` and ``is still pending``. A
hand-kept list of codes drifts the moment someone adds a producer, which is how
the gap opened.

So the set of codes is DERIVED FROM THE SOURCE, never listed by hand:

* journal actions: every call that reaches ``Repository.log_audit`` - directly,
  through a wrapper that forwards its own ``action`` parameter, or through a
  callback wired to such a wrapper - with the code each call can pass,
  resolved through literals, conditionals, f-strings, local assignments, loops,
  module constants and the callers of the enclosing function. A call whose code
  cannot be resolved FAILS the gate: a code nobody can enumerate is a code
  nobody can word. Plus the journal rows a migration writes in SQL.
* run actions and statuses: the ``tasks`` table's CHECK constraints in
  ``db/schema.lock`` - the database refuses anything else.

Teeth: delete ``"apply_refused"`` from ``dashboard/wording.JOURNAL`` and
``test_every_journal_code_has_words`` fails naming it and the line that writes
it; add a producer with a new code and it fails the same way; write a code from
a request field and ``test_every_journal_code_can_be_derived`` fails.
"""

from __future__ import annotations

import ast
import importlib
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from homepilot.dashboard import wording

SRC = Path(__file__).resolve().parents[1] / "src"
PKG = SRC / "homepilot"


# ── The source, parsed once ──────────────────────────────────────────────────


class _Source:
    def __init__(self, root: Path) -> None:
        self.mods = {
            f: ast.parse(f.read_text(encoding="utf-8")) for f in sorted(root.rglob("*.py"))
        }
        self.parent: dict[ast.AST, ast.AST] = {}
        self.file_of: dict[ast.AST, Path] = {}
        self.defs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        self.calls: list[ast.Call] = []
        for f, tree in self.mods.items():
            for node in ast.walk(tree):
                self.file_of[node] = f
                for child in ast.iter_child_nodes(node):
                    self.parent[child] = node
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    self.defs.setdefault(node.name, []).append(node)
                elif isinstance(node, ast.Call):
                    self.calls.append(node)

    def where(self, node: ast.AST) -> str:
        return (
            f"{self.file_of[node].relative_to(SRC.parent).as_posix()}:{getattr(node, 'lineno', 0)}"
        )

    def enclosing(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node
        return None


def _modname(f: Path) -> str:
    parts = list(f.relative_to(SRC).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], list[str]]:
    a = fn.args
    pos = [p.arg for p in a.posonlyargs + a.args]
    if pos and pos[0] in ("self", "cls"):
        pos = pos[1:]
    return pos, [p.arg for p in a.kwonlyargs]


def _arg(call: ast.Call, fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    pos, _ = _params(fn)
    if name in pos:
        i = pos.index(name)
        if i < len(call.args) and not isinstance(call.args[i], ast.Starred):
            return call.args[i]
    return None


class UnresolvableError(Exception):
    pass


class JournalCodes:
    """Every journal code the source can write, with where it is written."""

    def __init__(self, src: _Source) -> None:
        self.src = src
        # Writers: id(def) -> the parameter that carries the code.
        self.writers: dict[int, str] = {id(d): "action" for d in src.defs.get("log_audit", [])}
        # Callbacks: a name a writer is handed over as (`audit_fn=self._log_audit`)
        # -> the parameter position of the code.
        self.callbacks: dict[str, int] = {}
        self._find_writers()
        self.codes: dict[str, list[str]] = {}
        self.unresolved: list[str] = []
        self._collect()

    # A call reaches the definitions of its name in its own file, else every one.
    def targets(self, call: ast.Call) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        name = _callee(call)
        cands = self.src.defs.get(name or "", [])
        same = [d for d in cands if self.src.file_of[d] == self.src.file_of[call]]
        return same or cands

    def _writer_targets(self, call: ast.Call) -> list[tuple[Any, ast.AST | None]]:
        """(writer, the code argument) for every writer this call can reach."""
        out: list[tuple[Any, ast.AST | None]] = []
        for d in self.targets(call):
            if id(d) in self.writers:
                out.append((d, _arg(call, d, self.writers[id(d)])))
        name = _callee(call)
        if not out and name is not None and name.lstrip("_") in self.callbacks:
            i = self.callbacks[name.lstrip("_")]
            out.append((None, call.args[i] if i < len(call.args) else None))
        return out

    def _find_writers(self) -> None:
        changed = True
        while changed:
            changed = False
            # A writer handed over as a callback keyword.
            for call in self.src.calls:
                for kw in call.keywords:
                    if kw.arg and isinstance(kw.value, ast.Attribute | ast.Name):
                        ref = kw.value.attr if isinstance(kw.value, ast.Attribute) else kw.value.id
                        for d in self.src.defs.get(ref, []):
                            if id(d) in self.writers and kw.arg.lstrip("_") not in self.callbacks:
                                pos, _ = _params(d)
                                if self.writers[id(d)] in pos:
                                    self.callbacks[kw.arg.lstrip("_")] = pos.index(
                                        self.writers[id(d)]
                                    )
                                    changed = True
            # A function that forwards its own parameter as the code.
            for call in self.src.calls:
                fn = self.src.enclosing(call)
                if fn is None or id(fn) in self.writers:
                    continue
                for _, arg in self._writer_targets(call):
                    if isinstance(arg, ast.Name):
                        pos, kwo = _params(fn)
                        if arg.id in pos or arg.id in kwo:
                            self.writers[id(fn)] = arg.id
                            changed = True
                            break

    def _collect(self) -> None:
        for call in self.src.calls:
            for _writer, arg in self._writer_targets(call):
                if arg is None:
                    continue
                fn = self.src.enclosing(call)
                forwards = (
                    fn is not None
                    and id(fn) in self.writers
                    and isinstance(arg, ast.Name)
                    and self.writers[id(fn)] == arg.id
                )
                if forwards:
                    break
                try:
                    for code in self.resolve(arg, fn, 0, frozenset()):
                        self.codes.setdefault(code, []).append(self.src.where(call))
                except UnresolvableError as exc:
                    self.unresolved.append(f"{self.src.where(call)}: {ast.unparse(arg)} ({exc})")
                break
        self._migration_rows()

    def _migration_rows(self) -> None:
        """Journal rows a migration writes in SQL: `INSERT INTO audit_log ... SELECT 'u', 's', 'code'`."""
        for f, tree in self.src.mods.items():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if "INSERT INTO audit_log" not in node.value or f.name == "repository.py":
                    continue
                stmt = node.value
                parent = self.src.parent.get(node)
                # The SQL is usually split over adjacent literals in one expression.
                if isinstance(parent, ast.JoinedStr | ast.BinOp):
                    stmt = ast.unparse(parent)
                m = re.search(
                    r"INSERT INTO audit_log \(user_id, source, action\b.*?SELECT '[^']*', '[^']*', '([^']+)'",
                    stmt,
                    re.S,
                )
                if m is None:
                    # Look at the literal that follows (implicit concatenation is ONE constant).
                    m = re.search(r"SELECT '[^']*', '[^']*', '([^']+)'", stmt)
                if m is None:
                    self.unresolved.append(
                        f"{self.src.where(node)}: an audit_log INSERT this gate cannot read"
                    )
                    continue
                self.codes.setdefault(m.group(1), []).append(self.src.where(node))

    def _module_value(self, node: ast.AST, name: str) -> Any:
        f = self.src.file_of[node]
        try:
            value = getattr(importlib.import_module(_modname(f)), name, None)
        except ImportError:
            value = None
        if value is not None:
            return value
        for imp in ast.walk(self.src.mods[f]):
            if not isinstance(imp, ast.ImportFrom):
                continue
            for alias in imp.names:
                if (alias.asname or alias.name) != name:
                    continue
                if imp.level:
                    base = _modname(f).split(".")
                    if f.name != "__init__.py":
                        base = base[:-1]
                    base = base[: len(base) - (imp.level - 1)]
                    target = ".".join(base + ([imp.module] if imp.module else []))
                else:
                    target = imp.module or ""
                return getattr(importlib.import_module(target), alias.name, None)
        return None

    @staticmethod
    def _strings(value: Any) -> set[str] | None:
        if isinstance(value, str):
            return {value}
        items = value.values() if isinstance(value, dict) else value
        if isinstance(value, dict | tuple | list | set | frozenset) and all(
            isinstance(v, str) for v in items
        ):
            return set(items)
        return None

    def resolve(self, expr: ast.AST, fn: Any, depth: int, seen: frozenset[Any]) -> set[str]:
        if depth > 8:
            raise UnresolvableError("too many hops")
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.IfExp):
            return self.resolve(expr.body, fn, depth, seen) | self.resolve(
                expr.orelse, fn, depth, seen
            )
        if isinstance(expr, ast.JoinedStr):
            parts = [
                {v.value} if isinstance(v, ast.Constant) else self.resolve(v.value, fn, depth, seen)  # type: ignore[attr-defined]
                for v in expr.values
            ]
            return {"".join(p) for p in itertools.product(*parts)}
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
            # CODES[key] over a module-level dict: any of its values.
            found = self._strings(self._module_value(expr, expr.value.id))
            if found is not None and isinstance(self._module_value(expr, expr.value.id), dict):
                return found
            raise UnresolvableError(
                "subscript of something that is not a module-level dict of codes"
            )
        if isinstance(expr, ast.Name):
            return self._name(expr, fn, depth, seen)
        raise UnresolvableError("not a literal, a constant or a name")

    def _name(self, expr: ast.Name, fn: Any, depth: int, seen: frozenset[Any]) -> set[str]:
        if fn is not None:
            out: set[str] = set()
            bound = False
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == expr.id for t in node.targets)
                ) or (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == expr.id
                    and node.value is not None
                ):
                    out |= self.resolve(node.value, fn, depth + 1, seen)
                    bound = True
                elif (
                    isinstance(node, ast.For | ast.AsyncFor)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == expr.id
                ):
                    if isinstance(node.iter, ast.Tuple | ast.List | ast.Set):
                        for elt in node.iter.elts:
                            out |= self.resolve(elt, fn, depth + 1, seen)
                    elif isinstance(node.iter, ast.Name):
                        found = self._strings(self._module_value(node.iter, node.iter.id))
                        if found is None:
                            raise UnresolvableError(f"loop over {node.iter.id}")
                        out |= found
                    else:
                        raise UnresolvableError("loop over an expression")
                    bound = True
            if bound:
                return out
            pos, kwo = _params(fn)
            if expr.id in pos or expr.id in kwo:
                return self._param(expr.id, fn, depth, seen)
        value = self._module_value(expr, expr.id)
        if isinstance(value, str):
            return {value}
        raise UnresolvableError(f"{expr.id} is not bound to a code")

    def _param(self, name: str, fn: Any, depth: int, seen: frozenset[Any]) -> set[str]:
        """A parameter: whatever every caller of `fn` passes for it."""
        key = (id(fn), name)
        if key in seen:
            return set()
        seen = seen | {key}
        callers = [c for c in self.src.calls if _callee(c) == fn.name and fn in self.targets(c)]
        if not callers:
            raise UnresolvableError(f"parameter {name} of {fn.name}, which nothing calls")
        out: set[str] = set()
        for call in callers:
            arg = _arg(call, fn, name)
            if arg is None:
                raise UnresolvableError(f"parameter {name} of {fn.name} at {self.src.where(call)}")
            out |= self.resolve(arg, self.src.enclosing(call), depth + 1, seen)
        return out


@pytest.fixture(scope="module")
def journal() -> JournalCodes:
    return JournalCodes(_Source(PKG))


def _check_values(column: str) -> set[str]:
    """The values the locked schema's `tasks` table admits in `column`."""
    lock = (PKG / "db" / "schema.lock").read_text(encoding="utf-8")
    table = next(line for line in lock.splitlines() if "CREATE TABLE tasks " in line)
    m = re.search(column + r" TEXT NOT NULL[^,]*?CHECK\(" + column + r" IN \(([^)]*)\)\)", table)
    assert m, f"tasks.{column} has no CHECK in schema.lock - the run vocabulary is unbounded"
    return set(re.findall(r"'([^']+)'", m.group(1)))


# ── The gates ────────────────────────────────────────────────────────────────


def test_every_journal_code_can_be_derived(journal: JournalCodes) -> None:
    assert not journal.unresolved, (
        "journal writes whose code cannot be read off the source - make it a literal, "
        "a module constant, or a lookup in a module-level dict:\n  "
        + "\n  ".join(journal.unresolved)
    )


def test_the_derivation_finds_what_it_must(journal: JournalCodes) -> None:
    """The analysis is not blind: the codes behind the bug, and each producer shape."""
    for code, shape in {
        "apply_refused": "f-string over a parameter, resolved through its callers",
        "rollback_refused": "literal through lifecycle._log_audit",
        "guest_shutdown": "lookup in a module-level dict",
        "approve": "through the transitions audit callback",
        "token_minted": "an imported module constant",
        "duplicate_host_retired": "a migration's INSERT INTO audit_log",
        "replay_failed": "conditional expression",
    }.items():
        assert code in journal.codes, f"{code} ({shape}) was not derived"


def test_every_journal_code_has_words(journal: JournalCodes) -> None:
    missing = {c: w for c, w in journal.codes.items() if c not in wording.JOURNAL}
    assert not missing, (
        "journal codes with no words in dashboard/wording.JOURNAL (the Overview would "
        "show the raw code):\n  "
        + "\n  ".join(f"{c}  (written at {w[0]})" for c, w in sorted(missing.items()))
    )


def test_no_words_for_a_code_nothing_writes(journal: JournalCodes) -> None:
    """The table is the producers' vocabulary, not a guess at it."""
    extra = sorted(set(wording.JOURNAL) - set(journal.codes))
    assert not extra, f"words for codes no producer writes - delete them: {extra}"


def test_every_run_action_and_status_has_words() -> None:
    actions = _check_values("action")
    statuses = _check_values("status")
    assert "install_agent" in actions and "pending" in statuses  # the parse is not empty
    assert set(wording.RUN_ACTIONS) == actions, sorted(actions ^ set(wording.RUN_ACTIONS))
    assert set(wording.RUN_STATUSES) == statuses, sorted(statuses ^ set(wording.RUN_STATUSES))


# ── The words themselves ─────────────────────────────────────────────────────


def test_a_row_reads_as_a_sentence_never_as_its_code() -> None:
    row = {
        "action": "apply_refused",
        "artifact_id": "2026-11-18-shell-web01-abc",
        "details_json": None,
    }
    words = wording.journal_words(row)
    assert words["text"] == (
        "tried to apply 2026-11-18-shell-web01-abc - stopped: the machine had changed"
    )
    assert "apply_refused" not in words["text"]
    # An upgrade says so, with the version.
    up = wording.journal_words(
        {
            "action": "install_agent",
            "target_host": "web-01",
            "details_json": '{"upgrade": true, "agent_version": "3.14.0"}',
        }
    )
    assert up["text"] == "upgraded the agent on web-01 to 3.14.0"


def test_a_code_from_an_older_version_is_still_words() -> None:
    words = wording.journal_words({"action": "composite-rollback"})
    assert words["text"] == "recorded: composite rollback"


def test_run_lines_say_what_and_how_far() -> None:
    assert wording.run_words("install_agent", "pending", "web-01") == {
        "what": "Installing the agent on web-01",
        "status": "waiting to start",
    }


# ── The web's half of the contract ───────────────────────────────────────────
# The console's unit tests feed the Overview rows carrying the words the server
# would send. Those words live in a fixture GENERATED from this table, and this
# test holds the fixture equal to it - so a wording change here that the web
# tests were not regenerated for fails here, and the web tests never carry a
# hand-written copy of the server's sentences.

FIXTURE = Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "wording.fixture.json"


def _compact(details: dict[str, Any]) -> str:
    return json.dumps(details, separators=(",", ":"))  # JSON.stringify's spelling


SAMPLE_ROWS: list[dict[str, Any]] = [
    {
        "action": "install_agent",
        "target_host": "infra-docker",
        "details_json": _compact({"upgrade": True, "agent_version": "v3.11.0"}),
    },
    {
        "action": "host_forgotten",
        "target_host": "probe-1",
        "details_json": _compact({"reason": "destroyed by an applied change"}),
    },
    {"action": "approve", "artifact_id": "2026-11-08-retire-104"},
    *({"action": "withdraw", "artifact_id": a} for a in ("a-1", "a-2", "a-3", "a-4", "mine")),
    {"action": "apply_refused", "artifact_id": "2026-11-18-shell-web01-abc123"},
    {"action": "tailnet_join", "target_host": "relay"},
    {"action": "check_drift", "artifact_id": "nginx-conf"},
    {"action": "refresh"},
    {"action": "host_added", "target_host": "nas"},
    {"action": "composite-rollback"},
]
SAMPLE_RUNS: list[tuple[str, str, str]] = [
    ("apply", "failed", "nginx-conf"),
    ("apply", "failed", "pg-tune"),
    ("apply", "running", "task task"),
    ("apply", "pending", "nginx-conf"),
    ("install_agent", "failed", "relay"),
]


def _fixture() -> dict[str, Any]:
    journal = []
    for sample in SAMPLE_ROWS:
        row = {
            "action": sample["action"],
            "artifact_id": sample.get("artifact_id"),
            "target_host": sample.get("target_host"),
            "details_json": sample.get("details_json"),
        }
        # Who did it depends on the row's actor, not its code: the web tests
        # say it from their own rows' user_id (tests/test_journal_actor_names.py
        # holds the server's actor words), so the fixture carries the rest.
        words = {k: v for k, v in wording.journal_words(row).items() if k != "actor"}
        journal.append({"row": row, "words": words})
    runs = [
        {"action": a, "status": st, "target": t, "words": wording.run_words(a, st, t)}
        for a, st, t in SAMPLE_RUNS
    ]
    phrases = {
        code: {"verb": ph.verb, "on": ph.on, "tail": ph.tail, "many": ph.many}
        for code, ph in sorted(wording.JOURNAL.items())
    }
    return {
        "phrases": phrases,
        "run_actions": dict(sorted(wording.RUN_ACTIONS.items())),
        "run_statuses": dict(sorted(wording.RUN_STATUSES.items())),
        "_comment": (
            "Generated from src/homepilot/dashboard/wording.py by "
            "`python tests/test_what_happened_wording.py --fixture`; "
            "tests/test_what_happened_wording.py holds it equal to the server's words."
        ),
        "journal": journal,
        "runs": runs,
    }


def test_the_web_fixture_is_the_servers_words() -> None:
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == _fixture(), (
        "web/src/lib/wording.fixture.json is not what the server says - regenerate it: "
        "python tests/test_what_happened_wording.py --fixture"
    )


if __name__ == "__main__":  # pragma: no cover - manual: list the codes, or write the fixture
    if "--fixture" in sys.argv:
        FIXTURE.write_text(json.dumps(_fixture(), indent="\t") + "\n", encoding="utf-8")
        print(f"wrote {FIXTURE}")
    else:
        jc = JournalCodes(_Source(PKG))
        for code in sorted(jc.codes):
            print(code, jc.codes[code][0])
        print("unresolved:", *jc.unresolved, sep="\n  ", file=sys.stderr)


def test_words_are_the_phrase_and_its_object_unless_the_details_say_more() -> None:
    """The composition the web test helper (web/src/lib/wordingFixture.ts) mirrors.

    Only the codes in ``DETAIL_CODES`` read their details; every other row is
    its phrase, its change or host, and its tail - so a fixture of phrases plus
    the detail samples is the whole of what the server can say.
    """
    detail_codes = {"host_forgotten", "install_agent"}
    for code, phrase in wording.JOURNAL.items():
        row = {
            "action": code,
            "artifact_id": "c-1",
            "target_host": "h-1",
            "details_json": '{"upgrade": true, "agent_version": "9", "reason": "destroyed by an applied change"}',
        }
        obj = {"change": "c-1", "host": "h-1", "none": None}[phrase.on]
        plain = phrase.verb + (f" {obj}" if obj else "") + phrase.tail
        if code in detail_codes:
            assert wording.journal_words(row)["text"] != plain, code
            assert wording.journal_words({**row, "details_json": None})["text"] == plain, code
        else:
            assert wording.journal_words(row)["text"] == plain, code


def test_a_run_line_is_its_action_its_target_and_its_status() -> None:
    """The composition the web test helper mirrors for run lines."""
    for action, doing in wording.RUN_ACTIONS.items():
        for status, said in wording.RUN_STATUSES.items():
            assert wording.run_words(action, status, "t-1") == {
                "what": f"{doing} t-1",
                "status": said,
            }
