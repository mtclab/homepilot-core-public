"""Render a host description as a KB "as-built" note: a rebuild recipe (#818).

The note answers four questions an operator rebuilding the machine asks, in
order - what runs, how it is exposed, where its configuration lives, what it
depends on - then names what the agent could not see, and ends with a rebuild
checklist built from the same facts. Pure: a description in, Markdown out.
"""

from __future__ import annotations

from typing import Any

_HIDDEN = ("not_visible", "partial", "error")


def _yes_no(value: Any, yes: str, no: str, unknown: str) -> str:
    if value is True:
        return yes
    return no if value is False else unknown


def _cell(value: Any) -> str:
    """One Markdown table cell: no pipes or newlines from host data."""
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip() or "-"


def _list(node: Any, *path: str) -> list[dict[str, Any]]:
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return [x for x in node if isinstance(x, dict)] if isinstance(node, list) else []


def _sec(doc: dict[str, Any], *path: str) -> dict[str, Any]:
    node: Any = doc
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return [*out, ""]


def _reason(sec: dict[str, Any], what: str) -> str:
    """A section's reason to follow ``"<what>: "`` - without saying ``what`` twice.

    An agent's reason stands alone, so some name their subject ("docker: not
    readable - ..."); after the renderer's own label that read "Docker: docker:
    not readable" (3.14 walk). A leading ``<what>:`` (any case) is dropped here.
    """
    text = str(sec.get("reason") or sec.get("status") or "unknown")
    head, colon, rest = text.partition(":")
    if colon and rest.strip() and head.strip().lower() in {what.lower(), what.lower().split()[0]}:
        return rest.strip()
    return text


def _status_line(sec: dict[str, Any], what: str) -> list[str]:
    status = sec.get("status")
    if status == "not_installed":
        return [f"No {what} on this host.", ""]
    if status in _HIDDEN:
        return [f"_{what}: {_reason(sec, what)}_", ""]
    return []


def _what_runs(doc: dict[str, Any]) -> list[str]:
    out = ["## What runs", ""]
    units = _sec(doc, "units")
    services = [u for u in _list(units, "units") if u.get("type") != "timer"]
    timers = [u for u in _list(units, "units") if u.get("type") == "timer"]
    out += ["### Services (systemd)", "", *_status_line(units, "systemd units")]
    if services:
        out += _table(
            ["Unit", "State", "Enabled", "Program", "Unit file"],
            [
                [
                    u.get("name"),
                    "/".join(x for x in (u.get("active"), u.get("sub")) if x),
                    u.get("enabled"),
                    ", ".join(u.get("programs") or []),
                    (u.get("unit_file") or "") + (" (custom)" if u.get("custom") else ""),
                ]
                for u in services
            ],
        )
    if timers:
        out += ["### Timers", ""]
        out += _table(
            ["Timer", "State", "Enabled"],
            [[u.get("name"), u.get("active"), u.get("enabled")] for u in timers],
        )
    docker = _sec(doc, "docker")
    out += ["### Containers", "", *_status_line(docker, "Docker")]
    projects = _list(docker, "compose_projects")
    for p in projects:
        out.append(
            f"- compose project **{_cell(p.get('name'))}** in `{_cell(p.get('working_dir'))}` "
            f"from {', '.join(f'`{f}`' for f in p.get('config_files') or []) or 'unknown files'}: "
            f"services {', '.join(p.get('services') or []) or 'none'}"
        )
    if projects:
        out.append("")
    containers = _list(docker, "containers")
    if containers:
        out += _table(
            ["Container", "Image", "Digest", "State", "Restart", "Ports", "Mounts"],
            [
                [
                    c.get("name"),
                    c.get("image"),
                    c.get("digest"),
                    c.get("state"),
                    f"{c.get('restart_policy') or '-'} ({c.get('restart_count', 0)} restarts)",
                    ", ".join(
                        f"{p.get('ip') + ':' if p.get('ip') else ''}{p.get('host_port')}->"
                        f"{p.get('container_port')}/{p.get('proto')}"
                        if p.get("host_port")
                        else f"{p.get('container_port')}/{p.get('proto')}"
                        for p in c.get("ports") or []
                    ),
                    ", ".join(
                        f"{m.get('source') or m.get('volume')}:{m.get('destination')}"
                        + ("" if m.get("rw") else ":ro")
                        for m in c.get("mounts") or []
                    ),
                ]
                for c in containers
            ],
        )
    return out


def _how_exposed(doc: dict[str, Any]) -> list[str]:
    out = ["## How it is exposed", "", "### Listening sockets", ""]
    listening = _sec(doc, "listening")
    out += _status_line(listening, "listening sockets")
    sockets = _list(listening, "sockets")
    if sockets:
        out += _table(
            ["Proto", "Address", "Port", "Process", "Unit", "User"],
            [
                [
                    s.get("proto"),
                    s.get("address"),
                    s.get("port"),
                    s.get("process") or s.get("owner"),
                    s.get("unit"),
                    s.get("user"),
                ]
                for s in sockets
            ],
        )
    nginx = _sec(doc, "nginx")
    out += ["### Web server (nginx)", "", *_status_line(nginx, "nginx")]
    for s in _list(nginx, "servers"):
        names = ", ".join(s.get("server_names") or []) or "(default)"
        out.append(
            f"- **{_cell(names)}** ({s.get('context')}) listen {', '.join(s.get('listen') or [])}"
        )
        tls = s.get("tls") or {}
        if tls:
            client = (
                f"; client certificates {tls.get('verify_client')} "
                f"against `{tls.get('client_certificate')}`"
                if tls.get("verify_client")
                else ""
            )
            out.append(f"  - TLS certificate `{tls.get('certificate')}`{client}")
        for key, label in (("root", "serves"), ("proxy_pass", "proxies to"), ("return", "returns")):
            if s.get(key):
                out.append(f"  - {label} `{_cell(s.get(key))}`")
        for loc in s.get("locations") or []:
            target = (
                (f"proxy to `{loc['proxy_pass']}`" if loc.get("proxy_pass") else "")
                or (f"`{loc['pass']}`" if loc.get("pass") else "")
                or (
                    f"files from `{loc['alias'] or loc['root']}`"
                    if loc.get("alias") or loc.get("root")
                    else ""
                )
                or (f"return `{loc['return']}`" if loc.get("return") else "")
                or "(inherits)"
            )
            out.append(f"  - `location {_cell(loc.get('match'))}` -> {target}")
        out.append(f"  - defined in `{s.get('file')}`")
    upstreams = _list(nginx, "upstreams")
    if upstreams:
        out += ["", "Upstreams:"] + [
            f"- `{u.get('name')}`: {', '.join(u.get('servers') or [])}" for u in upstreams
        ]
    out += ["", "### Firewall", ""]
    ufw = _sec(doc, "firewall", "ufw")
    if ufw.get("status") == "not_installed":
        out.append("- ufw: not installed")
    else:
        enabled = _yes_no(ufw.get("enabled"), "enabled", "disabled", "state unknown")
        out.append(f"- ufw: {enabled}")
        if ufw.get("rules_status") == "not_visible":
            out.append("  - rules: not visible to the agent")
        for r in _list(ufw, "rules"):
            out.append(
                f"  - {r.get('action')} {r.get('proto')} {r.get('port')} "
                f"from {r.get('from')} ({r.get('direction')})"
            )
    nft = _sec(doc, "firewall", "nftables")
    if nft.get("status") == "ok":
        for t in _list(nft, "tables"):
            chains = ", ".join(
                f"{c.get('name')} ({c.get('hook') or 'regular'}, "
                f"policy {c.get('policy') or '-'}, {c.get('rules')} rules)"
                for c in t.get("chains") or []
            )
            out.append(f"- nftables {t.get('family')} {t.get('name')}: {chains or 'no chains'}")
    else:
        out.append(f"- nftables: {_reason(nft, 'nftables')}")
    return [*out, ""]


def _config_locations(doc: dict[str, Any]) -> list[str]:
    out = ["## Where its configuration lives", ""]
    files = [f.get("file") for f in _list(doc, "nginx", "files")]
    if files:
        out.append("- nginx: " + ", ".join(f"`{f}`" for f in files))
    custom = [u.get("unit_file") for u in _list(doc, "units", "units") if u.get("custom")]
    if custom:
        out.append("- custom systemd units: " + ", ".join(f"`{f}`" for f in custom))
    for p in _list(doc, "docker", "compose_projects"):
        out.append(
            f"- compose project {p.get('name')}: "
            + (
                ", ".join(f"`{f}`" for f in p.get("config_files") or [])
                or f"`{p.get('working_dir')}`"
            )
        )
    for r in _list(doc, "certificates", "letsencrypt"):
        out.append(
            f"- certbot lineage {r.get('name')}: `/etc/letsencrypt/renewal/{r.get('name')}.conf`"
        )
    if len(out) == 2:
        out.append("- nothing the agent could attribute")
    return [*out, ""]


def _depends_on(doc: dict[str, Any]) -> list[str]:
    out = ["## What it depends on", ""]
    targets: list[str] = []
    for s in _list(doc, "nginx", "servers"):
        if s.get("proxy_pass"):
            targets.append(str(s["proxy_pass"]))
        targets += [
            str(loc["proxy_pass"]) for loc in s.get("locations") or [] if loc.get("proxy_pass")
        ]
    for u in _list(doc, "nginx", "upstreams"):
        targets += [str(x) for x in u.get("servers") or []]
    if targets:
        out.append(
            "- backends it forwards to: " + ", ".join(f"`{t}`" for t in sorted(set(targets)))
        )
    certs = _list(doc, "certificates", "certificates")
    for c in certs:
        if c.get("status") == "ok":
            out.append(
                f"- certificate `{c.get('path')}` "
                f"({', '.join(c.get('names') or []) or c.get('subject')}) "
                f"expires {c.get('not_after')} ({c.get('days_left')} days)"
            )
        else:
            out.append(f"- certificate `{c.get('path')}`: {c.get('reason') or c.get('status')}")
    for r in _list(doc, "certificates", "letsencrypt"):
        out.append(
            f"- certbot renews {', '.join(r.get('domains') or [])} "
            f"via {r.get('authenticator') or 'unknown'}"
            + (f" (installer {r.get('installer')})" if r.get("installer") else "")
        )
    tm = _sec(doc, "time")
    if tm.get("status") == "ok":
        synced = _yes_no(tm.get("synchronized"), "synchronised", "NOT synchronised", "unknown")
        out.append(f"- time: {synced}, timezone {tm.get('timezone') or 'unknown'}")
    upd = _sec(doc, "updates")
    if upd.get("status") == "ok":
        out.append(
            f"- pending updates: {upd.get('pending')} ({upd.get('security')} security)"
            + ("; a reboot is required" if upd.get("reboot_required") else "")
        )
    return [*out, ""]


def _not_visible(doc: dict[str, Any]) -> list[str]:
    parts = {
        "listening sockets": _sec(doc, "listening"),
        "systemd units": _sec(doc, "units"),
        "Docker": _sec(doc, "docker"),
        "nginx": _sec(doc, "nginx"),
        "certificates": _sec(doc, "certificates"),
        "ufw": _sec(doc, "firewall", "ufw"),
        "nftables": _sec(doc, "firewall", "nftables"),
        "updates": _sec(doc, "updates"),
        "time sync": _sec(doc, "time"),
    }
    hidden = [
        f"- {name}: {_reason(sec, name)}"
        for name, sec in parts.items()
        if sec.get("status") in _HIDDEN
    ]
    truncated = doc.get("truncated") or []
    if not hidden and not truncated:
        return []
    out = ["## Not visible to the agent", "", *hidden]
    if truncated:
        out.append("- the description was trimmed to fit: " + "; ".join(str(t) for t in truncated))
    return [*out, ""]


def _checklist(doc: dict[str, Any]) -> list[str]:
    host = _sec(doc, "host")
    steps = [f"Install {host.get('os') or 'the operating system'} and the HomePilot agent."]
    custom = [u for u in _list(doc, "units", "units") if u.get("custom")]
    files = [f.get("file") for f in _list(doc, "nginx", "files")]
    if files:
        steps.append("Restore the nginx configuration (files above) and check it with `nginx -t`.")
    for r in _list(doc, "certificates", "letsencrypt"):
        steps.append(
            f"Re-issue the certificate for {', '.join(r.get('domains') or [])} with certbot "
            f"({r.get('authenticator') or 'its authenticator'})."
        )
    for p in _list(doc, "docker", "compose_projects"):
        files_arg = " ".join(f"-f {f}" for f in p.get("config_files") or [])
        steps.append(
            f"Restore `{p.get('working_dir')}` and its secrets, then "
            f"`docker compose {files_arg} up -d` (services {', '.join(p.get('services') or [])})."
        )
    if custom:
        steps.append(
            "Recreate and enable the custom units: "
            + ", ".join(str(u.get("name")) for u in custom)
            + "."
        )
    ufw = _sec(doc, "firewall", "ufw")
    if ufw.get("enabled"):
        steps.append("Re-apply the ufw rules above and enable ufw.")
    steps.append("Compare a fresh description of the rebuilt host with this note.")
    return ["## Rebuild checklist", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)] + [""]


def render_asbuilt(hostname: str, doc: dict[str, Any]) -> str:
    agent = _sec(doc, "agent")
    host = _sec(doc, "host")
    mode = (
        "privileged"
        if agent.get("privileged")
        else f"unprivileged, as {agent.get('run_as') or 'its own user'}"
    )
    lines = [
        f"# As-built: {hostname}",
        "",
        f"Collected {doc.get('collected_at', 'at an unknown time')} by the HomePilot agent "
        f"{agent.get('version') or ''} ({mode}). {host.get('os') or 'Unknown OS'}, kernel "
        f"{host.get('kernel') or 'unknown'}. Generated by HomePilot from the host description; "
        "it supersedes this host's earlier as-built notes. Secrets are never part of it: "
        "credentials, environments and command lines stay on the host.",
        "",
    ]
    lines += _what_runs(doc)
    lines += _how_exposed(doc)
    lines += _config_locations(doc)
    lines += _depends_on(doc)
    lines += _not_visible(doc)
    lines += _checklist(doc)
    return "\n".join(lines).rstrip() + "\n"
