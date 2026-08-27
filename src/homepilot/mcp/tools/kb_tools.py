"""Knowledge base tools: search and record facts."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homepilot.artifacts.lifecycle import ArtifactLifecycle

if TYPE_CHECKING:
    from homepilot.db.repository import Repository

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_kb",
        "description": (
            "Search the knowledge base using vector + keyword search. "
            "Returns matching notes, policies, and decisions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string",
                },
                "kind": {
                    "type": ["string", "null"],
                    "description": "Filter by kind: note, policy, or decision. Null for all.",
                },
            },
            "required": ["query"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "results": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["results", "total"],
        },
    },
    {
        "name": "record_fact",
        "description": (
            "Write a note, policy, or decision to the knowledge base. "
            "Non-mutating: auto-applied without approval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Host, service, or network this fact relates to",
                },
                "kind": {
                    "type": "string",
                    "description": "note, policy, or decision",
                },
                "content": {
                    "type": "string",
                    "description": "Fact content",
                },
                "supersedes": {
                    "type": ["string", "null"],
                    "description": "ID of prior fact this supersedes, or null",
                },
            },
            "required": ["target", "kind", "content"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["id", "status", "kind"],
        },
    },
    {
        "name": "list_kb",
        "description": (
            "Browse the knowledge base by page rather than by search: the stored "
            "documents (id, title, kind, target, source, embedded_at), newest-indexed "
            "first, optionally filtered by kind or target. `total` counts every "
            "matching document, not the page. Use search_kb when you know what you are "
            "looking for; use this to see what is there at all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": ["string", "null"], "description": "Filter by kind"},
                "target": {"type": ["string", "null"], "description": "Filter by target"},
                "limit": {"type": "integer", "description": "Page size (1-1000, default 100)"},
                "offset": {"type": "integer", "description": "Rows to skip (default 0)"},
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    {
        "name": "get_kb_doc",
        "description": (
            "One knowledge-base document's stored record: title, kind, target, source, "
            "path and when it was indexed. Read-only. Accepts either the numeric doc id "
            "listed by list_kb/search_kb, OR the artifact-slug id record_fact returns "
            "(e.g. `2026-08-27-kb-web01-a1b2c3`, with or without the `artifact:` prefix)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": ["integer", "string"],
                    "description": (
                        "Numeric KB row id, or the artifact-slug id record_fact returned "
                        "(optionally prefixed `artifact:`)"
                    ),
                },
            },
            "required": ["doc_id"],
        },
        "outputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}},
    },
    # get_kb_embedding_status was here (wave 1) but GET /kb/embedding-status is
    # API `admin`; with no admin MCP tier yet it is removed - see the admin-wave
    # exclusion in the read parity gate.
    #
    # ── Mutator (MCP<->API parity, wave 2). record_fact already covers writing a
    # note/policy (POST /kb and POST /kb/notes). PUT /kb/{doc_id} is API `write`,
    # so update_kb_doc is `full`. The DELETE/ingest/reindex routes are API
    # `admin`, so those tools wait for the admin wave. ────────────────────────────
    {
        "name": "update_kb_doc",
        "description": (
            "Edit an existing knowledge-base document: any of title, content, kind or "
            "target. Only the fields you pass change. Returns the updated record; an "
            "unknown id is an error. Accepts the numeric id from list_kb OR the "
            "artifact-slug id record_fact returns - but note that artifact-backed notes "
            "(source `artifact:...`) are immutable and cannot be edited in place: record "
            "a superseding fact instead (record_fact with `supersedes`)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": ["integer", "string"],
                    "description": (
                        "Numeric KB row id, or the artifact-slug id record_fact returned "
                        "(optionally prefixed `artifact:`)"
                    ),
                },
                "title": {"type": ["string", "null"]},
                "content": {"type": ["string", "null"]},
                "kind": {"type": ["string", "null"]},
                "target": {"type": ["string", "null"]},
            },
            "required": ["doc_id"],
        },
        "outputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}},
    },
    # ── Admin tier (MCP<->API parity, wave 3). DELETE /kb/{id}, POST /kb/ingest,
    # POST /kb/reindex and GET /kb/embedding-status are all API
    # require_scope("admin"), so these need an admin MCP token. ──────────────────
    {
        "name": "delete_kb_doc",
        "description": (
            "Permanently delete a knowledge-base document. Returns whether a row was "
            "removed; an unknown id is an error. Accepts the numeric id from list_kb OR "
            "the artifact-slug id record_fact returns; deleting an artifact-backed note "
            "revokes its backing artifact so the deletion sticks (a bare row delete would "
            "be resurrected by the next reindex). Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": ["integer", "string"],
                    "description": (
                        "Numeric KB row id, or the artifact-slug id record_fact returned "
                        "(optionally prefixed `artifact:`)"
                    ),
                },
            },
            "required": ["doc_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "deleted": {"type": "boolean"}},
            "required": ["id", "deleted"],
        },
    },
    {
        "name": "ingest_kb",
        "description": (
            "Ingest knowledge-base documents from files on the control-plane host: each "
            "source names a path, a kind (note/policy/fact), and an optional target. "
            "Returns how many were created, skipped and errored. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "description": "Files to ingest",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "kind": {"type": "string", "description": "note, policy, or fact"},
                            "target": {"type": ["string", "null"]},
                        },
                        "required": ["path"],
                    },
                },
            },
            "required": ["sources"],
        },
        "outputSchema": {"type": "object", "properties": {"created": {"type": "integer"}}},
    },
    {
        "name": "reindex_kb",
        "description": (
            "Rebuild the knowledge-base search index over every stored document. Pass "
            "no_embeddings=true to rebuild only the keyword index and skip recomputing "
            "vector embeddings. Returns counts of what was reindexed. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "no_embeddings": {
                    "type": "boolean",
                    "description": "Skip recomputing vector embeddings (keyword index only)",
                },
            },
        },
        "outputSchema": {"type": "object", "properties": {"status": {"type": "string"}}},
    },
    {
        "name": "get_kb_embedding_status",
        "description": (
            "Whether the embedding services backing vector search are configured and "
            "reachable, and how many documents are embedded vs pending. A read, but the "
            "API reserves it for admin, so it needs an admin token."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {}},
    },
]


async def handle_list_kb(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    raw_limit = arguments.get("limit")
    limit = 100 if raw_limit is None else max(1, min(1000, int(raw_limit)))
    offset = max(0, int(arguments.get("offset") or 0))
    items, total = await repo.list_doc_metadata(
        kind=arguments.get("kind"),
        target=arguments.get("target"),
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def _resolve_doc_row(repo: Repository, raw: Any) -> dict[str, Any]:
    """Resolve a `doc_id` argument to its doc_metadata row.

    The KB has two id spaces. `list_kb`/`search_kb` expose the integer row id;
    `record_fact` returns the artifact slug it wrote (indexed as source
    `artifact:<slug>`). Before #592 the read/update/delete handlers ran a bare
    `int(doc_id)` on whatever they were given, so feeding back the id record_fact
    just returned crashed with a raw `invalid literal for int()`.

    Accepts:
      * an integer, or a plain-digit string -> the numeric row id;
      * `artifact:<slug>` (or any `<prefix>:<rest>` source) -> looked up by source;
      * a bare `<slug>` -> looked up as source `artifact:<slug>`.

    Raises a clean ValueError (never a leaked int() error) on bad input or an
    unknown id; the MCP dispatch turns ValueError into a tool-error result.
    """
    if raw is None:
        raise ValueError("doc_id is required")
    # bool is an int subclass - reject it before the int branch swallows it.
    if isinstance(raw, bool):
        raise ValueError(f"invalid doc_id: {raw!r}")
    if isinstance(raw, int):
        row = await repo.get_doc_metadata(raw)
        if row is None:
            raise ValueError(f"KB entry not found: {raw}")
        return row
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ValueError("doc_id is required")
        if s.isdigit():
            row = await repo.get_doc_metadata(int(s))
            if row is None:
                raise ValueError(f"KB entry not found: {s}")
            return row
        source = s if ":" in s else f"artifact:{s}"
        row = await repo.get_doc_by_source(source)
        if row is None:
            raise ValueError(f"KB entry not found: {raw}")
        return row
    raise ValueError(f"invalid doc_id: {raw!r}")


async def handle_get_kb_doc(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    row = await _resolve_doc_row(repo, arguments.get("doc_id"))
    return dict(row)


async def handle_search_kb(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = arguments["query"]
    kind = arguments.get("kind")
    results = await ctx["kb_service"].search(query, kind=kind, limit=20)
    return {"results": results, "total": len(results)}


def _mcp_caller_id() -> str:
    """Return a stable identity string for the current MCP caller."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    if token is not None:
        return token.client_id or "mcp-http"
    from homepilot.mcp.server import _mcp_caller_id_var

    return _mcp_caller_id_var.get()


async def handle_record_fact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    lifecycle: ArtifactLifecycle = ctx["lifecycle"]
    target = arguments["target"]
    kind = arguments["kind"]
    content = arguments["content"]
    supersedes = arguments.get("supersedes")

    date_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
    slug = target.lower().replace(".", "-").replace(" ", "-")[:30]
    fact_id = f"{date_prefix}-kb-{slug}"
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:6]
    fact_id = f"{fact_id}-{content_hash}"

    spec = {
        "id": fact_id,
        "kind": "kb-note",
        "intent": f"{kind}: {content[:180]}",
        "body": content,
        "note_kind": kind,
        "target": {"kind": "global", "host": target},
        "produced_by": {"session": "mcp", "agent": "mcp-tool", "user": _mcp_caller_id()},
    }
    if supersedes:
        spec["supersedes"] = [supersedes]

    artifact_id = await lifecycle.propose(spec)

    # Index it NOW (#388). A kb-note is marked `applied` on propose, and this
    # path never reindexed - so an agent recorded a fact, was told
    # `{"status": "applied"}`, and an immediate `search_kb` found nothing until
    # the process restarted. "Recorded" that cannot be read back is not recorded.
    kb_service = ctx.get("kb_service")
    if kb_service is not None:
        try:
            await kb_service.reindex_if_needed(reason="record_fact")
        except Exception:
            logger.warning("could not index the recorded fact %s", artifact_id, exc_info=True)

    return {"id": artifact_id, "status": "applied", "kind": kind}


# ── Mutators (wave 2). Call the SAME repo/service the KB management routes call. ──


async def handle_update_kb_doc(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    existing = await _resolve_doc_row(repo, arguments.get("doc_id"))
    doc_id = int(existing["id"])
    updates = {
        k: arguments[k]
        for k in ("title", "content", "kind", "target")
        if arguments.get(k) is not None
    }
    if not updates:
        return dict(existing)
    # An artifact-backed note's doc_metadata row is a MIRROR the reindex rebuilds
    # from the artifact body: an in-place edit here would be silently overwritten
    # the next time the KB reindexes. Artifacts are immutable by design, so refuse
    # the edit and point the caller at the real path - supersede via record_fact
    # (#592). Integer/ingested/observed rows are the real record and edit freely.
    source = str(existing.get("source") or "")
    if source.startswith("artifact:"):
        slug = source[len("artifact:") :]
        raise ValueError(
            f"KB entry {doc_id} is backed by artifact '{slug}' and cannot be edited in "
            "place: artifact-backed notes are immutable and a reindex would overwrite "
            f"the change. Record a superseding fact instead (record_fact with "
            f"supersedes='{slug}')."
        )
    result: dict[str, Any] | None = await repo.update_doc_metadata(doc_id, **updates)
    if result is None:
        raise ValueError(f"KB entry not found: {doc_id}")
    return result


# ── Admin-tier handlers (wave 3). Call the SAME repo/service the KB admin routes
# call: DELETE /kb/{id}, POST /kb/ingest, POST /kb/reindex, GET
# /kb/embedding-status. ──────────────────────────────────────────────────────


async def handle_delete_kb_doc(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")
    row = await _resolve_doc_row(repo, arguments.get("doc_id"))
    doc_id = int(row["id"])
    source = str(row.get("source") or "")
    if source.startswith("artifact:"):
        # Deleting only the doc_metadata mirror desyncs: the backing artifact is
        # still `applied`, so the next reindex_if_needed re-creates the row. Revoke
        # the artifact (the source of truth) so the deletion sticks, then drop the
        # mirror row now for immediate consistency - revoke's own background
        # reindex would also drop it, but callers expect it gone on return (#592).
        lifecycle = ctx.get("lifecycle")
        if lifecycle is None:
            raise RuntimeError("Lifecycle not configured")
        artifact_id = source[len("artifact:") :]
        await lifecycle.revoke(
            artifact_id, user=_mcp_caller_id(), reason="deleted via delete_kb_doc"
        )
        await repo.delete_doc_metadata(doc_id)
        return {"id": doc_id, "deleted": True}
    deleted = await repo.delete_doc_metadata(doc_id)
    if not deleted:
        raise ValueError(f"KB entry not found: {doc_id}")
    return {"id": doc_id, "deleted": True}


def _kb_service(ctx: dict[str, Any]) -> Any:
    svc = ctx.get("kb_service")
    if svc is None:
        raise RuntimeError("KB service not configured")
    return svc


async def handle_ingest_kb(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sources = list(arguments.get("sources") or [])
    result: dict[str, Any] = await _kb_service(ctx).ingest(sources=sources)
    return result


async def handle_reindex_kb(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    no_embeddings = bool(arguments.get("no_embeddings", False))
    result: dict[str, Any] = await _kb_service(ctx).reindex(
        no_embeddings=no_embeddings, reason="manual"
    )
    return result


async def handle_get_kb_embedding_status(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = await _kb_service(ctx).embedding_status()
    return result
