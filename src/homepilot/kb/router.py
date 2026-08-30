from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ..auth.deps import require_scope
from .service import KBService, summarise_search_mode

logger = logging.getLogger(__name__)


class CreateNoteRequest(BaseModel):
    target: str | dict[str, str] | None = None
    kind: str = "note"
    content: str
    supersedes: list[str] | None = None


class UpdateNoteRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    kind: str | None = None
    target: str | None = None


class IngestSource(BaseModel):
    path: str
    kind: str = "note"
    target: str | None = None


class IngestRequest(BaseModel):
    sources: list[IngestSource] = []


router = APIRouter()


def _get_service(request: Request) -> KBService:
    svc: KBService = request.app.state.kb_service
    return svc


@router.get("", dependencies=[Depends(require_scope("read"))])
async def list_kb(
    request: Request,
    kind: str | None = Query(None),
    target: str | None = Query(None),
    # Default stays at the old hardcoded page size so existing clients see the
    # same rows; what changes is that `total` is now true and later pages exist.
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    repo = request.app.state.repo
    items, total = await repo.list_doc_metadata(
        kind=kind, target=target, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/search", dependencies=[Depends(require_scope("read"))])
async def search_kb(
    request: Request,
    q: str = Query(..., min_length=1),
    kind: str | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    svc = _get_service(request)
    results = await svc.search(q, kind=kind, limit=limit)
    # `search_mode` read a key the service never set, so this endpoint answered
    # "unknown" for every search that found anything and "empty" for every one
    # that did not - verified on 3.6.17 while vector search was demonstrably
    # running. The service labels each hit now, so the summary is a fact.
    return {"results": results, "total": len(results), **summarise_search_mode(results)}


@router.get("/embedding-status", dependencies=[Depends(require_scope("admin"))])
async def embedding_status(request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    return await svc.embedding_status()


async def _record_and_report(svc: KBService, body: CreateNoteRequest) -> dict[str, Any]:
    """Record a fact and say whether it is actually searchable.

    `POST /kb` returned `{"id": ...}` for a note that was NOT in the knowledge
    base: verified on 3.6.17, where the immediately following `GET /kb` returned
    `total: 0` and `GET /kb/search` found nothing. The note is indexed on propose
    now, and the answer says so rather than leaving the caller to assume.
    """
    artifact_id = await svc.record_fact(
        target=body.target,
        kind=body.kind,
        content=body.content,
        supersedes=body.supersedes,
    )
    row = await svc.repo.get_doc_by_source(f"artifact:{artifact_id}")
    return {
        "id": artifact_id,
        "indexed": row is not None,
        "doc_id": int(row["id"]) if row else None,
    }


@router.post("", dependencies=[Depends(require_scope("write"))])
async def record_fact(request: Request, body: CreateNoteRequest) -> dict[str, Any]:
    return await _record_and_report(_get_service(request), body)


@router.post("/notes", dependencies=[Depends(require_scope("write"))])
async def create_note(request: Request, body: CreateNoteRequest) -> dict[str, Any]:
    # ARTIFACT_SPEC.md §5.6 names the three note kinds: `note | policy |
    # decision`. This route accepted `fact` and REFUSED `decision`, so the one
    # kind the spec defines for a recorded choice could not be written here
    # while every other surface accepted it. `fact` stays accepted - installs
    # have written it - but `decision` is the spec's word.
    if body.kind not in ("note", "policy", "decision", "fact"):
        raise HTTPException(
            status_code=400, detail="kind must be note, policy or decision (fact is accepted too)"
        )
    return await _record_and_report(_get_service(request), body)


@router.post("/reindex", dependencies=[Depends(require_scope("admin"))])
async def reindex_kb(
    request: Request, no_embeddings: bool = False, force_embeddings: bool = False
) -> dict[str, Any]:
    logger.info(
        "KB reindex triggered: reason=manual, no_embeddings=%s, force_embeddings=%s",
        no_embeddings,
        force_embeddings,
    )
    svc = _get_service(request)
    result = await svc.reindex(
        no_embeddings=no_embeddings, reason="manual", force_embeddings=force_embeddings
    )
    return result


@router.post("/ingest", dependencies=[Depends(require_scope("admin"))])
async def ingest_kb(request: Request, body: IngestRequest) -> dict[str, Any]:
    logger.info("KB ingest triggered: sources=%d", len(body.sources))
    svc = _get_service(request)
    result = await svc.ingest(
        sources=[s.model_dump() for s in body.sources],
    )
    return result


@router.get("/{doc_id}", dependencies=[Depends(require_scope("read"))])
async def get_kb_entry(request: Request, doc_id: int) -> dict[str, Any]:
    repo = request.app.state.repo
    row = await repo.get_doc_metadata(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"KB entry not found: {doc_id}")
    return dict(row)


@router.put("/{doc_id}", dependencies=[Depends(require_scope("write"))])
async def update_kb_entry(
    request: Request,
    doc_id: int,
    body: UpdateNoteRequest,
) -> dict[str, Any]:
    repo = request.app.state.repo
    existing = await repo.get_doc_metadata(doc_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"KB entry not found: {doc_id}")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return dict(existing)
    result: dict[str, Any] | None = await repo.update_doc_metadata(doc_id, **updates)
    if result is None:
        raise HTTPException(status_code=404, detail=f"KB entry not found: {doc_id}")
    # The vector was left standing for the OLD text. Verified on 3.6.17: after
    # rewriting a document, a query on its new wording ranked it LAST and a
    # query on the text it no longer contains ranked it FIRST - the document
    # went on answering for words the operator had deleted (#648 tranche 6).
    if "content" in updates or "title" in updates:
        svc = _get_service(request)
        reembedded = await svc.reembed_doc(doc_id)
        result = {**result, "reembedded": reembedded}
    return result


@router.delete("/{doc_id}", dependencies=[Depends(require_scope("admin"))])
async def delete_kb_entry(request: Request, doc_id: int) -> Response:
    repo = request.app.state.repo
    row = await repo.get_doc_metadata(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"KB entry not found: {doc_id}")
    source = str(row.get("source") or "")
    if source.startswith("artifact:"):
        # A bare row delete does not stick: the artifact stays `applied` and the
        # next reindex puts the document straight back. Verified on 3.6.17 - a
        # policy deleted with HTTP 204 was in `GET /kb` again minutes later.
        # `delete_kb_doc` over MCP already revoked the artifact; the route it
        # claims parity with did not.
        lifecycle = getattr(request.app.state, "artifact_lifecycle", None)
        if lifecycle is None:
            raise HTTPException(
                status_code=503,
                detail="Artifact lifecycle unavailable, so this note cannot be deleted for good",
            )
        await lifecycle.revoke(
            source[len("artifact:") :],
            user=str(getattr(request.state, "user_id", None) or "api"),
            reason="deleted via DELETE /kb",
        )
    if not await repo.delete_doc_metadata(doc_id):
        raise HTTPException(status_code=404, detail=f"KB entry not found: {doc_id}")
    return Response(status_code=204)
