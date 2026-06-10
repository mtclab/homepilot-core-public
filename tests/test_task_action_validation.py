from __future__ import annotations

import pytest_asyncio

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.tasks.repository import TaskRepository


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def task_repo(db: Database) -> TaskRepository:
    return TaskRepository(db)


class TestCreateTaskActionValidation:
    async def test_valid_actions(self, task_repo: TaskRepository):
        for action in ("apply", "revoke", "replay"):
            tid = await task_repo.create_task("artifact-1", action)
            assert tid is not None

    async def test_invalid_action_raises_valueerror(self, task_repo: TaskRepository):
        import pytest

        with pytest.raises(ValueError, match="Invalid action"):
            await task_repo.create_task("artifact-1", "destroy")

    async def test_create_task_if_no_active_rejects_invalid(self, task_repo: TaskRepository):
        import pytest

        with pytest.raises(ValueError, match="Invalid action"):
            await task_repo.create_task_if_no_active("artifact-1", "explode")
