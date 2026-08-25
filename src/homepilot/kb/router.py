from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ..auth.deps import require_scope
from .service import KBService

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
    modes = {r.get("search_mode", "unknown") for r in results}
    primary = "vector" if "vector" in modes else (modes.pop() if modes else "empty")
    return {"results": results, "total": len(results), "search_mode": primary}


@router.get("/embedding-status", dependencies=[Depends(require_scope("admin"))])
async def embedding_status(request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    return await svc.embedding_status()


@router.post("", dependencies=[Depends(require_scope("write"))])
async def record_fact(request: Request, body: CreateNoteRequest) -> dict[str, Any]:
    svc = _get_service(request)
    artifact_id = await svc.record_fact(
        target=body.target,
        kind=body.kind,
        content=body.content,
        supersedes=body.supersedes,
    )
    return {"id": artifact_id}


@router.post("/notes", dependencies=[Depends(require_scope("write"))])
async def create_note(request: Request, body: CreateNoteRequest) -> dict[str, Any]:
    svc = _get_service(request)
    if body.kind not in ("note", "policy", "fact"):
        raise HTTPException(status_code=400, detail="kind must be note, policy, or fact")
    artifact_id = await svc.record_fact(
        target=body.target,
        kind=body.kind,
        content=body.content,
        supersedes=body.supersedes,
    )
    return {"id": artifact_id}


@router.post("/reindex", dependencies=[Depends(require_scope("admin"))])
async def reindex_kb(request: Request, no_embeddings: bool = False) -> dict[str, Any]:
    logger.info("KB reindex triggered: reason=manual, no_embeddings=%s", no_embeddings)
    svc = _get_service(request)
    result = await svc.reindex(no_embeddings=no_embeddings, reason="manual")
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
    return result


@router.delete("/{doc_id}", dependencies=[Depends(require_scope("admin"))])
async def delete_kb_entry(request: Request, doc_id: int) -> Response:
    repo = request.app.state.repo
    deleted = await repo.delete_doc_metadata(doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"KB entry not found: {doc_id}")
    return Response(status_code=204)
