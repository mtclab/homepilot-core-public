"""Backward-compat shim — prefer ``hp_agent.command_executor``."""

from hp_agent.command_executor import CommandAllowlist, CommandExecutor

__all__ = ["CommandAllowlist", "CommandExecutor"]
