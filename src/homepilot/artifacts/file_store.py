from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from .store import ArtifactStore


@contextmanager
def _file_lock(path: Any) -> Generator[None, None, None]:
    """Lock a SIDECAR, never the artifact itself (#388).

    This used to `os.open(artifact_path, O_CREAT)`, so merely taking the lock
    created the file. Approving an unknown id therefore wrote a zero-byte
    artifact, `store.read` raised "missing frontmatter delimiter" (a ValueError
    the router's FileNotFoundError handler does not catch, hence a 500), and the
    id was PERMANENTLY POISONED: `store.exists()` was now true, so propose
    refused to reuse it.

    A `.lock` sidecar gives the same mutual exclusion without the artifact path
    ever being created as a side effect of reading it.
    """
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class ArtifactFileStore:
    """Thin wrapper around ArtifactStore for file-lock-guarded writes with YAML serialization."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def read(self, id: str) -> tuple[dict[str, Any], str]:
        return self.store.read(id)

    def exists(self, id: str) -> bool:
        return self.store.exists(id)

    def write(self, id: str, fm_yml: str, body: str, event: str) -> None:
        self.store.write(id, fm_yml, body, event)

    def write_locked(self, id: str, fm: dict[str, Any], body: str, event: str) -> None:
        """Serialize frontmatter to YAML, acquire file lock, read/modify/write."""
        path = self.store.resolve_path(id)
        with _file_lock(path):
            fm_yml = self.serialize_frontmatter(fm)
            self.store.write(id, fm_yml, body, event)

    def resolve_path(self, id: str) -> Any:
        return self.store.resolve_path(id)

    def relative_path(self, id: str) -> str:
        return self.store.relative_path(id)

    @contextmanager
    def file_lock(self, id: str) -> Generator[None, None, None]:
        path = self.store.resolve_path(id)
        with _file_lock(path):
            yield

    def list(self, status: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return self.store.list(status=status, kind=kind)

    @staticmethod
    def serialize_frontmatter(fm: dict[str, Any]) -> str:
        result = yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return result or ""

    @staticmethod
    def load_frontmatter(fm_yml: str) -> dict[str, Any]:
        result: dict[str, Any] = yaml.safe_load(fm_yml)
        return result
