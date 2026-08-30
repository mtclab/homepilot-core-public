from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import re
import sqlite3
import struct
from typing import Any

import httpx

from homepilot.app_settings import effective
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.common import redact_endpoint
from homepilot.config import get_settings
from homepilot.db.repository import Repository
from homepilot.db.utils import escape_like

logger = logging.getLogger(__name__)


def _extract_embedding(data: Any) -> list[float] | None:
    """The vector out of either answer shape, whichever the server sent.

    Ollama answers `{"embedding": [...]}`; an OpenAI-compatible server answers
    `{"data": [{"embedding": [...]}]}`. Reading BOTH is free and removes a whole
    class of wrong answer: the previous code decided which shape it was talking
    to from `"/api/embeddings" in url`, so a correct Ollama endpoint reached by
    any other path - or an OpenAI-compatible one that happens to live under
    `/api/embeddings` - was parsed as the other kind and reported as "returned
    empty/null embedding" (#648 tranche 6).
    """
    if not isinstance(data, dict):
        return None
    direct = data.get("embedding")
    if isinstance(direct, list) and direct:
        return [float(v) for v in direct]
    rows = data.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        nested = rows[0].get("embedding")
        if isinstance(nested, list) and nested:
            return [float(v) for v in nested]
    return None


async def _call_embed_service(
    url: str, model: str, text: str, timeout: float = 30.0
) -> list[float] | None:
    if not url:
        return None
    # An embedding endpoint can carry credentials (userinfo, ?key=), and these
    # log lines are the ones an operator pastes into an issue. Log the redacted
    # form only - never the configured URL.
    safe_url = redact_endpoint(url)
    # The URL only ORDERS the attempts; it does not decide the answer. An
    # endpoint that refuses the first request body gets asked again in the other
    # dialect before we call it broken.
    first_key = "prompt" if "/api/embeddings" in url else "input"
    second_key = "input" if first_key == "prompt" else "prompt"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            embedding: list[float] | None = None
            last_data: Any = None
            for payload_key in (first_key, second_key):
                payload: dict[str, Any] = {"model": model, payload_key: text[:2000]}
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400 and payload_key == first_key:
                    # A 4xx on the first dialect is exactly what a server that
                    # speaks the other one returns. Try it before giving up.
                    logger.debug(
                        "Embedding service %s refused a '%s' payload (HTTP %s); "
                        "retrying in the other dialect",
                        safe_url,
                        payload_key,
                        resp.status_code,
                    )
                    continue
                resp.raise_for_status()
                last_data = resp.json()
                embedding = _extract_embedding(last_data)
                if embedding is not None:
                    if payload_key != first_key:
                        logger.info(
                            "Embedding service %s speaks the '%s' request shape, "
                            "not the one its URL suggested",
                            safe_url,
                            payload_key,
                        )
                    break
            if embedding is None:
                logger.error(
                    "Embedding service %s returned empty/null embedding "
                    "(model=%s, response_keys=%s)",
                    safe_url,
                    model,
                    list(last_data.keys()) if isinstance(last_data, dict) else last_data,
                )
            return embedding
    except (httpx.ConnectError, ConnectionRefusedError) as e:
        logger.error(
            "Embedding service unreachable at %s (model=%s): %s — "
            "KB search stays keyword-only until it answers",
            safe_url,
            model,
            e,
        )
        return None
    except (httpx.HTTPError, ConnectionError, OSError) as e:
        logger.warning("Embedding call to %s failed: %s", safe_url, e)
        return None
    except Exception as e:
        logger.error("Unexpected error calling embedding service %s: %s", safe_url, e)
        return None


async def _get_embedding(text: str) -> list[float] | None:
    settings = get_settings()
    # Resolved per CALL, not per process: the embedding service is an operator
    # setting that can be pointed somewhere else while HomePilot runs (#553 C2).
    primary_url = await effective("embedding_service_url", settings)
    primary_model = await effective("embedding_model", settings)
    fallback_url = settings.embedding_fallback_url
    fallback_model = settings.embedding_fallback_model

    if not primary_url and not fallback_url:
        # Off by choice, not broken: no service is configured, so say that rather
        # than reporting an outage the operator cannot act on. Search still works
        # in keyword mode - see search().
        logger.debug("No embedding service configured — KB search runs keyword-only")
        return None

    if primary_url:
        embedding = await _call_embed_service(primary_url, primary_model, text)
        if embedding is not None:
            return embedding

    if fallback_url:
        if primary_url:
            logger.info(
                "Primary embedding service failed, falling back to %s",
                redact_endpoint(fallback_url),
            )
        embedding = await _call_embed_service(fallback_url, fallback_model, text)
        if embedding is not None:
            return embedding

    logger.warning(
        "All configured embedding services unavailable for query (len=%d) — "
        "falling back to keyword search. Check HP_EMBEDDING_SERVICE_URL / "
        "HP_EMBEDDING_FALLBACK_URL and that the service is running.",
        len(text),
    )
    return None


def _embedding_to_bytes(embedding: list[float]) -> bytes:
    return b"".join(struct.pack("<f", v) for v in embedding)


# `score = 1 - distance`, so 0.0 is the midpoint: for unit-length vectors it is
# a cosine similarity of 0.5. Below it the nearest-neighbour query is returning
# a document because it had to return SOMETHING, not because it matched.
_VECTOR_SCORE_FLOOR = 0.0

# A word has to be worth matching on its own. Without this, "the" and "on" pull
# in every document in the KB and the ranking below is noise.
_SEARCH_MODE_DETAIL = {
    "keyword": (
        "Searches match words, not meaning: either no embedding service is "
        "reachable or no document has an embedding yet."
    ),
    "vector_partial": (
        "Some documents are embedded and some are not. A vector search can only "
        "see the embedded ones; the rest are reachable by keyword. Run "
        "`hp kb reindex` to embed the remainder."
    ),
    "vector": "Every document is embedded and the primary embedding service answers.",
    "fallback_vector": (
        "Every document is embedded and the FALLBACK embedding service is "
        "answering; the primary one is not."
    ),
}

_STOPWORDS = frozenset(
    [
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
        "have", "how", "in", "into", "is", "it", "its", "of", "on", "or",
        "that", "the", "their", "there", "they", "this", "to", "was", "were",
        "what", "when", "where", "which", "who", "why", "with",
    ]
)  # fmt: skip


def summarise_search_mode(results: list[dict[str, Any]]) -> dict[str, Any]:
    """How the hits in front of you were actually found.

    Shared by GET /kb/search and the `search_kb` MCP tool so the two cannot
    describe the same call differently. "vector" is claimed only when a vector
    hit is in the list.
    """
    modes = {str(r.get("search_mode") or "keyword") for r in results}
    if not results:
        summary = "no_matches"
    elif modes == {"vector"}:
        summary = "vector"
    elif "vector" in modes:
        summary = "vector+keyword"
    else:
        summary = "keyword"
    return {
        "search_mode": summary,
        "vector_hits": sum(1 for r in results if r.get("search_mode") == "vector"),
        "keyword_hits": sum(1 for r in results if r.get("search_mode") == "keyword"),
    }


def _query_terms(query: str) -> list[str]:
    """The words a keyword search should look for, in order, de-duplicated."""
    words = re.findall(r"[\w./:-]+", query.lower(), flags=re.UNICODE)
    terms: list[str] = []
    for word in words:
        if len(word) < 2 or word in _STOPWORDS:
            continue
        if word not in terms:
            terms.append(word)
    return terms


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
                        # Embed on ingest. Without this an ingested doc is
                        # keyword-only forever, which is the same defect one step
                        # earlier: the KB holds it and semantic search cannot see
                        # it (#433). Best-effort - a doc that failed to embed is
                        # still found by the keyword pass, which the merge above
                        # guarantees runs.
                        await self._embed_doc(doc_id, f"{title}\n{content}")
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

    async def _embed_doc(self, doc_id: int, text: str) -> bool:
        """Store an embedding for a doc. Returns False if none could be made."""
        from homepilot.executor.kb_note import _store_embedding

        try:
            embedding = await _get_embedding(text)
        except Exception as exc:
            logger.debug("No embedding for doc %s: %s", doc_id, exc)
            return False
        if not embedding:
            return False
        try:
            await _store_embedding(self.repo, doc_id, embedding)
        except (sqlite3.OperationalError, sqlite3.IntegrityError, ValueError) as exc:
            logger.warning("Could not store embedding for doc %s: %s", doc_id, exc)
            return False
        return True

    async def reembed_doc(self, doc_id: int) -> bool:
        """Recompute the embedding for a document whose text just changed.

        Editing a KB document rewrote `doc_metadata` and left `vec_docs` holding
        the vector of the text that is no longer there, so the document kept
        answering for its old wording and stopped answering for its new one.
        Returns False when no embedding could be made, which leaves the document
        keyword-only rather than wrong - so the stale vector is dropped either
        way.
        """
        row = await self.repo.get_doc_metadata(doc_id)
        if row is None:
            return False
        # Drop first: a document indexed under text it no longer contains is a
        # worse answer than one with no vector at all.
        try:
            await self.repo.db.execute("DELETE FROM vec_docs WHERE id = ?", (doc_id,))
            await self.repo.db.conn.commit()
        except sqlite3.OperationalError:
            logger.debug("vec_docs unavailable while re-embedding doc %s", doc_id)
        text = f"{row.get('title') or ''}\n{row.get('content') or ''}"
        return await self._embed_doc(doc_id, text)

    async def embed_missing(self) -> dict[str, int]:
        """Embed every doc that has none (#433).

        `reindex` only ever re-walked `source LIKE 'artifact:%'`, so a KB full of
        ingested documentation and observed-state notes stayed unembedded no
        matter how many times an operator reindexed.
        """
        rows = await self.repo.db.fetchall(
            "SELECT dm.id, dm.title, dm.content FROM doc_metadata dm "
            "LEFT JOIN vec_docs v ON dm.id = v.id WHERE v.id IS NULL"
        )
        embedded = 0
        failed = 0
        for row in rows:
            text = f"{row.get('title') or ''}\n{row.get('content') or ''}"
            if await self._embed_doc(int(row["id"]), text):
                embedded += 1
            else:
                failed += 1
        return {"embedded": embedded, "failed": failed}

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

    async def reindex(
        self,
        no_embeddings: bool = False,
        reason: str = "manual",
        force_embeddings: bool = False,
    ) -> dict[str, Any]:
        """Bring the index in line with the applied kb-note artifacts.

        NOT "delete everything, then rebuild". That shape lost documents and
        said `status: "completed", errors: 0` while doing it - reproduced on
        3.6.17: one unreadable artifact file turned into
        `{"deleted": 4, "reindexed": 3, "errors": 0}` with the note gone from
        the knowledge base and nothing anywhere saying so. It also reassigned
        every row id (so the id `list_kb` handed out meant a different document
        afterwards) and restamped every `embedded_at`.

        Now: upsert each applied note by source, then remove only the rows whose
        artifact is no longer applied. A failure leaves the existing document in
        place rather than deleting it first and failing to put it back.
        """
        if self._reindexing:
            return {
                "status": "already_running",
                "removed": 0,
                "reindexed": 0,
                "errors": 0,
                "errors_detail": [],
            }
        async with self._reindex_lock:
            self._reindexing = True
            try:
                logger.info("KB reindex starting: reason=%s", reason)
                from homepilot.executor import kb_note as kb_note_executor

                if force_embeddings and not no_embeddings:
                    # Re-embedding is keyed on the TEXT changing, so a document
                    # whose text is the same keeps the vector it has - which is
                    # right on every ordinary rebuild and wrong after the
                    # embedding MODEL changes, when every stored vector is from
                    # a different space. Dropping them makes each document
                    # "missing an embedding", which the pass below fixes.
                    try:
                        await self.repo.db.execute("DELETE FROM vec_docs")
                        await self.repo.db.conn.commit()
                        logger.info("KB reindex: dropped all embeddings on request")
                    except sqlite3.OperationalError as exc:
                        logger.warning("Could not drop embeddings: %s", exc)

                seen_sources: set[str] = set()
                reindexed = 0
                errors = 0
                errors_detail: list[str] = []

                for artifact_meta in self.store.list(kind="kb-note", status="applied"):
                    artifact_id = artifact_meta.get("id", "")
                    source = f"artifact:{artifact_id}"
                    try:
                        fm, body = self.store.read(artifact_id)
                        result = await kb_note_executor.execute(
                            fm, body, self.repo, no_embeddings=no_embeddings
                        )
                        if result.get("success"):
                            reindexed += 1
                            # Only a note that IS indexed protects itself from
                            # the prune below. A failed one keeps whatever row it
                            # already had rather than being swept away.
                            seen_sources.add(source)
                        else:
                            errors += 1
                            errors_detail.append(
                                f"{artifact_id}: {result.get('failure_reason') or 'execute failed'}"
                            )
                            seen_sources.add(source)
                    except (OSError, ValueError, sqlite3.OperationalError) as exc:
                        # An artifact that cannot be READ is an error, not a
                        # licence to drop its document: the note in the KB is
                        # the last copy of that knowledge we still have.
                        logger.warning("Reindex failed for %s: %s", artifact_id, exc)
                        errors += 1
                        errors_detail.append(f"{artifact_id}: {exc}")
                        seen_sources.add(source)

                removed, unverified = await self._prune_orphaned_artifact_docs(seen_sources)
                if unverified:
                    logger.warning(
                        "KB reindex: %d document(s) kept because their artifact could not "
                        "be read or has no artifacts row: %s",
                        len(unverified),
                        ", ".join(unverified[:10]),
                    )

                # Everything else that has no embedding, too: reindex only ever
                # re-walked artifact notes, so ingested docs and observed-state
                # notes stayed keyword-only however often it was run (#433).
                swept = {"embedded": 0, "failed": 0}
                if not no_embeddings:
                    swept = await self.embed_missing()

                return {
                    # "completed" is a verdict, so it may only be said when
                    # nothing failed. A half-done rebuild that calls itself
                    # completed is the reason an operator does not go looking.
                    "status": (
                        "completed" if errors == 0 and not unverified else "completed_with_errors"
                    ),
                    "removed": removed,
                    "reindexed": reindexed,
                    "errors": errors,
                    "errors_detail": errors_detail,
                    # Documents kept because their artifact could not be read.
                    # They are still searchable; what is NOT established is
                    # whether they should still be there.
                    "unverified": unverified,
                    "embedded_missing": swept["embedded"],
                    "embedding_failures": swept["failed"],
                }
            finally:
                self._reindexing = False

    async def _prune_orphaned_artifact_docs(self, keep_sources: set[str]) -> tuple[int, list[str]]:
        """Drop documents whose artifact is RETIRED - never merely unseen.

        `store.list` walks the filesystem and silently skips any file it cannot
        parse or open, so an unreadable artifact is absent from the listing in
        exactly the same way a deleted one is. Pruning on that alone deletes a
        note because we COULD NOT LOOK at it, which is the confident-verdict
        shape this whole review is chasing (#642: "I looked and it matches" and
        "I could not look" are different answers). Reproduced on the built
        image: `chmod 000` on one artifact file turned a reindex into
        `{"removed": 1, "errors": 0, "status": "completed"}` with the note gone
        from the knowledge base.

        So each candidate is asked about DIRECTLY rather than inferred from the
        listing, with the working tree as truth (ARTIFACT_SPEC D7):

          * no file      -> the artifact is gone; drop the document;
          * file, reads  -> drop only if its status is no longer `applied`;
          * file, raises -> we could not look. Keep the document and say so.

        Returns `(removed, unverified_artifact_ids)`.
        """
        rows = await self.repo.db.fetchall(
            "SELECT id, source FROM doc_metadata WHERE source LIKE 'artifact:%'"
        )
        removed = 0
        unverified: list[str] = []
        for row in rows:
            source = str(row["source"])
            if source in keep_sources:
                continue
            artifact_id = source[len("artifact:") :]
            if self.store.exists(artifact_id):
                try:
                    fm, _body = self.store.read(artifact_id)
                except (OSError, ValueError, sqlite3.OperationalError) as exc:
                    logger.warning(
                        "Keeping KB document for %s: its artifact could not be read (%s)",
                        artifact_id,
                        exc,
                    )
                    unverified.append(artifact_id)
                    continue
                if str(fm.get("status")) == "applied":
                    # Applied after all - the listing simply did not offer it.
                    unverified.append(artifact_id)
                    continue
            # Through the repository, so the embedding goes with it. Deleting
            # the row alone is what stranded vectors on reusable row ids.
            if await self.repo.delete_doc_metadata(int(row["id"])):
                removed += 1
        return removed, unverified

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
                # sqlite-vec REQUIRES the k constraint on the vec0 table's own
                # query - a `LIMIT` on an outer join does not count. The previous
                # shape (JOIN doc_metadata ... WHERE v.embedding MATCH ? ORDER BY
                # v.distance LIMIT ?) raised
                #   "A LIMIT or 'k = ?' constraint is required on vec0 knn queries"
                # on EVERY search, which the handler below turned into a keyword
                # fallback and a debug-level warning. Semantic search has
                # therefore never actually run (#433).
                #
                # k over-fetches because the `kind` filter is applied AFTER the
                # nearest-neighbour cut: filtering inside the vec query is not
                # possible, so asking for exactly `limit` neighbours and then
                # discarding some would silently return short.
                k = min(max(limit * 5, limit), 200)
                base_sql = (
                    "SELECT dm.id, dm.source, dm.kind, dm.target, dm.title, dm.content, "
                    "v.distance FROM ("
                    "  SELECT id, distance FROM vec_docs WHERE embedding MATCH ? AND k = ?"
                    ") v JOIN doc_metadata dm ON dm.id = v.id "
                )
                params: list[Any] = [vec_bytes, k]
                if kind:
                    base_sql += "WHERE dm.kind = ? "
                    params.append(kind)
                base_sql += "ORDER BY v.distance LIMIT ?"
                params.append(limit)
                rows = await self.repo.db.fetchall(base_sql, params)
                results = []
                for row in rows:
                    score = 1.0 - row.get("distance", 1.0)
                    if score <= _VECTOR_SCORE_FLOOR:
                        # A k-nearest query ALWAYS returns k rows; it has no
                        # notion of "no match". Without a floor every query
                        # returned every document - `xylophone quantum banana`
                        # came back with all four docs in the KB at score
                        # -0.4142, and a policy lookup for a host with no policy
                        # returned every policy there was. `score` is
                        # `1 - distance`, so > 0 is "nearer than the midpoint";
                        # anything at or below that is not an answer, and the
                        # keyword pass below still runs.
                        continue
                    results.append(
                        {
                            "id": row.get("id"),
                            "source": row.get("source"),
                            "kind": row.get("kind"),
                            "target": row.get("target"),
                            "title": row.get("title"),
                            "content": row.get("content"),
                            "score": score,
                            "search_mode": "vector",
                        }
                    )
                results.sort(key=lambda x: x.get("score", 0), reverse=True)
                # A doc with no embedding cannot appear above - the vector query
                # joins vec_docs - and only `kb-note` ARTIFACTS are embedded.
                # Ingested documentation and observed-state notes go straight to
                # doc_metadata, so they were returned ONLY by the fallback that
                # runs when the embedding service is DOWN: the KB hid what you
                # put into it precisely when it was configured correctly (#433).
                #
                # Vector hits keep their order and their scores; keyword hits
                # fill in behind them, which is also the honest ranking - one is
                # a semantic match and the other is a substring.
                return await self._merge_keyword_matches(results, query, kind, limit)
            except (sqlite3.OperationalError, ValueError) as e:
                logger.warning("Vector search failed, falling back to keyword: %s", e)

        return await self._keyword_search(query, kind, limit)

    async def _merge_keyword_matches(
        self,
        results: list[dict[str, Any]],
        query: str,
        kind: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Append keyword matches the vector search could not have found."""
        if len(results) >= limit:
            return results[:limit]
        seen = {r.get("id") for r in results}
        try:
            keyword = await self._keyword_search(query, kind, limit)
        except (sqlite3.OperationalError, ValueError) as exc:
            logger.warning("Keyword pass failed alongside vector search: %s", exc)
            return results
        for row in keyword:
            if row.get("id") in seen:
                continue
            results.append(row)
            if len(results) >= limit:
                break
        return results

    async def _keyword_search(
        self, query: str, kind: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Keyword search over the KB, per TERM rather than per query string.

        This was one `LIKE '%<the whole query>%'`, i.e. a contiguous-substring
        test wearing the word "search". Reproduced on dev 3.6.17 against a note
        reading *"Never restart nginx on dev-ct-web during business hours"*:
        `restart nginx` found it, `nginx business hours` returned nothing at
        all - and the UI then said "No entries match the current search". Since
        keyword mode is what every install runs by default (ARCHITECTURE.md:
        "By default neither URL is set, so KB search is keyword-only"), that was
        the KB's normal behaviour, not its degraded one.

        A document matches when it contains EVERY term somewhere - the ordinary
        meaning of a multi-word query - and ranks by how many of them are in the
        title, so the note about the thing you asked for beats one that merely
        mentions it.
        """
        terms = _query_terms(query)
        if not terms:
            # A query of nothing but stopwords/punctuation: fall back to the
            # literal string rather than matching every document in the KB.
            terms = [query.strip().lower()] if query.strip() else []
        if not terms:
            return []

        clauses: list[str] = []
        params: list[Any] = []
        for term in terms:
            like = f"%{escape_like(term)}%"
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
                "OR target LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like])
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        # Over-fetch, then rank in Python: SQLite has no ranking to offer here
        # and the alternative is returning whichever rows happened to be newest.
        params.append(max(limit * 5, limit))
        rows = await self.repo.db.fetchall(
            f"SELECT * FROM doc_metadata WHERE {where} ORDER BY embedded_at DESC LIMIT ?",  # nosec B608
            params,
        )

        def _rank(row: dict[str, Any]) -> tuple[int, int]:
            title = str(row.get("title") or "").lower()
            target = str(row.get("target") or "").lower()
            in_title = sum(1 for t in terms if t in title or t in target)
            return (in_title, len(terms))

        ranked = sorted(rows, key=lambda r: _rank(r)[0], reverse=True)
        results = []
        for row in ranked[:limit]:
            results.append(
                {
                    "id": row.get("id"),
                    "source": row.get("source"),
                    "kind": row.get("kind"),
                    "target": row.get("target"),
                    "title": row.get("title"),
                    "content": row.get("content"),
                    # Never a similarity: a keyword hit is a word match, and
                    # dressing it as 0.0-on-the-same-scale as a vector score
                    # invites a caller to compare them.
                    "score": 0.0,
                    "search_mode": "keyword",
                    "matched_terms": [
                        t
                        for t in terms
                        if t in str(row.get("title") or "").lower()
                        or t in str(row.get("content") or "").lower()
                        or t in str(row.get("target") or "").lower()
                    ],
                }
            )
        return results

    # A policy with no target, or one targeting any of these, is a rule about
    # the whole estate: `hp policy init` writes `target: {"kind": "global"}`,
    # which the kb-note executor stores as NULL.
    _GLOBAL_TARGETS = ("", "global", "all", "fleet", "*")

    async def policies_for_target(self, host: str) -> list[dict[str, Any]]:
        """Every `kind: "policy"` document that binds `host`, and why.

        Deterministic and explainable: an exact target match, or a global
        policy. No similarity anywhere near it - see `_policies_for` in
        `artifacts/router.py` for what similarity did to this screen.
        """
        needle = host.strip().lower()
        rows = await self.repo.db.fetchall(
            "SELECT id, title, content, target, source FROM doc_metadata "
            "WHERE kind = 'policy' ORDER BY embedded_at DESC"
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            target = str(row.get("target") or "").strip().lower()
            if target == needle and needle:
                applies_via = "target"
            elif target in self._GLOBAL_TARGETS:
                applies_via = "global"
            else:
                continue
            out.append(
                {
                    "id": row.get("id"),
                    "title": row.get("title", ""),
                    "content": row.get("content", ""),
                    "target": row.get("target"),
                    # The screen says "these are the rules for this machine";
                    # this is the evidence for that sentence, per row.
                    "applies_via": applies_via,
                }
            )
        # Host-specific rules first: they are the ones that override.
        out.sort(key=lambda p: 0 if p["applies_via"] == "target" else 1)
        return out

    async def embedding_status(self) -> dict[str, Any]:
        settings = get_settings()
        primary_url = await effective("embedding_service_url", settings)
        primary_model = await effective("embedding_model", settings)
        fallback_url = settings.embedding_fallback_url
        # Only probe what is configured: an unconfigured service is off, and
        # "off" must never be reported through the same path as "reachable".
        primary_ok = False
        if primary_url:
            test_vec = await _call_embed_service(primary_url, primary_model, "test")
            primary_ok = test_vec is not None
        fallback_ok = False
        if fallback_url and not primary_ok:
            fb_vec = await _call_embed_service(fallback_url, primary_model, "test")
            fallback_ok = fb_vec is not None
        rows = await self.repo.db.fetchone("SELECT COUNT(*) AS c FROM vec_docs")
        indexed = rows["c"] if rows else 0
        total = 0
        t_rows = await self.repo.db.fetchone("SELECT COUNT(*) AS c FROM doc_metadata")
        if t_rows:
            total = t_rows["c"]
        orphans = await self.repo.count_orphan_embeddings()
        # `search_mode` described the SERVICE, not the index - so it read
        # "vector" over an index with zero embeddings, where no search could
        # possibly be a vector search. Reproduced on 3.6.17:
        # `{"primary_ok": true, "indexed_with_embeddings": 0,
        #   "search_mode": "vector"}` while every query fell through to
        # keyword. A mode is a claim about what will happen to the next query.
        service_ok = primary_ok or fallback_ok
        if not service_ok or indexed == 0:
            mode = "keyword"
        elif indexed < total:
            mode = "vector_partial"
        else:
            mode = "vector" if primary_ok else "fallback_vector"
        return {
            # Redacted: an embedding endpoint can carry credentials, and this is
            # a report, not the configuration itself.
            "primary_url": redact_endpoint(primary_url),
            "primary_ok": primary_ok,
            "fallback_url": redact_endpoint(fallback_url or ""),
            "fallback_ok": fallback_ok,
            # Keyword mode has two causes that need opposite actions: nothing is
            # configured (fine, by design) or something is configured and down.
            # A caller that sees only search_mode cannot tell them apart.
            "configured": bool(primary_url or fallback_url),
            "indexed_with_embeddings": indexed,
            "total_docs": total,
            # How many documents a vector search cannot see. This is the number
            # that decides whether "vector search" means anything yet.
            "pending_embeddings": max(total - (indexed - orphans), 0),
            # Should always be 0. Anything else means vectors are stranded on
            # row ids that a future document can inherit.
            "orphan_embeddings": orphans,
            "search_mode": mode,
            "search_mode_detail": _SEARCH_MODE_DETAIL[mode],
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
                # Never cancel a rebuild that is already running: cancellation
                # used to land between the wholesale DELETE and the re-insert.
                # The rebuild is now an upsert, so waiting costs nothing.
                logger.debug("KB reindex already running; not starting a second")
                return
            self._reindex_task = asyncio.create_task(self._run_reindex(reason))

    async def _run_reindex(self, reason: str) -> None:
        try:
            # `no_embeddings=False`. This ran with embeddings SKIPPED, and the
            # old reindex deleted every vector before rebuilding - so one
            # unindexed note plus a restart took the whole vector index with it
            # and nothing put it back. Reproduced on 3.6.17 against a healthy
            # embedding service: `indexed_with_embeddings` 2 -> 0, while
            # `search_mode` still said "vector".
            #
            # The rebuild is an upsert now, so an unchanged note is not
            # re-embedded and this costs one embedding call per note that
            # actually changed, plus the sweep over anything still missing one.
            result = await self.reindex(reason=reason)
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
            await self.index_note(result_id)
            return result_id
        except (FileExistsError, ValueError, OSError, LifecycleError):
            fm, _body = self.store.read(artifact_id)
            from homepilot.artifacts.models import compute_body_hash

            if fm.get("hash") == compute_body_hash(content):
                return artifact_id
            artifact_id = f"{date_str}-kb-{hashlib.sha256(content.encode()).hexdigest()[:6]}"
            spec["id"] = artifact_id
            result_id = await self.lifecycle.propose(spec)
            await self.index_note(result_id)
            return result_id

    async def index_note(self, artifact_id: str) -> dict[str, Any]:
        """Put an applied kb-note into the knowledge base NOW.

        A `kb-note` is written `applied` on propose and the executor never runs
        (ARTIFACT_SPEC.md §"kb-note shortcut": *"No approved_by, no executor
        run, no apply log"*). Nothing then indexed it. Reproduced on dev 3.6.17:
        `propose_artifact` returned `status: "applied"`, and `get_kb_doc` on the
        id it returned answered *"KB entry not found"* while `search_kb` found
        nothing - until some UNRELATED KB write or a restart happened to trigger
        a rebuild. `hp policy init` writes 19 notes down that path and then
        prints "These policies are now in the KB - the agent will find them via
        search_kb" (#648 tranche 6).

        Best-effort by design: a note whose indexing fails is still a proposed,
        applied artifact on disk, and the next reindex picks it up. What it must
        not do is stay silent - the caller gets `indexed: false` and a reason.
        """
        from homepilot.executor import kb_note as kb_note_executor

        try:
            fm, body = self.store.read(artifact_id)
        except (FileNotFoundError, OSError, ValueError) as exc:
            logger.warning("Could not read %s to index it: %s", artifact_id, exc)
            return {"indexed": False, "reason": f"artifact unreadable: {exc}"}
        if fm.get("kind") != "kb-note":
            return {"indexed": False, "reason": f"not a kb-note: {fm.get('kind')}"}
        try:
            result = await kb_note_executor.execute(fm, body, self.repo)
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            logger.warning("Indexing %s failed: %s", artifact_id, exc)
            return {"indexed": False, "reason": str(exc)}
        if not result.get("success"):
            return {"indexed": False, "reason": result.get("failure_reason", "index failed")}
        return {
            "indexed": True,
            "doc_id": result.get("doc_id"),
            "embedding_stored": result.get("embedding_stored", False),
        }
