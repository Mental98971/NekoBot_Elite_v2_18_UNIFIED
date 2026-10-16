"""
Blue-text cleaner (v2.17): /cleanblue, /ignoreblue, /unignoreblue, /listblue, /gignoreblue, /ungignoreblue.

From the Saitama / Masha ``cleaner.py``.  With /cleanblue on, a message that starts with a /command this bot does
not know (a made-up "blue text" such as /hello or /fuck that people tap and nothing happens) is deleted.  Commands
the bot *does* know are never touched, and neither is a command addressed to another bot (``/play@OtherBot``).
Other bots' commands typed without an @suffix would be deleted too - list them with /ignoreblue play (this chat)
or, for sudoers, /gignoreblue play (every chat).

Differences from the original, all fixes: the global ignore list is editable by sudoers only (it was open to any
chat admin), /listblue prints the lists (the original only replied when they were empty) and is available to chat
admins, and commands aimed at other bots are left alone.

The listener is handler group 17 (see the map in group_mgmt.py).
"""
from __future__ import annotations

import re
import time
from typing import Dict, Optional, Set, Tuple

from sqlalchemy import delete, func, select
from telegram.error import TelegramError
from telegram.ext import CommandHandler, MessageHandler, filters

from bot.config import settings
from bot.core.database import BlueIgnore, Chat, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils import modtools as mt
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

CACHE_TTL = 30.0
MAX_LOCAL_IGNORES = 100
GLOBAL = 0                      # BlueIgnore.chat_id of the bot-wide list
_NAME = re.compile(r"^[a-z0-9_]{1,32}$")

_chat_cache: Dict[int, Tuple[float, bool, frozenset]] = {}      # chat_id -> (when, enabled, local ignores)
_global_cache: Optional[Tuple[float, frozenset]] = None
_known_cache: Tuple[int, frozenset] = (-1, frozenset())          # (handler count, command names)


def invalidate() -> None:
    global _global_cache
    _chat_cache.clear()
    _global_cache = None


# ── pure helpers ────────────────────────────────────────────────────────────
def normalize_command(raw: str) -> Optional[str]:
    """``'/Play@Bot'`` -> ``'play'``; None if it can't be a command name."""
    name = (raw or "").strip().lstrip("/").split("@", 1)[0].lower()
    return name if _NAME.match(name) else None


def parse_message_command(text: str, own_username: Optional[str]) -> Optional[str]:
    """The command a message starts with, or None if it isn't one *for us*: not a command, or addressed to
    another bot."""
    word = (text or "").lstrip().split(None, 1)[0] if (text or "").strip() else ""
    if len(word) < 2 or word[0] != "/":
        return None
    name, _, target = word[1:].partition("@")
    if target and (not own_username or target.lower() != own_username.lower().lstrip("@")):
        return None
    return name.lower() if name else None


def known_commands(application) -> frozenset:
    """Every command any registered CommandHandler answers to (recomputed only when handlers are added)."""
    global _known_cache
    handlers = getattr(application, "handlers", None) or {}
    count = sum(len(v) for v in handlers.values())
    if count != _known_cache[0]:
        names: Set[str] = set()
        for group in handlers.values():
            for h in group:
                if isinstance(h, CommandHandler):
                    names |= {str(c).lower() for c in h.commands}
        _known_cache = (count, frozenset(names))
    return _known_cache[1]


# ── cached lookups ──────────────────────────────────────────────────────────
async def _global_ignores() -> frozenset:
    global _global_cache
    now = time.monotonic()
    if _global_cache and now - _global_cache[0] < CACHE_TTL:
        return _global_cache[1]
    async with async_session() as session:
        rows = (await session.execute(select(BlueIgnore.command).where(BlueIgnore.chat_id == GLOBAL))).scalars().all()
    _global_cache = (now, frozenset(rows))
    return _global_cache[1]


async def _chat_state(chat_id: int) -> Tuple[bool, frozenset]:
    now = time.monotonic()
    hit = _chat_cache.get(chat_id)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1], hit[2]
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        enabled = bool(chat is not None and chat.clean_blue)
        local = frozenset((await session.execute(select(BlueIgnore.command).where(BlueIgnore.chat_id == chat_id))).scalars().all()) if enabled else frozenset()
    if len(_chat_cache) > 5000:
        _chat_cache.clear()
    _chat_cache[chat_id] = (now, enabled, local)
    return enabled, local


# ── listener ────────────────────────────────────────────────────────────────
@safe_handler
async def blue_listener(update, context) -> None:
    """Delete made-up commands in chats that turned /cleanblue on. Never blocks later handlers."""
    msg, chat = update.effective_message, update.effective_chat
    if msg is None or chat is None or chat.type not in ("group", "supergroup") or not getattr(msg, "text", None):
        return
    command = parse_message_command(msg.text, getattr(context.bot, "username", None))
    if command is None:
        return
    if command in known_commands(getattr(context, "application", None)):
        return
    enabled, local = await _chat_state(chat.id)
    if not enabled or command in local or command in await _global_ignores():
        return
    try:
        await msg.delete()
    except TelegramError as exc:
        logger.debug("cleanblue_delete_failed chat=%s err=%s", chat.id, exc)


# ── commands ────────────────────────────────────────────────────────────────
@admin_only
@group_only
@safe_handler
async def cleanblue_cmd(update, context) -> None:
    """/cleanblue [on|off] - delete made-up commands ("blue text") in this chat."""
    chat, msg = update.effective_chat, update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    if arg and arg not in ("on", "yes", "off", "no"):
        await msg.reply_text("Invalid argument. Accepted values are 'yes', 'on', 'no', 'off'.")
        return
    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        if row is None:
            row = Chat(id=chat.id, type=chat.type, title=chat.title)
            session.add(row)
        if arg:
            row.clean_blue = arg in ("on", "yes")
            await session.commit()
        state = bool(row.clean_blue)
    invalidate()
    note = ""
    if arg and state:
        problem = await mt.check_bot(context, chat.id, "delete")
        if problem:
            note = f"\n⚠️ {escape_html(problem)} Until I can delete messages this does nothing."
    await msg.reply_text(f"🧹 Bluetext cleaning for <b>{escape_html(chat.title or str(chat.id))}</b>: <b>{'Enabled' if state else 'Disabled'}</b>{note}",
                         parse_mode="HTML")


async def _add_ignore(update, context, chat_id: int, scope: str) -> None:
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("No command supplied to be ignored.")
        return
    name = normalize_command(context.args[0])
    if name is None:
        await msg.reply_text("That isn't a valid command name (letters, digits and _ only).")
        return
    async with async_session() as session:
        exists = (await session.execute(select(BlueIgnore.id).where(BlueIgnore.chat_id == chat_id, BlueIgnore.command == name))).first()
        if exists:
            await msg.reply_text("Command is already ignored.")
            return
        count = (await session.execute(select(func.count()).select_from(BlueIgnore).where(BlueIgnore.chat_id == chat_id))).scalar() or 0
        if chat_id != GLOBAL and count >= MAX_LOCAL_IGNORES:
            await msg.reply_text(f"A chat can ignore at most {MAX_LOCAL_IGNORES} commands.")
            return
        session.add(BlueIgnore(chat_id=chat_id, command=name))
        await session.commit()
    invalidate()
    await msg.reply_text(f"<b>{escape_html(name)}</b> has been added to the {scope}bluetext cleaner ignore list.", parse_mode="HTML")


async def _remove_ignore(update, context, chat_id: int, scope: str) -> None:
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("No command supplied to be unignored.")
        return
    name = normalize_command(context.args[0])
    if name is None:
        await msg.reply_text("That isn't a valid command name (letters, digits and _ only).")
        return
    async with async_session() as session:
        res = await session.execute(delete(BlueIgnore).where(BlueIgnore.chat_id == chat_id, BlueIgnore.command == name))
        await session.commit()
    invalidate()
    await msg.reply_text(
        f"<b>{escape_html(name)}</b> has been removed from the {scope}bluetext cleaner ignore list." if res.rowcount
        else "Command isn't ignored currently.", parse_mode="HTML")


@admin_only
@group_only
@safe_handler
async def ignoreblue_cmd(update, context) -> None:
    """/ignoreblue <command> - never delete this command in this chat."""
    await _add_ignore(update, context, update.effective_chat.id, "")


@admin_only
@group_only
@safe_handler
async def unignoreblue_cmd(update, context) -> None:
    """/unignoreblue <command> - clean this command again in this chat."""
    await _remove_ignore(update, context, update.effective_chat.id, "")


@safe_handler
async def gignoreblue_cmd(update, context) -> None:
    """/gignoreblue <command> - never delete this command in any chat (sudo)."""
    if not settings.is_admin_id(update.effective_user.id):
        await update.effective_message.reply_text("🚫 Sudo only.")
        return
    await _add_ignore(update, context, GLOBAL, "global ")


@safe_handler
async def ungignoreblue_cmd(update, context) -> None:
    """/ungignoreblue <command> - remove a command from the global ignore list (sudo)."""
    if not settings.is_admin_id(update.effective_user.id):
        await update.effective_message.reply_text("🚫 Sudo only.")
        return
    await _remove_ignore(update, context, GLOBAL, "global ")


@admin_only
@safe_handler
async def listblue_cmd(update, context) -> None:
    """/listblue - commands the cleaner leaves alone (global and for this chat)."""
    chat = update.effective_chat
    async with async_session() as session:
        glob = (await session.execute(select(BlueIgnore.command).where(BlueIgnore.chat_id == GLOBAL).order_by(BlueIgnore.command))).scalars().all()
        local = [] if chat.type == "private" else (await session.execute(
            select(BlueIgnore.command).where(BlueIgnore.chat_id == chat.id).order_by(BlueIgnore.command))).scalars().all()
    if not glob and not local:
        await update.effective_message.reply_text("No commands are currently ignored from bluetext cleaning.")
        return
    parts = []
    if glob:
        parts.append("Ignored globally:\n" + "\n".join(f" - <code>{escape_html(c)}</code>" for c in glob))
    if local:
        parts.append("Ignored in this chat:\n" + "\n".join(f" - <code>{escape_html(c)}</code>" for c in local))
    await update.effective_message.reply_text("\n\n".join(parts), parse_mode="HTML")


def register(app) -> None:
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, blue_listener), group=17)
    app.add_handler(CommandHandler("cleanblue", cleanblue_cmd))
    app.add_handler(CommandHandler("ignoreblue", ignoreblue_cmd))
    app.add_handler(CommandHandler("unignoreblue", unignoreblue_cmd))
    app.add_handler(CommandHandler("gignoreblue", gignoreblue_cmd))
    app.add_handler(CommandHandler("ungignoreblue", ungignoreblue_cmd))
    app.add_handler(CommandHandler("listblue", listblue_cmd))
