"""Backward-compat shim — prefer ``hp_agent.main``."""

from hp_agent.main import HPAgent, main

__all__ = ["HPAgent", "main"]
