import sqlite3
from unittest.mock import AsyncMock, patch

from homepilot.executor.kb_note import execute as kb_note_execute


async def test_index_success_with_embedding(mock_repo):
    fm = {"id": "note-1", "intent": "Test note", "target": {"host": "web1"}}
    body = "This is the body of the note."
    mock_repo.create_doc_metadata.return_value = 42

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
    mock_repo.create_doc_metadata.assert_called_once()


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


async def test_doc_insert_fails_still_succeeds(mock_repo):
    fm = {"id": "note-4", "intent": "Test", "target": {"host": "web1"}}
    body = "Some content."
    mock_repo.create_doc_metadata.side_effect = sqlite3.OperationalError("db locked")

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        return_value=[0.1] * 8,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    assert "doc insert failed" in result["execution_log"]


async def test_target_extraction_host(mock_repo):
    fm = {"id": "note-5", "intent": "Test", "target": {"host": "web1"}}
    body = "Content."
    mock_repo.create_doc_metadata.return_value = 1

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.create_doc_metadata.call_args
    assert call_kwargs.kwargs.get("target") == "web1" or call_kwargs[1].get("target") == "web1"


async def test_target_extraction_service(mock_repo):
    fm = {"id": "note-6", "intent": "Test", "target": {"service": "api"}}
    body = "Content."
    mock_repo.create_doc_metadata.return_value = 1

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.create_doc_metadata.call_args
    assert call_kwargs.kwargs.get("target") == "api" or call_kwargs[1].get("target") == "api"


async def test_note_kind_default(mock_repo):
    fm = {"id": "note-7", "intent": "Test", "target": {"host": "x"}}
    body = "Content."
    mock_repo.create_doc_metadata.return_value = 1

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.create_doc_metadata.call_args
    assert call_kwargs.kwargs.get("kind") == "note" or call_kwargs[1].get("kind") == "note"


async def test_note_kind_custom(mock_repo):
    fm = {"id": "note-8", "intent": "Test", "note_kind": "decision", "target": {"host": "x"}}
    body = "Content."
    mock_repo.create_doc_metadata.return_value = 1

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        side_effect=Exception,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
    call_kwargs = mock_repo.create_doc_metadata.call_args
    assert call_kwargs.kwargs.get("kind") == "decision" or call_kwargs[1].get("kind") == "decision"


async def test_embedding_store_failure_graceful(mock_repo):
    fm = {"id": "note-9", "intent": "Test", "target": {"host": "x"}}
    body = "Content."
    mock_repo.create_doc_metadata.return_value = 99
    mock_repo.db.conn.execute = AsyncMock(side_effect=sqlite3.OperationalError("vec write failed"))
    mock_repo.db.conn.commit = AsyncMock()

    with patch(
        "homepilot.executor.kb_note._compute_embedding",
        new_callable=AsyncMock,
        return_value=[0.1] * 8,
    ):
        result = await kb_note_execute(fm, body, mock_repo)

    assert result["success"] is True
