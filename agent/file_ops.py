"""Backward-compat shim — prefer ``hp_agent.file_ops``."""

from hp_agent.file_ops import FileOps, FileOpsError

__all__ = ["FileOps", "FileOpsError"]
