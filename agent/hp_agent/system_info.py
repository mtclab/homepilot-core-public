from __future__ import annotations

import asyncio
import os
import platform
from typing import Any


async def collect_system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": platform.node(),
        "os": platform.system(),
        "os_version": platform.version(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }

    def _get_disk() -> dict[str, float]:
        try:
            stat = os.statvfs("/")
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            return {
                "total_gb": round(total / 1e9, 2),
                "free_gb": round(free / 1e9, 2),
            }
        except Exception:
            return {}

    def _get_memory() -> dict[str, float]:
        try:
            with open("/proc/meminfo") as f:
                lines = {
                    k.strip(): v.strip()
                    for k, v in (line.split(":", 1) for line in f if ":" in line)
                }
            total_kb = int(lines.get("MemTotal", "0").rstrip(" kB"))
            free_kb = int(lines.get("MemAvailable", "0").rstrip(" kB"))
            return {
                "total_gb": round(total_kb / 1e6, 2),
                "free_gb": round(free_kb / 1e6, 2),
            }
        except Exception:
            return {}

    def _get_load() -> dict[str, float]:
        try:
            load1, load5, load15 = os.getloadavg()
            return {
                "load_1m": round(load1, 2),
                "load_5m": round(load5, 2),
                "load_15m": round(load15, 2),
            }
        except Exception:
            return {}

    disk = await asyncio.to_thread(_get_disk)
    memory = await asyncio.to_thread(_get_memory)
    load = await asyncio.to_thread(_get_load)

    info["disk"] = disk
    info["memory"] = memory
    info["load"] = load
    info["cpu_count"] = os.cpu_count() or 0

    return info
