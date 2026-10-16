"""
Access-control middleware — from YukkiMusicBot's sudo/blacklistchat.py,
sudo/private.py, and sudo/maintenance.py, adapted into one middleware
pass instead of three separate plugin-level checks.

Unlike antiflood_middleware / antispam_middleware (which take an action
but let the update keep flowing to other handlers), this one actually
stops processing with ApplicationHandlerStop when it denies access —
letting a blacklisted chat's messages fall through to every other
plugin anyway would defeat the point of blacklisting it.

BotConfig (maintenance_mode / private_bot_mode) is cached in memory for
a short TTL rather than queried on every single update — it's a value
that changes rarely (an admin flipping a toggle), and this middleware
runs on every message, so a DB round-trip per message for it would be
wasteful. plugins/access_control.py invalidates the cache immediately
whenever it writes a new value, so toggles still take effect instantly.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot.config import settings
from bot.core.database import BlockedUser, Chat, GlobalBan, async_session, get_bot_config
from bot.identity import bot_name

_config_cache = {"maintenance_mode": False, "private_bot_mode": settings.private_bot_mode, "cached_at": 0.0}
_CACHE_TTL = 15.0
_banned_users_cache: dict = {"ids": set(), "cached_at": 0.0}


def invalidate_config_cache() -> None:
    """Called by plugins/access_control.py right after writing new
    BotConfig values, so a toggle is effective immediately rather than
    waiting out the cache TTL."""
    _config_cache["cached_at"] = 0.0


def invalidate_banned_users_cache() -> None:
    """Called after /gban, /ungban, /block, /unblock."""
    _banned_users_cache["cached_at"] = 0.0


async def _get_cached_config() -> dict:
    if time.time() - _config_cache["cached_at"] > _CACHE_TTL:
        async with async_session() as session:
            row = await get_bot_config(session)
            _config_cache["maintenance_mode"] = row.maintenance_mode
            _config_cache["private_bot_mode"] = row.private_bot_mode
            await session.commit()
        _config_cache["cached_at"] = time.time()
    return _config_cache


async def _get_banned_user_ids() -> set:
    if time.time() - _banned_users_cache["cached_at"] > _CACHE_TTL:
        async with async_session() as session:
            gbanned = (await session.execute(select(GlobalBan.user_id))).scalars().all()
            blocked = (await session.execute(select(BlockedUser.user_id))).scalars().all()
        _banned_users_cache["ids"] = set(gbanned) | set(blocked)
        _banned_users_cache["cached_at"] = time.time()
    return _banned_users_cache["ids"]


async def access_control_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if settings.is_admin_id(user.id):
        return  # sudoers/owner always get through, including to flip these settings back off

    if user.id in await _get_banned_user_ids():
        raise ApplicationHandlerStop  # silent — gban/block both just stop responding, no callout

    cfg = await _get_cached_config()

    async with async_session() as session:
        chat_row = await session.get(Chat, chat.id)

    if chat_row and chat_row.bot_blacklisted:
        raise ApplicationHandlerStop  # silent — a blacklisted chat shouldn't get chatty bot replies either

    if chat_row and chat_row.disabled_commands and update.effective_message and update.effective_message.text:
        text = update.effective_message.text
        if text.startswith("/"):
            cmd_name = text.split()[0][1:].split("@")[0].lower()
            if cmd_name in chat_row.disabled_commands:
                try:
                    member = await context.bot.get_chat_member(chat.id, user.id)
                    is_chat_admin = member.status in ("creator", "administrator")
                except Exception:
                    is_chat_admin = False
                if not is_chat_admin:
                    raise ApplicationHandlerStop  # silent — that's the point of disabling it

    if cfg["maintenance_mode"]:
        if update.effective_message and update.effective_message.text and update.effective_message.text.startswith("/"):
            try:
                await update.effective_message.reply_text(f"🔧 {bot_name()} is under maintenance right now — back shortly.")
            except Exception:
                pass
        raise ApplicationHandlerStop

    if cfg["private_bot_mode"] and chat.type != "private":
        if not (chat_row and chat_row.private_mode_authorized):
            raise ApplicationHandlerStop  # silent, same reasoning as blacklist
