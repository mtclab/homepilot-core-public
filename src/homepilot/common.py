from __future__ import annotations

from typing import Any


class APIError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, detail: str | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            error["detail"] = self.detail
        return {"error": error}
