"""
Cleanmode — from YukkiMusicBot's misc/cleanmode.py.

Scope note: this toggles Chat.cleanmode_enabled, which
bot/utils/helpers.py's reply_with_cleanup() checks before scheduling a
sent message for deletion. It's wired into the live-music plugin's
highest-traffic replies (play/skip/nowplaying confirmations) — see
plugins/music_live.py — rather than every reply_text call in the
project; see reply_with_cleanup's docstring for why.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.errors import safe_handler
from bot.core.database import Chat, async_session
from bot.core.decorators import admin_only, group_only


@group_only
@admin_only
@safe_handler
async def cleanmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.message.reply_text(
            f"Usage: /cleanmode on|off\nWhen on, music command replies "
            f"auto-delete after {settings.cleanmode_delete_minutes} minutes."
        )
        return
    async with async_session() as session:
        row = await session.get(Chat, update.effective_chat.id)
        if row is None:
            row = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(row)
        row.cleanmode_enabled = arg == "on"
        await session.commit()
    await update.message.reply_text(f"🧹 Cleanmode is now {'ON' if arg == 'on' else 'OFF'}.")


def register(app):
    app.add_handler(CommandHandler("cleanmode", cleanmode_cmd))
