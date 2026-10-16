"""
SQLAlchemy 2.0 Async ORM — the single database backing every plugin:
group management, moderation, AI memory, the personality layer, font
preferences, and the live-music queue.

Defaults to a local SQLite file (aiosqlite) so the bot runs with zero
external services. Set DATABASE_URL to a postgresql+asyncpg:// URL for
production; connection-pool arguments are only applied for Postgres,
since SQLite doesn't support them.
"""
from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer,
    JSON, String, Text, UniqueConstraint, event, func,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.pool import NullPool

from bot.config import settings
from bot.utils.logger import get_logger
from bot.utils.retry import with_retry

logger = get_logger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = {"echo": settings.debug, "pool_pre_ping": not _is_sqlite}
if _is_sqlite:
    # SQLite allows only one writer at a time; without WAL + a busy
    # timeout, a second concurrent write doesn't queue — it fails
    # immediately with "database is locked" (visible under any real
    # concurrent load, e.g. an active group chat, not just tests).
    # Postgres isn't affected: it handles concurrent writers natively.
    _engine_kwargs["connect_args"] = {"timeout": 15}
    # NullPool: open a fresh connection per checkout instead of pooling
    # (cheap for a local SQLite file, unlike a networked Postgres). This
    # avoids aiosqlite's pooled-connection-tied-to-the-event-loop-that-
    # created-it problem, which otherwise surfaces as a spurious
    # "bound to a different event loop" error any time the engine
    # outlives the loop that first used it (e.g. across test cases, or
    # a reconnect cycle).
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs.update(pool_size=20, max_overflow=10)

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ─── Users & Chats ──────────────────────────────────────────────────
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    username = Column(String(32), index=True)
    first_name = Column(String(64))
    last_name = Column(String(64))
    language = Column(String(5), default="en")
    is_bot = Column(Boolean, default=False)
    warns = Column(Integer, default=0)
    karma = Column(Integer, default=0)
    afk_reason = Column(Text, nullable=True)
    afk_since = Column(DateTime, nullable=True)
    notes = Column(JSON, default=dict)
    # Name/username change history — [{"field", "old", "new", "at"}, ...],
    # newest last. Populated by bot/middleware/name_history.py.
    name_history = Column(JSON, default=list)

    # AI memory (the /ai /chat /code /summarize plugin)
    ai_persona = Column(Text, nullable=True)
    ai_history = Column(JSON, default=list)

    # Font plugin (formerly font_bot_ultimate.py)
    default_font = Column(String(8), nullable=True)
    font_auto_delete = Column(Boolean, default=False)
    font_show_preview = Column(Boolean, default=True)


class Chat(Base, TimestampMixin):
    __tablename__ = "chats"

    id = Column(BigInteger, primary_key=True)
    title = Column(String(128))
    type = Column(String(20))  # private, group, supergroup, channel
    username = Column(String(32), nullable=True)

    # Settings
    welcome_enabled = Column(Boolean, default=True)
    welcome_text = Column(Text, nullable=True)
    welcome_media = Column(String(512), nullable=True)
    goodbye_enabled = Column(Boolean, default=False)
    goodbye_text = Column(Text, nullable=True)

    # Security
    antiflood_enabled = Column(Boolean, default=True)
    antiflood_limit = Column(Integer, default=5)
    captcha_enabled = Column(Boolean, default=False)
    captcha_mode = Column(String(20), default="button")  # button, math, image
    reactions_enabled = Column(Boolean, default=False)

    # Locks
    lock_url = Column(Boolean, default=False)
    lock_forward = Column(Boolean, default=False)
    lock_photo = Column(Boolean, default=False)
    lock_video = Column(Boolean, default=False)
    lock_sticker = Column(Boolean, default=False)
    lock_gif = Column(Boolean, default=False)
    lock_contact = Column(Boolean, default=False)
    lock_location = Column(Boolean, default=False)

    # Filters
    blacklist_words = Column(JSON, default=list)
    blacklist_action = Column(String(20), default="warn")  # warn, mute, kick, ban

    # Logging
    log_channel = Column(BigInteger, nullable=True)

    # Nightmode
    nightmode_enabled = Column(Boolean, default=False)
    nightmode_start = Column(String(5), default="23:00")
    nightmode_end = Column(String(5), default="06:00")
    nightmode_lock = Column(Boolean, default=True)

    # Federation
    federation_id = Column(String(36), ForeignKey("federations.id"), nullable=True)
    fed_admin = Column(Boolean, default=False)
    # v2.16: enforce gbans and fedbans when a banned user joins or speaks (None = on). /banenforce
    ban_enforce = Column(Boolean, nullable=True)
    # v2.16: sticker-pack blacklist action (plugins/warnfilters.py). None = delete only.
    clean_blue = Column(Boolean, nullable=True)                  # /cleanblue: delete unknown "blue text" commands (None = off)
    welcome_mute = Column(String(10), nullable=True)             # /welcomemute: off | soft | strong (None = follow /captcha)
    name_alerts = Column(Boolean, nullable=True)                 # /namealert: announce name/username changes (None = off)
    blsticker_action = Column(String(10), nullable=True)
    blsticker_duration = Column(Integer, nullable=True)

    # Per-chat disabled commands (from FallenRobot/YaeMiko's /disable)
    disabled_commands = Column(JSON, default=list)

    # Misc
    rules = Column(Text, nullable=True)
    filters = Column(JSON, default=dict)  # trigger -> response
    pinned_message = Column(BigInteger, nullable=True)

    # Personality layer (formerly nanora_bot) — tracked per-conversation,
    # same as the original standalone bot tracked it per chat_id.
    personality_enabled = Column(Boolean, nullable=True)  # None = use settings default
    personality_interactions = Column(Integer, default=0)
    personality_running_gags = Column(JSON, default=list)
    # Free-text persona description parsed by custom_instructions.py
    # (e.g. "be sarcastic and obsessed with pineapple pizza").
    custom_instructions = Column(Text, nullable=True)
    # Named response-pool skin (see data/personality/skins/*.json).
    # None = settings.personality_default_skin.
    personality_skin = Column(String(32), nullable=True)
    # Parsed CustomPersonalityTraits, cached as JSON so it survives a
    # restart without re-parsing custom_instructions every time.
    personality_custom_config = Column(JSON, nullable=True)
    # Per-chat override of settings.personality_user_limit_default.
    personality_user_limit = Column(Integer, nullable=True)
    # Per-chat override of settings.personality_response_delay_default,
    # in seconds.
    personality_response_delay = Column(Float, nullable=True)

    # Auto-moderation
    warn_limit = Column(Integer, nullable=True)  # None = use settings default_warn_limit
    warn_action = Column(String(10), nullable=True)  # None = use settings default_warn_action
    antiraid_enabled = Column(Boolean, default=False)
    antiraid_join_threshold = Column(Integer, nullable=True)
    antiraid_join_window = Column(Integer, nullable=True)
    lockdown_until = Column(DateTime, nullable=True)
    antichannel_enabled = Column(Boolean, default=False)  # /antichannel: delete+ban channel-identity posts

    # v2.12 — per-chat settings that used to be global-only or schema-only.
    # All are nullable on purpose: _auto_migrate adds them to old databases as NULL, and
    # NULL always means "use the default" (see bot/utils/modtools.py::flag).
    antiflood_action = Column(String(10), nullable=True)          # mute|kick|ban|tmute|tban (None = legacy temp-ban)
    antiflood_action_seconds = Column(Integer, nullable=True)     # duration for tmute/tban (None = settings default)
    antispam_enabled = Column(Boolean, nullable=True)             # None = on (the heuristic engine was always on)
    slowmode_seconds = Column(Integer, nullable=True)             # /slowmode: bot-enforced gap between a member's messages
    blacklist_duration = Column(Integer, nullable=True)           # seconds, for blacklist_action tmute/tban
    reports_enabled = Column(Boolean, nullable=True)              # None = on
    connect_allowed = Column(Boolean, nullable=True)              # None = on: may admins /connect to this chat from DM
    private_rules = Column(Boolean, nullable=True)                # /privaterules: /rules answers in DM
    clean_welcome = Column(Boolean, nullable=True)                # delete the previous welcome when a new one is sent
    last_welcome_id = Column(BigInteger, nullable=True)
    clean_service = Column(Boolean, nullable=True)                # delete join/leave/pin service messages
    nightmode_active = Column(Boolean, nullable=True)             # True while the night restriction is applied
    nightmode_prev_permissions = Column(JSON, nullable=True)      # chat default permissions to restore at dawn

    # Bot-wide access control (from YukkiMusicBot's blacklist/private-mode)
    # bot_blacklisted: this specific chat is refused entirely, regardless
    # of PRIVATE_BOT_MODE. private_mode_authorized: when the *global*
    # settings.private_bot_mode is on, only chats with this set to True
    # get served — distinct from bot_blacklisted, which is a deny-list
    # that applies either way.
    bot_blacklisted = Column(Boolean, default=False)
    private_mode_authorized = Column(Boolean, default=False)

    # Play preferences (from YukkiMusicBot's /playmode)
    playmode = Column(String(10), default="direct")  # direct | inline
    play_admin_only = Column(Boolean, default=False)  # only admins may /play
    cleanmode_enabled = Column(Boolean, default=False)
    video_enabled = Column(Boolean, default=False)  # opt-in: /play sends video, not just audio
    # Off by default so existing chats see no behavior change — once on,
    # /skip /stop /pause /resume /shuffle /repeat /effects /volume need
    # a chat admin or an explicit /auth user (see plugins/music_live.py).
    restrict_music_controls = Column(Boolean, default=False)

    # Character-collector game (from anime_collector_bot / anime_catcher_bot)
    # — how many chat messages between ambient spawns. Per-chat since
    # activity level varies enormously between groups.
    collector_spawn_rate = Column(Integer, nullable=True)  # None = settings default
    language = Column(String(8), nullable=True)  # /lang: UI/AI reply language of this chat; None = follow each user's Telegram language


class ChatMember(Base, TimestampMixin):
    __tablename__ = "chat_members"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    role = Column(String(20), default="member")  # member, admin, owner
    custom_title = Column(String(128), nullable=True)
    until_date = Column(DateTime, nullable=True)

    # Economy & leveling — per-chat by design (a user's level/coins in one
    # group shouldn't leak into another the way a global column would).
    xp = Column(Integer, default=0)
    level = Column(Integer, default=0)
    coins = Column(Integer, default=100)
    last_xp_at = Column(DateTime, nullable=True)
    last_daily_at = Column(DateTime, nullable=True)

    # Auth users (from YukkiMusicBot) — non-admins an admin has explicitly
    # allowed to control playback (skip/pause/etc.) without full admin
    # rights. Orthogonal to `role`, since an auth user is still a member.
    is_auth_user = Column(Boolean, default=False)

    # Personality layer — messages received within the current reset
    # window, for the per-user reply limit (see memory.py:increment_and_check).
    personality_msg_count = Column(Integer, default=0)
    personality_msg_count_reset_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_chat_user"),
        Index("idx_chatmember_chat_xp", "chat_id", "xp"),
    )


# ─── Moderation ─────────────────────────────────────────────────────
class Warn(Base, TimestampMixin):
    __tablename__ = "warns"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    reason = Column(Text)
    warned_by = Column(BigInteger)

    __table_args__ = (Index("idx_warns_chat_user", "chat_id", "user_id"),)


class Ban(Base, TimestampMixin):
    __tablename__ = "bans"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    reason = Column(Text)
    banned_by = Column(BigInteger)
    until = Column(DateTime, nullable=True)
    is_fed_ban = Column(Boolean, default=False)


class Mute(Base, TimestampMixin):
    __tablename__ = "mutes"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    until = Column(DateTime)
    reason = Column(Text)


class Report(Base, TimestampMixin):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger)
    message_id = Column(BigInteger)
    reporter_id = Column(BigInteger)
    reported_user_id = Column(BigInteger)
    reason = Column(Text)
    resolved = Column(Boolean, default=False)


# ─── Federations ────────────────────────────────────────────────────
class Federation(Base, TimestampMixin):
    __tablename__ = "federations"

    id = Column(String(36), primary_key=True)
    name = Column(String(64))
    owner_id = Column(BigInteger, ForeignKey("users.id"))
    admins = Column(JSON, default=list)
    log_channel = Column(BigInteger, nullable=True)
    # v2.16 (from the Saitama/Masha feds module) — all nullable so _auto_migrate can add them to old databases:
    rules = Column(Text, nullable=True)                 # /setfrules
    notify = Column(Boolean, nullable=True)             # /fednotif: None = on
    subscribed = Column(JSON, nullable=True)            # /subfed: ids of federations whose ban lists this one also enforces
    bans = relationship("FedBan", back_populates="federation", cascade="all, delete-orphan")


class FedBan(Base, TimestampMixin):
    __tablename__ = "fed_bans"

    id = Column(Integer, primary_key=True)
    federation_id = Column(String(36), ForeignKey("federations.id", ondelete="CASCADE"))
    user_id = Column(BigInteger)
    reason = Column(Text)
    banned_by = Column(BigInteger)

    federation = relationship("Federation", back_populates="bans")


# ─── Warn filters & sticker blacklist (v2.16, from the Saitama/Masha warns + blacklist_stickers modules) ───
class WarnFilter(Base, TimestampMixin):
    __tablename__ = "warn_filters"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    keyword = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("chat_id", "keyword", name="uq_warn_filter"),)


class StickerBlacklist(Base, TimestampMixin):
    __tablename__ = "sticker_blacklist"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    trigger = Column(String(64), nullable=False)   # sticker-pack name (lower-case); '*' is a wildcard

    __table_args__ = (UniqueConstraint("chat_id", "trigger", name="uq_sticker_blacklist"),)


class RssFeed(Base, TimestampMixin):
    """A feed a chat follows (/addrss). ``last_entry`` is the newest entry id already delivered."""
    __tablename__ = "rss_feeds"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    url = Column(String(500), nullable=False)
    last_entry = Column(String(500), nullable=True)
    failures = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint("chat_id", "url", name="uq_rss_feed"),)


class PollRecord(Base, TimestampMixin):
    """A poll created with the Masha-style /apoll (5-digit id), kept so /stoppoll can close it and /forgotpollid can reset it."""
    __tablename__ = "poll_records"

    id = Column(Integer, primary_key=True)
    poll_code = Column(String(5), nullable=False, index=True)
    creator_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)


class BlueIgnore(Base, TimestampMixin):
    """Commands the blue-text cleaner leaves alone. chat_id 0 = global (/gignoreblue)."""
    __tablename__ = "blue_ignore"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    command = Column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("chat_id", "command", name="uq_blue_ignore"),)


class VerifiedMember(Base, TimestampMixin):
    """Members who already passed the /welcomemute strong check in a chat (not challenged again on rejoin)."""
    __tablename__ = "verified_members"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_verified_member"),)


class PendingVerification(Base, TimestampMixin):
    """A muted newcomer waiting to press the verify button; swept by a job so restarts never strand anyone."""
    __tablename__ = "pending_verifications"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=True)
    deadline = Column(DateTime, nullable=False, index=True)

    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_pending_verification"),)


# ─── Anti-Spam & Analytics ──────────────────────────────────────────
class SpamLog(Base, TimestampMixin):
    __tablename__ = "spam_logs"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger)
    user_id = Column(BigInteger)
    message_text = Column(Text, nullable=True)
    confidence = Column(Float)
    action_taken = Column(String(20))
    features = Column(JSON, default=dict)


class MessageStat(Base, TimestampMixin):
    __tablename__ = "message_stats"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    date = Column(DateTime, server_default=func.now())
    count = Column(Integer, default=1)

    __table_args__ = (Index("idx_stats_chat_date", "chat_id", "date"),)


# ─── Notes & Tags ───────────────────────────────────────────────────
class Note(Base, TimestampMixin):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    name = Column(String(64), index=True)
    content = Column(Text)
    file_id = Column(String(512), nullable=True)
    message_type = Column(String(20), default="text")  # text, photo, video, audio, document
    created_by = Column(BigInteger)

    # v2.12: /save used to INSERT a second row for an existing name, after which /get crashed with
    # MultipleResultsFound. One row per (chat, name) is now enforced (see _dedupe_notes for old data).
    __table_args__ = (Index("uq_notes_chat_name", "chat_id", "name", unique=True),)


# ─── AI Conversations (the LLM plugin's own history) ───────────────
class AIConversation(Base, TimestampMixin):
    __tablename__ = "ai_conversations"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    chat_id = Column(BigInteger, nullable=True)
    role = Column(String(20))  # user, assistant, system
    content = Column(Text)
    tokens = Column(Integer, default=0)
    model = Column(String(32))


# ─── Personality layer (formerly nanora_bot's own SQLite file) ─────
# Kept separate from AIConversation above: this backs the trigger/banter
# pipeline (intent tracking, running gags, joke callbacks), which is a
# different concern from the LLM plugin's conversation history.
class PersonalityMessage(Base, TimestampMixin):
    __tablename__ = "personality_messages"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    role = Column(String(10))  # user, bot
    content = Column(Text)
    intent = Column(String(32), nullable=True)


class PersonalityCallback(Base, TimestampMixin):
    __tablename__ = "personality_callbacks"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    joke_id = Column(String(32))
    context = Column(Text, nullable=True)

    __table_args__ = (
        # Speeds up "has this joke already been told in this chat
        # recently" lookups, which filter on all three columns together.
        Index("idx_pcb_chat_joke_time", "chat_id", "joke_id", "created_at"),
    )


# ─── Live music queue (formerly harmony's Mongo/Redis-backed queues) ─
# One row per chat; `data` is the JSON dump of a bot.models.queue.Queue.
# Replaces harmony's hard dependency on a separate MongoDB instance.
class MusicQueueState(Base, TimestampMixin):
    __tablename__ = "music_queues"

    chat_id = Column(BigInteger, primary_key=True)
    data = Column(JSON, default=dict)


# ─── Economy & Leveling ──────────────────────────────────────────────
class ShopItem(Base, TimestampMixin):
    __tablename__ = "shop_items"

    id = Column(Integer, primary_key=True)
    key = Column(String(32), unique=True)
    name = Column(String(64))
    description = Column(Text)
    price = Column(Integer)
    emoji = Column(String(8), default="🎁")


class UserInventory(Base, TimestampMixin):
    __tablename__ = "user_inventory"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    item_key = Column(String(32))
    quantity = Column(Integer, default=1)

    __table_args__ = (UniqueConstraint("user_id", "item_key", name="uq_user_item"),)


# ─── Reminders & Scheduling ─────────────────────────────────────────
class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    chat_id = Column(BigInteger)
    text = Column(Text)
    remind_at = Column(DateTime, index=True)
    fired = Column(Boolean, default=False)


class ScheduledMessage(Base, TimestampMixin):
    __tablename__ = "scheduled_messages"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    text = Column(Text)
    send_at = Column(DateTime, index=True)
    created_by = Column(BigInteger)
    sent = Column(Boolean, default=False)


# ─── Music Playlists ────────────────────────────────────────────────
class Playlist(Base, TimestampMixin):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True)
    owner_id = Column(BigInteger, index=True)
    name = Column(String(64))

    tracks = relationship(
        "PlaylistTrack", back_populates="playlist",
        cascade="all, delete-orphan", order_by="PlaylistTrack.position",
    )

    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_owner_playlist_name"),)


class PlaylistTrack(Base, TimestampMixin):
    __tablename__ = "playlist_tracks"

    id = Column(Integer, primary_key=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id", ondelete="CASCADE"))
    position = Column(Integer, default=0)
    track_data = Column(JSON)  # a bot.models.track.Track, JSON-dumped

    playlist = relationship("Playlist", back_populates="tracks")


# ─── Precise temp-mute/temp-ban expiry ──────────────────────────────
# Telegram's own `until_date` on restrict/ban calls handles the actual
# enforcement, but doesn't give us a queryable "what's currently muted"
# list or a way to react precisely when something expires. This table
# plus a JobQueue entry (re-armed on startup, see core/bot.py) covers
# both.
class TempAction(Base, TimestampMixin):
    __tablename__ = "temp_actions"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    action = Column(String(10))  # mute, ban
    reason = Column(Text, nullable=True)
    expires_at = Column(DateTime, index=True)
    reversed = Column(Boolean, default=False)

    __table_args__ = (Index("idx_tempaction_lookup", "chat_id", "action", "reversed", "expires_at"),)


# ─── Approvals & remote connections (v2.12; from FallenRobot / Gojo_Satoru) ─────────────
class Approval(Base, TimestampMixin):
    """A user an admin trusts: locks, the word blacklist, anti-flood and anti-spam skip them.
    first_name is stored at approval time so /approved needs no Telegram API call per row."""
    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    first_name = Column(String(128), nullable=True)
    approved_by = Column(BigInteger, nullable=True)

    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_approval_chat_user"),)


class Connection(Base, TimestampMixin):
    """Which group a user is currently managing from the bot's DM (/connect)."""
    __tablename__ = "connections"

    user_id = Column(BigInteger, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)


# ─── Dashboard heartbeat ─────────────────────────────────────────────
# The dashboard is a separate process (see dashboard/) with no direct
# access into the bot's memory, so the bot periodically writes a status
# snapshot here for it to read. See bot/plugins/automod.py's job and
# bot/core/bot.py's post_init.
class BotStats(Base, TimestampMixin):
    __tablename__ = "bot_stats"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=True)
    last_heartbeat = Column(DateTime, nullable=True)
    total_chats = Column(Integer, default=0)
    total_users = Column(Integer, default=0)
    live_music_active_chats = Column(Integer, default=0)
    extra = Column(JSON, default=dict)


# Runtime-toggleable global flags (from YukkiMusicBot's /maintenance and
# PRIVATE_BOT_MODE) — single row, id=1. Distinct from bot/config.py's
# Settings: those are read once from the environment at startup, these
# are meant to be flipped live by a sudoer command and persist across a
# restart without touching .env.
class BotConfig(Base, TimestampMixin):
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True)
    maintenance_mode = Column(Boolean, default=False)
    private_bot_mode = Column(Boolean, default=False)
    # Auto-leave a voice chat once no real participants are left (checked
    # via PyTgCalls.get_participants — distinct from
    # ASSISTANT_AUTO_LEAVE_SECONDS, which is about no *playback* activity
    # rather than whether anyone is actually listening). From AnonXMusic's
    # /autoend.
    autoend_enabled = Column(Boolean, default=False)


class PersonaOptOut(Base, TimestampMixin):
    """A registered person (bot/personality/persons.py) who asked not to be imitated.

    Keyed by person id, not Telegram account, so opting out on one account covers all of them.
    Presence of a row = opted out; /pconsent optin deletes it.
    """
    __tablename__ = "persona_optouts"

    person_id = Column(String(64), primary_key=True)
    by_user_id = Column(BigInteger, nullable=True)


async def get_bot_config(session) -> "BotConfig":
    row = await session.get(BotConfig, 1)
    if row is None:
        row = BotConfig(id=1)
        session.add(row)
        await session.flush()
    return row


async def _dedupe_notes(conn, existing_cols: "dict[str, set[str]]") -> None:
    """Keep only the newest row per (chat_id, name) so the unique index can be created.

    Older builds inserted a new row on every /save of an existing name; the newest row is the
    one the user last saved, so it is the one that survives."""
    from sqlalchemy import text

    if "notes" not in existing_cols or not {"chat_id", "name"} <= existing_cols["notes"]:
        return
    try:
        res = await conn.execute(text(
            "DELETE FROM notes WHERE id NOT IN (SELECT MAX(id) FROM notes GROUP BY chat_id, name)"
        ))
        if getattr(res, "rowcount", 0):
            logger.info("deduped_notes", removed=res.rowcount)
    except Exception as e:  # never block startup on a cleanup step
        logger.error("dedupe_notes_failed", error=str(e))


async def _auto_migrate() -> None:
    """Add any columns or indexes present on the ORM models but missing
    from an existing database (e.g. upgrading from an earlier version of
    NovaBot) without requiring a full migration tool. Only ever ADDS —
    never drops or alters anything — so it's safe to run on every
    startup. Brand-new tables are handled by create_all() before this
    runs; this covers changes to tables that already existed."""
    from sqlalchemy import inspect as sa_inspect, text

    def _existing_columns(sync_conn) -> dict[str, set[str]]:
        inspector = sa_inspect(sync_conn)
        return {
            name: {col["name"] for col in inspector.get_columns(name)}
            for name in inspector.get_table_names()
        }

    def _existing_index_names(sync_conn) -> dict[str, set[str]]:
        inspector = sa_inspect(sync_conn)
        return {
            name: {idx["name"] for idx in inspector.get_indexes(name)}
            for name in inspector.get_table_names()
        }

    async with engine.begin() as conn:
        existing_cols = await conn.run_sync(_existing_columns)

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_cols:
                continue  # brand-new table — create_all already made it
            have = existing_cols[table.name]
            for column in table.columns:
                if column.name in have:
                    continue
                try:
                    ddl_type = column.type.compile(dialect=engine.dialect)
                    await conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}')
                    )
                    logger.info("auto_migrated_column", table=table.name, column=column.name)
                except Exception as e:
                    logger.error(
                        "auto_migrate_column_failed", table=table.name, column=column.name, error=str(e)
                    )

        await _dedupe_notes(conn, existing_cols)

        existing_idx = await conn.run_sync(_existing_index_names)
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_idx:
                continue
            have = existing_idx[table.name]
            for index in table.indexes:
                if index.name in have:
                    continue
                try:
                    await conn.run_sync(lambda sync_conn, idx=index: idx.create(sync_conn))
                    logger.info("auto_migrated_index", table=table.name, index=index.name)
                except Exception as e:
                    logger.error(
                        "auto_migrate_index_failed", table=table.name, index=index.name, error=str(e)
                    )


# ─── Bot-wide user moderation (from AnonXMusic) ─────────────────────
# Two deliberately distinct mechanisms:
#   GlobalBan (/gban)  — actually removes the user from every chat
#                          NovaBot moderates. A real, visible action.
#   BlockedUser (/block) — NovaBot just stops responding to them
#                          everywhere; no effect on their chat
#                          memberships. Lighter-weight, reversible,
#                          no collateral notification to the group.
class GlobalBan(Base, TimestampMixin):
    __tablename__ = "global_bans"

    user_id = Column(BigInteger, primary_key=True)
    reason = Column(Text, nullable=True)
    banned_by = Column(BigInteger)


class BlockedUser(Base, TimestampMixin):
    __tablename__ = "blocked_users"

    user_id = Column(BigInteger, primary_key=True)
    reason = Column(Text, nullable=True)
    blocked_by = Column(BigInteger)


# ─── Track play counts (from AnonXMusic's /toptracks) ───────────────
class TrackPlay(Base, TimestampMixin):
    __tablename__ = "track_plays"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, nullable=True)
    title = Column(String(256))
    artist = Column(String(128), nullable=True)
    source_url = Column(String(512), nullable=True)

    __table_args__ = (Index("idx_trackplay_chat_title", "chat_id", "title"),)


# ─── Character-collector game (from anime_collector_bot family +
# WAIFU-HUSBANDO-CATCHER/shivu) ──────────────────────────────────────
# Characters are shared bot-wide (uploaded once, catchable in every
# chat), same scope as CollectorModerator below — this is a bot-wide
# content-curation role, distinct from being an admin of any one chat.
class Character(Base, TimestampMixin):
    __tablename__ = "characters"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), index=True)
    anime = Column(String(128))
    rarity = Column(String(16), default="common")  # common, uncommon, rare, epic, legendary, divine
    image_url = Column(String(512))
    added_by = Column(BigInteger, nullable=True)


class UserCharacter(Base, TimestampMixin):
    """One row per character a user has caught. A user can catch the
    same Character more than once (duplicates are common in this genre —
    tradeable, and some designs let you feed dupes back for currency),
    so this is NOT unique on (user_id, character_id)."""
    __tablename__ = "user_characters"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    character_id = Column(Integer, ForeignKey("characters.id", ondelete="CASCADE"))
    is_favorite = Column(Boolean, default=False)

    __table_args__ = (Index("idx_userchar_user_char", "user_id", "character_id"),)


class CollectorModerator(Base, TimestampMixin):
    __tablename__ = "collector_moderators"

    user_id = Column(BigInteger, primary_key=True)
    added_by = Column(BigInteger)


class Giveaway(Base, TimestampMixin):
    __tablename__ = "giveaways"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    prize_coins = Column(Integer, default=0)
    character_id = Column(Integer, ForeignKey("characters.id"), nullable=True)
    created_by = Column(BigInteger)
    ends_at = Column(DateTime)
    claimed_by = Column(BigInteger, nullable=True)
    ended = Column(Boolean, default=False)


# ─── Activity rankings (ported from ChatFight) ──────────────────────
class ActivityStat(Base, TimestampMixin):
    """Per-(chat, member) message counters behind /rankings, /topusers, /userstats, /mytop.

    ``day_count`` / ``week_count`` belong to ``day_key`` / ``week_key``. A reader treats a
    counter whose key is not the current one as 0, and a writer resets it in the same
    UPDATE, so nothing has to run at midnight and a missed job can never leave stale totals.
    """

    __tablename__ = "activity_stats"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    name = Column(String(256))
    username = Column(String(64))
    username_lc = Column(String(64))
    chat_title = Column(String(256))
    overall = Column(BigInteger, nullable=False, default=0)
    day_key = Column(String(10))
    day_count = Column(Integer, nullable=False, default=0)
    week_key = Column(String(10))
    week_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_activity_chat_user"),
        Index("idx_activity_chat_overall", "chat_id", "overall"),
        Index("idx_activity_user_overall", "user_id", "overall"),
        Index("idx_activity_chat_username", "chat_id", "username_lc"),
    )


# ─── Whispers (ported from PsstRobot) ───────────────────────────────
class Whisper(Base):
    """One pending whisper. Deleted when first read by someone other than its sender, when
    its sender cancels it, or when it is older than WHISPER_MAX_EXPIRY_DAYS."""

    __tablename__ = "whispers"

    unique_id = Column(String(48), primary_key=True)
    inline_message_id = Column(String(255), nullable=False, default="")
    sender = Column(BigInteger, nullable=False, index=True)
    recipient = Column(BigInteger, nullable=False, default=0)  # 0 = nobody by id
    recipient_username = Column(String(64), nullable=False, default="")  # "@name" or ""
    text = Column(Text, nullable=False, default="")  # text, or the caption of single media
    file_id = Column(Text, nullable=False, default="")  # single media file_id / dice emoji
    media_json = Column(Text)  # album: JSON list of {"type", "file_id", "caption"}
    type = Column(Integer, nullable=False, default=1)
    created_ts = Column(BigInteger, nullable=False, index=True)  # unix seconds (dialect independent)


class WhisperUser(Base):
    """Per-user whisper preferences. Absent row == all defaults."""

    __tablename__ = "whisper_users"

    user_id = Column(BigInteger, primary_key=True)
    banned = Column(Boolean, nullable=False, default=False)
    privacy_mode = Column(Boolean, nullable=False, default=False)
    history_disabled = Column(Boolean, nullable=False, default=False)
    notify_on_read = Column(Boolean, nullable=False, default=False)


class WhisperHistory(Base):
    """The last few distinct people who read a user's whispers (one-tap recipients)."""

    __tablename__ = "whisper_history"

    id = Column(Integer, primary_key=True)
    owner_id = Column(BigInteger, nullable=False)
    target_id = Column(BigInteger, nullable=False)
    target_name = Column(String(64), nullable=False, default="")
    last_used_ts = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("owner_id", "target_id", name="uq_whisper_history_pair"),
        Index("idx_whisper_history_owner", "owner_id", "last_used_ts"),
    )


async def init_db() -> None:
    """Create tables and run migrations, retrying the initial connection.

    In container deployments the DB service can still be coming up when
    the bot starts (no compose healthcheck ordering guarantee) — retry
    with backoff instead of crashing on the very first attempt.
    """
    @with_retry(
        attempts=5, min_wait=1.0, max_wait=15.0,
        exceptions=(OperationalError, ConnectionError, TimeoutError, OSError),
    )
    async def _connect_and_create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    await _connect_and_create()
    await _auto_migrate()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
