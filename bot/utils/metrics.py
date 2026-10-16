"""Lightweight metrics counters (in-process). Optional Prometheus export later."""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Dict

_lock = Lock()
_counters: Dict[str, int] = defaultdict(int)
_timings: Dict[str, list] = defaultdict(list)
_started = time.time()


def incr(name: str, value: int = 1) -> None:
    with _lock:
        _counters[name] += value


def timing(name: str, seconds: float) -> None:
    with _lock:
        bucket = _timings[name]
        bucket.append(seconds)
        if len(bucket) > 200:
            del bucket[:100]


def snapshot() -> dict:
    with _lock:
        out = {
            "uptime_seconds": int(time.time() - _started),
            "counters": dict(_counters),
            "timings_avg": {
                k: (sum(v) / len(v) if v else 0.0) for k, v in _timings.items()
            },
        }
    return out
