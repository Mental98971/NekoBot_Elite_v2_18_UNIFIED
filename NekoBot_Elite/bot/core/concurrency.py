"""Per-chat ordered, cross-chat concurrent update processing.

python-telegram-bot handles updates strictly one at a time by default, so one
slow handler (a yt-dlp download, an LLM call, an image render) stalls *every*
chat the bot is in. Plain ``concurrent_updates(True)`` removes that stall but
also removes ordering, which would expose handlers that quietly rely on it
(two ``/grab`` presses in one chat, command-then-reply sequences).

``PerChatUpdateProcessor`` keeps the guarantee that matters — updates from the
same chat are processed in arrival order, one at a time — while letting
different chats run in parallel. ``KeyedSerializer`` is the dependency-free
core of that and is what the unit tests exercise.

Set ``CONCURRENT_UPDATES=0`` to restore the legacy fully sequential behaviour.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Hashable, Optional, Tuple


class KeyedSerializer:
    """FIFO mutual exclusion per key, with no state left behind once a key is idle."""

    def __init__(self) -> None:
        # key -> [lock, number of tasks holding or waiting for it]
        self._entries: Dict[Hashable, list] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._entries.pop(key, None)

    def active_keys(self) -> int:
        return len(self._entries)


def update_serial_key(update: object) -> Optional[Tuple[str, int]]:
    """Which updates must stay ordered relative to each other.

    Chat-bound updates share their chat id; anything without a chat (inline
    queries, ...) falls back to the user, and anonymous updates are not
    serialised at all.
    """
    chat = getattr(update, "effective_chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        return ("chat", chat.id)
    user = getattr(update, "effective_user", None)
    if user is not None and getattr(user, "id", None) is not None:
        return ("user", user.id)
    return None


def build_update_processor(max_concurrent_updates: int) -> Any:
    """Create the PTB update processor (imported lazily so this module stays importable without PTB)."""
    from telegram.ext import BaseUpdateProcessor

    class PerChatUpdateProcessor(BaseUpdateProcessor):
        def __init__(self, max_concurrent: int) -> None:
            super().__init__(max_concurrent)
            self._serializer = KeyedSerializer()

        async def process_update(self, update: object, coroutine: Any) -> None:
            # Take the per-chat turn *before* the global concurrency slot: queued updates of one
            # busy chat then wait here instead of occupying slots that other chats need.
            key = update_serial_key(update)
            if key is None:
                await super().process_update(update, coroutine)
                return
            async with self._serializer.hold(key):
                await super().process_update(update, coroutine)

        async def do_process_update(self, update: object, coroutine: Any) -> None:
            await coroutine

        async def initialize(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    return PerChatUpdateProcessor(max_concurrent_updates)
