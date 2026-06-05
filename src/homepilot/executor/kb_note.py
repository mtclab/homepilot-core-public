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

    embedding = None
    if not no_embeddings:
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
                "ensure llm-embed service is running (see docker-compose gpu profile)",
                artifact_id,
                exc,
            )

    try:
        doc_id = await db_repo.create_doc_metadata(
            source=f"artifact:{artifact_id}",
            kind=note_kind,
            target=target or None,
            title=intent,
            content=body,
        )
    except (sqlite3.OperationalError, sqlite3.DatabaseError, json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to insert kb-note into doc_metadata: %s", e)
        return {
            "success": True,
            "execution_log": f"kb-note indexed (keyword-only, doc insert failed: {e})",
        }

    if embedding and doc_id is not None:
        try:
            await _store_embedding(db_repo, doc_id, embedding)
        except (sqlite3.OperationalError, sqlite3.DatabaseError, ValueError) as e:
            logger.warning("Failed to store embedding for kb-note: %s", e)

    log = f"kb-note indexed as {note_kind}, target={target or 'none'}"
    if embedding:
        log += ", embedding stored"
    else:
        log += ", keyword-only (no embedding)"

    return {"success": True, "execution_log": log}


async def _compute_embedding(text: str) -> list[float]:
    import httpx

    from homepilot.config import get_settings

    settings = get_settings()
    primary_url = settings.embedding_service_url
    primary_model = settings.embedding_model
    fallback_url = settings.embedding_fallback_url
    fallback_model = settings.embedding_fallback_model

    is_ollama_primary = "/api/embeddings" in primary_url
    payload_key_primary = "prompt" if is_ollama_primary else "input"
    payload = {"model": primary_model, payload_key_primary: text[:2000]}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(primary_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if is_ollama_primary:
                embedding: list[float] | None = data.get("embedding")
            else:
                embedding_data = data.get("data", [])
                embedding = embedding_data[0].get("embedding") if embedding_data else None
            if embedding:
                return embedding
            logger.error(
                "Primary embedding service returned null embedding (url=%s, model=%s)",
                primary_url,
                primary_model,
            )
    except (httpx.ConnectError, ConnectionRefusedError) as exc:
        logger.error(
            "Primary embedding service unreachable (url=%s): %s — "
            "ensure llm-embed service is running (docker compose --profile gpu up)",
            primary_url,
            exc,
        )
    except Exception as exc:
        logger.warning("Primary embedding service failed, trying fallback: %s", exc)

    if fallback_url:
        is_ollama_fallback = "/api/embeddings" in fallback_url
        payload_key_fallback = "prompt" if is_ollama_fallback else "input"
        fallback_payload = {"model": fallback_model, payload_key_fallback: text[:2000]}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(fallback_url, json=fallback_payload)
                resp.raise_for_status()
                data = resp.json()
                if is_ollama_fallback:
                    embedding = data.get("embedding")
                else:
                    embedding_data = data.get("data", [])
                    embedding = embedding_data[0].get("embedding") if embedding_data else None
                if embedding:
                    return embedding
                logger.error(
                    "Fallback embedding service returned null embedding (url=%s, model=%s)",
                    fallback_url,
                    fallback_model,
                )
        except (httpx.ConnectError, ConnectionRefusedError) as exc:
            logger.error(
                "Fallback embedding service unreachable (url=%s): %s — "
                "KB search will use keyword-only mode",
                fallback_url,
                exc,
            )
        except Exception as exc:
            logger.error("Fallback embedding service also failed: %s", exc)

    raise ValueError("No embedding available from any service")


async def _store_embedding(repo: Any, doc_id: int, embedding: list[float]) -> None:

    vec_bytes = b""
    for val in embedding:
        vec_bytes += _float32_to_bytes(val)

    conn = repo.db.conn
    await conn.execute(
        "INSERT INTO vec_docs (id, embedding) VALUES (?, ?)",
        (doc_id, vec_bytes),
    )
    await conn.commit()


def _float32_to_bytes(val: float) -> bytes:
    import struct

    return struct.pack("<f", val)
