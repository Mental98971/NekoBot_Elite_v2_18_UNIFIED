"""/lang - choose the language the bot answers in (per chat).

    /lang            show the current language and the available ones
    /lang <code>     set it (group admins only in groups; anyone in a private chat)
    /lang auto       follow the Telegram language of whoever writes

Translated: /start, /help headings, permission and cooldown messages, this command.  AI replies (/ai, the Neko personality) are
told to answer in the chat language unless the user clearly writes in another one.  Command names and most feature output stay
as they are; catalogs are plain JSON in data/locales/ - add a file to add a language (tests/test_i18n.py checks it).
"""
from __future__ import annotations

import html
from typing import Optional

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from bot.core.errors import safe_handler
from bot.utils import i18n
from bot.utils.langctx import chat_language, set_chat_language, stored_language, supported
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_AUTO = {"auto", "reset", "default", "off", "clear"}


async def _may_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Private chats: yes.  Groups: admins and the owner (including admins posting anonymously).  Fails closed."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None:
        return False
    if chat.type == "private":
        return True
    sender_chat = getattr(msg, "sender_chat", None)
    if sender_chat is not None and sender_chat.id == chat.id:
        return True
    if user is None:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


def _available_lines(codes) -> str:
    return "\n".join(f"<code>{html.escape(c)}</code> — {html.escape(i18n.language_name(c))}" for c in codes)


@safe_handler
async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if msg is None or chat is None:
        return
    codes = supported()
    args = context.args or []
    current = await chat_language(update)

    if not args:
        explicit = await stored_language(chat.id)
        lines = [i18n.t("lang.title", current),
                 i18n.t("lang.current", current, name=html.escape(i18n.language_name(current)), code=current)]
        if not explicit:
            lines.append(i18n.t("lang.auto_note", current))
        lines += ["", i18n.t("lang.available", current), _available_lines(codes), "", i18n.t("lang.usage", current)]
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if not await _may_change(update, context):
        await msg.reply_text(i18n.t("lang.admins_only", current))
        return

    raw = args[0].strip().lower()
    new: Optional[str]
    if raw in _AUTO:
        new = None
    else:
        new = i18n.normalize_language(raw)
        if new is None or new not in codes:
            await msg.reply_text(i18n.t("lang.unknown", current, code=html.escape(raw[:12]), available=", ".join(codes)), parse_mode="HTML")
            return
    try:
        await set_chat_language(chat.id, chat.type, getattr(chat, "title", None), new)
    except Exception as exc:
        logger.warning("lang_save_failed", error=type(exc).__name__)
        await msg.reply_text(i18n.t("lang.fail", current))
        return
    if new is None:
        await msg.reply_text(i18n.t("lang.reset", current))
    else:  # confirm in the language that was just chosen
        await msg.reply_text(i18n.t("lang.set", new, name=html.escape(i18n.language_name(new))), parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler(["lang", "language", "setlang", "languages"], lang_cmd))
