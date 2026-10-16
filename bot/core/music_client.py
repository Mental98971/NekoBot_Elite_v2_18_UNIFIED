"""
MTProto client wrapper for live voice-chat music.

Ported from harmony-music-bot's `bot/core/client.py`. A plain Telegram Bot
API token (what python-telegram-bot uses for every other plugin in this
project) cannot join or stream into a voice chat — that requires an
MTProto session, which is what Pyrogram + PyTgCalls provide here, using
the SAME bot account (BOT_TOKEN) plus a second credential pair
(MUSIC_API_ID / MUSIC_API_HASH from my.telegram.org).

This client is only started when `settings.live_music_configured` is
True (see bot/core/bot.py). Every other plugin in NovaBot works fine
without it.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pytgcalls import PyTgCalls

from bot.config import get_settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class MusicClient:
    """Owns one Pyrogram MTProto session and PyTgCalls voice-chat layer.
    Normally there's exactly one (see get_music_client()); when
    MUSIC_EXTRA_BOT_TOKENS is set, AssistantPool below creates one of
    these per token to spread voice-chat load across several accounts."""

    def __init__(self, bot_token: Optional[str] = None, session_name: Optional[str] = None) -> None:
        self.settings = get_settings()
        self.bot_token = bot_token or self.settings.bot_token
        self.session_name = session_name or self.settings.music_session_name
        self.pyrogram: Optional[Client] = None
        self.pytgcalls: Optional[PyTgCalls] = None
        self._handlers: list[tuple[Any, int]] = []
        self._stream_end_callback: Optional[Callable] = None
        self._running = False

    async def initialize(self) -> None:
        """Start the Pyrogram session and PyTgCalls. Requires
        MUSIC_API_ID/MUSIC_API_HASH — callers should check
        `settings.live_music_configured` before calling this."""
        if not self.settings.live_music_configured:
            raise RuntimeError(
                "MusicClient.initialize() called without MUSIC_API_ID/MUSIC_API_HASH set."
            )

        logger.info("music_client_initializing", session=self.session_name)

        self.pyrogram = Client(
            name=self.session_name,
            api_id=self.settings.music_api_id,
            api_hash=self.settings.music_api_hash.get_secret_value(),
            bot_token=self.bot_token,
            workers=self.settings.music_workers,
            max_concurrent_transmissions=self.settings.music_max_connections,
            workdir=str(self.settings.sessions_dir),
        )

        self.pytgcalls = PyTgCalls(self.pyrogram)

        for handler, group in self._handlers:
            self.pyrogram.add_handler(handler, group)

        await self.pyrogram.start()
        await self.pytgcalls.start()

        # Auto-advance the queue when a track finishes. PyTgCalls versions
        # differ in exactly how they expose this event; this targets the
        # 1.x `py-tgcalls` line pinned in requirements.txt. If you upgrade
        # pytgcalls, re-check this against its current changelog.
        if hasattr(self.pytgcalls, "on_stream_end"):
            self.pytgcalls.on_stream_end()(self._on_stream_end)

        self._running = True
        me = await self.pyrogram.get_me()
        logger.info("music_client_ready", username=me.username, id=me.id)

    async def _on_stream_end(self, _client, update) -> None:
        chat_id = getattr(update, "chat_id", None)
        if chat_id is not None and self._stream_end_callback:
            try:
                await self._stream_end_callback(chat_id)
            except Exception:
                logger.error("stream_end_callback_failed", chat_id=chat_id, exc_info=True)

    def on_stream_end(self, callback: Callable) -> None:
        """Register a single `async def callback(chat_id)` to run whenever
        playback naturally finishes (used by music_live.py to auto-advance
        the queue)."""
        self._stream_end_callback = callback

    async def shutdown(self) -> None:
        logger.info("music_client_shutting_down")
        self._running = False

        if self.pytgcalls:
            try:
                for chat_id in list(getattr(self.pytgcalls, "active_calls", []) or []):
                    leave = getattr(self.pytgcalls, 'leave_call', None) or getattr(self.pytgcalls, 'leave_group_call', None)
                    if leave:
                        await leave(chat_id)
            except Exception:
                pass
            try:
                await self.pytgcalls.stop()
            except Exception:
                pass

        if self.pyrogram:
            await self.pyrogram.stop()

        logger.info("music_client_shutdown_complete")

    def add_handler(self, handler: Any, group: int = 0) -> None:
        if self.pyrogram:
            self.pyrogram.add_handler(handler, group)
        else:
            self._handlers.append((handler, group))

    def on_message(self, filters_obj: Any = None, group: int = 0) -> Any:
        def decorator(func: Any) -> Any:
            self.add_handler(MessageHandler(func, filters_obj or filters.all), group)
            return func
        return decorator

    def on_callback(self, group: int = 0) -> Any:
        def decorator(func: Any) -> Any:
            self.add_handler(CallbackQueryHandler(func), group)
            return func
        return decorator

    @property
    def is_running(self) -> bool:
        return self._running


_client: Optional[MusicClient] = None


def get_music_client() -> MusicClient:
    """Get or create the global (primary) MusicClient singleton."""
    global _client
    if _client is None:
        _client = MusicClient()
    return _client


class AssistantPool:
    """Coordinates the primary assistant plus any extras from
    MUSIC_EXTRA_BOT_TOKENS (see bot/config.py for why these are extra
    *bot* accounts rather than YukkiMusicBot's user-account string
    sessions). One AudioEngine per assistant; chats are assigned to
    whichever assistant currently has the fewest active calls, and stick
    with that assistant for the rest of the session so /skip, /volume
    etc. always land on the right connection.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.pairs: list[tuple[MusicClient, Any]] = []  # (MusicClient, AudioEngine)
        self._assignment: dict[int, int] = {}  # chat_id -> index into self.pairs
        self._last_activity: dict[int, float] = {}  # chat_id -> unix time, for auto-leave

    async def initialize(self) -> None:
        from bot.services.audio_engine import AudioEngine

        tokens = [self.settings.bot_token, *self.settings.music_extra_bot_tokens]
        for i, token in enumerate(tokens):
            session_name = self.settings.music_session_name if i == 0 else f"{self.settings.music_session_name}_{i + 1}"
            client = MusicClient(bot_token=token, session_name=session_name)
            try:
                await client.initialize()
            except Exception:
                logger.error("assistant_init_failed", index=i, exc_info=True)
                if i == 0:
                    raise  # the primary assistant failing is fatal for live music
                continue  # an extra assistant failing just means less scaling, not fatal
            engine = AudioEngine(client.pytgcalls)
            self.pairs.append((client, engine))

        logger.info("assistant_pool_ready", assistants=len(self.pairs))

    def on_stream_end(self, callback: Callable) -> None:
        for client, _ in self.pairs:
            client.on_stream_end(callback)

    def _pick_assistant_index(self) -> int:
        """Least-loaded: whichever assistant currently has the fewest
        assigned chats. Falls back to index 0 if the pool is somehow
        empty (shouldn't happen — initialize() always keeps the primary)."""
        if not self.pairs:
            return 0
        load = [0] * len(self.pairs)
        for idx in self._assignment.values():
            if idx < len(load):
                load[idx] += 1
        return load.index(min(load))

    def get_engine(self, chat_id: int):
        """Returns the AudioEngine assigned to this chat, assigning one
        (least-loaded) on first use."""
        import time

        self._last_activity[chat_id] = time.time()
        if not self.pairs:
            return None
        if chat_id not in self._assignment:
            self._assignment[chat_id] = self._pick_assistant_index()
        idx = min(self._assignment[chat_id], len(self.pairs) - 1)
        return self.pairs[idx][1]

    def release(self, chat_id: int) -> None:
        """Frees up a chat's assistant slot — call when playback fully
        stops so the next /play can rebalance onto whichever assistant
        is least loaded at that time."""
        self._assignment.pop(chat_id, None)
        self._last_activity.pop(chat_id, None)

    async def sweep_idle(self) -> int:
        """Stops playback for any chat idle longer than
        ASSISTANT_AUTO_LEAVE_SECONDS. Returns how many were swept.
        Called periodically from core/bot.py; no-op if the setting is 0."""
        import time

        if not self.settings.assistant_auto_leave_seconds:
            return 0
        cutoff = time.time() - self.settings.assistant_auto_leave_seconds
        stale = [cid for cid, ts in self._last_activity.items() if ts < cutoff]
        for chat_id in stale:
            engine = self.pairs[self._assignment.get(chat_id, 0)][1] if self.pairs else None
            if engine and not engine.is_active(chat_id):
                self.release(chat_id)
        return len(stale)

    def is_active(self, chat_id: int) -> bool:
        if chat_id not in self._assignment or not self.pairs:
            return False
        return self.pairs[self._assignment[chat_id]][1].is_active(chat_id)

    @property
    def total_active_calls(self) -> int:
        return len(self._assignment)

    @property
    def active_chats(self) -> dict[int, int]:
        """chat_id -> assistant index, for admin visibility (/activevc)."""
        return dict(self._assignment)

    async def shutdown(self) -> None:
        for client, _ in self.pairs:
            await client.shutdown()


_pool: Optional[AssistantPool] = None


def get_assistant_pool() -> AssistantPool:
    global _pool
    if _pool is None:
        _pool = AssistantPool()
    return _pool
