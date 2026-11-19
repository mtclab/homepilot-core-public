"""The everyday operator commands of `hp`, over the API (#remote-cli).

`hp status`, `hp hosts`, `hp agents`, `hp changes`, `hp tasks`, `hp kb`,
`hp alerts`, `hp channels`, `hp guests` and `hp power`: what an operator (or an
assistant) does every day, as readable tables with `--json` for scripts. Every
command goes through the backend's API (``main._backend_session``), so it works
the same on the control-plane host (loopback, the host's own credential) and
from a workstation (the profile's server and token).

Exit codes: 0 done; 1 refused or failed, with the server's reason; 2 a usage
mistake; 3 (``EXIT_OUTCOME_UNKNOWN``) a task whose outcome could not be read.

The groups `hosts`, `agents` and `changes` are the existing `inventory`,
`agent` and `artifacts` apps under the names the console uses; the old names
keep working (hidden), and the commands they already had are reused, not
copied.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import typer
from rich.table import Table

from homepilot.cli import remote
from homepilot.dashboard.wording import actor_words

# ── Plumbing ─────────────────────────────────────────────────────────────────


def _cli() -> Any:
    from homepilot.cli import main

    return main


def _print(text: str = "", *, err: bool = False) -> None:
    """Plain text that may hold names from the server: never Rich markup."""
    console = _cli().err_console if err else _cli().console
    # soft_wrap: a long line is one line (the terminal may wrap it); off a
    # terminal Rich would otherwise break it at 80 columns.
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def _table(*columns: str) -> Table:
    table = Table(show_header=True, header_style="bold")
    for column in columns:
        table.add_column(column)
    return table


def _emit_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def api(what: str, method: str, path: str, body: Any = None, scope: str = "admin") -> Any:
    """One call; on a refusal or a failure, the reason on stderr and exit 1."""
    return _cli()._via_backend(what, method, path, body, scope=scope)


def _query(path: str, **params: Any) -> str:
    kept = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v}
    return f"{path}?{urlencode(kept)}" if kept else path


def confirm_or_refuse(question: str, yes: bool) -> None:
    """Ask on a terminal; refuse (exit 1) without one unless ``--yes``."""
    if yes:
        return
    if not sys.stdin.isatty():
        _print(f"Refusing: {question} Pass --yes to confirm (no terminal to ask on).", err=True)
        raise typer.Exit(1)
    if not typer.confirm(question, default=False, err=True):
        _print("Left alone.", err=True)
        raise typer.Exit(1)


def follow_task(task_id: str, label: str) -> dict[str, Any]:
    """Follow ``GET /tasks/{id}`` to a terminal status, printing each change of
    status on stderr. Returns the last record seen (a still-running one when
    the wait ran out or the backend stopped answering - an unknown outcome)."""
    cli = _cli()

    async def go() -> dict[str, Any]:
        async with cli._backend_session("read,write") as call:
            started = time.monotonic()
            deadline = started + cli._TASK_WAIT_SECONDS
            last_status = ""
            failures = 0
            task: dict[str, Any] = {"id": task_id, "status": "pending"}
            while True:
                try:
                    polled = await call("GET", f"/tasks/{quote(task_id, safe='')}")
                    failures = 0
                    if isinstance(polled, dict):
                        task = polled
                except Exception as exc:  # transport or refusal: count it, keep trying
                    failures += 1
                    task = {**task, "_poll_error": remote.redact(str(exc)) or type(exc).__name__}
                    if failures >= cli._TASK_POLL_MAX_FAILURES:
                        return task
                status = str(task.get("status") or "")
                if status != last_status:
                    _print(f"{label}: {status} ({int(time.monotonic() - started)}s)", err=True)
                    last_status = status
                if status in cli.TERMINAL_TASK_STATUSES or time.monotonic() >= deadline:
                    return task
                await asyncio.sleep(cli._TASK_POLL_INTERVAL)

    return asyncio.run(go())


def task_outcome(task: dict[str, Any], label: str) -> int:
    """The exit code for a followed task, with its reason printed when it failed."""
    status = task.get("status")
    if status == "succeeded":
        return 0
    if status in ("failed", "cancelled"):
        _print(f"{label}: {status} - {task.get('error') or 'no reason recorded'}", err=True)
        return 1
    why = task.get("_poll_error") or f"still {status or 'in an unknown state'}"
    _print(
        f"{label}: outcome unknown ({why}); follow it with `hp tasks follow {task.get('id')}`.",
        err=True,
    )
    return int(_cli().EXIT_OUTCOME_UNKNOWN)


def find_host(name: str) -> dict[str, Any]:
    """The inventory row named exactly ``name`` (gone machines included)."""
    page = api("hp hosts", "GET", _query("/inventory", q=name, limit=1000), scope="read") or {}
    for host in page.get("items", []):
        if host.get("hostname") == name:
            return dict(host)
    _print(f"No host named '{name}' in the inventory.", err=True)
    raise typer.Exit(1)


# ── hp status (remote) ───────────────────────────────────────────────────────


def _needs_you(summary: dict[str, Any], firing: list[dict[str, Any]]) -> list[str]:
    """The Overview's "Needs you", one sentence per thing an operator can act on."""
    lines: list[str] = []
    attention = summary.get("attention") or {}
    # A run line in the server's words (dashboard/wording.py), never its codes.
    for run in attention.get("failed_runs") or []:
        words = run.get("words") or {}
        lines.append(
            f"{words.get('what') or 'A run'} failed: {run.get('error') or 'no reason recorded'}"
            f" - `hp tasks follow {run.get('task_id')}`"
        )
    for run in attention.get("stuck_runs") or []:
        words = run.get("words") or {}
        lines.append(
            f"{words.get('what') or 'A run'} is still {words.get('status') or 'not finished'} "
            f"since {run.get('created_at')} - `hp tasks cancel {run.get('task_id')}`"
        )
    gone = (attention.get("gone") or {}).get("hosts") or []
    if gone:
        names = ", ".join(h.get("hostname", "?") for h in gone)
        lines.append(
            f"{len(gone)} machine(s) Proxmox no longer reports: {names} - `hp hosts forget --gone`"
        )
    behind = attention.get("agents_behind") or {}
    if behind.get("agents"):
        lines.append(
            f"{len(behind['agents'])} agent(s) are older than HomePilot "
            f"{behind.get('control_version')} - `hp agents upgrade --all`"
        )
    changes = summary.get("changes") or {}
    if changes.get("proposed"):
        lines.append(f"{changes['proposed']} change(s) wait for review - `hp changes list --inbox`")
    if changes.get("ready"):
        lines.append(f"{changes['ready']} approved change(s) are ready to apply")
    if changes.get("expired"):
        lines.append(f"{changes['expired']} approval(s) are too old to run as they stand")
    for alert in firing:
        lines.append(
            f"Alert {alert.get('name') or alert.get('rule_id')} is firing on "
            f"{alert.get('hostname') or '?'} - `hp alerts`"
        )
    updates = summary.get("updates") or {}
    if updates.get("update_available"):
        lines.append(f"HomePilot {updates.get('latest')} is available")
    return lines


# How many of the latest people/assistant journal rows `hp status` shows - the
# Overview's "What happened" list is as long.
WHAT_HAPPENED_LIMIT = 8


def _what_happened(rows: list[dict[str, Any]]) -> list[str]:
    """The Overview's "What happened", one line per row, in the server's words."""
    out: list[str] = []
    for row in rows:
        if row.get("action") == "login":  # a sign-in is not news about the estate
            continue
        words = row.get("words") or {}
        # Who, in the server's words too: it names an internal-id actor by its
        # token's label (3.14 walk printed the uuid). An older server's row
        # without it is still never the id.
        who = words.get("actor") or actor_words(row.get("user_id"))
        out.append(f"{who} {words.get('text') or 'did something'}")
    return out


def remote_status(json_output: bool) -> None:
    summary = api("hp status", "GET", "/dashboard/summary", scope="read") or {}
    firing = (api("hp status", "GET", "/monitoring/alerts", scope="read") or {}).get("items", [])
    journal = api(
        "hp status",
        "GET",
        _query("/audit", actor="people", limit=WHAT_HAPPENED_LIMIT),
        scope="read",
    )
    needs = _needs_you(summary, firing)
    happened = _what_happened(list((journal or {}).get("items", [])))
    target = remote.active_target()
    if json_output:
        _emit_json(
            {
                "server": target.base() if target else None,
                "needs_you": needs,
                "what_happened": happened,
                **summary,
            }
        )
        return
    updates = summary.get("updates") or {}
    agents = summary.get("agents") or {}
    machines = (summary.get("inventory") or {}).get("machines") or {}
    changes = summary.get("changes") or {}
    _print(
        f"HomePilot {updates.get('running_version') or '?'} at "
        f"{target.base() if target else 'this host'}"
    )
    if needs:
        _print("\nNeeds you:")
        for line in needs:
            _print(f"  - {line}")
    else:
        _print("\nNothing needs you.")
    _print(
        f"\nMachines: {machines.get('running', 0)} running, {machines.get('stopped', 0)} stopped, "
        f"{machines.get('watched', 0)} watched by an agent"
    )
    _print(f"Agents: {agents.get('connected', 0)} connected of {agents.get('known', 0)} known")
    _print(f"Alerts firing: {len(firing)}")
    waiting = int(changes.get("proposed", 0)) + int(changes.get("ready", 0))
    _print(f"Changes waiting: {waiting}")
    if updates.get("update_available"):
        _print(f"Update available: {updates.get('latest')}")
    if happened:
        _print("\nWhat happened:")
        for line in happened:
            _print(f"  - {line}")


# ── hp hosts ─────────────────────────────────────────────────────────────────


def hosts_forget(names: list[str], gone: bool, yes: bool, json_output: bool) -> None:
    if bool(names) == gone:
        _print("Name the hosts to forget, or pass --gone (not both).", err=True)
        raise typer.Exit(2)
    if gone:
        page = api(
            "hp hosts forget", "GET", _query("/inventory", absent=True, limit=1000), scope="read"
        )
        hosts = list((page or {}).get("items", []))
        if not hosts:
            if json_output:
                _emit_json({"forgotten": [], "failed": []})
            else:
                _print("No gone machines to forget.")
            return
    else:
        hosts = [find_host(name) for name in names]
    listed = ", ".join(str(h.get("hostname")) for h in hosts)
    confirm_or_refuse(f"Forget {len(hosts)} host(s): {listed}? They leave the inventory.", yes)
    cli = _cli()
    forgotten: list[str] = []
    failed: list[dict[str, str]] = []

    async def go() -> None:
        async with cli._backend_session("write") as call:
            for host in hosts:
                name = str(host.get("hostname"))
                try:
                    await call("DELETE", f"/inventory/{quote(str(host['id']), safe='')}")
                    forgotten.append(name)
                except cli.BackendRefusedError as exc:
                    failed.append({"hostname": name, "reason": remote.redact(exc.detail)})

    asyncio.run(go())
    if json_output:
        _emit_json({"forgotten": forgotten, "failed": failed})
    else:
        if forgotten:
            _print(f"Forgot {len(forgotten)} host(s): {', '.join(forgotten)}")
        for f in failed:
            _print(f"Could not forget {f['hostname']}: {f['reason']}", err=True)
    if failed:
        raise typer.Exit(1)


def hosts_describe(name: str, refresh: bool, export: bool, json_output: bool) -> None:
    """How a host is built (#818): the stored description, optionally fresh,
    optionally exported to the knowledge base as the host's as-built note."""
    page = api("hp hosts describe", "GET", _query("/inventory", q=name, limit=1000), scope="read")
    named = [h for h in (page or {}).get("items", []) if h.get("hostname") == name]
    if not named:
        _print(f"No host named '{name}' in the inventory.", err=True)
        raise typer.Exit(1)
    # The live machine of that name, not a retired row that kept it.
    host = sorted(named, key=lambda h: h.get("absent_since") is not None)[0]
    path = f"/inventory/{quote(str(host['id']), safe='')}/description"
    if refresh:
        view = api("hp hosts describe --refresh", "POST", path, scope="read")
    else:
        view = api("hp hosts describe", "GET", path, scope="read")
    exported = None
    if export:
        exported = api("hp hosts describe --export", "POST", f"{path}/export", scope="write")
    if json_output:
        _emit_json({**(view or {}), "export": exported} if export else view)
        return
    _print_description(name, view or {})
    if exported:
        line = f"As-built note: {exported.get('note_id')}"
        if exported.get("unchanged"):
            line += " (unchanged: the description has not changed since the last export)"
        elif exported.get("superseded"):
            line += f" (supersedes {exported['superseded']})"
        _print(line)


def _print_description(name: str, view: dict[str, Any]) -> None:
    from homepilot.inventory.asbuilt import render_asbuilt

    doc = view.get("description")
    if view.get("last_error"):
        _print(f"Last attempt failed ({view.get('last_error_at')}): {view['last_error']}", err=True)
    if not doc:
        agent = view.get("agent") or {}
        why = (
            "its agent is not connected"
            if not agent.get("connected")
            else "its agent is too old to describe it: upgrade it"
            if not agent.get("can_describe")
            else "collect one with --refresh"
        )
        _print(f"No description of {name} yet - {why}.")
        return
    changes = view.get("changes") or []
    if view.get("previous_collected_at"):
        _print(f"Changes since {view['previous_collected_at']}: {len(changes) or 'none'}")
        for c in changes:
            for item in c.get("added") or []:
                _print(f"  + {c['part']}: {item}")
            for item in c.get("removed") or []:
                _print(f"  - {c['part']}: {item}")
        _print()
    _print(render_asbuilt(name, doc).rstrip())
    note = view.get("as_built") or {}
    if note:
        _print(
            f"\nAs-built note in the knowledge base: {note.get('note_id')} "
            f"({note.get('exported_at')})"
        )


# ── hp agents ────────────────────────────────────────────────────────────────


def _install(host_id: str, label: str, upgrade: bool) -> int:
    started = api(
        f"hp agents {'upgrade' if upgrade else 'install'}",
        "POST",
        "/agents/install",
        {"host_id": host_id, "upgrade": upgrade},
    )
    return task_outcome(follow_task(str((started or {}).get("task_id")), label), label)


def agents_upgrade(names: list[str], all_behind: bool) -> None:
    if bool(names) == all_behind:
        _print("Name the hosts whose agents to upgrade, or pass --all (not both).", err=True)
        raise typer.Exit(2)
    behind = list(api("hp agents upgrade", "GET", "/agents/upgrades", scope="read") or [])
    by_name = {str(a.get("hostname")): a for a in behind}
    if all_behind:
        chosen = behind
        if not chosen:
            _print("No agent is older than this HomePilot.")
            return
    else:
        unknown = [n for n in names if n not in by_name]
        if unknown:
            _print(
                f"Not behind (or not connected), so nothing to upgrade: {', '.join(unknown)}",
                err=True,
            )
            raise typer.Exit(1)
        chosen = [by_name[n] for n in names]
    total = len(chosen)
    for index, agent in enumerate(chosen, start=1):
        name = str(agent.get("hostname"))
        label = f"[{index}/{total}] {name}"
        if not agent.get("host_id"):
            _print(
                f"{label}: stopped - HomePilot cannot reach this host through the guest agent "
                "(not a QEMU guest it knows the node and VMID of); upgrade it on the host.",
                err=True,
            )
            raise typer.Exit(1)
        _print(
            f"{label}: {agent.get('agent_version') or 'unknown'} -> {agent.get('control_version')}",
            err=True,
        )
        code = _install(str(agent["host_id"]), label, upgrade=True)
        if code:
            if index < total:
                _print(f"Stopped at {name}; {total - index} agent(s) not attempted.", err=True)
            raise typer.Exit(code)
    _print(f"Upgraded {total} agent(s).")


def agents_install(name: str) -> None:
    host = find_host(name)
    host_id = quote(str(host["id"]), safe="")
    check = api("hp agents install", "GET", f"/agents/install/{host_id}") or {}
    if not check.get("eligible"):
        _print(f"Cannot install on {name}: {check.get('message') or check.get('reason')}", err=True)
        raise typer.Exit(1)
    code = _install(str(host["id"]), name, upgrade=False)
    if code:
        raise typer.Exit(code)
    _print(f"Installed and enrolled the agent on {name}.")


# ── hp changes ───────────────────────────────────────────────────────────────


def changes_list(
    status: str | None,
    kind: str | None,
    inbox: bool,
    json_output: bool,
    tag: str | None = None,
) -> None:
    statuses = ["proposed", "approved"] if inbox else [status]
    items: list[dict[str, Any]] = []
    for one in statuses:
        page = api(
            "hp changes list",
            "GET",
            _query("/artifacts", status=one, kind=kind, tag=tag, limit=1000),
            scope="read",
        )
        items += list((page or {}).get("items", []))
    if json_output:
        _emit_json(items)
        return
    if not items:
        _print("Nothing waits for you." if inbox else "No changes.")
        return
    table = _table("ID", "Kind", "Status", "Target", "Intent", "Tags")
    for a in items:
        target = a.get("target") or {}
        where = target.get("host") or target.get("hostname") or target.get("kind") or ""
        tags = a.get("tags")
        table.add_row(
            str(a.get("id", "")),
            str(a.get("kind", "")),
            str(a.get("status", "")),
            str(where),
            str(a.get("intent", "")),
            ", ".join(str(t) for t in tags) if isinstance(tags, list) else "",
        )
    _cli().console.print(table)


def _print_policy_warnings(warnings: list[dict[str, Any]]) -> None:
    """The structured policy rules a proposed change matches (#506), before approving."""
    matched = [w for w in warnings if w.get("matched")]
    for w in matched:
        _print(
            f"Policy: {w.get('policy_title') or w.get('policy_id')} ({w.get('rule')}) "
            f"matches {w.get('artifact_id')}: {'; '.join(w.get('matched') or [])}"
        )
    for w in warnings:
        for error in w.get("errors") or []:
            _print(f"Policy {w.get('policy_title') or w.get('policy_id')}: {error}")
    if matched:
        _print("Approving it needs --acknowledge-policy.")


def changes_show(artifact_id: str, json_output: bool) -> None:
    data = api(
        "hp changes show",
        "GET",
        f"/artifacts/{quote(artifact_id, safe='')}",
        scope="read",
    )
    if json_output:
        _emit_json(data)
        return
    data = data or {}
    meta: dict[str, Any] = (
        data["frontmatter"] if isinstance(data.get("frontmatter"), dict) else data
    )
    summary = data.get("summary") or {}
    table = Table(show_header=False, box=None)
    for key in ("id", "kind", "status", "intent", "target", "approved_by", "applied_at"):
        value = meta.get(key)
        if value not in (None, "", {}):
            shown = json.dumps(value) if isinstance(value, dict | list) else str(value)
            table.add_row(key, shown)
    _cli().console.print(table)
    for step in summary.get("steps") or []:
        _print(f"  - {step}")
    if summary.get("danger"):
        _print(f"Irreversible: {summary['danger']}")
    if data.get("approval_code"):
        _print(f"Approval code: {data['approval_code']}")
    _print_policy_warnings(data.get("policy_warnings") or [])
    last = data.get("last_task") or {}
    if last:
        _print(f"Last run: {last.get('status')} {last.get('error') or ''}".rstrip())
    body = data.get("body")
    if body:
        _print("")
        _print(str(body))


def changes_propose(path: str) -> None:
    import yaml

    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        spec = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        _print(f"Cannot read the spec {path}: {exc}", err=True)
        raise typer.Exit(2) from None
    if not isinstance(spec, dict):
        _print("The spec file must hold one mapping (YAML or JSON).", err=True)
        raise typer.Exit(2)
    created = api("hp changes propose", "POST", "/artifacts", spec, scope="write") or {}
    _print(f"Proposed: {created.get('id')}")


def changes_run(
    artifact_id: str, route: str, wait: bool, past: str, acknowledge_policy: bool = False
) -> None:
    started = (
        api(
            f"hp changes {route}",
            "POST",
            f"/artifacts/{quote(artifact_id, safe='')}/{route}",
            {"acknowledge_policy": True} if acknowledge_policy else {},
        )
        or {}
    )
    task_id = str(started.get("task_id") or "")
    if not wait:
        _print(
            f"Started task {task_id} for {artifact_id}; follow it with `hp tasks follow {task_id}`."
        )
        return
    code = task_outcome(follow_task(task_id, artifact_id), artifact_id)
    if code:
        raise typer.Exit(code)
    _print(f"{past}: {artifact_id}")


# ── hp tasks ─────────────────────────────────────────────────────────────────


def tasks_list(change: str | None, limit: int, json_output: bool) -> None:
    page = api(
        "hp tasks list", "GET", _query("/tasks", artifact_id=change, limit=limit), scope="read"
    )
    items = page if isinstance(page, list) else (page or {}).get("items", [])
    if json_output:
        _emit_json(items)
        return
    if not items:
        _print("No tasks.")
        return
    table = _table("ID", "Action", "Status", "Change", "Created", "Error")
    for t in items:
        table.add_row(
            str(t.get("id", "")),
            str(t.get("action", "")),
            str(t.get("status", "")),
            str(t.get("artifact_id") or ""),
            str(t.get("created_at") or ""),
            str(t.get("error") or ""),
        )
    _cli().console.print(table)


def tasks_follow(task_id: str) -> None:
    code = task_outcome(follow_task(task_id, f"task {task_id}"), f"task {task_id}")
    if code:
        raise typer.Exit(code)
    _print(f"task {task_id}: succeeded")


def tasks_cancel(task_id: str, yes: bool) -> None:
    confirm_or_refuse(f"Cancel task {task_id}? What it already did stays done.", yes)
    api("hp tasks cancel", "POST", f"/tasks/{quote(task_id, safe='')}/cancel", None, scope="write")
    _print(f"Cancelled task {task_id}.")


# ── hp kb ────────────────────────────────────────────────────────────────────


def _kb_rows(items: list[dict[str, Any]], json_output: bool, empty: str) -> None:
    if json_output:
        _emit_json(items)
        return
    if not items:
        _print(empty)
        return
    table = _table("ID", "Kind", "Target", "Title")
    for d in items:
        table.add_row(
            str(d.get("id", "")),
            str(d.get("kind") or ""),
            str(d.get("target") or ""),
            str(d.get("title") or ""),
        )
    _cli().console.print(table)


def kb_list(kind: str | None, target: str | None, limit: int, json_output: bool) -> None:
    page = api(
        "hp kb list", "GET", _query("/kb", kind=kind, target=target, limit=limit), scope="read"
    )
    _kb_rows(list((page or {}).get("items", [])), json_output, "The knowledge base is empty.")


def kb_show(doc_id: str, json_output: bool) -> None:
    doc = api("hp kb show", "GET", f"/kb/{quote(doc_id, safe='')}", scope="read") or {}
    if json_output:
        _emit_json(doc)
        return
    _print(f"{doc.get('title') or doc_id}  [{doc.get('kind')}, {doc.get('target') or 'global'}]")
    _print("")
    _print(str(doc.get("content") or ""))


def kb_search(query: str, limit: int, json_output: bool) -> None:
    page = api("hp kb search", "GET", _query("/kb/search", q=query, limit=limit), scope="read")
    _kb_rows(list((page or {}).get("results", [])), json_output, f"Nothing matches '{query}'.")


def kb_add(source: str, target: str | None, kind: str, json_output: bool) -> None:
    try:
        content = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        _print(f"Cannot read {source}: {exc.strerror}", err=True)
        raise typer.Exit(2) from None
    if not content.strip():
        _print("The note is empty.", err=True)
        raise typer.Exit(2)
    body: dict[str, Any] = {"content": content, "kind": kind}
    if target:
        body["target"] = target
    result = api("hp kb add", "POST", "/kb/notes", body, scope="write") or {}
    if json_output:
        _emit_json(result)
        return
    where = target or "the estate"
    if result.get("indexed"):
        _print(
            f"Recorded {kind} for {where}: KB entry {result.get('doc_id')} ({result.get('id')})."
        )
        return
    # POST /kb used to answer with an id for a note nobody could find; the
    # route now says whether it is searchable, and so does this.
    _print(
        f"Recorded {kind} for {where} as {result.get('id')}, but it is not searchable.", err=True
    )
    raise typer.Exit(1)


# ── hp alerts, hp channels, hp guests ────────────────────────────────────────


def alerts(json_output: bool) -> None:
    items = (api("hp alerts", "GET", "/monitoring/alerts", scope="read") or {}).get("items", [])
    if json_output:
        _emit_json(items)
        return
    if not items:
        _print("No alert is firing.")
        return
    table = _table("Rule", "Host", "Value", "Since")
    for a in items:
        table.add_row(
            str(a.get("name") or a.get("rule_id") or ""),
            str(a.get("hostname") or ""),
            str(a.get("last_value") if a.get("last_value") is not None else ""),
            str(a.get("firing_since") or ""),
        )
    _cli().console.print(table)


def channels_list(json_output: bool) -> None:
    items = (api("hp channels list", "GET", "/admin/notifications/channels") or {}).get("items", [])
    if json_output:
        _emit_json(items)
        return
    if not items:
        _print("No notification channels.")
        return
    table = _table("ID", "Name", "Kind", "Endpoint", "Enabled", "Last result")
    for c in items:
        table.add_row(
            str(c.get("id", "")),
            str(c.get("name") or ""),
            str(c.get("kind") or ""),
            str(c.get("endpoint") or ""),
            "yes" if c.get("enabled") else "no",
            str(c.get("last_error") or (f"ok {c['last_ok_at']}" if c.get("last_ok_at") else "")),
        )
    _cli().console.print(table)


def channels_test(channel_id: str) -> None:
    result = (
        api(
            "hp channels test",
            "POST",
            f"/admin/notifications/channels/{quote(channel_id, safe='')}/test",
        )
        or {}
    )
    if result.get("ok") is False or result.get("delivered") is False:
        _print(f"Channel {channel_id} did NOT deliver: {result.get('error') or result}", err=True)
        raise typer.Exit(1)
    _print(f"Test message delivered through channel {channel_id}.")


def guests_list(json_output: bool) -> None:
    data = api("hp guests list", "GET", "/admin/guests/friends") or {}
    friends = list(data.get("friends", []))
    if json_output:
        _emit_json(friends)
        return
    if not friends:
        _print("No friends yet - `hp invite create` mints the first invite.")
        return
    for f in friends:
        limits, usage = f.get("limits") or {}, f.get("usage") or {}
        cap = limits.get("vms")
        budget = f"{usage.get('vms', 0)} of {cap} machines" if cap else "no budget set"
        _print(f"{f.get('cn')}: {budget}")
        machines = f.get("machines") or []
        if not machines:
            _print("  no machine")
        for m in machines:
            _print(f"  {m.get('name')} (VMID {m.get('vmid')} on {m.get('node')}): {m.get('state')}")
        open_invites = [i for i in f.get("invites") or [] if i.get("state") == "open"]
        if open_invites:
            _print(f"  {len(open_invites)} open invite(s)")


# ── hp power ─────────────────────────────────────────────────────────────────

POWER_ACTIONS = ("start", "shutdown", "reboot", "stop")


def power(name: str, action: str, yes: bool, confirm_name: str | None, json_output: bool) -> None:
    if action not in POWER_ACTIONS:
        _print(f"Unknown action '{action}': use {', '.join(POWER_ACTIONS)}.", err=True)
        raise typer.Exit(2)
    host = find_host(name)
    host_id = quote(str(host["id"]), safe="")
    state = api("hp power", "GET", f"/inventory/{host_id}/power", scope="read") or {}
    allowed = (state.get("actions") or {}).get(action) or {}
    if not allowed.get("available", False):
        _print(f"Cannot {action} {name}: {allowed.get('reason') or state.get('error')}", err=True)
        raise typer.Exit(1)
    body: dict[str, Any] = {"action": action}
    # As the console does: a hard stop, or any action on the machine HomePilot
    # runs on or on a protected guest, needs the name typed back - never just "y".
    if allowed.get("needs_typed_confirmation") or action == "stop":
        why = state.get("protected_reason") or (
            "it is the machine HomePilot runs on" if state.get("is_self") else "a hard stop"
        )
        typed = confirm_name
        if typed is None:
            if not sys.stdin.isatty():
                _print(
                    f"Refusing: {action} on {name} ({why}) needs the machine's name typed back. "
                    f"Pass --confirm-name {name}.",
                    err=True,
                )
                raise typer.Exit(1)
            typed = typer.prompt(
                f"{action} {name}: {why}. Type the machine's name to go ahead", err=True
            )
        if typed != name:
            _print(f"'{typed}' is not '{name}'; left alone.", err=True)
            raise typer.Exit(1)
        body["confirm_name"] = typed
    elif action != "start":
        confirm_or_refuse(f"{action.capitalize()} {name}?", yes)
    result = api("hp power", "POST", f"/inventory/{host_id}/power", body) or {}
    if json_output:
        _emit_json(result)
    outcome = str(result.get("outcome") or result.get("status") or "unknown")
    if not json_output:
        message = result.get("message") or result.get("detail") or ""
        _print(f"{action} {name}: {outcome}" + (f" - {message}" if message else ""))
    if outcome == "ok":
        return
    raise typer.Exit(int(_cli().EXIT_OUTCOME_UNKNOWN) if outcome == "unknown" else 1)


# ── Registration ─────────────────────────────────────────────────────────────

_JSON = typer.Option(False, "--json", help="Output as JSON")
_YES = typer.Option(False, "--yes", "-y", help="Do not ask first")


def register(
    app: typer.Typer,
    *,
    hosts_app: typer.Typer,
    agents_app: typer.Typer,
    changes_app: typer.Typer,
    kb_app: typer.Typer,
) -> None:
    """Attach the operator commands to `hp` (called once, from cli.main)."""

    @hosts_app.command("forget")
    def _hosts_forget(
        names: list[str] | None = typer.Argument(None, help="Host names to forget"),  # noqa: B008
        gone: bool = typer.Option(False, "--gone", help="Every machine Proxmox no longer reports"),
        yes: bool = _YES,
        json_output: bool = _JSON,
    ) -> None:
        """Forget hosts: names them all first, asks, then removes exactly those."""
        hosts_forget(list(names or []), gone, yes, json_output)

    @hosts_app.command("describe")
    def _hosts_describe(
        name: str = typer.Argument(..., help="Hostname"),
        refresh: bool = typer.Option(
            False, "--refresh", help="Ask the host's agent for a fresh description first"
        ),
        export: bool = typer.Option(
            False, "--export", help="Record it in the knowledge base as the host's as-built note"
        ),
        json_output: bool = _JSON,
    ) -> None:
        """How a host is built: what runs, how it is exposed, where its config lives."""
        hosts_describe(name, refresh, export, json_output)

    @agents_app.command("upgrade")
    def _agents_upgrade(
        names: list[str] | None = typer.Argument(None, help="Hosts whose agents to upgrade"),  # noqa: B008
        all_behind: bool = typer.Option(False, "--all", help="Every agent older than HomePilot"),
    ) -> None:
        """Upgrade agents one host at a time, stopping at the first failure."""
        agents_upgrade(list(names or []), all_behind)

    @agents_app.command("install")
    def _agents_install(
        name: str = typer.Argument(..., help="Host to install the agent on"),
    ) -> None:
        """Install and enrol the agent on a guest through qemu-guest-agent."""
        agents_install(name)

    @changes_app.command("approve-and-apply")
    def _changes_approve_and_apply(
        artifact_id: str = typer.Argument(..., help="Change ID"),
        wait: bool = typer.Option(True, "--wait/--no-wait", help="Follow the run to its outcome"),
        acknowledge_policy: bool = typer.Option(
            False,
            "--acknowledge-policy",
            help="Approve over the structured policy rules this change matches",
        ),
    ) -> None:
        """Approve a proposed change and run it, as an operator."""
        changes_run(
            artifact_id,
            "approve-and-apply",
            wait,
            "Approved and applied",
            acknowledge_policy=acknowledge_policy,
        )

    @changes_app.command("propose")
    def _changes_propose(
        spec_file: str = typer.Argument(..., help="Spec file (YAML or JSON), - for stdin"),
    ) -> None:
        """Propose a change from a spec file."""
        changes_propose(spec_file)

    tasks_app = typer.Typer(help="Tasks: the runs behind applies, installs and upgrades")
    app.add_typer(tasks_app, name="tasks")

    @tasks_app.command("list")
    def _tasks_list(
        change: str | None = typer.Option(None, "--change", help="Only this change's runs"),
        limit: int = typer.Option(50, "--limit", help="How many, newest first"),
        json_output: bool = _JSON,
    ) -> None:
        """List recent tasks."""
        tasks_list(change, limit, json_output)

    @tasks_app.command("follow")
    def _tasks_follow(task_id: str = typer.Argument(..., help="Task ID")) -> None:
        """Follow a task to its outcome, printing each change of status."""
        tasks_follow(task_id)

    @tasks_app.command("cancel")
    def _tasks_cancel(task_id: str = typer.Argument(..., help="Task ID"), yes: bool = _YES) -> None:
        """Cancel a running task."""
        tasks_cancel(task_id, yes)

    @kb_app.command("list")
    def _kb_list(
        kind: str | None = typer.Option(None, "--kind", help="note, policy, decision, fact"),
        target: str | None = typer.Option(None, "--target", help="Only notes about this target"),
        limit: int = typer.Option(100, "--limit"),
        json_output: bool = _JSON,
    ) -> None:
        """List knowledge-base entries."""
        kb_list(kind, target, limit, json_output)

    @kb_app.command("show")
    def _kb_show(
        doc_id: str = typer.Argument(..., help="Entry ID"), json_output: bool = _JSON
    ) -> None:
        """Show one knowledge-base entry."""
        kb_show(doc_id, json_output)

    @kb_app.command("search")
    def _kb_search(
        query: str = typer.Argument(..., help="What to look for"),
        limit: int = typer.Option(10, "--limit"),
        json_output: bool = _JSON,
    ) -> None:
        """Search the knowledge base."""
        kb_search(query, limit, json_output)

    @kb_app.command("add")
    def _kb_add(
        source: str = typer.Argument("-", help="Markdown file with the note, - for stdin"),
        target: str | None = typer.Option(None, "--target", help="Host or service it is about"),
        kind: str = typer.Option("note", "--kind", help="note, policy or decision"),
        json_output: bool = _JSON,
    ) -> None:
        """Record a note in the knowledge base from a file or stdin."""
        kb_add(source, target, kind, json_output)

    @app.command("alerts")
    def _alerts(json_output: bool = _JSON) -> None:
        """The alerts firing now."""
        alerts(json_output)

    channels_app = typer.Typer(help="Notification channels")
    app.add_typer(channels_app, name="channels")

    @channels_app.command("list")
    def _channels_list(json_output: bool = _JSON) -> None:
        """List notification channels (endpoints redacted)."""
        channels_list(json_output)

    @channels_app.command("test")
    def _channels_test(channel_id: str = typer.Argument(..., help="Channel ID")) -> None:
        """Send a test message through a channel and say whether it arrived."""
        channels_test(channel_id)

    guests_app = typer.Typer(help="Guests: friends, their machines and budgets")
    app.add_typer(guests_app, name="guests")

    @guests_app.command("list")
    def _guests_list(json_output: bool = _JSON) -> None:
        """Each friend with their machines, budget and open invites."""
        guests_list(json_output)

    @app.command("power")
    def _power(
        name: str = typer.Argument(..., help="Host name"),
        action: str = typer.Argument(..., help="start | shutdown | reboot | stop"),
        yes: bool = _YES,
        confirm_name: str | None = typer.Option(
            None, "--confirm-name", help="The machine's name, typed back (stop / protected)"
        ),
        json_output: bool = _JSON,
    ) -> None:
        """Start, shut down, reboot or stop a guest directly, as the operator."""
        power(name, action, yes, confirm_name, json_output)
