"""Vault — age-encrypted secrets with AES-GCM identity protection."""

from .manager import VaultError, VaultManager

__all__ = ["VaultError", "VaultManager"]
