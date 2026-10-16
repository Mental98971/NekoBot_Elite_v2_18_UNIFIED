"""Host resource snapshot for /sysstats (ported from ChatFight's ``system_stats``).

Standard library only. Linux reads ``/proc``; elsewhere (macOS, Windows, minimal containers) every
probe falls back to a coarser source or ``0.0`` rather than raising, so the command never fails.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import Dict, Tuple

_PROCESS_START = time.monotonic()


def format_uptime(seconds: float) -> str:
    """``3725`` -> ``"1h:2m:5s"`` (the format ChatFight used)."""
    total = int(max(seconds, 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h:{minutes}m:{secs}s"


def bot_uptime_seconds() -> float:
    return time.monotonic() - _PROCESS_START


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _read_cpu_snapshot() -> Tuple[int, int]:
    with open("/proc/stat", "r", encoding="utf-8") as handle:
        fields = handle.readline().split()
    values = [int(v) for v in fields[1:]]
    idle = values[3] + values[4] if len(values) > 4 else values[3]
    return sum(values), idle


async def cpu_percent() -> float:
    try:
        total_1, idle_1 = _read_cpu_snapshot()
        await asyncio.sleep(0.2)
        total_2, idle_2 = _read_cpu_snapshot()
        delta = total_2 - total_1
        if delta <= 0:
            return 0.0
        return _clamp((delta - (idle_2 - idle_1)) / delta * 100.0)
    except Exception:
        try:
            return _clamp(os.getloadavg()[0] / (os.cpu_count() or 1) * 100.0)
        except Exception:
            return 0.0


def memory_percent() -> float:
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                info[key] = int(value.strip().split()[0])
        total = info.get("MemTotal", 0)
        if total <= 0:
            return 0.0
        return _clamp((total - info.get("MemAvailable", 0)) / total * 100.0)
    except Exception:
        return 0.0


def disk_percent(path: str = "/") -> float:
    try:
        usage = shutil.disk_usage(path)
        return _clamp(usage.used / usage.total * 100.0) if usage.total > 0 else 0.0
    except Exception:
        return 0.0


def host_uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.readline().split()[0])
    except Exception:
        return bot_uptime_seconds()


async def collect() -> Dict[str, object]:
    cpu = await cpu_percent()
    return {
        "uptime": format_uptime(host_uptime_seconds()),
        "bot_uptime": format_uptime(bot_uptime_seconds()),
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(memory_percent(), 1),
        "disk_percent": round(disk_percent(), 1),
    }
