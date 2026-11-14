"""`hp api`: every operation of the server, generated from its OpenAPI document.

The curated commands (`hp hosts`, `hp changes`, ...) cover what operators do
every day. Everything else the API can do is reachable here, built at run time
from the document of the SERVER the run talks to - so a route a newer server
release adds is usable before this CLI is updated, and the CLI never carries a
stale copy of the API.

    hp api --list                                  every operation
    hp api <group> <operation> [PATH PARAMS] [--option value ...]
           [--body JSON | --body - | --body-file FILE] [--table] [--yes]
    hp api raw METHOD PATH [--body ...]            the escape hatch

``<group>`` is the operation's first OpenAPI tag, ``<operation>`` its handler
name in kebab case. Path parameters are positional, query parameters and the
simple top-level fields of a JSON request body are options; anything else goes
in ``--body``. Output is JSON (this is the scripting surface); ``--table``
prints a list of flat objects as a table.

A DELETE, or an operation the server marks ``x-hp-destructive``, names what it
will affect and asks first; without a terminal it refuses unless ``--yes``.

The document is cached per server URL and version under
``~/.cache/homepilot/openapi/``; a server upgrade is a new version, so the cache
can never serve an old server's API for a new one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import click

from homepilot.cli import remote
from homepilot.common import write_private

# Operations under these tags are in the document but cannot be used with an
# API token, so the CLI does not offer them. The coverage gate reads this map.
HIDDEN_TAGS: dict[str, str] = {
    "guest": (
        "the guest portal: a friend's own view, authenticated by their mTLS client "
        "certificate at the proxy - an API token is refused there"
    ),
    "invite": ("the invite portal a friend redeems a link on; the same mTLS trust, no API token"),
    "claim": (
        "the first-run claim, made once from a browser with the claim code before any token exists"
    ),
}

DESTRUCTIVE_EXTENSION = "x-hp-destructive"
# The scope the on-box CLI mints for itself to drive this layer: every tier,
# since the operation decides what it needs and the server enforces it.
LOCAL_SCOPE = "read,write,admin"
_RESERVED = {"body", "body-file", "table", "yes", "timeout", "help"}
_LABEL_KEYS = ("hostname", "name", "title", "label", "cn", "intent", "agent_id", "prefix", "id")

SessionFactory = Callable[..., Any]


class ApiLayerError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ── The document ─────────────────────────────────────────────────────────────


def _kebab(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass(frozen=True)
class Operation:
    group: str
    name: str
    method: str
    path: str
    spec: dict[str, Any]

    @property
    def destructive(self) -> bool:
        return self.method == "DELETE" or bool(self.spec.get(DESTRUCTIVE_EXTENSION))

    @property
    def summary(self) -> str:
        return str(self.spec.get("summary") or "")


def _handler_name(op_id: str, path: str, method: str) -> str:
    """FastAPI's operationId is ``<handler><path with non-word chars as _>_<method>``;
    the handler name alone is the readable operation name."""
    suffix = re.sub(r"\W", "_", path) + "_" + method.lower()
    return op_id[: -len(suffix)] if op_id.endswith(suffix) and len(op_id) > len(suffix) else op_id


def operations(spec: dict[str, Any]) -> list[Operation]:
    """Every operation the CLI offers: in the document, with an operationId, and
    not under a hidden tag."""
    found: list[Operation] = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            op_id = op.get("operationId")
            if not op_id:
                continue
            tags = op.get("tags") or ["other"]
            if tags[0] in HIDDEN_TAGS:
                continue
            found.append(
                Operation(
                    group=_kebab(tags[0]),
                    name=_kebab(_handler_name(op_id, path, method)),
                    method=method.upper(),
                    path=path,
                    spec=op,
                )
            )
    # Two operations sharing a name in one group both fall back to their full id.
    seen: dict[tuple[str, str], int] = {}
    for op in found:
        seen[(op.group, op.name)] = seen.get((op.group, op.name), 0) + 1
    return sorted(
        (
            Operation(op.group, _kebab(op.spec["operationId"]), op.method, op.path, op.spec)
            if seen[(op.group, op.name)] > 1
            else op
            for op in found
        ),
        key=lambda o: (o.group, o.name),
    )


def _resolve(spec: dict[str, Any], schema: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(schema, dict) or depth > 8:
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        target = (spec.get("components") or {}).get("schemas", {}).get(ref.rsplit("/", 1)[1])
        return _resolve(spec, target, depth + 1)
    for key in ("anyOf", "oneOf"):
        options = [s for s in schema.get(key) or [] if s.get("type") != "null"]
        if not options:
            continue
        # `X | None` is X. A union with a primitive branch (`str | dict`) is
        # offered as that primitive; the other shapes still go in --body.
        resolved = [_resolve(spec, o, depth + 1) for o in options]
        simple = [
            r for r in resolved if r.get("type") in ("string", "integer", "number", "boolean")
        ]
        chosen = resolved[0] if len(resolved) == 1 else (simple[0] if simple else None)
        if chosen is not None:
            merged = {**schema, **chosen}
            merged.pop(key, None)
            return merged
    return schema


def _click_type(schema: dict[str, Any]) -> Any:
    if schema.get("enum"):
        return click.Choice([str(v) for v in schema["enum"]])
    return {"integer": click.INT, "number": click.FLOAT, "boolean": click.BOOL}.get(
        str(schema.get("type")), click.STRING
    )


def _is_simple(schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    if kind in ("string", "integer", "number", "boolean"):
        return True
    return kind == "array" and (schema.get("items") or {}).get("type") in (
        "string",
        "integer",
        "number",
    )


def _option_name(name: str, taken: set[str]) -> str:
    flag = _kebab(name) or "value"
    if flag in _RESERVED or flag in taken:
        flag = f"param-{flag}"
    taken.add(flag)
    return flag


# ── Calling ──────────────────────────────────────────────────────────────────


def _error_text(resp: Any) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:
        return str(resp.text)[:500]
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        for key in ("message", "reason", "error"):
            if isinstance(detail.get(key), str):
                return str(detail[key])
    return json.dumps(detail) if detail is not None else str(resp.text)[:500]


def _flat_rows(data: Any) -> list[dict[str, Any]] | None:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        return None
    scalar = (str, int, float, bool, type(None))
    if all(isinstance(v, scalar) for row in data for v in row.values()):
        return data
    return None


def _emit(data: Any, table: bool) -> None:
    if table:
        rows = _flat_rows(data)
        if rows is None:
            click.echo("The answer is not a list of flat objects; printing JSON.", err=True)
        else:
            from rich.table import Table

            from homepilot.cli.output import CliConsole

            columns: list[str] = []
            for row in rows:
                columns += [k for k in row if k not in columns]
            out = Table(*columns)
            for row in rows:
                out.add_row(*("" if row.get(c) is None else str(row.get(c)) for c in columns))
            CliConsole().print(out)
            return
    click.echo(json.dumps(data, indent=2, default=str))


class _Runner:
    """Holds the session factory and the document for one `hp api` run."""

    def __init__(self, session: SessionFactory, spec: dict[str, Any]) -> None:
        self.session = session
        self.spec = spec
        self.ops = operations(spec)

    def _request(self, method: str, path: str, body: Any, timeout: float) -> Any:
        async def go() -> Any:
            async with self.session(LOCAL_SCOPE) as call:
                return await call(method, path, body, timeout=timeout, raw=True)

        try:
            return asyncio.run(go())
        except ApiLayerError:
            raise
        except Exception as exc:
            raise ApiLayerError(
                f"{method} {path} failed: {remote.redact(str(exc)) or type(exc).__name__}"
            ) from None

    def _identity(self, template: str, path: str) -> str:
        """A short name for what a destructive call will affect, read from the
        GET on the same resource when the API has one."""
        if "get" not in (self.spec.get("paths") or {}).get(template, {}):
            return ""
        try:
            resp = self._request("GET", path, None, 15.0)
        except ApiLayerError:
            return ""
        if resp.status_code != 200:
            return ""
        try:
            data = resp.json()
        except ValueError:
            return ""
        if isinstance(data, dict):
            for key in _LABEL_KEYS:
                if data.get(key) not in (None, ""):
                    return f" ({key} {data[key]})"
        return ""

    def _confirm(self, method: str, path: str, template: str, body: Any, yes: bool) -> None:
        if yes:
            return
        what = f"{method} {path}{self._identity(template, path)}"
        if body:
            what += f" with {json.dumps(body)[:200]}"
        if not sys.stdin.isatty():
            raise ApiLayerError(
                f"Refusing: {what} is destructive. Pass --yes to confirm (no terminal to ask on)."
            )
        if not click.confirm(f"This will {what}. Go ahead?", default=False, err=True):
            raise ApiLayerError("Left alone.")

    def execute(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        destructive: bool,
        template: str,
        yes: bool,
        table: bool,
        timeout: float,
    ) -> None:
        if destructive:
            self._confirm(method, path, template, body, yes)
        resp = self._request(method, path, body, timeout)
        if resp.status_code >= 400:
            raise ApiLayerError(
                f"{method} {path} refused ({resp.status_code}): {remote.redact(_error_text(resp))}"
            )
        if resp.status_code == 204 or not resp.content:
            click.echo(f"{method} {path}: done ({resp.status_code})", err=True)
            return
        try:
            data = resp.json()
        except ValueError:
            click.echo(remote.redact(resp.text))
            return
        _emit(data, table)

    def match(self, method: str, path: str) -> Operation | None:
        bare = path.split("?", 1)[0]
        for op in self.ops:
            if op.method != method:
                continue
            pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(op.path)) + "$"
            if re.match(pattern, bare):
                return op
        return None


def _read_body(body: str | None, body_file: str | None) -> Any:
    if body is not None and body_file is not None:
        raise click.UsageError("Pass --body or --body-file, not both.")
    text: str | None = None
    if body_file is not None:
        text = sys.stdin.read() if body_file == "-" else Path(body_file).read_text("utf-8")
    elif body is not None:
        text = sys.stdin.read() if body == "-" else body
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise click.UsageError(f"The request body is not JSON: {exc}") from None


def _common_options(body: bool = True) -> list[click.Parameter]:
    body_options: list[click.Parameter] = [
        click.Option(["--body"], default=None, help="Request body as JSON, or - for stdin."),
        click.Option(
            ["--body-file"], default=None, help="Request body from a JSON file (- = stdin)."
        ),
    ]
    return [
        *(body_options if body else []),
        click.Option(["--table"], is_flag=True, help="Print a list of flat objects as a table."),
        click.Option(["--yes", "-y"], is_flag=True, help="Do not ask before a destructive call."),
        click.Option(["--timeout"], type=click.FLOAT, default=60.0, show_default=True),
    ]


def _command(runner: _Runner, op: Operation) -> click.Command:
    spec = runner.spec
    params: list[click.Parameter] = []
    taken: set[str] = set()
    path_names = re.findall(r"\{([^}]+)\}", op.path)
    for name in path_names:
        params.append(click.Argument([f"path_{len(params)}"], metavar=name.upper()))
    path_keys = [p.name for p in params]
    query: list[tuple[str, str, dict[str, Any]]] = []
    for raw in op.spec.get("parameters") or []:
        param = _resolve(spec, raw)
        if param.get("in") != "query":
            continue  # path params are positional; header/cookie are never user-set
        schema = _resolve(spec, param.get("schema"))
        key = f"q_{len(query)}"
        flag = _option_name(str(param["name"]), taken)
        is_list = schema.get("type") == "array"
        item = _resolve(spec, schema.get("items")) if is_list else schema
        params.append(
            click.Option(
                [f"--{flag}", key],
                type=_click_type(item),
                multiple=is_list,
                required=bool(param.get("required")),
                help=str(param.get("description") or schema.get("description") or ""),
            )
        )
        query.append((key, str(param["name"]), schema))
    fields: list[tuple[str, str]] = []
    body_schema: dict[str, Any] = {}
    request = op.spec.get("requestBody") or {}
    content = (request.get("content") or {}).get("application/json") or {}
    if content:
        body_schema = _resolve(spec, content.get("schema"))
        for name, raw in (body_schema.get("properties") or {}).items():
            prop = _resolve(spec, raw)
            if not _is_simple(prop):
                continue
            key = f"b_{len(fields)}"
            flag = _option_name(name, taken)
            is_list = prop.get("type") == "array"
            item = _resolve(spec, prop.get("items")) if is_list else prop
            required = name in (body_schema.get("required") or [])
            params.append(
                click.Option(
                    [f"--{flag}", key],
                    type=_click_type(item),
                    multiple=is_list,
                    help=(
                        str(prop.get("description") or "")
                        + (" [body, required]" if required else " [body]")
                    ).strip(),
                )
            )
            fields.append((key, name))
    params += _common_options(body=bool(request))

    def callback(**kwargs: Any) -> None:
        path = op.path
        for name, key in zip(path_names, path_keys, strict=True):
            path = path.replace("{" + name + "}", quote(str(kwargs[key]), safe=""), 1)
        qs = {
            name: kwargs[key]
            for key, name, _s in query
            if kwargs[key] is not None and kwargs[key] != ()
        }
        if qs:
            path += "?" + urlencode(
                {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in qs.items()},
                doseq=True,
            )
        body = _read_body(kwargs.get("body"), kwargs.get("body_file"))
        set_fields = {
            name: list(kwargs[key]) if isinstance(kwargs[key], tuple) else kwargs[key]
            for key, name in fields
            if kwargs[key] is not None and kwargs[key] != ()
        }
        if set_fields:
            if body is None:
                body = {}
            if not isinstance(body, dict):
                raise click.UsageError("Field options need a JSON object body.")
            body.update(set_fields)
        if body is None and request.get("required"):
            body = {}
        runner.execute(
            op.method,
            path,
            body,
            destructive=op.destructive,
            template=op.path,
            yes=kwargs["yes"],
            table=kwargs["table"],
            timeout=kwargs["timeout"],
        )

    description = str(op.spec.get("description") or "").strip()
    head = f"{op.method} {op.path}" + ("  [destructive: asks first]" if op.destructive else "")
    return click.Command(
        op.name,
        params=params,
        callback=callback,
        help=f"{op.summary}\n\n{head}\n\n{description}".strip(),
        short_help=op.summary,
    )


def build(runner: _Runner) -> click.Group:
    root = click.Group(
        "api",
        help=(
            "Every operation of the server, from its OpenAPI document. "
            "`hp api --list` lists them; JSON out, --table for lists."
        ),
    )
    groups: dict[str, click.Group] = {}
    for op in runner.ops:
        group = groups.get(op.group)
        if group is None:
            group = groups[op.group] = click.Group(op.group, help=f"{op.group} operations")
            root.add_command(group)
        group.add_command(_command(runner, op))

    @click.command(
        "raw",
        params=[
            click.Argument(
                ["method"],
                type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE"], case_sensitive=False),
            ),
            click.Argument(["path"]),
            *_common_options(),
        ],
    )
    def raw(**kwargs: Any) -> None:
        """Call any path of the API: hp api raw METHOD /path[?query]."""
        method, path = str(kwargs["method"]).upper(), str(kwargs["path"])
        # Only a path on THIS server: an absolute URL would send the token elsewhere.
        if not path.startswith("/") or path.startswith("//"):
            raise click.UsageError("PATH is a path on the server, starting with a single /.")
        op = runner.match(method, path)
        runner.execute(
            method,
            path,
            _read_body(kwargs["body"], kwargs["body_file"]),
            destructive=method == "DELETE" or bool(op and op.destructive),
            template=op.path if op else "",
            yes=kwargs["yes"],
            table=kwargs["table"],
            timeout=kwargs["timeout"],
        )

    root.add_command(raw)
    return root


def listing(ops: list[Operation]) -> str:
    rows = [
        (
            op.method,
            op.path,
            f"hp api {op.group} {op.name}",
            ("! " if op.destructive else "") + op.summary,
        )
        for op in ops
    ]
    widths = [max((len(r[i]) for r in rows), default=0) for i in range(3)]
    lines = [
        f"{m:<{widths[0]}}  {p:<{widths[1]}}  {c:<{widths[2]}}  {s}".rstrip() for m, p, c, s in rows
    ]
    lines.append(f"\n{len(rows)} operations; ! = destructive, asks first unless --yes.")
    return "\n".join(lines)


# ── Fetching the document ────────────────────────────────────────────────────


def _cache_file(base: str, version: str) -> Path:
    key = hashlib.sha256(base.encode()).hexdigest()[:16]
    safe = re.sub(r"[^0-9A-Za-z.+-]", "_", version)
    return remote.cache_dir() / "openapi" / f"{key}-{safe}.json"


def load_spec(session: SessionFactory) -> dict[str, Any]:
    target = remote.active_target()
    base = target.base() if target is not None else "local"

    async def go() -> dict[str, Any]:
        async with session(LOCAL_SCOPE) as call:
            health = await call("GET", "/health", None, timeout=15.0, raw=True)
            version = ""
            try:
                version = str(health.json().get("version") or "")
            except ValueError:
                version = ""
            cached = _cache_file(base, version) if version else None
            if cached is not None and cached.exists():
                try:
                    loaded: dict[str, Any] = json.loads(cached.read_text("utf-8"))
                    return loaded
                except (OSError, ValueError):
                    pass  # unreadable cache: fetch it again
            resp = await call("GET", "/openapi.json", None, timeout=30.0, raw=True)
            if resp.status_code != 200:
                raise ApiLayerError(
                    f"The server did not serve its API document ({resp.status_code}): "
                    f"{remote.redact(_error_text(resp))}"
                )
            doc: dict[str, Any] = resp.json()
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                write_private(cached, json.dumps(doc))
            return doc

    try:
        return asyncio.run(go())
    except ApiLayerError:
        raise
    except Exception as exc:
        raise ApiLayerError(
            "Could not read the server's API document: "
            f"{remote.redact(str(exc)) or type(exc).__name__}"
        ) from None


def run(args: list[str], session: SessionFactory) -> int:
    """Run `hp api ARGS`; returns the exit code (0 ok, 1 refused/failed, 2 usage)."""
    try:
        runner = _Runner(session, load_spec(session))
        if args[:1] == ["--list"]:
            click.echo(listing(runner.ops))
            return 0
        if not args:
            args = ["--help"]
        build(runner).main(args=args, prog_name="hp api", standalone_mode=False)
        return 0
    except ApiLayerError as exc:
        click.echo(str(exc), err=True)
        return exc.exit_code
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        return 2 if isinstance(exc, click.UsageError) else exc.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 1
