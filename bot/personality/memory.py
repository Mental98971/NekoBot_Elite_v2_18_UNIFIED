"""
Personality memory v2.3 — conversation history, running gags, joke
callbacks, per-user message counting, per-chat settings persistence,
and startup loading of persisted settings.

v2.3 changes:
  - increment_and_check() performs the actual increment as a single SQL
    `count + 1` UPDATE (the same idiom as update_profile's interaction
    counter and economy_service's balance updates) instead of a Python
    read-modify-write, so it's genuinely race-free under concurrent
    writers — not just "safe in practice because SQLite serializes
    writes", which was the old implementation's real guarantee even
    though its docstring claimed otherwise.
  - _local_cache uses LRUCache instead of unbounded dict (fix 2.3).
  - get_stats() caches results for 60s per chat (fix 2.5).
  - Removed all hasattr/getattr defensive guards (fix 1.8).
  - load_persisted_settings() loads custom instructions, skins, and
    configs from DB on startup (fix 1.1/3.1).
  - cleanup_old_data() prunes stale messages and callbacks (feature 4.6).
  - All per-chat settings methods use direct column access.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from cachetools import LRUCache, TTLCache
from sqlalchemy import desc, select, update, func, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.core.database import (
    Chat,
    ChatMember,
    PersonalityCallback,
    PersonalityMessage,
    async_session,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MessageContext:
    """A single message in conversation history."""
    role: str
    content: str
    timestamp: str
    intent: Optional[str] = None
    sentiment: Optional[str] = None


@dataclass
class ChatPersonalitySettings:
    """Per-chat personality settings."""
    custom_instructions: Optional[str] = None
    user_limit: Optional[int] = None       # None = global default
    response_delay: Optional[float] = None  # None = global default
    skin: Optional[str] = None
    custom_config: Optional[Dict] = None


class MemoryManager:
    """Neko remembers — not because she cares, but because callbacks land
    better when you know what you already mocked."""

    def __init__(self) -> None:
        # v2.3: LRU cache instead of unbounded dict
        self._local_cache: LRUCache = LRUCache(maxsize=500)
        # v2.3: Stats cache with 60s TTL
        self._stats_cache: TTLCache = TTLCache(maxsize=500, ttl=60)
        # v2.3: Cache for _is_enabled_for
        self._enabled_cache: TTLCache = TTLCache(maxsize=500, ttl=30)

    # ─── Message History ───

    async def save_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        intent: Optional[str] = None,
        sentiment: Optional[str] = None,
    ) -> None:
        async with async_session() as session:
            session.add(
                PersonalityMessage(chat_id=chat_id, role=role, content=content, intent=intent)
            )
            await session.commit()

        bucket = self._local_cache.setdefault(chat_id, [])
        bucket.append(
            MessageContext(role, content, datetime.utcnow().isoformat(), intent, sentiment)
        )
        if len(bucket) > settings.personality_max_context * 2:
            self._local_cache[chat_id] = bucket[-settings.personality_max_context:]

    async def get_context(self, chat_id: int, limit: int = 10) -> List[MessageContext]:
        if chat_id in self._local_cache and len(self._local_cache[chat_id]) >= limit:
            return self._local_cache[chat_id][-limit:]

        async with async_session() as session:
            result = await session.execute(
                select(
                    PersonalityMessage.role,
                    PersonalityMessage.content,
                    PersonalityMessage.created_at,
                    PersonalityMessage.intent,
                )
                .where(PersonalityMessage.chat_id == chat_id)
                .order_by(desc(PersonalityMessage.id))
                .limit(limit)
            )
            rows = list(reversed(result.all()))
            context = [
                MessageContext(
                    r.role, r.content,
                    r.created_at.isoformat() if r.created_at else "",
                    r.intent, None,
                )
                for r in rows
            ]
            self._local_cache[chat_id] = context
            return context

    # ─── Profile ───

    @staticmethod
    async def _get_or_create_chat(
        session: AsyncSession, chat_id: int, chat_type: Optional[str] = None,
    ) -> Chat:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            chat = Chat(id=chat_id, type=chat_type or "private")
            session.add(chat)
            await session.flush()
        return chat

    async def get_user_profile(self, chat_id: int) -> Optional[Dict[str, Any]]:
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None:
                return None
            return {
                "chat_id": chat.id,
                "interaction_count": chat.personality_interactions or 0,
                "running_gags": chat.personality_running_gags or [],
                "skin": chat.personality_skin or "neko",
                "custom_config": chat.personality_custom_config,
            }

    async def update_profile(
        self,
        chat_id: int,
        username: Optional[str] = None,
        add_gag: Optional[str] = None,
        chat_type: Optional[str] = None,
    ) -> None:
        async with async_session() as session:
            await self._get_or_create_chat(session, chat_id, chat_type)
            await session.execute(
                update(Chat)
                .where(Chat.id == chat_id)
                .values(personality_interactions=Chat.personality_interactions + 1)
            )
            if add_gag:
                chat = await session.get(Chat, chat_id)
                if chat is not None:
                    gags = list(chat.personality_running_gags or [])
                    if add_gag not in gags:
                        gags.append(add_gag)
                        chat.personality_running_gags = gags[-10:]
            await session.commit()

    # ─── Joke Callbacks ───

    async def record_callback(self, chat_id: int, joke_id: str, context: str = "") -> None:
        async with async_session() as session:
            session.add(
                PersonalityCallback(chat_id=chat_id, joke_id=joke_id, context=context)
            )
            await session.commit()

    async def was_callback_used(
        self, chat_id: int, joke_id: str, within_hours: int = 48,
    ) -> bool:
        cutoff = datetime.utcnow() - timedelta(hours=within_hours)
        async with async_session() as session:
            result = await session.execute(
                select(PersonalityCallback.id).where(
                    PersonalityCallback.chat_id == chat_id,
                    PersonalityCallback.joke_id == joke_id,
                    PersonalityCallback.created_at > cutoff,
                ).limit(1)
            )
            return result.first() is not None

    async def get_recent_joke_ids(self, chat_id: int, limit: int = 15) -> List[str]:
        async with async_session() as session:
            result = await session.execute(
                select(PersonalityCallback.joke_id)
                .where(PersonalityCallback.chat_id == chat_id)
                .order_by(desc(PersonalityCallback.created_at))
                .limit(limit)
            )
            return [row[0] for row in result.all()]

    async def can_tell_joke(self, chat_id: int, min_interval: int = 300) -> bool:
        cutoff = datetime.utcnow() - timedelta(seconds=min_interval)
        async with async_session() as session:
            result = await session.execute(
                select(PersonalityCallback.created_at)
                .where(
                    PersonalityCallback.chat_id == chat_id,
                    PersonalityCallback.created_at > cutoff,
                )
                .order_by(desc(PersonalityCallback.created_at))
                .limit(1)
            )
            row = result.first()
            return row is None

    # ─── Stats (v2.3: cached for 60s) ───

    async def get_stats(self, chat_id: int) -> Optional[Dict[str, Any]]:
        # v2.3: Check cache first
        if chat_id in self._stats_cache:
            return self._stats_cache[chat_id]

        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None:
                return None

            count_result = await session.execute(
                select(func.count(PersonalityCallback.id))
                .where(PersonalityCallback.chat_id == chat_id)
            )
            jokes_told = count_result.scalar() or 0

            msg_count_result = await session.execute(
                select(func.count(PersonalityMessage.id))
                .where(PersonalityMessage.chat_id == chat_id)
            )
            messages = msg_count_result.scalar() or 0

            result = {
                "chat_id": chat.id,
                "enabled": chat.personality_enabled,
                "interactions": chat.personality_interactions or 0,
                "running_gags": len(chat.personality_running_gags or []),
                "jokes_told": jokes_told,
                "messages": messages,
                "skin": chat.personality_skin or "neko",
                "custom_config": chat.personality_custom_config,
                "custom_instructions": chat.custom_instructions,
                "user_limit": chat.personality_user_limit,
                "response_delay": chat.personality_response_delay,
            }

            self._stats_cache[chat_id] = result
            return result

    def invalidate_stats_cache(self, chat_id: int) -> None:
        """Call when settings change to force a stats refresh."""
        self._stats_cache.pop(chat_id, None)

    # ─── Skin Persistence ───

    async def set_skin(self, chat_id: int, skin_name: str) -> None:
        async with async_session() as session:
            await self._get_or_create_chat(session, chat_id)
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.personality_skin = skin_name
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    async def clear_skin(self, chat_id: int) -> None:
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.personality_skin = None
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    async def set_custom_config(self, chat_id: int, config: Dict[str, Any]) -> None:
        async with async_session() as session:
            await self._get_or_create_chat(session, chat_id)
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.personality_custom_config = config
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    async def clear_custom_config(self, chat_id: int) -> None:
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.personality_custom_config = None
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    # ─── v2.2: Per-User Message Counting (v2.3: atomic) ─────────────

    async def increment_and_check(self, chat_id: int, user_id: int) -> bool:
        """Atomically increment and return whether the user is still within
        the limit.

        The increment/reset write is a single SQL `count + 1` UPDATE
        executed by the database — the same idiom update_profile() already
        uses for the interaction counter — so concurrent calls for the
        same user can't lose an update the way a Python read-then-write
        would under a real concurrent database like Postgres. The row's
        first creation is guarded with a retry against a concurrent
        INSERT racing on the (chat_id, user_id) unique constraint.

        Returns True if the user is WITHIN the limit (Neko should
        respond), False if they've exceeded it (Neko is "tired" of them).
        """
        now = datetime.utcnow()
        reset_cutoff = now - timedelta(hours=settings.personality_msg_count_reset_hours)

        for _attempt in range(2):  # retry once if a concurrent first-insert wins the race
            async with async_session() as session:
                result = await session.execute(
                    select(ChatMember.personality_msg_count_reset_at).where(
                        ChatMember.chat_id == chat_id,
                        ChatMember.user_id == user_id,
                    )
                )
                row = result.first()

                if row is None:
                    session.add(ChatMember(
                        chat_id=chat_id, user_id=user_id,
                        personality_msg_count=1,
                        personality_msg_count_reset_at=now,
                    ))
                    try:
                        await session.commit()
                        count = 1
                    except IntegrityError:
                        # Another request created this row between our
                        # SELECT and our INSERT — retry as an UPDATE.
                        await session.rollback()
                        continue
                else:
                    needs_reset = row[0] is None or row[0] < reset_cutoff
                    if needs_reset:
                        stmt = (
                            update(ChatMember)
                            .where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
                            .values(personality_msg_count=1, personality_msg_count_reset_at=now)
                            .returning(ChatMember.personality_msg_count)
                        )
                    else:
                        stmt = (
                            update(ChatMember)
                            .where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
                            .values(personality_msg_count=ChatMember.personality_msg_count + 1)
                            .returning(ChatMember.personality_msg_count)
                        )
                    exec_result = await session.execute(stmt)
                    count = exec_result.scalar_one_or_none()
                    await session.commit()
                    if count is None:
                        # Row vanished between our SELECT and UPDATE
                        # (nothing in this codebase deletes individual
                        # ChatMember rows, so this is defensive only) —
                        # retry the whole create-or-update flow.
                        continue

                chat = await session.get(Chat, chat_id)
                per_chat_limit = chat.personality_user_limit if chat else None
                limit = per_chat_limit or settings.personality_user_limit_default
                return count <= limit

        # Two consecutive races is pathological. Fail open (respond)
        # rather than silently going quiet on a user over a transient
        # DB hiccup.
        logger.warning(
            "increment_and_check exhausted retries", chat_id=chat_id, user_id=user_id,
        )
        return True

    async def check_user_limit(self, chat_id: int, user_id: int) -> bool:
        """Check if the user has exceeded the per-chat message limit.

        Returns True if the user is WITHIN the limit.
        Note: Prefer increment_and_check() for atomic operations.
        """
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            per_chat_limit = chat.personality_user_limit if chat else None

        limit = per_chat_limit or settings.personality_user_limit_default

        async with async_session() as session:
            member = await session.execute(
                select(ChatMember.personality_msg_count).where(
                    ChatMember.chat_id == chat_id,
                    ChatMember.user_id == user_id,
                ).limit(1)
            )
            row = member.first()
            count = row[0] if row else 0

        return count < limit

    async def reset_user_msg_counts(self, chat_id: int) -> int:
        """Reset all per-user message counts for a chat. Returns count reset."""
        async with async_session() as session:
            result = await session.execute(
                update(ChatMember)
                .where(ChatMember.chat_id == chat_id)
                .values(
                    personality_msg_count=0,
                    personality_msg_count_reset_at=datetime.utcnow(),
                )
            )
            await session.commit()
            return result.rowcount

    # ─── v2.2: Settings Persistence ──────────────────────────────────

    async def get_settings(self, chat_id: int) -> ChatPersonalitySettings:
        """Retrieve all personality settings for a chat."""
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None:
                return ChatPersonalitySettings()

            return ChatPersonalitySettings(
                custom_instructions=chat.custom_instructions,
                user_limit=chat.personality_user_limit,
                response_delay=chat.personality_response_delay,
                skin=chat.personality_skin,
                custom_config=chat.personality_custom_config,
            )

    async def set_custom_instructions(self, chat_id: int, instructions: str) -> None:
        async with async_session() as session:
            await self._get_or_create_chat(session, chat_id)
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.custom_instructions = instructions if instructions.strip() else None
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    async def clear_custom_instructions(self, chat_id: int) -> None:
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.custom_instructions = None
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    async def set_user_limit(self, chat_id: int, limit: int) -> None:
        async with async_session() as session:
            await self._get_or_create_chat(session, chat_id)
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.personality_user_limit = limit
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    async def set_response_delay(self, chat_id: int, delay: float) -> None:
        async with async_session() as session:
            await self._get_or_create_chat(session, chat_id)
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.personality_response_delay = delay
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    async def clear_all_settings(self, chat_id: int) -> None:
        """Reset all settings to defaults."""
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            if chat is not None:
                chat.custom_instructions = None
                chat.personality_user_limit = None
                chat.personality_response_delay = None
                chat.personality_skin = None
                chat.personality_custom_config = None
            await session.commit()
        self.invalidate_stats_cache(chat_id)

    # ─── v2.3: Startup Loading of Persisted Settings ────────────────

    async def load_persisted_settings(self) -> int:
        """Load all persisted personality settings from the database.

        Called on bot startup. Returns the number of chats with settings loaded.

        This populates the in-memory dicts in PersonalityLayer:
        - custom_instructions -> personality.set_custom_instructions()
        - personality_skin -> personality.set_chat_skin()
        - personality_custom_config -> personality.set_chat_config()
        """
        from bot.personality.personality import personality

        count = 0
        async with async_session() as session:
            result = await session.execute(
                select(
                    Chat.id,
                    Chat.custom_instructions,
                    Chat.personality_skin,
                    Chat.personality_custom_config,
                ).where(
                    (Chat.custom_instructions.isnot(None)) |
                    (Chat.personality_skin.isnot(None)) |
                    (Chat.personality_custom_config.isnot(None))
                )
            )
            for row in result.all():
                chat_id, ci, skin, config = row
                if ci:
                    personality.set_custom_instructions(chat_id, ci)
                if skin:
                    personality.set_chat_skin(chat_id, skin)
                if config:
                    try:
                        personality.set_chat_config(chat_id, **config)
                    except Exception as e:
                        logger.warning("Failed to load config for chat %d: %s", chat_id, e)
                count += 1

        logger.info("Loaded persisted settings for %d chats", count)
        return count

    # ─── v2.3: Message History Cleanup ───────────────────────────────

    async def cleanup_old_data(self, days: int = 30) -> int:
        """Delete personality messages and callbacks older than N days.

        Returns the total number of rows deleted.
        Scheduled via JobQueue to run daily.
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        total_deleted = 0

        async with async_session() as session:
            result = await session.execute(
                delete(PersonalityMessage)
                .where(PersonalityMessage.created_at < cutoff)
            )
            total_deleted += result.rowcount

            result = await session.execute(
                delete(PersonalityCallback)
                .where(PersonalityCallback.created_at < cutoff)
            )
            total_deleted += result.rowcount

            await session.commit()

        # Clear local cache since old entries may be stale
        self._local_cache.clear()

        logger.info("Cleaned up %d old personality rows (older than %d days)", total_deleted, days)
        return total_deleted


memory = MemoryManager()
