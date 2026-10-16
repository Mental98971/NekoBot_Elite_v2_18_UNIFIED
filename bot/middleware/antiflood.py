"""Anti-flood and slow mode (group -2, runs before every plugin handler).

v2.12 rewrite. What changed from v2.10 and why:

* **Per-chat settings are honoured.** ``Chat.antiflood_enabled`` / ``antiflood_limit`` existed in the
  schema and were documented, but only the global ``ANTIFLOOD_*`` env values were ever read.
  ``/setflood`` and ``/setfloodmode`` (plugins/modsettings.py) now drive this module.
* **Only the flooder's own messages are deleted.** v2.10 deleted the whole *range* of message ids
  ``[latest - count + 1 .. latest]`` - i.e. it deleted other members' messages posted in between.
  The ids of the offender's burst are tracked and only those are removed (in bulk).
* **Exemptions** are admins, approved users, the bot owners and Telegram's non-user identities
  (``modtools.is_exempt``) instead of a private copy of the admin cache.
* **Edits are not floods.** ``filters.ALL`` also delivers edited messages; five quick typo-fixes
  used to count as five messages.
* **No unbounded growth.** The burst tracker kept one list per (chat, user) forever.
* **Slow mode** (``/slowmode``) is enforced here: Telegram's Bot API has no slow-mode method, so v2.10's
  ``/slowmode`` always failed. Messages sent too soon after the member's previous one are deleted.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, Tuple

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot.config import settings
from bot.utils import chatcache, modtools as mt
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_FLOOD_ACTION = "tban"                       # v2.10 behaviour: temporary ban
FLOOD_ACTIONS = ("ban", "mute", "kick", "tban", "tmute")
_MAX_TRACKED = 20000

# (chat_id, user_id) -> recent (monotonic time, message_id) of that user's messages
_burst: Dict[Tuple[int, int], Deque[Tuple[float, int]]] = {}
# (chat_id, user_id) -> monotonic time of the last message slow mode let through
_last_post: Dict[Tuple[int, int], float] = {}


def _prune(now: float, window: float) -> None:
    for key in [k for k, dq in _burst.items() if not dq or now - dq[-1][0] >= window]:
        _burst.pop(key, None)
    for key in [k for k, t in _last_post.items() if now - t > 3600]:
        _last_post.pop(key, None)
    if len(_burst) > _MAX_TRACKED:      # nothing expired: shed the oldest half instead of growing
        for key in list(_burst)[: _MAX_TRACKED // 2]:
            _burst.pop(key, None)


def flood_config(snap) -> Tuple[bool, int, str, int]:
    """``(enabled, limit, action, action_seconds)`` for a chat settings snapshot (or ``None``)."""
    enabled = True if snap is None or snap.antiflood_enabled is None else bool(snap.antiflood_enabled)
    limit = (snap.antiflood_limit if snap is not None and snap.antiflood_limit else None) or settings.antiflood_threshold
    action = (snap.antiflood_action if snap is not None and snap.antiflood_action else None) or DEFAULT_FLOOD_ACTION
    seconds = (snap.antiflood_action_seconds if snap is not None and snap.antiflood_action_seconds else None) or settings.antiflood_ban_duration
    return enabled, int(limit), action, int(seconds)


async def antiflood_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if msg is None or chat is None or user is None or chat.type == "private" or user.is_bot:
        return
    if update.message is None:        # edited messages are not new messages
        return
    if getattr(msg, "sender_chat", None) is not None or msg.new_chat_members or msg.left_chat_member:
        return                          # channel / anonymous-admin identities and service messages

    snap = await chatcache.get_chat_settings(chat.id)
    enabled, limit, action, action_seconds = flood_config(snap)
    slow = int(getattr(snap, "slowmode_seconds", None) or 0)
    if not enabled and not slow:
        return
    if await mt.is_exempt(context.bot, chat.id, user.id):
        return

    key = (chat.id, user.id)
    now = time.monotonic()
    if len(_burst) + len(_last_post) > _MAX_TRACKED:
        _prune(now, settings.antiflood_interval)

    if slow:
        last = _last_post.get(key)
        if last is not None and now - last < slow:
            try:
                await msg.delete()
            except TelegramError as exc:
                logger.debug("slowmode_delete_failed", chat_id=chat.id, error=str(exc))
            raise ApplicationHandlerStop
        _last_post[key] = now
    if not enabled:
        return

    window = settings.antiflood_interval
    burst = _burst.setdefault(key, deque())
    while burst and now - burst[0][0] >= window:
        burst.popleft()
    burst.append((now, msg.message_id))
    count = len(burst)

    if count > limit + 2:
        ids = [mid for _t, mid in burst]
        burst.clear()
        notice = await mt.enforce_action(context, chat.id, user.id, mt.display_name(user), action,
                                         seconds=action_seconds, reason="Flooding", by=context.bot.id)
        await mt.delete_messages(context.bot, chat.id, ids)
        if notice:
            await context.bot.send_message(chat.id, f"🛡️ <b>Anti-Flood</b>\n{notice}", parse_mode="HTML")
            await mt.log_event(context, chat.id, "Flood", target=(user.id, mt.display_name(user)), reason=f"{count} messages in {window}s")
        raise ApplicationHandlerStop
    if count == limit:
        await msg.reply_text(
            f"⚠️ <b>Slow down, {escape_html(user.first_name or '')}!</b> You are sending messages too fast.",
            parse_mode="HTML",
        )
