from __future__ import annotations


def escape_like(value: str, escape: str = "\\") -> str:
    return value.replace(escape, escape * 2).replace("%", f"{escape}%").replace("_", f"{escape}_")
