"""Lightweight distributed-friendly rate limiter backed by Cache."""
from __future__ import annotations

from typing import Tuple

from bot.utils.cache import cache
from bot.utils.logger import get_logger

logger = get_logger(__name__)


async def hit(key: str, *, limit: int, window_seconds: int) -> Tuple[bool, int]:
    """Record a hit. Returns (allowed, current_count).

    allowed is True when current_count <= limit.
    """
    if limit <= 0 or window_seconds <= 0:
        return True, 0
    try:
        count = await cache.incr(f"rl:{key}", ttl=window_seconds)
        return count <= limit, count
    except Exception as e:
        logger.debug("rate limit check failed for %s: %s — allowing", key, e)
        return True, 0


async def remaining(key: str, *, limit: int) -> int:
    try:
        from bot.utils.cache import cache as c
        val = await c.get(f"rl:{key}")
        if val is None:
            return limit
        return max(0, limit - int(val))
    except Exception:
        return limit
