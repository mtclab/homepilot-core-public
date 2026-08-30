import sqlite3
from unittest.mock import AsyncMock, patch

from homepilot.executor.kb_note import execute as kb_note_execute


async def test_index_success_with_embedding(mock_repo):
    fm = {"id": "note-1", "intent": "Test note", "target": {"host": "web1"}}
    body = "This is the body of the note."
    mock_repo.upsert_doc_metadata.return_value = (42, True)

    with (
        patch(
            "homepilot.executor.kb_note._compute_embedding",
            new_callable=AsyncMock,
            return_value=[0.1] * 8,
        ),
        patch("homepilot.executor.kb_note._store_embedding", new_callable=AsyncMock),
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    assert "embedding stored" in result["execution_log"]
    mock_repo.upsert_doc_metadata.assert_called_once()


async def test_index_keyword_only_no_embedding(mock_repo):
    fm = {"id": "note-2", "intent": "Test", "target": {"host": "web1"}}
    body = "Note body text."

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception("ollama down"),
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    assert "keyword-only" in result["execution_log"]


async def test_empty_body_fails(mock_repo):
    fm = {"id": "note-3", "intent": "Empty"}
    result = await kb_note_execute(fm, "", mock_repo)
    assert result["success"] is False
    assert result["failure_reason"] == "empty body"


async def test_a_failed_doc_write_is_not_success_and_never_says_indexed(mock_repo):
    """A note that is NOT in the knowledge base must not report itself indexed.

    The executor returned `success: True` with an execution log starting
    "kb-note indexed" when the `doc_metadata` write had raised - so the artifact
    went `applied`, the operator was told their decision was recorded, and no
    `search_kb` would ever find it. This is #642's shape in the KB: a verdict
    produced from a write that did not happen.
    """
    fm = {"id": "note-4", "intent": "Test", "target": {"host": "web1"}}
    body = "Some content."
    mock_repo.upsert_doc_metadata.side_effect = sqlite3.OperationalError("db locked")

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        return_value=[0.1] * 8,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is False
    assert "NOT indexed" in result["execution_log"]
    assert "db locked" in result["failure_reason"]
    # The word that made the old log a lie.
    assert not result["execution_log"].startswith("kb-note indexed")


async def test_target_extraction_host(mock_repo):
    fm = {"id": "note-5", "intent": "Test", "target": {"host": "web1"}}
    body = "Content."
    mock_repo.upsert_doc_metadata.return_value = (1, True)

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.upsert_doc_metadata.call_args
    assert call_kwargs.kwargs.get("target") == "web1" or call_kwargs[1].get("target") == "web1"


async def test_target_extraction_service(mock_repo):
    fm = {"id": "note-6", "intent": "Test", "target": {"service": "api"}}
    body = "Content."
    mock_repo.upsert_doc_metadata.return_value = (1, True)

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.upsert_doc_metadata.call_args
    assert call_kwargs.kwargs.get("target") == "api" or call_kwargs[1].get("target") == "api"


async def test_note_kind_default(mock_repo):
    fm = {"id": "note-7", "intent": "Test", "target": {"host": "x"}}
    body = "Content."
    mock_repo.upsert_doc_metadata.return_value = (1, True)

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.upsert_doc_metadata.call_args
    assert call_kwargs.kwargs.get("kind") == "note" or call_kwargs[1].get("kind") == "note"


async def test_note_kind_custom(mock_repo):
    fm = {"id": "note-8", "intent": "Test", "note_kind": "decision", "target": {"host": "x"}}
    body = "Content."
    mock_repo.upsert_doc_metadata.return_value = (1, True)

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.upsert_doc_metadata.call_args
    assert call_kwargs.kwargs.get("kind") == "decision" or call_kwargs[1].get("kind") == "decision"


async def test_embedding_store_failure_graceful(mock_repo):
    """A failed vector write leaves the note keyword-only - and SAYS so.

    The old log line appended ", embedding stored" whenever the embedding was
    non-empty, which it decided AFTER this call had been allowed to fail. On a
    real install that failure was `UNIQUE constraint failed on vec_docs primary
    key`, and the document went on carrying a DELETED document's vector while
    the log said its own was stored.
    """
    fm = {"id": "note-9", "intent": "Test", "target": {"host": "x"}}
    body = "Content."
    mock_repo.upsert_doc_metadata.return_value = (99, True)
    mock_repo.db.conn.execute = AsyncMock(side_effect=sqlite3.OperationalError("vec write failed"))
    mock_repo.db.conn.commit = AsyncMock()

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        return_value=[0.1] * 8,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    assert result["embedding_stored"] is False
    assert "embedding stored" not in result["execution_log"]
    assert "NOT stored" in result["execution_log"]
    assert "vec write failed" in result["execution_log"]
