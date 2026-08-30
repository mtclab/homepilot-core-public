from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    repo: object,
    *,
    no_embeddings: bool = False,
) -> dict[str, Any]:
    from homepilot.db.repository import Repository

    db_repo: Repository = repo  # type: ignore[assignment]

    if not body or not body.strip():
        return {
            "success": False,
            "execution_log": "kb-note body is empty",
            "failure_reason": "empty body",
        }

    note_kind = frontmatter.get("note_kind", "note")
    artifact_id = frontmatter.get("id", "")
    intent = frontmatter.get("intent", "")
    target = ""
    target_data = frontmatter.get("target")
    if target_data and isinstance(target_data, dict):
        target = (
            target_data.get("host") or target_data.get("service") or target_data.get("node", "")
        )

    tags_raw = frontmatter.get("tags") or []
    keywords = intent
    if tags_raw:
        keywords += " " + " ".join(tags_raw)

    try:
        # Upsert BY SOURCE, never delete-then-insert: the row id is the identity
        # every KB surface hands out, and a reused rowid takes over a deleted
        # document's embedding (#648 tranche 6).
        doc_id, text_changed = await db_repo.upsert_doc_metadata(
            source=f"artifact:{artifact_id}",
            kind=note_kind,
            target=target or None,
            title=intent,
            content=body,
        )
    except (sqlite3.OperationalError, sqlite3.DatabaseError, json.JSONDecodeError, ValueError) as e:
        # NOT success, and not the word "indexed". The note is not in the
        # knowledge base, no search will ever find it, and the difference
        # between an operator who knows to retry and one who believes their
        # decision is written down is this return value.
        logger.error("Failed to index kb-note %s into doc_metadata: %s", artifact_id, e)
        return {
            "success": False,
            "execution_log": (
                f"kb-note NOT indexed: the write to the knowledge base failed ({e}). "
                "It is not searchable; re-run once the database accepts writes."
            ),
            "failure_reason": f"doc_metadata write failed: {e}",
        }

    # Embed AFTER the row exists, and only when the note actually needs it: text
    # that changed, or a document with no vector yet. A reindex over unchanged
    # notes now costs no embedding calls at all, which is what makes it safe to
    # run the background rebuild WITH embeddings instead of wiping them.
    embedding = None
    embedding_stored = False
    embedding_error = ""
    already_embedded = False
    if not no_embeddings and doc_id:
        already_embedded = await db_repo.has_embedding(doc_id)
        if text_changed or not already_embedded:
            try:
                embedding = await _compute_embedding(body)
            except ValueError as exc:
                logger.warning(
                    "Embedding computation failed for %s: %s — doc will be indexed keyword-only",
                    artifact_id,
                    exc,
                )
            except Exception as exc:
                logger.error(
                    "Unexpected error computing embedding for %s: %s — "
                    "check the service named by HP_EMBEDDING_SERVICE_URL",
                    artifact_id,
                    exc,
                )

    if embedding and doc_id:
        try:
            await _store_embedding(db_repo, doc_id, embedding)
            embedding_stored = True
        except (sqlite3.OperationalError, sqlite3.DatabaseError, ValueError) as e:
            # Not fatal - the keyword pass still finds the note - but never
            # reported as "embedding stored". The old line said that on the
            # strength of the embedding being non-empty, AFTER this call had
            # been allowed to fail.
            embedding_error = str(e)
            logger.warning("Failed to store embedding for kb-note %s: %s", artifact_id, e)

    log = f"kb-note indexed as {note_kind}, target={target or 'none'}"
    if embedding_stored:
        log += ", embedding stored"
    elif embedding:
        log += f", keyword-only (embedding computed but NOT stored: {embedding_error})"
    elif already_embedded and not text_changed:
        log += ", existing embedding kept (text unchanged)"
    elif no_embeddings:
        log += ", keyword-only (embeddings skipped for this run)"
    else:
        log += ", keyword-only (no embedding)"
    embedding_stored = embedding_stored or (already_embedded and not text_changed)

    return {
        "success": True,
        "execution_log": log,
        "doc_id": doc_id,
        "embedding_stored": embedding_stored,
        "text_changed": text_changed,
    }


async def _compute_embedding(text: str) -> list[float]:
    """The embedding for a note body, from the one embedding client there is.

    This used to be a SECOND copy of the primary/fallback protocol, and the two
    copies had already drifted: `kb/service.py` redacts the endpoint before
    logging it because an embedding URL can carry credentials, while this copy
    logged `url=%s` raw on every failure. One protocol, one place (#648
    tranche 6).

    Raises ValueError when no embedding could be had, which every caller here
    already treats as "index this note keyword-only".
    """
    from homepilot.kb.service import _get_embedding

    embedding = await _get_embedding(text)
    if not embedding:
        raise ValueError("No embedding available from any service")
    return embedding


async def _store_embedding(repo: Any, doc_id: int, embedding: list[float]) -> None:
    """Write THE embedding for this document, replacing whatever was there.

    A bare INSERT raised `UNIQUE constraint failed on vec_docs primary key`
    whenever the row already existed - on a re-embed after an edit, and on any
    document that landed on a rowid whose vector had been orphaned by a delete.
    The failure was swallowed one frame up and the document kept the OTHER
    document's vector. Replacing is the only correct semantics here: one
    document has one current meaning.
    """
    vec_bytes = b""
    for val in embedding:
        vec_bytes += _float32_to_bytes(val)

    conn = repo.db.conn
    await conn.execute("DELETE FROM vec_docs WHERE id = ?", (doc_id,))
    await conn.execute(
        "INSERT INTO vec_docs (id, embedding) VALUES (?, ?)",
        (doc_id, vec_bytes),
    )
    await conn.commit()


def _float32_to_bytes(val: float) -> bytes:
    import struct

    return struct.pack("<f", val)
