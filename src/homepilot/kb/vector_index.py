"""The KB vector table's dimension follows the embedding model (#777).

`vec_docs` was created once as `embedding float[768]` and never looked at again.
`embedding_model` is an operator setting, so a model with any other output size
(qwen3-embedding 4096, bge-large 1024, OpenAI 1536/3072...) was accepted and then
failed every single insert - "Dimension mismatch ... Expected 768 dimensions but
received 4096" - while a reindex said `status: completed`.

The table's own DDL is the record of its dimension: it is what sqlite-vec
enforces, so reading it cannot drift from the truth the way a stored copy could.

The rules:
  * an EMPTY table takes the dimension of the first vector stored into it -
    nothing is lost, so there is nothing to ask;
  * a table that HOLDS vectors of another dimension is never rebuilt by a single
    insert: that insert is refused with both numbers, and counted as a failure.
    The rebuild belongs to a reindex, which re-embeds every document and says in
    its result how many vectors of the old model it dropped.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# sqlite-vec's own ceiling for a vec0 float column.
MAX_DIMENSION = 8192

_DIMENSION_RE = re.compile(r"embedding\s+float\[(\d+)\]", re.IGNORECASE)


class EmbeddingDimensionMismatchError(ValueError):
    """A vector whose length is not the index's dimension.

    A ValueError so every existing "could not store this embedding" handler
    counts it as the failure it is, rather than letting it escape.
    """

    def __init__(self, got: int, index: int):
        self.got = got
        self.index = index
        super().__init__(
            f"the embedding model returned {got} dimensions but the KB vector index "
            f"holds {index}; run a KB reindex to rebuild the index for the current model"
        )


async def index_dimension(db: Any) -> int | None:
    """The dimension `vec_docs` was created with, or None when there is no table."""
    row = await db.fetchone("SELECT sql FROM sqlite_master WHERE name = 'vec_docs'")
    sql = row["sql"] if row is not None else None
    if not isinstance(sql, str):
        return None
    match = _DIMENSION_RE.search(sql)
    return int(match.group(1)) if match else None


async def stored_vector_count(db: Any) -> int:
    row = await db.fetchone("SELECT COUNT(*) AS c FROM vec_docs")
    return int(row["c"]) if row else 0


async def rebuild_index(db: Any, dimension: int) -> int:
    """Recreate `vec_docs` for `dimension`. Returns how many vectors it dropped.

    Every vector in the old table is from a model whose space the new one does
    not share, so none of them can be kept; the documents themselves are
    untouched and become "missing an embedding", which a reindex refills.
    """
    if not 0 < dimension <= MAX_DIMENSION:
        raise ValueError(
            f"an embedding of {dimension} dimensions cannot be indexed "
            f"(sqlite-vec takes 1 to {MAX_DIMENSION})"
        )
    dropped = await stored_vector_count(db)
    await db.execute("DROP TABLE IF EXISTS vec_docs")
    await db.execute(
        "CREATE VIRTUAL TABLE vec_docs "
        f"USING vec0(id INTEGER PRIMARY KEY, embedding float[{int(dimension)}])"
    )
    await db.conn.commit()
    return dropped


async def fit_index_to(db: Any, dimension: int) -> None:
    """Make sure a vector of `dimension` can be stored - or say why it cannot.

    Re-shapes an EMPTY table on the spot (nothing to lose). Refuses with
    EmbeddingDimensionMismatchError when the table holds vectors of another
    dimension: that rebuild drops every one of them and is a reindex's call.
    """
    current = await index_dimension(db)
    if current is None or current == dimension:
        return
    if await stored_vector_count(db) > 0:
        raise EmbeddingDimensionMismatchError(dimension, current)
    await rebuild_index(db, dimension)
    logger.info(
        "KB vector index was empty and is now %d-dimensional (was %d), "
        "to match the embedding model",
        dimension,
        current,
    )


__all__ = [
    "MAX_DIMENSION",
    "EmbeddingDimensionMismatchError",
    "fit_index_to",
    "index_dimension",
    "rebuild_index",
    "stored_vector_count",
]
