"""Manage a group from a private chat — /connect /disconnect /connection /allowconnect (v2.12).

Ported from FallenRobot / Gojo_Satoru. Python-telegram-bot's ``Update`` objects are immutable, so
(unlike those bots) commands cannot be transparently re-pointed at another chat: each command that
supports it resolves its chat through ``modtools.resolve_ctx`` / ``@mod_command`` instead. Those are:
bans, mutes, kicks, warns, approvals, blacklist, notes, filters, locks, rules, captcha, command
disabling, anti-flood / slow mode / spam / warn / log / report settings, promote / demote / title,
admin list, invite link, group title / description and pinned-message link. Commands that need a
replied-to message (/purge, /del, /pin, /report) stay group-only.

Rules that keep this safe:
* only a current member of the chat can connect (and it is re-checked on every remote command, so
  leaving the chat ends the connection);
* every remote admin command still verifies the sender's admin right in *that* chat;
* a chat's admins can forbid connecting with ``/allowconnect off``.
"""
from __future__ import annotations

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import Forbidden, TelegramError
from telegram.ext import CommandHandler, ContextTypes

from bot.core.errors import safe_handler
from bot.utils import chatcache, modtools as mt
from bot.utils.helpers import escape_html

_IN_CHAT = mt._IN_CHAT


async def _connect_checks(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> str | None:
    """Why ``user_id`` may not connect to ``chat_id`` (user-facing text), or ``None``."""
    member = await mt.get_member(context.bot, chat_id, user_id)
    if member is None or member.status not in _IN_CHAT:
        return "You need to be a member of that chat first."
    snap = await chatcache.get_chat_settings(chat_id)
    if snap is not None and snap.connect_allowed is False and member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return "This chat's admins only allow admins to connect."
    return None


@safe_handler
async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage a group from your private chat: /connect (in the group) or /connect <chat id|@group> (here)"""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if mt.is_anonymous_admin(update) or user is None:
        await msg.reply_text("Anonymous admins can't connect — post as yourself.")
        return
    if chat.type in ("group", "supergroup"):
        problem = await _connect_checks(context, user.id, chat.id)
        if problem:
            await msg.reply_text(f"🚫 {problem}")
            return
        await mt.set_connection(user.id, chat.id)
        title = escape_html(chat.title or str(chat.id))
        try:
            await context.bot.send_message(user.id, f"🔗 Connected to <b>{title}</b>. Use its commands here; /disconnect to stop.", parse_mode="HTML")
            await msg.reply_text("🔗 Connected — I sent you a private message. Manage this group from there.")
        except Forbidden:
            await msg.reply_text("🔗 Connected, but I can't message you yet — open a chat with me and press Start.")
        return
    if chat.type != "private":
        return
    if not context.args:
        current = await mt.get_connection(user.id)
        if current is None:
            await msg.reply_text("Usage: /connect <chat id or @groupname> — or send /connect inside your group.")
        else:
            snap = await chatcache.get_chat_settings(current)
            await msg.reply_text(f"🔗 Connected to <b>{escape_html((snap.title if snap and snap.title else str(current)))}</b>. /disconnect to stop.", parse_mode="HTML")
        return
    arg = context.args[0]
    try:
        target = await context.bot.get_chat(int(arg) if arg.lstrip("-").isdigit() else arg)
    except TelegramError:
        await msg.reply_text("❌ I can't find that chat — is it correct, and am I a member?")
        return
    if target.type not in ("group", "supergroup"):
        await msg.reply_text("❌ That isn't a group.")
        return
    me = await mt.get_member(context.bot, target.id, context.bot.id)
    if me is None or me.status not in _IN_CHAT:
        await msg.reply_text("❌ I'm not a member of that chat.")
        return
    problem = await _connect_checks(context, user.id, target.id)
    if problem:
        await msg.reply_text(f"🚫 {problem}")
        return
    await mt.set_connection(user.id, target.id)
    await msg.reply_text(f"🔗 Connected to <b>{escape_html(target.title or str(target.id))}</b>.", parse_mode="HTML")


@safe_handler
async def disconnect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop managing a group from your private chat: /disconnect"""
    user = update.effective_user
    done = user is not None and await mt.clear_connection(user.id)
    await update.effective_message.reply_text("🔌 Disconnected." if done else "You weren't connected to any chat.")


@safe_handler
async def connection_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show which group you're connected to: /connection"""
    user = update.effective_user
    current = await mt.get_connection(user.id) if user is not None else None
    if current is None:
        await update.effective_message.reply_text("You're not connected to a group. Use /connect.")
        return
    snap = await chatcache.get_chat_settings(current)
    await update.effective_message.reply_text(
        f"🔗 Connected to <b>{escape_html((snap.title if snap and snap.title else str(current)))}</b> (<code>{current}</code>).", parse_mode="HTML")


@mt.mod_command("info")
async def allowconnect_cmd(update, context, ctx):
    """Allow or forbid non-admin members to /connect to this chat: /allowconnect [on|off]"""
    msg = update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    if arg in ("on", "off"):
        await mt.update_chat(ctx.chat_id, connect_allowed=(arg == "on"))
        await msg.reply_text(f"🔗 Connecting from private chats is now <b>{arg}</b> for members; admins can always connect.", parse_mode="HTML")
        return
    on = snap is None or snap.connect_allowed is None or bool(snap.connect_allowed)
    await msg.reply_text(f"🔗 Members may connect: <b>{'yes' if on else 'admins only'}</b>. Use /allowconnect on|off.", parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("connect", connect_cmd))
    app.add_handler(CommandHandler("disconnect", disconnect_cmd))
    app.add_handler(CommandHandler(["connection", "connected"], connection_cmd))
    app.add_handler(CommandHandler("allowconnect", allowconnect_cmd))
