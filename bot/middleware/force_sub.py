"""
Force-subscribe middleware — require joining settings.force_sub_channel
before using the bot.

Registered as its own MessageHandler in group -4, the group right after
access_control_middleware (group=-5; PTB runs only one handler per group,
so they cannot share one), so
bans/blacklist/maintenance still take priority — a banned user
shouldn't get a join prompt at all — while everything downstream
(logging, antiflood, antispam, and every plugin) still runs after both.

Design notes:
  - Fails OPEN, not closed: if the membership check itself errors (bot
    not an admin of the channel, channel doesn't exist, transient
    Telegram API error), the user is let through rather than the whole
    bot becoming unusable for everyone because of a misconfigured
    channel. A hard "no" from Telegram (user genuinely not a member) is
    the only thing that blocks.
  - Cached per user for force_sub_cache_seconds so normal chatting
    doesn't hit getChatMember on every single message.
  - Only intercepts Message-type updates (see registration in
    bot/core/bot.py) — CallbackQueryHandler-based buttons, including
    this module's own "Check Again" button, are a separate PTB update
    type and are never blocked by this middleware, so the button always
    works even for a user this same middleware is currently blocking.
  - Sudo/owner bypass, matching every other bot-wide gate in this
    codebase (maintenance mode, private_bot_mode, global bans).
"""
from __future__ import annotations

import time
from typing import Dict, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot.config import settings
from bot.identity import bot_name
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# user_id -> (is_subscribed, checked_at)
_membership_cache: Dict[int, Tuple[bool, float]] = {}


def _channel_url() -> str:
    channel = (settings.force_sub_channel or "").lstrip("@")
    return f"https://t.me/{channel}"


def _join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=_channel_url())],
        [InlineKeyboardButton("✅ Check Again", callback_data="fsub:check")],
    ])


async def _is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int, *, force: bool = False) -> bool:
    """Check (and cache) whether user_id is a member of the force-sub
    channel. Fails open on any API error — see module docstring."""
    now = time.time()
    if not force:
        cached = _membership_cache.get(user_id)
        if cached is not None and now - cached[1] < settings.force_sub_cache_seconds:
            return cached[0]

    try:
        member = await context.bot.get_chat_member(settings.force_sub_channel, user_id)
        subscribed = member.status in ("member", "administrator", "creator")
    except TelegramError as e:
        # Bot not admin of the channel, channel doesn't exist, user
        # never started a chat with the bot for a lookup that needs it,
        # etc. — don't let a misconfiguration lock out the whole bot.
        logger.warning("force_sub membership check failed, failing open: %s", e)
        subscribed = True
    except Exception as e:
        logger.warning("force_sub membership check failed unexpectedly, failing open: %s", e)
        subscribed = True

    _membership_cache[user_id] = (subscribed, now)
    return subscribed


def _invalidate(user_id: int) -> None:
    _membership_cache.pop(user_id, None)


async def force_sub_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.force_sub_enabled or not settings.force_sub_channel:
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == "channel":
        return
    if settings.is_admin_id(user.id):
        return  # sudo/owner bypass, matching every other bot-wide gate

    if await _is_subscribed(context, user.id):
        return

    message = update.effective_message
    if message is not None:
        try:
            await message.reply_text(
                f"🔒 <b>Join our channel to use {bot_name()}</b>\n\n"
                f"Tap below, join, then tap <b>Check Again</b>.",
                parse_mode="HTML",
                reply_markup=_join_keyboard(),
            )
        except Exception:
            logger.exception("force_sub_prompt_failed")
    raise ApplicationHandlerStop


async def force_sub_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if await _is_subscribed(context, user.id, force=True):
        await query.answer("✅ Thanks for joining!")
        try:
            await query.edit_message_text("✅ Verified — you're all set. Send your command again.")
        except Exception:
            pass
    else:
        await query.answer("You haven't joined yet — tap Join Channel first.", show_alert=True)
