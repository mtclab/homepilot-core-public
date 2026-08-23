"""One backend per data directory (#431).

Two things went wrong when a second process touched a live install:

* the backend called `fail_orphaned_tasks()` unconditionally at every start,
  marking every `pending`/`running` task failed. A rolling restart or a stray
  `docker compose up` therefore **killed the first backend's in-flight work** -
  an apply that was halfway through a host was reported as failed while it
  carried on running;
* `hp agent revoke`, `hp init` and `hp token create` each call `run_migrations()`
  on the same file, so a CLI could migrate the schema under a running server.

Both are the same missing fact: nothing knew whether another process was already
using this data directory. An advisory `flock` on a lockfile answers that. It is
released by the kernel when the holder exits, so a crashed backend never leaves a
stale lock behind - which is the failure mode a PID file has and this does not.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from pathlib import Path
from types import TracebackType

logger = logging.getLogger(__name__)

LOCK_FILENAME = "homepilot.lock"


class InstanceLockError(RuntimeError):
    """Another process already holds this data directory."""


class InstanceLock:
    """An exclusive, advisory lock on a data directory.

    Used as a context manager by the backend for its whole lifetime, and probed
    (never held) by the CLI to find out whether a server is running.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / LOCK_FILENAME
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise InstanceLockError(
                    f"another HomePilot process is already using {self.path.parent}. "
                    "Two backends on one data directory corrupt each other's in-flight "
                    "work: the second one marks the first one's running tasks failed."
                ) from None
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        logger.info("instance lock held on %s (pid %d)", self.path, os.getpid())

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def another_instance_is_running(data_dir: str | Path) -> bool:
    """True when some other process holds this data directory.

    Probes by trying to take the lock and immediately dropping it, so it answers
    without becoming the holder. Used by the CLI before it migrates a schema the
    running server is using.

    Fails OPEN - an unreadable or unwritable lock path answers "no" - because
    this guards a convenience, and a filesystem quirk must not make the CLI
    refuse to work at all.
    """
    lock = InstanceLock(data_dir)
    try:
        lock.acquire()
    except InstanceLockError:
        return True
    except OSError as exc:
        logger.debug("could not probe the instance lock: %s", exc)
        return False
    lock.release()
    return False
