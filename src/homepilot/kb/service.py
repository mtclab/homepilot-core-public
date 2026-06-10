from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import sqlite3
import struct
from typing import Any

import httpx

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.config import get_settings
from homepilot.db.repository import Repository
from homepilot.db.utils import escape_like

logger = logging.getLogger(__name__)


async def _call_embed_service(url: str, model: str, text: str) -> list[float] | None:
    is_ollama = "/api/embeddings" in url
    payload_key = "prompt" if is_ollama else "input"
    payload: dict[str, Any] = {"model": model, payload_key: text[:2000]}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if is_ollama:
                embedding: list[float] | None = data.get("embedding")
            else:
                embedding_data = data.get("data", [])
                embedding = embedding_data[0].get("embedding") if embedding_data else None
            if embedding is None:
                logger.error(
                    "Embedding service %s returned empty/null embedding "
                    "(model=%s, response_keys=%s)",
                    url,
                    model,
                    list(data.keys()),
                )
            return embedding
    except (httpx.ConnectError, ConnectionRefusedError) as e:
        logger.error(
            "Embedding service unreachable at %s (model=%s): %s — "
            "ensure llm-embed service is running or configure HP_EMBEDDING_FALLBACK_URL",
            url,
            model,
            e,
        )
        return None
    except (httpx.HTTPError, ConnectionError, OSError) as e:
        logger.warning("Embedding call to %s failed: %s", url, e)
        return None
    except Exception as e:
        logger.error("Unexpected error calling embedding service %s: %s", url, e)
        return None


async def _get_embedding(text: str) -> list[float] | None:
    settings = get_settings()
    primary_url = settings.embedding_service_url
    primary_model = settings.embedding_model
    fallback_url = settings.embedding_fallback_url
    fallback_model = settings.embedding_fallback_model

    embedding = await _call_embed_service(primary_url, primary_model, text)
    if embedding is not None:
        return embedding

    if fallback_url:
        logger.info("Primary embedding service failed, falling back to %s", fallback_url)
        embedding = await _call_embed_service(fallback_url, fallback_model, text)
        if embedding is not None:
            return embedding

    logger.warning(
        "All embedding services unavailable for query (len=%d) — "
        "falling back to keyword search. Ensure llm-embed service is running "
        "or HP_EMBEDDING_SERVICE_URL/HP_EMBEDDING_FALLBACK_URL are configured.",
        len(text),
    )
    return None


def _embedding_to_bytes(embedding: list[float]) -> bytes:
    return b"".join(struct.pack("<f", v) for v in embedding)


class KBService:
    def __init__(self, repo: Repository, store: ArtifactStore, lifecycle: ArtifactLifecycle):
        self.repo = repo
        self.store = store
        self.lifecycle = lifecycle
        self._reindex_lock = asyncio.Lock()
        self._reindexing = False
        self._reindex_task: asyncio.Task[None] | None = None

    async def ingest(
        self,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        created = 0
        skipped = 0
        errors = 0
        errors_detail: list[str] = []

        for src in sources:
            path = src.get("path")
            kind = src.get("kind", "note")
            target = src.get("target")

            if not path:
                errors += 1
                errors_detail.append(f"missing path in source: {src}")
                continue

            files: list[tuple[str, str]] = []
            try:
                files = await asyncio.to_thread(self._walk_dir, path)
            except FileNotFoundError:
                errors += 1
                errors_detail.append(f"directory not found: {path}")
                continue
            except ValueError as e:
                errors += 1
                errors_detail.append(str(e))
                continue
            except OSError as exc:
                logger.exception("Failed to walk directory: %s: %s", path, exc)
                errors += 1
                errors_detail.append(f"failed to walk directory: {path}")
                continue

            for rel_path, content in files:
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                title = (
                    rel_path.replace("/", " ").replace("\\", " ").replace("-", " ").strip() or kind
                )

                try:
                    doc_id = await self.repo.create_doc_metadata(
                        source=f"ingest:{content_hash}",
                        title=title,
                        content=content,
                        kind=kind,
                        target=target if isinstance(target, str) else None,
                    )
                    if doc_id is None:
                        skipped += 1
                    else:
                        created += 1
                except (sqlite3.OperationalError, sqlite3.IntegrityError, ValueError) as exc:
                    logger.exception("Failed to insert ingested doc: %s: %s", rel_path, exc)
                    errors += 1
                    errors_detail.append(f"insert failed: {rel_path}")

        return {
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "errors_detail": errors_detail,
        }

    @staticmethod
    def _walk_dir(directory_path: str) -> list[tuple[str, str]]:
        root = pathlib.Path(directory_path).resolve()
        if not root.exists():
            raise FileNotFoundError(f"Directory not found: {directory_path}")
        if not root.is_dir():
            raise ValueError(f"Not a directory: {directory_path}")

        results: list[tuple[str, str]] = []
        for f in sorted(root.rglob("*")):
            resolved = f.resolve()
            if not resolved.is_relative_to(root):
                continue
            if f.is_dir():
                continue
            if f.suffix.lower() not in (".md", ".txt", ".markdown"):
                continue
            try:
                content = resolved.read_text(encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                logger.warning("Skipping non-UTF-8 file: %s", f)
                continue
            except OSError as exc:
                logger.debug("Skipping file %s: %s", f, exc)
                continue
            if not content.strip():
                continue
            rel = str(resolved.relative_to(root))
            results.append((rel, content))
        return results

    async def reindex(self, no_embeddings: bool = False, reason: str = "manual") -> dict[str, Any]:
        if self._reindexing:
            return {"status": "already_running", "deleted": 0, "reindexed": 0, "errors": 0}
        async with self._reindex_lock:
            self._reindexing = True
            try:
                logger.info("KB reindex starting: reason=%s", reason)
                rows = await self.repo.db.fetchall(
                    "SELECT id FROM doc_metadata WHERE source LIKE 'artifact:%'"
                )
                artifact_doc_ids = [r["id"] for r in rows]
                for doc_id in artifact_doc_ids:
                    await self.repo.db.execute("DELETE FROM vec_docs WHERE id = ?", (doc_id,))
                await self.repo.db.execute(
                    "DELETE FROM doc_metadata WHERE source LIKE 'artifact:%'"
                )
                await self.repo.db.conn.commit()

                deleted = len(artifact_doc_ids)
                reindexed = 0
                errors = 0

                from homepilot.executor import kb_note as kb_note_executor

                for artifact_meta in self.store.list(kind="kb-note", status="applied"):
                    artifact_id = artifact_meta.get("id", "")
                    try:
                        fm, body = self.store.read(artifact_id)
                        result = await kb_note_executor.execute(
                            fm, body, self.repo, no_embeddings=no_embeddings
                        )
                        if result.get("success"):
                            reindexed += 1
                        else:
                            errors += 1
                    except (OSError, ValueError, sqlite3.OperationalError) as exc:
                        logger.warning("Reindex failed for %s: %s", artifact_id, exc)
                        errors += 1

                return {
                    "status": "completed",
                    "deleted": deleted,
                    "reindexed": reindexed,
                    "errors": errors,
                }
            finally:
                self._reindexing = False

    async def search(
        self,
        query: str,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        embedding = await _get_embedding(query)

        if embedding:
            vec_bytes = _embedding_to_bytes(embedding)
            try:
                base_sql = (
                    "SELECT dm.id, dm.source, dm.kind, dm.target, dm.title, dm.content, "
                    "v.distance FROM doc_metadata dm "
                    "JOIN vec_docs v ON dm.id = v.id "
                    "WHERE v.embedding MATCH ? "
                )
                params: list[Any] = [vec_bytes]
                if kind:
                    base_sql += "AND dm.kind = ? "
                    params.append(kind)
                base_sql += "ORDER BY v.distance LIMIT ?"
                params.append(limit)
                rows = await self.repo.db.fetchall(base_sql, params)
                results = []
                for row in rows:
                    score = 1.0 - row.get("distance", 1.0)
                    results.append(
                        {
                            "id": row.get("id"),
                            "source": row.get("source"),
                            "kind": row.get("kind"),
                            "target": row.get("target"),
                            "title": row.get("title"),
                            "content": row.get("content"),
                            "score": score,
                        }
                    )
                results.sort(key=lambda x: x.get("score", 0), reverse=True)
                return results
            except (sqlite3.OperationalError, ValueError) as e:
                logger.warning("Vector search failed, falling back to keyword: %s", e)

        return await self._keyword_search(query, kind, limit)

    async def _keyword_search(
        self, query: str, kind: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        clauses = [
            "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\')"
        ]
        params: list[Any] = [
            f"%{escape_like(query)}%",
            f"%{escape_like(query)}%",
            f"%{escape_like(query)}%",
        ]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        params.append(limit)
        rows = await self.repo.db.fetchall(
            f"SELECT * FROM doc_metadata WHERE {where} ORDER BY embedded_at DESC LIMIT ?",
            params,
        )
        results = []
        for row in rows:
            results.append(
                {
                    "id": row.get("id"),
                    "source": row.get("source"),
                    "kind": row.get("kind"),
                    "target": row.get("target"),
                    "title": row.get("title"),
                    "content": row.get("content"),
                    "score": 0.0,
                }
            )
        return results

    async def embedding_status(self) -> dict[str, Any]:
        settings = get_settings()
        primary_url = settings.embedding_service_url
        fallback_url = settings.embedding_fallback_url
        test_vec = await _call_embed_service(primary_url, settings.embedding_model, "test")
        primary_ok = test_vec is not None
        fallback_ok = False
        if fallback_url and not primary_ok:
            fb_vec = await _call_embed_service(fallback_url, settings.embedding_model, "test")
            fallback_ok = fb_vec is not None
        rows = await self.repo.db.fetchone("SELECT COUNT(*) AS c FROM vec_docs")
        indexed = rows["c"] if rows else 0
        total = 0
        t_rows = await self.repo.db.fetchone("SELECT COUNT(*) AS c FROM doc_metadata")
        if t_rows:
            total = t_rows["c"]
        return {
            "primary_url": primary_url,
            "primary_ok": primary_ok,
            "fallback_url": fallback_url or "",
            "fallback_ok": fallback_ok,
            "indexed_with_embeddings": indexed,
            "total_docs": total,
            "search_mode": (
                "vector" if primary_ok else ("fallback_vector" if fallback_ok else "keyword")
            ),
        }

    async def reindex_if_needed(self, reason: str = "lifecycle") -> None:
        applied_notes = self.store.list(kind="kb-note", status="applied")
        row = await self.repo.db.fetchone(
            "SELECT COUNT(*) AS c FROM doc_metadata WHERE source LIKE 'artifact:%'"
        )
        index_count = row["c"] if row else 0
        if len(applied_notes) != index_count:
            logger.info(
                "KB reindex triggered: reason=%s, %d applied notes vs %d indexed",
                reason,
                len(applied_notes),
                index_count,
            )
            if self._reindex_task and not self._reindex_task.done():
                self._reindex_task.cancel()
            self._reindex_task = asyncio.create_task(self._run_reindex(reason))

    async def _run_reindex(self, reason: str) -> None:
        try:
            result = await self.reindex(no_embeddings=True, reason=reason)
            logger.info("KB reindex complete: reason=%s, result=%s", reason, result)
        except Exception:  # background task must not crash, logs full exception
            logger.exception("KB reindex failed: reason=%s", reason)

    async def record_fact(
        self,
        target: str | dict[str, str] | None,
        kind: str,
        content: str,
        supersedes: list[str] | None = None,
    ) -> str:
        from datetime import datetime

        from homepilot.artifacts.models import utcnow_iso

        date_str = datetime.now().strftime("%Y-%m-%d")
        slug = kind.lower()
        target_str = (
            target
            if isinstance(target, str)
            else (
                target.get("service") or target.get("host") or target.get("kind", "")
                if isinstance(target, dict)
                else ""
            )
        )
        target_slug = target_str[:20].replace(" ", "-") if target_str else "global"
        artifact_id = f"{date_str}-kb-{slug}-{target_slug}"
        from homepilot.artifacts.models import validate_artifact_id

        if not validate_artifact_id(artifact_id):
            artifact_id = f"{date_str}-kb-note"

        spec: dict[str, Any] = {
            "id": artifact_id,
            "kind": "kb-note",
            "intent": content[:200],
            "note_kind": kind,
            "target": target
            if isinstance(target, dict)
            else ({"kind": "service", "service": target} if target_str else None),
            "produced_by": {
                "session": "kb-service",
                "agent": "homepilot",
                "user": "system",
                "at": utcnow_iso(),
            },
            "body": content,
        }
        if supersedes:
            spec["supersedes"] = supersedes

        try:
            result_id = await self.lifecycle.propose(spec)
            return result_id
        except (FileExistsError, ValueError, OSError, LifecycleError):
            fm, _body = self.store.read(artifact_id)
            from homepilot.artifacts.models import compute_body_hash

            if fm.get("hash") == compute_body_hash(content):
                return artifact_id
            artifact_id = f"{date_str}-kb-{hashlib.sha256(content.encode()).hexdigest()[:6]}"
            spec["id"] = artifact_id
            return await self.lifecycle.propose(spec)
