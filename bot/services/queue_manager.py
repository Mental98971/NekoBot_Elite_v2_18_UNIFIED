"""
Smart queue management with persistence.

Ported from harmony-music-bot's `bot/services/queue/manager.py`. The
original persisted queues to MongoDB (source of truth) with a Redis
cache in front. To avoid requiring two extra database services just to
remember what's queued, this version persists to the same SQLAlchemy
database everything else in NovaBot already uses (one row per chat,
storing the queue as a JSON blob — see `MusicQueueState` in
bot/core/database.py). An in-memory cache still avoids a DB round trip
on every read. All queue algorithms (shuffle, repeat modes, history,
priority insertion, etc.) are unchanged from the original.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from bot.core.database import MusicQueueState, async_session
from bot.models.queue import Queue, QueueEntry, QueueHistoryEntry, QueueState, RepeatMode
from bot.models.track import Track
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class QueueManager:
    """Manages playback queues for all chats."""

    def __init__(self) -> None:
        self._queues: dict[int, Queue] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._listeners: list[Callable[[int, str, Any], None]] = []

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    async def get_queue(self, chat_id: int) -> Queue:
        """Get or create the queue for a chat. Tries memory, then the DB."""
        if chat_id in self._queues:
            return self._queues[chat_id]

        async with async_session() as session:
            row = await session.get(MusicQueueState, chat_id)
            if row and row.data:
                queue = Queue.model_validate(row.data)
                self._queues[chat_id] = queue
                return queue

        queue = Queue(chat_id=chat_id)
        self._queues[chat_id] = queue
        return queue

    async def _save_queue(self, chat_id: int) -> None:
        queue = self._queues.get(chat_id)
        if not queue:
            return
        data = queue.model_dump(mode="json")

        async with async_session() as session:
            row = await session.get(MusicQueueState, chat_id)
            if row is None:
                row = MusicQueueState(chat_id=chat_id, data=data)
                session.add(row)
            else:
                row.data = data
            await session.commit()

    async def add_track(
        self,
        chat_id: int,
        track: Track,
        position: Optional[int] = None,
        priority: int = 0,
    ) -> QueueEntry:
        """Add a track to the queue.

        Args:
            chat_id: Target chat.
            track: Track to add.
            position: Insert position (None = append).
            priority: Priority level (higher = earlier).

        Returns:
            The created queue entry.
        """
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)

            entry = QueueEntry(
                entry_id=str(uuid.uuid4())[:8],
                track=track,
                priority=priority,
            )

            if position is not None and 0 <= position <= len(queue.entries):
                queue.entries.insert(position, entry)
                for i, e in enumerate(queue.entries):
                    e.position = i
            else:
                entry.position = len(queue.entries)
                queue.entries.append(entry)

            if position is None and priority > 0:
                queue.entries.sort(key=lambda e: (-e.priority, e.position))
                for i, e in enumerate(queue.entries):
                    e.position = i

            queue.updated_at = datetime.utcnow()
            await self._save_queue(chat_id)

            logger.info("track_added", chat_id=chat_id, track=track.display_name, position=entry.position)
            await self._notify(chat_id, "track_added", entry)
            return entry

    async def remove_track(self, chat_id: int, entry_id: str) -> bool:
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            for i, entry in enumerate(queue.entries):
                if entry.entry_id == entry_id:
                    if i < queue.current_index:
                        queue.current_index -= 1
                    queue.entries.pop(i)
                    await self._save_queue(chat_id)
                    logger.info("track_removed", chat_id=chat_id, entry_id=entry_id)
                    await self._notify(chat_id, "track_removed", entry_id)
                    return True
            return False

    async def clear(self, chat_id: int) -> None:
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            queue.entries.clear()
            queue.current_index = -1
            queue.state = QueueState.IDLE
            queue.updated_at = datetime.utcnow()
            await self._save_queue(chat_id)
            logger.info("queue_cleared", chat_id=chat_id)
            await self._notify(chat_id, "queue_cleared", None)

    async def next_track(self, chat_id: int) -> Optional[Track]:
        """Advance to the next track, handling repeat modes and shuffle."""
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)

            if queue.is_empty:
                queue.state = QueueState.IDLE
                queue.current_index = -1
                await self._save_queue(chat_id)
                return None

            if queue.loop_remaining > 0 and queue.current_index >= 0:
                queue.loop_remaining -= 1
                await self._save_queue(chat_id)
                return queue.current_track

            if queue.repeat_mode == RepeatMode.ONE and queue.current_index >= 0:
                return queue.current_track

            next_idx = queue.current_index + 1

            if queue.shuffle_enabled and next_idx < len(queue.entries):
                import random
                if next_idx < len(queue.entries) - 1:
                    swap_idx = random.randint(next_idx, len(queue.entries) - 1)
                    queue.entries[next_idx], queue.entries[swap_idx] = (
                        queue.entries[swap_idx],
                        queue.entries[next_idx],
                    )

            if next_idx >= len(queue.entries):
                if queue.repeat_mode == RepeatMode.ALL:
                    next_idx = 0
                else:
                    queue.state = QueueState.IDLE
                    queue.current_index = -1
                    await self._save_queue(chat_id)
                    return None

            if 0 <= queue.current_index < len(queue.entries):
                current = queue.entries[queue.current_index]
                queue.history.append(QueueHistoryEntry(entry_id=current.entry_id, track_id=current.track.id))
                if len(queue.history) > 1000:
                    queue.history = queue.history[-500:]

            queue.current_index = next_idx
            queue.state = QueueState.PLAYING
            queue.updated_at = datetime.utcnow()
            await self._save_queue(chat_id)

            track = queue.current_track
            logger.info("next_track", chat_id=chat_id, track=track.display_name if track else None)
            await self._notify(chat_id, "next_track", track)
            return track

    async def previous_track(self, chat_id: int) -> Optional[Track]:
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            if queue.current_index > 0:
                queue.current_index -= 1
                queue.state = QueueState.PLAYING
                queue.updated_at = datetime.utcnow()
                await self._save_queue(chat_id)
                return queue.current_track
            return None

    async def jump_to(self, chat_id: int, position: int) -> Optional[Track]:
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            if 0 <= position < len(queue.entries):
                queue.current_index = position
                queue.state = QueueState.PLAYING
                queue.updated_at = datetime.utcnow()
                await self._save_queue(chat_id)
                return queue.current_track
            return None

    async def move(self, chat_id: int, from_pos: int, to_pos: int) -> bool:
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            if 0 <= from_pos < len(queue.entries) and 0 <= to_pos < len(queue.entries):
                entry = queue.entries.pop(from_pos)
                queue.entries.insert(to_pos, entry)
                for i, e in enumerate(queue.entries):
                    e.position = i
                if queue.current_index == from_pos:
                    queue.current_index = to_pos
                elif from_pos < queue.current_index <= to_pos:
                    queue.current_index -= 1
                elif to_pos <= queue.current_index < from_pos:
                    queue.current_index += 1
                queue.updated_at = datetime.utcnow()
                await self._save_queue(chat_id)
                return True
            return False

    async def shuffle(self, chat_id: int) -> None:
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            if queue.current_index < len(queue.entries) - 1:
                import random
                remaining = queue.entries[queue.current_index + 1:]
                random.shuffle(remaining)
                queue.entries = queue.entries[: queue.current_index + 1] + remaining
                for i, e in enumerate(queue.entries):
                    e.position = i
                queue.updated_at = datetime.utcnow()
                await self._save_queue(chat_id)

    async def set_repeat(self, chat_id: int, mode: RepeatMode) -> None:
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            queue.repeat_mode = mode
            queue.updated_at = datetime.utcnow()
            await self._save_queue(chat_id)

    async def set_loop(self, chat_id: int, count: int) -> int:
        """Set (not add to) how many more times the current track repeats.
        Returns the clamped value actually set (0-10)."""
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            queue.loop_remaining = max(0, min(10, count))
            queue.updated_at = datetime.utcnow()
            await self._save_queue(chat_id)
            return queue.loop_remaining

    async def add_loop(self, chat_id: int, delta: int) -> int:
        """Add to the current loop count (used by /loop <n>, as opposed
        to /loop enable|disable which calls set_loop directly). Returns
        the new clamped total."""
        async with self._get_lock(chat_id):
            queue = await self.get_queue(chat_id)
            queue.loop_remaining = max(0, min(10, queue.loop_remaining + delta))
            await self._save_queue(chat_id)
            return queue.loop_remaining

    async def get_history(self, chat_id: int, limit: int = 50) -> list[QueueHistoryEntry]:
        queue = await self.get_queue(chat_id)
        return queue.history[-limit:]

    async def get_stats(self, chat_id: int) -> dict[str, Any]:
        queue = await self.get_queue(chat_id)
        return {
            "total_tracks": len(queue.entries),
            "played": len(queue.history),
            "remaining_duration": queue.remaining_duration,
            "repeat_mode": queue.repeat_mode.value,
            "shuffle": queue.shuffle_enabled,
            "state": queue.state.value,
        }

    def register_listener(self, callback: Callable[[int, str, Any], None]) -> None:
        self._listeners.append(callback)

    async def _notify(self, chat_id: int, event: str, data: Any) -> None:
        for listener in self._listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(chat_id, event, data)
                else:
                    listener(chat_id, event, data)
            except Exception:
                logger.error("listener_error", exc_info=True)


queue_manager = QueueManager()
