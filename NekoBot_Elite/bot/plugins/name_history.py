"""
Name/username change history — "sangmata", cross-referenced from
Mikobot during a feature-gap review. Unlike AFK and karma (which
turned out to already exist under different names in fun.py), NovaBot
had no equivalent at all: User.username/first_name were set once, the
first time any plugin happened to create that row, and never refreshed
— so /topcatchers, /owners, and anywhere else a display name is shown
could be using a name from months ago with no way to see it had
changed, let alone to what.

Design notes:
  - Runs on group messages only, same scope as other ambient listeners
    (collector.py's spawn_listener, group_mgmt.py's welcome_listener).
  - Throttled per user via an in-memory cache, not a DB read on every
    single message — a busy group would otherwise mean a query just to
    confirm nothing changed. Only an actual detected difference costs
    a write; the common case (nothing changed, and we checked
    recently) costs one dict lookup.
  - This is also, incidentally, the fix for the "stale display name"
    problem above: keeping User.username/first_name/last_name current
    is a side effect of tracking their history.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.core.database import Chat, User, async_session
from bot.core.errors import safe_handler
from bot.core.decorators import admin_only
from bot.utils.helpers import escape_html, resolve_target_user
from bot.utils.logger import get_logger

logger = get_logger(__name__)

NAME_CHECK_INTERVAL_SECONDS = 600  # re-check any one user at most every 10 minutes
MAX_HISTORY_ENTRIES = 20  # per user, oldest dropped first once exceeded

# user_id -> ((username, first_name, last_name), last_checked_monotonic)
_last_checked: Dict[int, Tuple[Tuple[Optional[str], str, Optional[str]], float]] = {}


def _record_change(history: list, field: str, old: Optional[str], new: Optional[str]) -> list:
    history = list(history or [])
    history.append({"field": field, "old": old, "new": new, "at": datetime.utcnow().isoformat()})
    return history[-MAX_HISTORY_ENTRIES:]


async def _announce(context, chat_id: int, user, entries: list) -> None:
    """SangMata-style notice, only in chats that switched /namealert on. Never raises."""
    lines = []
    for e in entries:
        old = escape_html(e["old"]) if e.get("old") else "<i>none</i>"
        new = escape_html(e["new"]) if e.get("new") else "<i>none</i>"
        lines.append(f"• {escape_html(str(e.get('field')))}: {old} → {new}")
    if not lines:
        return
    try:
        await context.bot.send_message(
            chat_id, f"📇 <code>{user.id}</code> changed:\n" + "\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.debug("name_alert_failed", chat_id=chat_id)


@safe_handler
async def name_history_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == "private" or user.is_bot:
        return

    current = (user.username, user.first_name or "", user.last_name)
    now = time.monotonic()
    cached = _last_checked.get(user.id)
    if cached is not None:
        cached_values, last_checked = cached
        if now - last_checked < NAME_CHECK_INTERVAL_SECONDS:
            return
        if cached_values == current:
            _last_checked[user.id] = (current, now)
            return

    try:
        async with async_session() as session:
            row = await session.get(User, user.id)
            if row is None:
                # New to us — nothing to compare against yet, just seed it.
                session.add(User(id=user.id, username=user.username, first_name=user.first_name or "", last_name=user.last_name))
                await session.commit()
                _last_checked[user.id] = (current, now)
                return

            history = row.name_history or []
            changed = False
            if (row.username or None) != (user.username or None):
                history = _record_change(history, "username", row.username, user.username)
                row.username = user.username
                changed = True
            if (row.first_name or "") != (user.first_name or ""):
                history = _record_change(history, "first_name", row.first_name, user.first_name)
                row.first_name = user.first_name or ""
                changed = True
            if (row.last_name or None) != (user.last_name or None):
                history = _record_change(history, "last_name", row.last_name, user.last_name)
                row.last_name = user.last_name
                changed = True
            if changed:
                new_entries = history[len(row.name_history or []):] if len(history) > len(row.name_history or []) else history[-3:]
                row.name_history = history
                chat_row = await session.get(Chat, chat.id)
                alert = bool(chat_row is not None and chat_row.name_alerts)
                await session.commit()
                if alert:
                    await _announce(context, chat.id, user, new_entries)
    except Exception:
        logger.exception("name_history_check_failed", user_id=user.id)
        return

    _last_checked[user.id] = (current, now)


@safe_handler
async def names_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/names [reply | user id | @username] - recorded name/username changes (also /sangmata, /history)."""
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        if context.args:
            await update.message.reply_text("I haven't seen that user, so I have no history for them. A numeric id works if I've seen them.")
            return
        target_id, target_name = update.effective_user.id, update.effective_user.first_name

    async with async_session() as session:
        row = await session.get(User, target_id)

    shown = escape_html(target_name or (row.first_name if row else None) or str(target_id))
    if not row or not row.name_history:
        await update.message.reply_text(f"No name changes recorded for {shown}.", parse_mode="HTML")
        return

    lines = [f"📇 <b>Name history - {shown}</b> (<code>{target_id}</code>)\n"]
    for entry in row.name_history[-15:]:
        old = escape_html(entry["old"]) if entry.get("old") else "<i>(none)</i>"
        new = escape_html(entry["new"]) if entry.get("new") else "<i>(none)</i>"
        date = entry.get("at", "")[:10]
        lines.append(f"• {date} - {entry.get('field')}: {old} → {new}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@admin_only
@safe_handler
async def namealert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/namealert [on|off] - announce members' name/username changes in this chat."""
    arg = (context.args[0].lower() if context.args else "")
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type, title=update.effective_chat.title)
            session.add(chat)
        if arg in ("on", "off"):
            chat.name_alerts = arg == "on"
            await session.commit()
        state = bool(chat.name_alerts)
    await update.message.reply_text(
        f"📇 Name-change alerts are <b>{'on' if state else 'off'}</b>." + ("" if arg in ("on", "off") else "\nUsage: /namealert on|off"),
        parse_mode="HTML")


@safe_handler
async def forgetnames_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/forgetnames - erase your own recorded name history (DM or group)."""
    async with async_session() as session:
        row = await session.get(User, update.effective_user.id)
        had = bool(row and row.name_history)
        if row is not None:
            row.name_history = []
            await session.commit()
    _last_checked.pop(update.effective_user.id, None)
    await update.message.reply_text("🧹 Your name history was erased." if had else "There was no history stored for you.")


def register(app) -> None:
    # Passive observer — never blocks anything, so its exact position
    # relative to other listener groups doesn't matter functionally.
    # See group_mgmt.py's register() for the full documented list of
    # handler groups in use; this sits at 9, between collector.py's
    # upload-caption listener (8) and personality.py's banter (10).
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, name_history_middleware), group=9)
    app.add_handler(CommandHandler(["names", "sangmata", "history"], names_cmd))
    app.add_handler(CommandHandler("namealert", namealert_cmd))
    app.add_handler(CommandHandler("forgetnames", forgetnames_cmd))
