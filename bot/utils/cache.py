"""Optional cache layer with in-memory fallback.

When REDIS_URL is set, uses Redis. Otherwise falls back to a process-local
LRU + TTL dict. Designed so callers never need to care which backend is active.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from bot.config import settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class _MemoryBackend:
    def __init__(self, maxsize: int = 4096) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._maxsize = maxsize
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            value, expires = item
            if expires and time.time() > expires:
                self._store.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any, ttl: int = 300) -> None:
        async with self._lock:
            if len(self._store) >= self._maxsize:
                # crude eviction of oldest expired or first key
                now = time.time()
                expired = [k for k, (_, exp) in self._store.items() if exp and now > exp]
                for k in expired[: max(1, len(expired) // 4)]:
                    self._store.pop(k, None)
                if len(self._store) >= self._maxsize:
                    self._store.pop(next(iter(self._store)), None)
            expires = time.time() + ttl if ttl else 0.0
            self._store[key] = (value, expires)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def incr(self, key: str, ttl: int = 60) -> int:
        async with self._lock:
            item = self._store.get(key)
            now = time.time()
            if not item or (item[1] and now > item[1]):
                self._store[key] = (1, now + ttl)
                return 1
            val = int(item[0]) + 1
            self._store[key] = (val, item[1])
            return val


class _RedisBackend:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis
        self._r = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Optional[Any]:
        raw = await self._r.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    async def set(self, key: str, value: Any, ttl: int = 300) -> None:
        raw = json.dumps(value) if not isinstance(value, str) else value
        if ttl:
            await self._r.setex(key, ttl, raw)
        else:
            await self._r.set(key, raw)

    async def delete(self, key: str) -> None:
        await self._r.delete(key)

    async def incr(self, key: str, ttl: int = 60) -> int:
        val = await self._r.incr(key)
        if val == 1 and ttl:
            await self._r.expire(key, ttl)
        return int(val)


class Cache:
    def __init__(self) -> None:
        self._backend: Any = None
        self._init_attempted = False

    def _ensure(self) -> None:
        if self._init_attempted:
            return
        self._init_attempted = True
        url = getattr(settings, "redis_url", None)
        if url:
            try:
                self._backend = _RedisBackend(url)
                logger.info("Cache backend: Redis")
                return
            except Exception as e:
                logger.warning("Redis unavailable (%s) — falling back to memory cache", e)
        self._backend = _MemoryBackend()
        logger.info("Cache backend: in-memory LRU")

    async def get(self, key: str) -> Optional[Any]:
        self._ensure()
        try:
            return await self._backend.get(key)
        except Exception as e:
            logger.debug("Cache get failed for %s: %s", key, e)
            return None

    async def set(self, key: str, value: Any, ttl: int = 300) -> None:
        self._ensure()
        try:
            await self._backend.set(key, value, ttl=ttl)
        except Exception as e:
            logger.debug("Cache set failed for %s: %s", key, e)

    async def delete(self, key: str) -> None:
        self._ensure()
        try:
            await self._backend.delete(key)
        except Exception as e:
            logger.debug("Cache delete failed for %s: %s", key, e)

    async def incr(self, key: str, ttl: int = 60) -> int:
        self._ensure()
        try:
            return await self._backend.incr(key, ttl=ttl)
        except Exception as e:
            logger.debug("Cache incr failed for %s: %s", key, e)
            return 1


cache = Cache()
