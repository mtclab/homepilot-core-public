from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_GIT_TIMEOUT = int(os.environ.get("HP_GIT_TIMEOUT", "30"))

_TIMEOUT_INIT = 10
_TIMEOUT_ADD_COMMIT = 15
_TIMEOUT_PUSH = 60


class GitOperationErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    LOCK_CONTENTION = "lock_contention"
    NETWORK = "network"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class GitOperationError(Exception):
    def __init__(self, category: GitOperationErrorCategory, operation: str, detail: str) -> None:
        self.category = category
        self.operation = operation
        self.detail = detail
        super().__init__(f"git {operation} failed ({category.value}): {detail}")


_LOCK_PATTERNS = [
    re.compile(r"another git process seems to be running", re.IGNORECASE),
    re.compile(r"lock file.*already exists", re.IGNORECASE),
    re.compile(r"Unable to create.*\.lock", re.IGNORECASE),
    re.compile(r"fatal: Unable to create.*\.lock", re.IGNORECASE),
]

_NETWORK_PATTERNS = [
    re.compile(r"fatal: could not read from remote", re.IGNORECASE),
    re.compile(r"fatal: the remote end hung up", re.IGNORECASE),
    re.compile(r"fatal: unable to access", re.IGNORECASE),
    re.compile(r"fatal: Connection timed out", re.IGNORECASE),
    re.compile(r"fatal: Could not resolve host", re.IGNORECASE),
    re.compile(r"fatal: early EOF", re.IGNORECASE),
]

_CORRUPT_PATTERNS = [
    re.compile(r"fatal: bad object", re.IGNORECASE),
    re.compile(r"fatal: corrupt", re.IGNORECASE),
    re.compile(r"fatal: object directory", re.IGNORECASE),
    re.compile(r"error: object file.*is empty", re.IGNORECASE),
]


def _classify_stderr(stderr: str) -> GitOperationErrorCategory:
    for pat in _LOCK_PATTERNS:
        if pat.search(stderr):
            return GitOperationErrorCategory.LOCK_CONTENTION
    for pat in _NETWORK_PATTERNS:
        if pat.search(stderr):
            return GitOperationErrorCategory.NETWORK
    for pat in _CORRUPT_PATTERNS:
        if pat.search(stderr):
            return GitOperationErrorCategory.CORRUPT
    return GitOperationErrorCategory.UNKNOWN


class ArtifactStore:
    def __init__(self, artifacts_dir: Path, remote: str = "", ssh_key: str = ""):
        self.root = Path(artifacts_dir)
        self.remote = remote
        self.ssh_key = ssh_key
        self.root.mkdir(parents=True, exist_ok=True)
        self._lockfile_path = self.root / ".git" / "homepilot.lock"
        self.init_repo()

    @contextmanager
    def _git_locked(self, timeout: float = 30.0) -> Generator[None, None, None]:
        self._lockfile_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lockfile_path), os.O_CREAT | os.O_RDONLY)
        acquired = False
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (OSError, BlockingIOError) as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GitOperationError(
                            GitOperationErrorCategory.LOCK_CONTENTION,
                            "lock",
                            f"git lock contention: could not acquire lock within {timeout}s",
                        ) from exc
                    time.sleep(min(0.5, remaining))
            yield
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _run_git(
        self,
        args: list[str],
        operation: str,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        if timeout is None:
            timeout = _DEFAULT_GIT_TIMEOUT
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                check=check,
                timeout=timeout,
                env=env,
            )
            return result
        except subprocess.TimeoutExpired:
            raise GitOperationError(
                GitOperationErrorCategory.TIMEOUT,
                operation,
                f"git {operation} timed out after {timeout}s",
            ) from None
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            category = _classify_stderr(stderr)
            short = stderr.strip().split("\n")[-1] if stderr.strip() else "unknown error"
            raise GitOperationError(category, operation, short) from exc

    def init_repo(self) -> None:
        git_dir = self.root / ".git"
        if not git_dir.exists():
            self._run_git(["init"], "init", timeout=_TIMEOUT_INIT)
        self._run_git(["config", "user.email", "homepilot@localhost"], "config")
        self._run_git(["config", "user.name", "HomePilot"], "config")
        if self.remote:
            result = self._run_git(["remote", "get-url", "origin"], "remote", check=False)
            if result.returncode == 0:
                self._run_git(["remote", "set-url", "origin", self.remote], "remote")
            else:
                self._run_git(["remote", "add", "origin", self.remote], "remote")
        gitattr = self.root / ".gitattributes"
        if not gitattr.exists():
            gitattr.write_text("*.md text eol=lf\n", encoding="utf-8")
            self._git_add_and_commit(".gitattributes", "init: add .gitattributes")
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text(_README_TEMPLATE, encoding="utf-8")
            self._git_add_and_commit("README.md", "init: add README")

    def _current_branch(self) -> str:
        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], "branch", check=False)
        if result.returncode == 0:
            branch = result.stdout.decode("utf-8", errors="replace").strip()
            if branch and branch != "HEAD":
                return branch
        return "master"

    def _git_push(self) -> None:
        if not self.remote:
            return
        env = None
        if self.ssh_key:
            ssh_cmd = f"ssh -i {self.ssh_key} -o StrictHostKeyChecking=no"
            env = {**os.environ, "GIT_SSH_COMMAND": ssh_cmd}
        branch = self._current_branch()
        self._run_git(
            ["push", "--set-upstream", "origin", branch],
            "push",
            timeout=_TIMEOUT_PUSH,
            env=env,
            check=False,
        )

    def _git_env(self, remote: str = "") -> dict[str, str] | None:
        ssh_key = self.ssh_key
        if not ssh_key and remote:
            settings_remote = self.remote
            if settings_remote and remote == "origin":
                pass
        if ssh_key:
            ssh_cmd = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
            return {**os.environ, "GIT_SSH_COMMAND": ssh_cmd}
        return None

    def _check_remote_exists(self, name: str) -> bool:
        result = self._run_git(["remote"], "remote", check=False)
        if result.returncode != 0:
            return False
        remotes = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        return name in remotes

    def push(self, remote: str = "origin") -> str:
        if not self._check_remote_exists(remote):
            raise GitOperationError(
                GitOperationErrorCategory.UNKNOWN,
                "push",
                f"No git remote '{remote}' configured. "
                f"Run: git remote add {remote} <url> inside your artifacts directory, "
                f"or set HP_ARTIFACTS_REMOTE in your environment.",
            )
        branch = self._current_branch()
        env = self._git_env(remote)
        result = self._run_git(
            ["push", remote, branch],
            "push",
            timeout=_TIMEOUT_PUSH,
            env=env,
        )
        return result.stdout.decode("utf-8", errors="replace")

    def pull(self, remote: str = "origin") -> str:
        if not self._check_remote_exists(remote):
            raise GitOperationError(
                GitOperationErrorCategory.UNKNOWN,
                "pull",
                f"No git remote '{remote}' configured. "
                f"Run: git remote add {remote} <url> inside your artifacts directory, "
                f"or set HP_ARTIFACTS_REMOTE in your environment.",
            )
        branch = self._current_branch()
        env = self._git_env(remote)
        result = self._run_git(
            ["pull", "--ff-only", remote, branch],
            "pull",
            timeout=_TIMEOUT_PUSH,
            env=env,
        )
        return result.stdout.decode("utf-8", errors="replace")

    def sync_status(self) -> dict[str, str]:
        status_result = self._run_git(["status", "--porcelain"], "status", check=False)
        log_result = self._run_git(["log", "--oneline", "-5"], "log", check=False)
        return {
            "status": status_result.stdout.decode("utf-8", errors="replace"),
            "log": log_result.stdout.decode("utf-8", errors="replace"),
        }

    def _git_add_and_commit(self, rel_path: str, message: str) -> None:
        with self._git_locked():
            self._run_git(["add", rel_path], "add", timeout=_TIMEOUT_ADD_COMMIT)
            self._run_git(
                ["commit", "-m", message, "--allow-empty"],
                "commit",
                timeout=_TIMEOUT_ADD_COMMIT,
            )
            self._git_push()

    def resolve_path(self, id: str) -> Path:
        m = re.match(r"^(\d{4})-(\d{2})-", id)
        if not m:
            raise ValueError(f"Invalid artifact ID: {id}")
        year, month = m.group(1), m.group(2)
        path = self.root / year / month / f"{id}.md"
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError(f"Artifact ID escapes store root: {id}")
        return resolved

    def _compose_file(self, id: str, frontmatter_yml: str, body: str) -> str:
        return f"---\n{frontmatter_yml}---\n\n{body}"

    def write(self, id: str, frontmatter_yml: str, body: str, event: str) -> Path:
        path = self.resolve_path(id)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self._compose_file(id, frontmatter_yml, body)
        path.write_text(content, encoding="utf-8")

        fm = yaml.safe_load(frontmatter_yml) or {}
        intent_line = (fm.get("intent") or "").split("\n")[0]
        commit_msg = f"{event}: {id} — {intent_line}"

        rel = path.relative_to(self.root)
        with self._git_locked():
            self._run_git(["add", str(rel)], "add", timeout=_TIMEOUT_ADD_COMMIT)
            self._run_git(
                ["commit", "-m", commit_msg],
                "commit",
                timeout=_TIMEOUT_ADD_COMMIT,
                check=False,
            )
            self._git_push()
        return path

    def _extract_body(self, text: str, end: int) -> str:
        body = text[end + 4 :]
        body = body.lstrip("\n")
        return body

    def read(self, id: str) -> tuple[dict[str, Any], str]:
        path = self.resolve_path(id)
        if not path.exists():
            raise FileNotFoundError(f"Artifact not found: {id}")
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(f"Artifact {id}: missing frontmatter delimiter")
        end = text.find("\n---", 3)
        if end == -1:
            raise ValueError(f"Artifact {id}: missing closing frontmatter delimiter")
        fm_text = text[3:end]
        body = self._extract_body(text, end)
        fm = yaml.safe_load(fm_text) or {}
        return fm, body

    def list(self, status: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for year_dir in sorted(self.root.iterdir()):
            if not year_dir.is_dir() or not re.match(r"^\d{4}$", year_dir.name):
                continue
            if year_dir.name == ".git":
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir() or not re.match(r"^\d{2}$", month_dir.name):
                    continue
                for md_file in sorted(month_dir.glob("*.md")):
                    try:
                        fm, _ = self.parse_file(md_file)
                    except (ValueError, OSError, sqlite3.OperationalError):
                        continue
                    if status is not None and fm.get("status") != status:
                        continue
                    if kind is not None and fm.get("kind") != kind:
                        continue
                    results.append(fm)
        return results

    def parse_file(self, path: Path) -> tuple[dict[str, Any], str]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(f"Missing frontmatter in {path}")
        end = text.find("\n---", 3)
        if end == -1:
            raise ValueError(f"Missing closing frontmatter in {path}")
        fm_text = text[3:end]
        body = self._extract_body(text, end)
        fm = yaml.safe_load(fm_text) or {}
        return fm, body

    def exists(self, id: str) -> bool:
        try:
            return self.resolve_path(id).exists()
        except ValueError:
            return False


_README_TEMPLATE = """\
# HomePilot Artifacts

This directory contains HomePilot artifact files. Each artifact is a Markdown
file with YAML frontmatter describing its metadata, and a body containing the
spec, plan, and optional rollback sections.

## Layout

Files are organized by date: `<YYYY>/<MM>/<id>.md`

## Artifact ID format

`YYYY-MM-DD-<kebab-slug>[-<6-char-hex>]`

## Lifecycle

Each artifact passes through: proposed → approved → applied (or failed).
Artifacts can also be rejected, superseded, or revoked.

## Editing

Use `hp artifacts edit <id>` to edit artifacts. Manual edits to approved
artifacts will be caught by hash verification at apply time.

## Format

```
---
<YAML frontmatter>
---

<Markdown body>
```

See the HomePilot Artifact Spec for full details.
"""
