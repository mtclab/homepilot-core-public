"""HomePilot tells its operator when a newer release exists (review A+). Notify only."""

from .checker import EVENT_UPDATE_AVAILABLE, UpdateChecker, update_status

__all__ = ["EVENT_UPDATE_AVAILABLE", "UpdateChecker", "update_status"]
