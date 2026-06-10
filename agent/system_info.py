"""Backward-compat shim — prefer ``hp_agent.system_info``."""

from hp_agent.system_info import collect_system_info

__all__ = ["collect_system_info"]
