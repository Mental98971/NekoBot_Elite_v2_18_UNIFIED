"""Shared moderation core (v2.12).

Before v2.12 every moderation command re-implemented its own target lookup, admin check, duration
handling and error reporting, and they disagreed. The visible defects that this module removes at
the root (see docs/CHANGELOG.md, "Moderation core"):

* ``until_date`` was built from a *naive UTC* ``datetime.utcnow().timestamp()``, which Python
  interprets as local time. On any host east of UTC (e.g. IST, UTC+5:30) a ``/tmute 2h`` ended
  3.5 hours early and a ``/tmute 30m`` landed in the past - which Telegram treats as *permanent*.
  Durations are now ``time.time() + seconds``: no time zone is involved.
* Telegram also treats any restriction shorter than 30 s or longer than 366 days as permanent;
  ``/tmute 10s`` therefore silently became a permanent mute. Such durations are now rejected.
* Commands only checked "is the caller an admin", not "may the caller restrict members", and never
  refused to act on admins, the owner or the bot itself.
* ``/unmute`` granted ``can_change_info`` / ``can_pin_messages`` (a privilege escalation); it now
  restores the chat's own default permissions.
* ``/unban <id>`` crashed (it awaited an attribute of an un-awaited coroutine).
* Replying to a message inside a forum topic "replied" to the topic's creator.
* Raw Telegram error text was echoed to users; it is now mapped to actionable messages.

Everything here is I/O through ``context.bot`` and the shared database, so it is unit-testable
with fakes (tests/test_modtools.py).
"""
from __future__ import annotations

import functools
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select
from telegram import ChatPermissions, MessageEntity, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import settings
from bot.core.database import Ban, Chat, Connection, TempAction, User, Warn, async_session
from bot.core.errors import safe_handler
from bot.utils import chatcache
from bot.utils.helpers import escape_html, mention_html, time_parser
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# Telegram "users" that are not people: never valid moderation targets.
ANON_ADMIN_BOT_ID = 1087968824   # @GroupAnonymousBot: what an anonymous admin's messages are sent as
CHANNEL_BOT_ID = 136817688       # @Channel_Bot: legacy identity of messages sent as a channel
TELEGRAM_SERVICE_ID = 777000     # Telegram's own account (linked-channel auto-forwards)
NON_USER_IDS = frozenset({ANON_ADMIN_BOT_ID, CHANNEL_BOT_ID, TELEGRAM_SERVICE_ID})

MIN_TEMP_SECONDS = 30            # Telegram: restrictions < 30 s are treated as forever
MAX_TEMP_SECONDS = 366 * 86400   # ... and so are those > 366 days
REASON_MAX = 500
ACTIONS = ("nothing", "delete", "warn", "mute", "kick", "ban", "tmute", "tban")
TEMP_ACTIONS = ("tmute", "tban")
DEFAULT_TEMP_SECONDS = 3600      # tmute/tban configured without an explicit duration

RIGHT_FIELDS = {
    "restrict": "can_restrict_members",
    "delete": "can_delete_messages",
    "promote": "can_promote_members",
    "pin": "can_pin_messages",
    "info": "can_change_info",
    "invite": "can_invite_users",
}
RIGHT_LABELS = {
    "restrict": "Ban users",
    "delete": "Delete messages",
    "promote": "Add new admins",
    "pin": "Pin messages",
    "info": "Change group info",
    "invite": "Invite users via link",
}


# ── time ─────────────────────────────────────────────────────────────────────
def utcnow() -> datetime:
    """Naive-UTC "now" (the database stores naive UTC); avoids the deprecated ``datetime.utcnow()``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def epoch_after(seconds: int, *, now: Optional[float] = None) -> int:
    """Unix timestamp ``seconds`` from now - independent of the host's time zone."""
    return int((time.time() if now is None else now) + seconds)


class DurationError(ValueError):
    """A duration Telegram would silently turn into a permanent restriction."""


def parse_duration(token: Optional[str]) -> Optional[int]:
    """``'30m'`` -> 1800; ``None`` when the token is not a duration (``30s 15m 2h 1d 1w``)."""
    return time_parser(token) if token else None


def validate_duration(seconds: int) -> int:
    if seconds < MIN_TEMP_SECONDS:
        raise DurationError(f"The shortest time I can use is {MIN_TEMP_SECONDS}s - Telegram treats anything shorter as permanent.")
    if seconds > MAX_TEMP_SECONDS:
        raise DurationError("The longest time is 366d - leave the time out for a permanent action.")
    return seconds


def format_span(seconds: int) -> str:
    """``5400`` -> ``'1h 30m'`` (at most two units, unlike ``helpers.format_duration`` which truncates)."""
    seconds = max(0, int(seconds))
    parts = []
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
        if len(parts) == 2:
            break
    return " ".join(parts) or "0s"


def clean_reason(words: List[str], default: str = "No reason given") -> str:
    text = " ".join(words).strip()
    return (text[:REASON_MAX] or default)


# ── errors ───────────────────────────────────────────────────────────────────
_ERROR_HINTS: Tuple[Tuple[str, str], ...] = (
    ("can't remove chat owner", "That's the chat owner - nobody can do that to them."),
    ("user is an administrator", "That user is an admin - demote them first."),
    ("can't demote chat creator", "That's the chat owner - they can't be demoted."),
    ("not enough rights", "I don't have the admin right that needs. Grant it in the group's admin settings."),
    ("chat_admin_required", "I need to be an admin with the right permission to do that."),
    ("need administrator rights", "I need to be an admin with the right permission to do that."),
    ("user_not_participant", "That user isn't in this chat."),
    ("participant_id_invalid", "I can't find that user."),
    ("user_id_invalid", "I can't find that user."),
    ("user not found", "I can't find that user."),
    ("peer_id_invalid", "I can't find that user or chat."),
    ("chat not found", "I can't see that chat - is the bot a member?"),
    ("bot was kicked", "I was removed from that chat."),
    ("method is available for supergroup", "That only works in supergroups."),
    ("method is available only for supergroups", "That only works in supergroups."),
    ("message to delete not found", "That message is already gone."),
    ("message can't be deleted", "I can't delete that message (too old, or I lack the right)."),
    ("message to pin not found", "I can't find that message to pin."),
    ("not modified", "Nothing changed - it was already like that."),
    ("administrator_rank_invalid", "That title isn't valid (max 16 characters, no emoji)."),
    ("user_admin_invalid", "I can't change that admin - they weren't promoted by me."),
    ("rights_not_modified", "Their rights were already like that."),
    ("forbidden", "Telegram won't let me do that here."),
)


def friendly_telegram_error(exc: BaseException) -> str:
    text = str(exc).lower()
    for needle, message in _ERROR_HINTS:
        if needle in text:
            return message
    logger.warning("telegram_request_failed", error=str(exc), error_type=type(exc).__name__)
    return "Telegram refused that request."


# ── admin / rights ───────────────────────────────────────────────────────────
ADMIN_CACHE_TTL = 45.0
_admin_cache: Dict[Tuple[int, int], Tuple[bool, float]] = {}


def invalidate_admin(chat_id: int, user_id: Optional[int] = None) -> None:
    if user_id is not None:
        _admin_cache.pop((chat_id, user_id), None)
        return
    for key in [k for k in _admin_cache if k[0] == chat_id]:
        _admin_cache.pop(key, None)


async def get_member(bot, chat_id: int, user_id: int):
    """``bot.get_chat_member`` that returns ``None`` instead of raising."""
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except TelegramError as exc:
        logger.debug("get_chat_member_failed", chat_id=chat_id, user_id=user_id, error=str(exc))
        return None


def member_has_right(member, right: Optional[str]) -> bool:
    """Owner: always. Admin: the specific right (``None`` = any admin). Everyone else: no."""
    if member is None:
        return False
    if member.status == ChatMemberStatus.OWNER:
        return True
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return True if right is None else bool(getattr(member, RIGHT_FIELDS[right], False))


async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    """Cached admin test for passive (per-message) checks; failures are cached only briefly."""
    key = (chat_id, user_id)
    now = time.monotonic()
    hit = _admin_cache.get(key)
    if hit is not None and hit[1] > now:
        return hit[0]
    member = await get_member(bot, chat_id, user_id)
    verdict = member is not None and member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    ttl = ADMIN_CACHE_TTL if member is not None else 5.0
    if len(_admin_cache) > 5000:
        for k in [k for k, v in _admin_cache.items() if v[1] <= now][:2000] or list(_admin_cache)[:2500]:
            _admin_cache.pop(k, None)
    _admin_cache[key] = (verdict, now + ttl)
    return verdict


async def is_exempt(bot, chat_id: int, user_id: int) -> bool:
    """True for users automatic moderation (flood, spam, locks, blacklist) must leave alone:
    admins, approved users, the bot's owners and Telegram's non-user identities."""
    if user_id in NON_USER_IDS or settings.is_admin_id(user_id):
        return True
    if await chatcache.is_approved(chat_id, user_id):
        return True
    return await is_chat_admin(bot, chat_id, user_id)


def is_anonymous_admin(update: Update, chat_id: Optional[int] = None) -> bool:
    """Messages an admin sends "as the group" carry ``sender_chat == the chat itself``."""
    msg, chat = update.effective_message, update.effective_chat
    sender = getattr(msg, "sender_chat", None)
    if sender is None or chat is None:
        return False
    return sender.id == (chat.id if chat_id is None else chat_id)


def actor_of(update: Update):
    """``(actor_id, (id, name))`` of whoever ran the command; ``(None, None)`` for an anonymous admin
    (nobody to name, and their ``from_user`` is the placeholder @GroupAnonymousBot)."""
    if is_anonymous_admin(update):
        return None, None
    user = update.effective_user
    return user.id, (user.id, display_name(user))


async def check_caller(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, right: Optional[str]) -> Optional[str]:
    """``None`` when the sender may do ``right`` in ``chat_id``, else a user-facing reason.

    Anonymous admins are accepted (Telegram only lets admins post anonymously) except for
    ``promote``, whose per-admin right cannot be verified for an anonymous sender."""
    user = update.effective_user
    if is_anonymous_admin(update, chat_id):
        return "Anonymous admins can't use this - post as yourself." if right == "promote" else None
    if user is None:
        return "I can't tell who you are."
    member = await get_member(context.bot, chat_id, user.id)
    if member is None:
        return "I couldn't verify your admin status in that chat."
    if member_has_right(member, right):
        return None
    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return f"You need the “{RIGHT_LABELS[right]}” admin right for that."
    return "Admins only."


async def check_bot(context: ContextTypes.DEFAULT_TYPE, chat_id: int, right: Optional[str]) -> Optional[str]:
    """``None`` when the bot itself holds ``right`` in ``chat_id`` - gives a clear message instead of Telegram's raw error."""
    member = await get_member(context.bot, chat_id, context.bot.id)
    if member is None or member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return "I need to be an admin in that chat first."
    if right and not member_has_right(member, right):
        return f"I need the “{RIGHT_LABELS[right]}” admin right to do that."
    return None


# ── connection (manage a group from DM) ──────────────────────────────────────
@dataclass(frozen=True)
class ChatCtx:
    """The chat a command acts on: this group, or the one the sender ``/connect``ed to from DM."""
    chat_id: int
    title: str
    remote: bool


async def get_connection(user_id: int) -> Optional[int]:
    async with async_session() as session:
        row = await session.get(Connection, user_id)
        return row.chat_id if row else None


async def set_connection(user_id: int, chat_id: int) -> None:
    async with async_session() as session:
        row = await session.get(Connection, user_id)
        if row is None:
            session.add(Connection(user_id=user_id, chat_id=chat_id))
        else:
            row.chat_id = chat_id
        await session.commit()


async def clear_connection(user_id: int) -> bool:
    async with async_session() as session:
        res = await session.execute(delete(Connection).where(Connection.user_id == user_id))
        await session.commit()
        return bool(res.rowcount)


async def is_chat_owner(bot, chat_id: int, user_id: int) -> bool:
    """The bot's owners, or the group's creator."""
    if settings.is_admin_id(user_id):
        return True
    member = await get_member(bot, chat_id, user_id)
    return member is not None and member.status == ChatMemberStatus.OWNER


_IN_CHAT = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, ChatMemberStatus.RESTRICTED)


async def resolve_ctx(update: Update, context: ContextTypes.DEFAULT_TYPE, *, remote_ok: bool = True) -> Optional[ChatCtx]:
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None:
        return None
    if chat.type in ("group", "supergroup"):
        return ChatCtx(chat.id, chat.title or str(chat.id), False)
    if chat.type == "private" and remote_ok and update.effective_user is not None:
        user_id = update.effective_user.id
        chat_id = await get_connection(user_id)
        if chat_id is None:
            await msg.reply_text("You're not connected to a group. Run /connect in your group, or /connect <chat id> here.")
            return None
        # A connection must not outlive membership: someone who left (or was removed) keeps no window into the chat.
        member = await get_member(context.bot, chat_id, user_id)
        if member is None or member.status not in _IN_CHAT:
            await clear_connection(user_id)
            await msg.reply_text("You're no longer a member of that chat, so I disconnected you.")
            return None
        snap = await chatcache.get_chat_settings(chat_id)
        return ChatCtx(chat_id, (snap.title if snap and snap.title else str(chat_id)), True)
    await msg.reply_text("📢 This command works in groups only.")
    return None


def mod_command(right: Optional[str] = None, *, remote_ok: bool = True, bot_right: Optional[str] = None) -> Callable:
    """Decorator for admin commands: resolves the target chat (group or ``/connect``), checks that
    the *sender* holds ``right`` there (and the bot holds ``bot_right``), then calls
    ``func(update, context, ctx)``. Errors fall through ``safe_handler`` as usual."""

    def decorate(func: Callable) -> Callable:
        @safe_handler
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            ctx = await resolve_ctx(update, context, remote_ok=remote_ok)
            if ctx is None:
                return None
            problem = await check_caller(update, context, ctx.chat_id, right)
            if problem is None and bot_right is not None:
                problem = await check_bot(context, ctx.chat_id, bot_right)
            if problem is not None:
                await update.effective_message.reply_text(f"🚫 {problem}")
                return None
            return await func(update, context, ctx)

        return functools.wraps(func)(wrapper)

    return decorate


# ── target resolution ────────────────────────────────────────────────────────
_ID_RE = re.compile(r"\d{1,16}", re.ASCII)
_USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{3,32}", re.ASCII)
_TOPIC_SERVICE_ATTRS = (
    "forum_topic_created", "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened",
    "general_forum_topic_hidden", "general_forum_topic_unhidden",
)


def display_name(user: Any) -> str:
    return (getattr(user, "first_name", None) or getattr(user, "username", None) or str(getattr(user, "id", "?")))


def real_reply(message: Any) -> Any:
    """The message actually replied to. In a forum topic Telegram links every message to the topic's
    service message, whose author is the topic creator - that is not a reply to a person."""
    reply = getattr(message, "reply_to_message", None)
    if reply is None or any(getattr(reply, attr, None) for attr in _TOPIC_SERVICE_ATTRS):
        return None
    return reply


@dataclass
class Target:
    user_id: int
    name: str
    user: Any = None
    rest: List[str] = field(default_factory=list)   # arguments left after the target (time, reason, ...)
    from_reply: bool = False
    member: Any = None
    _member_loaded: bool = False

    async def load_member(self, bot, chat_id: int):
        if not self._member_loaded:
            self.member = await get_member(bot, chat_id, self.user_id)
            self._member_loaded = True
            if self.member is not None and self.name == str(self.user_id):
                self.name = display_name(self.member.user)
        return self.member

    @property
    def mention(self) -> str:
        return mention_html(self.user_id, self.name)


async def _user_by_username(username: str) -> Optional[Tuple[int, str]]:
    async with async_session() as session:
        row = (await session.execute(
            select(User).where(func.lower(User.username) == username.lower().lstrip("@")).limit(1)
        )).scalar_one_or_none()
    return (row.id, row.first_name or row.username or str(row.id)) if row else None


async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, *, allow_reply: bool = True) -> Optional[Target]:
    """Who a command is aimed at: the replied-to user, else the first argument as a numeric id, a
    ``text_mention`` (tapped from the member list) or an ``@username`` the bot has seen before."""
    msg = update.effective_message
    args = list(context.args or [])
    reply = real_reply(msg) if allow_reply else None
    if reply is not None and reply.from_user is not None:
        return Target(reply.from_user.id, display_name(reply.from_user), reply.from_user, args, True)
    if not args:
        return None
    for ent in (msg.entities or ()):
        if ent.type == MessageEntity.TEXT_MENTION and ent.user is not None:
            tokens = msg.parse_entity(ent).split()
            if tokens and args[:len(tokens)] == tokens:
                return Target(ent.user.id, display_name(ent.user), ent.user, args[len(tokens):])
    first = args[0]
    if _ID_RE.fullmatch(first) and int(first) > 0:
        return Target(int(first), first, None, args[1:])
    if _USERNAME_RE.fullmatch(first):
        found = await _user_by_username(first)
        if found is None:
            try:  # works for users the bot can see (e.g. it shares a chat with); harmless otherwise
                tg_chat = await context.bot.get_chat(first)
                if getattr(tg_chat, "type", None) == "private":
                    found = (tg_chat.id, tg_chat.first_name or first)
            except TelegramError:
                found = None
        if found is not None:
            return Target(found[0], found[1], None, args[1:])
    return None


NO_TARGET_HELP = ("Reply to someone, or give a numeric user ID / tap-mention. "
                  "(An @username only works if I've seen that person speak.)")


async def target_block_reason(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: Target, actor_id: Optional[int],
    *, allow_admin: bool = False, allow_self: bool = False,
) -> Optional[str]:
    """Why ``target`` must not be moderated (the bot, the actor, owners, admins...) or ``None``."""
    if target.user_id == context.bot.id:
        return "I'm not going to do that to myself."
    if not allow_self and actor_id is not None and target.user_id == actor_id:
        return "You can't do that to yourself."
    if target.user_id in NON_USER_IDS:
        return "I can't do that to anonymous admins, channels or Telegram itself."
    if settings.is_admin_id(target.user_id):
        return "That's one of my owners - I won't do that to them."
    member = await target.load_member(context.bot, chat_id)
    if member is not None:
        if member.status == ChatMemberStatus.OWNER:
            return "That's the chat owner - nobody can do that to them."
        if member.status == ChatMemberStatus.ADMINISTRATOR and not allow_admin:
            return "That user is an admin - demote them first if you really mean it."
    return None


async def get_or_create_chat(session, chat_id: int, *, title: Optional[str] = None, chat_type: str = "supergroup") -> Chat:
    """The chat's settings row, created on first use (settings may be changed before the bot has
    stored anything about the chat, e.g. from a private chat via /connect)."""
    row = await session.get(Chat, chat_id)
    if row is None:
        row = Chat(id=chat_id, title=title, type=chat_type)
        session.add(row)
        await session.flush()
    return row


async def update_chat(chat_id: int, **fields) -> None:
    """Set columns on the chat's row in one transaction (the commit hook refreshes ``chatcache``)."""
    async with async_session() as session:
        row = await get_or_create_chat(session, chat_id)
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"Chat has no column {key!r}")
            setattr(row, key, value)
        await session.commit()


def message_link(chat_id: int, message_id: int, username: Optional[str] = None) -> Optional[str]:
    """``https://t.me/...`` link to a message in a public group or a supergroup; ``None`` for basic groups."""
    if username:
        return f"https://t.me/{username}/{message_id}"
    text = str(chat_id)
    return f"https://t.me/c/{text[4:]}/{message_id}" if text.startswith("-100") else None


# ── bookkeeping ──────────────────────────────────────────────────────────────
async def ensure_rows(session, chat_id: int, user_id: int, *, chat_title: Optional[str] = None,
                      chat_type: str = "supergroup", first_name: Optional[str] = None,
                      username: Optional[str] = None) -> None:
    """Make sure the ``Chat`` and ``User`` rows that warn/ban rows reference exist (matters on Postgres,
    where those foreign keys are enforced; SQLite does not enforce them)."""
    if await session.get(Chat, chat_id) is None:
        session.add(Chat(id=chat_id, title=chat_title, type=chat_type))
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id, first_name=first_name or "", username=username))
    await session.flush()


async def record_ban(chat_id: int, user_id: int, *, reason: Optional[str], by: Optional[int], until: Optional[datetime]) -> None:
    async with async_session() as session:
        await ensure_rows(session, chat_id, user_id)
        await session.execute(delete(Ban).where(Ban.chat_id == chat_id, Ban.user_id == user_id))
        session.add(Ban(chat_id=chat_id, user_id=user_id, reason=reason, banned_by=by, until=until))
        await session.commit()


async def _add_temp_action(chat_id: int, user_id: int, action: str, reason: Optional[str], seconds: int) -> int:
    async with async_session() as session:
        await ensure_rows(session, chat_id, user_id)
        row = TempAction(chat_id=chat_id, user_id=user_id, action=action, reason=reason,
                         expires_at=utcnow() + timedelta(seconds=seconds))
        session.add(row)
        await session.commit()
        return row.id


def _schedule_expiry_notice(context: ContextTypes.DEFAULT_TYPE, action_id: int, seconds: int) -> None:
    app = getattr(context, "application", None)
    if app is None or getattr(app, "job_queue", None) is None:
        return
    from bot.plugins.automod import schedule_temp_action_notice  # late: plugin imports this module

    schedule_temp_action_notice(app, action_id, seconds)


async def _reverse_temp_actions(chat_id: int, user_id: int, action: str) -> None:
    async with async_session() as session:
        rows = (await session.execute(select(TempAction).where(
            TempAction.chat_id == chat_id, TempAction.user_id == user_id,
            TempAction.action == action, TempAction.reversed.is_(False),
        ))).scalars().all()
        for row in rows:
            row.reversed = True
        await session.commit()


# ── actions ──────────────────────────────────────────────────────────────────
_MUTED = ChatPermissions(can_send_messages=False)
_FALLBACK_UNMUTED = ChatPermissions(can_send_messages=True, can_send_polls=True,
                                    can_send_other_messages=True, can_add_web_page_previews=True)


async def default_permissions(bot, chat_id: int) -> ChatPermissions:
    """The chat's own default member permissions (what an unrestricted member has)."""
    try:
        perms = (await bot.get_chat(chat_id)).permissions
        if perms is not None:
            return perms
    except TelegramError as exc:
        logger.debug("default_permissions_unavailable", chat_id=chat_id, error=str(exc))
    return _FALLBACK_UNMUTED


async def punish(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, action: str, *,
                 seconds: Optional[int] = None, reason: Optional[str] = None, by: Optional[int] = None) -> Optional[str]:
    """Apply ``mute|tmute|kick|ban|tban``. Returns ``None`` on success or a user-facing error string."""
    if action in TEMP_ACTIONS:
        try:
            seconds = validate_duration(seconds or DEFAULT_TEMP_SECONDS)
        except DurationError as exc:
            return str(exc)
    bot = context.bot
    try:
        if action in ("mute", "tmute"):
            until = epoch_after(seconds) if action == "tmute" else None
            await bot.restrict_chat_member(chat_id, user_id, _MUTED, until_date=until)
            if action == "tmute":
                _schedule_expiry_notice(context, await _add_temp_action(chat_id, user_id, "mute", reason, seconds), seconds)
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        elif action in ("ban", "tban"):
            until = epoch_after(seconds) if action == "tban" else None
            await bot.ban_chat_member(chat_id, user_id, until_date=until)
            await record_ban(chat_id, user_id, reason=reason, by=by,
                             until=(utcnow() + timedelta(seconds=seconds)) if action == "tban" else None)
            if action == "tban":
                _schedule_expiry_notice(context, await _add_temp_action(chat_id, user_id, "ban", reason, seconds), seconds)
        else:
            return f"Unknown action: {action}"
    except TelegramError as exc:
        return friendly_telegram_error(exc)
    invalidate_admin(chat_id, user_id)
    return None


async def lift(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, kind: str) -> Optional[str]:
    """``kind`` = ``'ban'`` (unban) or ``'mute'`` (restore default permissions). ``None`` on success."""
    bot = context.bot
    try:
        if kind == "ban":
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            async with async_session() as session:
                await session.execute(delete(Ban).where(Ban.chat_id == chat_id, Ban.user_id == user_id))
                await session.commit()
        else:
            await bot.restrict_chat_member(chat_id, user_id, await default_permissions(bot, chat_id))
    except TelegramError as exc:
        return friendly_telegram_error(exc)
    await _reverse_temp_actions(chat_id, user_id, kind)
    return None


async def delete_messages(bot, chat_id: int, message_ids: List[int], *, fallback_budget: int = 50) -> int:
    """Bulk delete (100 ids per call). Telegram skips ids that no longer exist; a batch that is refused
    outright (e.g. it contains messages older than 48 h) is retried one message at a time, but at most
    ``fallback_budget`` single deletes are attempted per call so a big purge of old history cannot
    turn into thousands of API calls. Returns how many ids were deleted (or already gone)."""
    done = 0
    ids = sorted(set(message_ids))
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, batch)
            done += len(batch)
        except TelegramError:
            for mid in batch:
                if fallback_budget <= 0:
                    break
                fallback_budget -= 1
                try:
                    await bot.delete_message(chat_id, mid)
                    done += 1
                except TelegramError:
                    pass
    return done


async def _delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(data["chat_id"], data["message_id"])
    except TelegramError:
        pass


def schedule_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: float = 5.0) -> bool:
    """Delete ``message_id`` after ``delay`` seconds *without* holding the handler open: a ``sleep()``
    inside a handler blocks that chat's ordered update queue for its whole duration (v2.10's /purge
    froze the chat for 5 s). Returns False when there is no job queue (the message is then left alone)."""
    queue = getattr(getattr(context, "application", None), "job_queue", None) or getattr(context, "job_queue", None)
    if queue is None:
        return False
    queue.run_once(_delete_job, delay, data={"chat_id": chat_id, "message_id": message_id},
                   name=f"autodelete:{chat_id}:{message_id}")
    return True


# ── warns ────────────────────────────────────────────────────────────────────
@dataclass
class WarnOutcome:
    count: int
    limit: int
    action: str
    punished: bool = False
    error: Optional[str] = None


async def warn_settings(chat_id: int) -> Tuple[int, str]:
    snap = await chatcache.get_chat_settings(chat_id)
    limit = (snap.warn_limit if snap and snap.warn_limit else None) or settings.default_warn_limit
    action = (snap.warn_action if snap and snap.warn_action else None) or settings.default_warn_action
    return int(limit), str(action)


async def reset_warns(chat_id: int, user_id: int) -> int:
    async with async_session() as session:
        res = await session.execute(delete(Warn).where(Warn.chat_id == chat_id, Warn.user_id == user_id))
        await session.commit()
        return res.rowcount or 0


async def count_warns(chat_id: int, user_id: int) -> int:
    async with async_session() as session:
        return (await session.execute(
            select(func.count()).select_from(Warn).where(Warn.chat_id == chat_id, Warn.user_id == user_id)
        )).scalar() or 0


async def add_warn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, *, by: Optional[int],
                   reason: str, user_name: Optional[str] = None) -> WarnOutcome:
    """Record a warning; when the chat's limit is reached apply its warn action and clear the user's warns."""
    limit, action = await warn_settings(chat_id)
    async with async_session() as session:
        await ensure_rows(session, chat_id, user_id, first_name=user_name)
        session.add(Warn(chat_id=chat_id, user_id=user_id, reason=reason, warned_by=by))
        await session.flush()
        count = (await session.execute(
            select(func.count()).select_from(Warn).where(Warn.chat_id == chat_id, Warn.user_id == user_id)
        )).scalar() or 0
        await session.commit()
    outcome = WarnOutcome(count=count, limit=limit, action=action)
    if count >= limit:
        err = await punish(context, chat_id, user_id, action, reason=f"Reached {limit} warnings", by=by)
        outcome.error = err
        outcome.punished = err is None
        if err is None:
            await reset_warns(chat_id, user_id)
    return outcome


_ACTION_PAST = {"mute": "muted", "tmute": "muted", "kick": "kicked", "ban": "banned", "tban": "banned"}
_ACTION_ICON = {"mute": "🔇", "tmute": "🔇", "kick": "👢", "ban": "🔨", "tban": "🔨"}


def describe_warn_outcome(who_html: str, outcome: WarnOutcome, reason: str) -> str:
    text = (f"⚠️ <b>Warned</b> {who_html} — <b>{outcome.count}/{outcome.limit}</b>\n"
            f"<b>Reason:</b> {escape_html(reason)}")
    if outcome.punished:
        text += f"\n{_ACTION_ICON.get(outcome.action, '🚫')} <b>Limit reached — {_ACTION_PAST.get(outcome.action, outcome.action)}.</b> Warnings cleared."
    elif outcome.error:
        text += f"\n❌ Limit reached but I couldn't act: {escape_html(outcome.error)}"
    return text


async def enforce_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, name: str, action: Optional[str],
                         *, seconds: Optional[int] = None, reason: str = "", by: Optional[int] = None) -> Optional[str]:
    """Apply an *automatic* action (blacklist, flood...). Returns an HTML notice to post, or ``None``
    when nothing should be said. Failures are logged, never raised: a bot without the right to
    ban must not turn every violation into an error message."""
    action = action or "delete"
    if action in ("nothing", "delete"):
        return None
    who = mention_html(user_id, name)
    if action == "warn":
        outcome = await add_warn(context, chat_id, user_id, by=by, reason=reason, user_name=name)
        return describe_warn_outcome(who, outcome, reason)
    err = await punish(context, chat_id, user_id, action, seconds=seconds, reason=reason, by=by)
    if err is not None:
        logger.warning("auto_action_failed", chat_id=chat_id, user_id=user_id, action=action, error=err)
        return None
    span = f" for {format_span(seconds or DEFAULT_TEMP_SECONDS)}" if action in TEMP_ACTIONS else ""
    return f"{_ACTION_ICON[action]} {who} was {_ACTION_PAST[action]}{span}.\n<b>Reason:</b> {escape_html(reason)}"


# ── log channel ──────────────────────────────────────────────────────────────
async def log_event(context: ContextTypes.DEFAULT_TYPE, chat_id: int, tag: str, *, actor: Optional[Tuple[int, str]] = None,
                    target: Optional[Tuple[int, str]] = None, reason: Optional[str] = None, extra: Optional[str] = None) -> None:
    """Post a moderation event to the chat's log channel (``/setlog``). Never raises."""
    try:
        snap = await chatcache.get_chat_settings(chat_id)
        channel = getattr(snap, "log_channel", None) if snap else None
        if not channel:
            return
        lines = [f"<b>{escape_html((snap.title if snap and snap.title else str(chat_id)))}</b>", f"#{escape_html(tag.upper())}"]
        if actor:
            lines.append(f"<b>Admin:</b> {mention_html(*actor)}")
        if target:
            lines.append(f"<b>User:</b> {mention_html(*target)} (<code>{target[0]}</code>)")
        if reason:
            lines.append(f"<b>Reason:</b> {escape_html(reason)}")
        if extra:
            lines.append(extra)
        await context.bot.send_message(channel, "\n".join(lines), parse_mode="HTML")
    except Exception as exc:  # a broken log channel must never break moderation
        logger.debug("log_event_failed", chat_id=chat_id, error=str(exc))
