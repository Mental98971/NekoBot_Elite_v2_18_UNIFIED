"""
Group Management Suite
Welcome/Goodbye, Rules, Notes, Filters, Locks, Nightmode, CAPTCHA, Disable/Enable

(/log and /report moved to plugins/modsettings.py in v2.12.) Notes, filters, locks, rules and
nightmode honour ``/connect`` and anonymous admins through ``bot/utils/modtools.py``.
"""
import functools
import logging
import re
import time
from datetime import datetime, time as dt_time, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import delete as sa_delete, or_, select
from sqlalchemy.exc import IntegrityError
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.core.database import Chat, Note, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils import chatcache, modtools as mt
from bot.utils.helpers import escape_html, mention_html
from bot.utils.telegram_safe import html_or_plain
from bot.utils.textutil import render_template

logger = logging.getLogger("bot.plugins.group_mgmt")


# ─── Welcome / Goodbye ───
@admin_only
@group_only
@safe_handler
async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.reply_to_message.text if update.message.reply_to_message else " ".join(context.args)
    if not text:
        await update.message.reply_text("Reply to a message or provide text.\nVariables: {first}, {last}, {username}, {mention}, {chat}")
        return

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.welcome_text = text
        await session.commit()

    await update.message.reply_text("✅ Welcome message updated.")


@admin_only
@group_only
@safe_handler
async def setgoodbye_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.reply_to_message.text if update.message.reply_to_message else " ".join(context.args)
    if not text:
        await update.message.reply_text("Reply to a message or provide text.")
        return

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.goodbye_text = text
        chat.goodbye_enabled = True
        await session.commit()

    await update.message.reply_text("✅ Goodbye message updated.")


def _member_template_values(user, chat) -> dict:
    """Values for {first} {last} {username} {mention} {chat} — all HTML-safe."""
    return {
        "first": escape_html(user.first_name or ""),
        "last": escape_html(user.last_name or ""),
        "username": escape_html(user.username or ""),
        "mention": mention_html(user.id, user.first_name or str(user.id)),
        "chat": escape_html(chat.title or ""),
    }


def _member_template_plain_values(user, chat) -> dict:
    """The same variables as raw text, for the plain-text fallback (no markup, so no escaping)."""
    return {
        "first": user.first_name or "",
        "last": user.last_name or "",
        "username": user.username or "",
        "mention": user.first_name or str(user.id),
        "chat": chat.title or "",
    }


async def _send_member_notice(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, users, template_attr: str, flag_attr: str, default_enabled: bool,
    track_welcome: bool = False,
) -> None:
    chat = update.effective_chat
    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        if not row:
            return
        enabled = getattr(row, flag_attr)
        if enabled is None:  # rows created before the column existed carry NULL: use the column's default
            enabled = default_enabled
        template = getattr(row, template_attr)
        if not enabled or not template:
            return
        clean_previous = bool(getattr(row, "clean_welcome", None)) and track_welcome
        previous_id = getattr(row, "last_welcome_id", None)
    send = functools.partial(context.bot.send_message, chat.id)
    last = None
    # A bulk add can list many users in one service message; greet a handful, not a flood.
    for user in [u for u in users if not u.is_bot][:5]:
        try:
            sent = await html_or_plain(
                send,
                render_template(template, _member_template_values(user, chat)),
                plain=render_template(template, _member_template_plain_values(user, chat)),
            )
            last = sent or last
        except Exception:
            logger.debug("member_notice_failed chat_id=%s", chat.id, exc_info=True)
    if clean_previous and last is not None:
        if previous_id:
            try:
                await context.bot.delete_message(chat.id, previous_id)
            except TelegramError:
                pass
        await mt.update_chat(chat.id, last_welcome_id=last.message_id)


@safe_handler
async def welcome_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greet new members. Registered on NEW_CHAT_MEMBERS *message* updates, so the
    joiners live on ``message.new_chat_members`` (``update.chat_member`` is only
    set for ChatMemberUpdated updates, which this handler never receives)."""
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    users = list(msg.new_chat_members)
    try:  # members still waiting on the /welcomemute strong check are greeted after they verify
        from bot.plugins.welcomemute import filter_deferred
        users = await filter_deferred(update.effective_chat.id, users)
    except Exception:
        logger.debug("welcome_deferral_unavailable", exc_info=True)
    if not users:
        return
    await _send_member_notice(update, context, users=users, template_attr="welcome_text", flag_attr="welcome_enabled", default_enabled=True, track_welcome=True)


@safe_handler
async def goodbye_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the /setgoodbye text when a member leaves (it was stored but never sent before)."""
    msg = update.effective_message
    if not msg or not msg.left_chat_member:
        return
    await _send_member_notice(update, context, users=[msg.left_chat_member], template_attr="goodbye_text", flag_attr="goodbye_enabled", default_enabled=False)


@admin_only
@group_only
@safe_handler
async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/welcome [on|off] — show the current welcome message, or switch it on/off."""
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("", "on", "off"):
        await update.message.reply_text("Usage: /welcome [on|off]")
        return
    chat = update.effective_chat
    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        if arg and not row:
            row = Chat(id=chat.id, title=chat.title, type=chat.type)
            session.add(row)
        if arg:
            row.welcome_enabled = arg == "on"
            await session.commit()
        enabled = True if row is None or row.welcome_enabled is None else bool(row.welcome_enabled)
        text = row.welcome_text if row else None
    if arg:
        await update.message.reply_text(f"👋 Welcome messages are now {'ON' if enabled else 'OFF'}.")
        return
    body = f"<pre>{escape_html(text)}</pre>" if text else "No welcome text yet — set one with /setwelcome &lt;text&gt;."
    await update.message.reply_text(
        f"👋 <b>Welcome message:</b> {'ON' if enabled else 'OFF'}\n{body}\n"
        "Variables: <code>{first} {last} {username} {mention} {chat}</code>",
        parse_mode="HTML",
    )


# ─── Rules ───
MAX_RULES = 3500


@mt.mod_command(None)
async def setrules_cmd(update, context, ctx):
    """Set the chat rules: reply to a message, or /setrules <text>"""
    msg = update.effective_message
    reply = None if ctx.remote else mt.real_reply(msg)
    text = ((reply.text or reply.caption) if reply is not None else " ".join(context.args or [])) or ""
    text = text.strip()
    if not text:
        await msg.reply_text("Reply to a message or provide rules text.")
        return
    if len(text) > MAX_RULES:
        await msg.reply_text(f"Rules are limited to {MAX_RULES} characters (this is {len(text)}).")
        return
    await mt.update_chat(ctx.chat_id, rules=text)
    await msg.reply_text("✅ Rules updated.")


@safe_handler
async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the chat rules: /rules"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    msg, user = update.effective_message, update.effective_user
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    if snap is None or not snap.rules:
        await msg.reply_text("No rules set. Admins can set with /setrules")
        return
    body = f"<b>📜 Rules</b>\n\n{snap.rules}"
    if snap.private_rules and not ctx.remote and update.effective_chat.type != "private":
        try:
            await html_or_plain(functools.partial(context.bot.send_message, user.id), body)
            await msg.reply_text("📬 I sent you the rules in a private message.")
        except Forbidden:
            username = getattr(context.bot, "username", None)
            markup = (InlineKeyboardMarkup([[InlineKeyboardButton("Start me first", url=f"https://t.me/{username}")]])
                      if isinstance(username, str) else None)
            await msg.reply_text("I can't message you yet — start me in private first, then run /rules again.", reply_markup=markup)
        return
    await html_or_plain(msg.reply_text, body)


async def _toggle_setting(update, context, ctx, column: str, label: str, *, needs_delete_right: bool = False, default: bool = False) -> None:
    """Shared body of the on/off commands: ``/cmd`` shows, ``/cmd on|off`` sets."""
    msg = update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    current = default if snap is None or getattr(snap, column) is None else bool(getattr(snap, column))
    if arg not in ("on", "off"):
        await msg.reply_text(f"{label} is <b>{'on' if current else 'off'}</b>. Use on|off to change it.", parse_mode="HTML")
        return
    if arg == "on" and needs_delete_right:
        problem = await mt.check_bot(context, ctx.chat_id, "delete")
        if problem:
            await msg.reply_text(f"🚫 {label} works by deleting messages. {problem}")
            return
    await mt.update_chat(ctx.chat_id, **{column: arg == "on"})
    await msg.reply_text(f"{label} is now <b>{arg}</b>.", parse_mode="HTML")


@mt.mod_command(None)
async def goodbye_cmd(update, context, ctx):
    """Turn goodbye messages on or off: /goodbye [on|off]"""
    await _toggle_setting(update, context, ctx, "goodbye_enabled", "👋 Goodbye messages")


@mt.mod_command(None)
async def resetwelcome_cmd(update, context, ctx):
    """Remove the welcome message text: /resetwelcome"""
    await mt.update_chat(ctx.chat_id, welcome_text=None)
    await update.effective_message.reply_text("✅ Welcome message cleared.")


@mt.mod_command(None)
async def resetgoodbye_cmd(update, context, ctx):
    """Remove the goodbye message text: /resetgoodbye"""
    await mt.update_chat(ctx.chat_id, goodbye_text=None)
    await update.effective_message.reply_text("✅ Goodbye message cleared.")


@mt.mod_command(None)
async def clearrules_cmd(update, context, ctx):
    """Remove the chat rules: /clearrules"""
    await mt.update_chat(ctx.chat_id, rules=None)
    await update.effective_message.reply_text("✅ Rules cleared.")


@mt.mod_command("info")
async def privaterules_cmd(update, context, ctx):
    """Send /rules in a private message instead of the group: /privaterules [on|off]"""
    await _toggle_setting(update, context, ctx, "private_rules", "📬 Private rules")


@mt.mod_command("info")
async def cleanwelcome_cmd(update, context, ctx):
    """Delete the previous welcome message when a new one is sent: /cleanwelcome [on|off]"""
    await _toggle_setting(update, context, ctx, "clean_welcome", "🧹 Clean welcome", needs_delete_right=True)


@mt.mod_command("info")
async def cleanservice_cmd(update, context, ctx):
    """Delete join / leave / pin service messages: /cleanservice [on|off]"""
    await _toggle_setting(update, context, ctx, "clean_service", "🧹 Clean service messages", needs_delete_right=True)


# ─── Notes ───
MAX_NOTE_NAME = 64
_NOTE_REPLY_METHODS = {
    "photo": "reply_photo", "video": "reply_video", "animation": "reply_animation", "document": "reply_document",
    "audio": "reply_audio", "voice": "reply_voice",
}
# animation before document: Telegram sets *both* on a GIF, and a GIF saved as a document stops animating
_NOTE_MEDIA_ORDER = ("photo", "video", "animation", "document", "audio", "voice", "sticker")


def _note_from_message(reply, inline_text: str):
    """``(content, file_id, message_type)`` for a note built from a replied-to message or inline text."""
    if reply is None:
        return (inline_text or None), None, "text"
    content = reply.text or reply.caption
    for attr in _NOTE_MEDIA_ORDER:
        media = getattr(reply, attr, None)
        if media:
            return content, (media[-1].file_id if attr == "photo" else media.file_id), attr
    return content, None, "text"


async def _upsert_note(ctx, name: str, content, file_id, kind: str, created_by: int) -> bool:
    """Create the note or replace the existing one of that name. True when it replaced one.

    v2.10 inserted a new row on every /save, so saving an existing name made /get crash with
    MultipleResultsFound. One row per (chat, name) is now a unique index; a concurrent first /save
    that loses the race hits it and simply retries as an update."""
    for attempt in (1, 2):
        try:
            async with async_session() as session:
                await mt.get_or_create_chat(session, ctx.chat_id, title=ctx.title)
                note = (await session.execute(
                    select(Note).where(Note.chat_id == ctx.chat_id, Note.name == name).limit(1)
                )).scalars().first()
                replaced = note is not None
                if note is None:
                    note = Note(chat_id=ctx.chat_id, name=name)
                    session.add(note)
                note.content, note.file_id, note.message_type, note.created_by = content, file_id, kind, created_by
                await session.commit()
                return replaced
        except IntegrityError:
            if attempt == 2:
                raise
    return True


async def _find_note(chat_id: int, name: str):
    async with async_session() as session:
        return (await session.execute(
            select(Note).where(Note.chat_id == chat_id, Note.name == name.lower().lstrip("#")).limit(1)
        )).scalars().first()


async def _send_note(message, note) -> None:
    kind = note.message_type or "text"
    try:
        if kind == "text":
            await message.reply_text(note.content or "")
        elif kind == "sticker":
            await message.reply_sticker(note.file_id)
        elif kind in _NOTE_REPLY_METHODS:
            await getattr(message, _NOTE_REPLY_METHODS[kind])(note.file_id, caption=note.content or None)
    except TelegramError:
        logger.debug("note_send_failed name=%s", note.name, exc_info=True)


@mt.mod_command(None)
async def save_cmd(update, context, ctx):
    """Save a note: reply to content with /save <name>, or /save <name> <text>"""
    msg = update.effective_message
    args = list(context.args or [])
    if not args:
        await msg.reply_text("Usage: /save <note name> (reply to the content, or add the text after the name)")
        return
    name = args[0].lower().lstrip("#")
    if not name or len(name) > MAX_NOTE_NAME:
        await msg.reply_text(f"Note names must be 1-{MAX_NOTE_NAME} characters.")
        return
    reply = None if ctx.remote else mt.real_reply(msg)
    content, file_id, kind = _note_from_message(reply, " ".join(args[1:]))
    if not content and not file_id:
        await msg.reply_text("Reply to a message with content to save, or give the text after the name.")
        return
    replaced = await _upsert_note(ctx, name, content, file_id, kind, update.effective_user.id)
    await msg.reply_text(f"✅ Note '<code>{escape_html(name)}</code>' {'updated' if replaced else 'saved'}!", parse_mode="HTML")


@safe_handler
async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a saved note: /get <name> (or just write #name)"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /get <note name> — or write #name in the chat.")
        return
    note = await _find_note(ctx.chat_id, context.args[0])
    if note is None:
        await update.effective_message.reply_text("No note with that name. /notes lists them.")
        return
    await _send_note(update.effective_message, note)


@safe_handler
async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List this chat's notes: /notes"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    async with async_session() as session:
        notes = (await session.execute(select(Note).where(Note.chat_id == ctx.chat_id).order_by(Note.name))).scalars().all()
    if not notes:
        await update.effective_message.reply_text("📝 No notes saved.")
        return
    lines = ["<b>📝 Saved Notes</b>\n"] + [f"• <code>{escape_html(n.name)}</code> ({escape_html(n.message_type or 'text')})" for n in notes]
    lines.append("\n<i>/get &lt;name&gt; or #name</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


@mt.mod_command(None)
async def clear_cmd(update, context, ctx):
    """Delete a saved note: /clear <name>"""
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /clear <note name>")
        return
    name = context.args[0].lower().lstrip("#")
    async with async_session() as session:
        result = await session.execute(sa_delete(Note).where(Note.chat_id == ctx.chat_id, Note.name == name))
        await session.commit()
    await msg.reply_text(f"🗑 Note <code>{escape_html(name)}</code> removed." if result.rowcount
                         else f"No note named <code>{escape_html(name)}</code>.", parse_mode="HTML")


@safe_handler
async def clearall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete every note of this chat (owner only, asks first): /clearall"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    msg = update.effective_message
    if mt.is_anonymous_admin(update, ctx.chat_id) or not await mt.is_chat_owner(context.bot, ctx.chat_id, update.effective_user.id):
        await msg.reply_text("🚫 Only the chat owner can delete every note at once.")
        return
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Delete all notes", callback_data=f"clearnotes:y:{ctx.chat_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="clearnotes:n"),
    ]])
    await msg.reply_text("Delete <b>every</b> note in this chat? This can't be undone.", parse_mode="HTML", reply_markup=markup)


@safe_handler
async def clearall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) < 3 or parts[1] != "y":
        await query.answer("Cancelled")
        await query.edit_message_text("Cancelled — no notes were deleted.")
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("That button is no longer valid.", show_alert=True)
        return
    if not await mt.is_chat_owner(context.bot, chat_id, query.from_user.id):
        await query.answer("Only the chat owner can do this.", show_alert=True)
        return
    async with async_session() as session:
        result = await session.execute(sa_delete(Note).where(Note.chat_id == chat_id))
        await session.commit()
    await query.answer("Done")
    await query.edit_message_text(f"🗑 Deleted {result.rowcount or 0} note(s).")


# ─── Filters ───
# Stored as chat.filters[trigger] = {"type": "text|photo|video|animation|sticker", "text"/"file_id"/"caption": ...}
MAX_FILTERS = 200
MAX_TRIGGER = 64


def _normalize_filters(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, str):
            out[str(k).lower()] = {"type": "text", "text": v}
        elif isinstance(v, dict) and v.get("type"):
            out[str(k).lower()] = v
    return out


@functools.lru_cache(maxsize=2048)
def _trigger_regex(trigger: str):
    return re.compile(r"(?<!\w)" + re.escape(trigger) + r"(?!\w)", re.IGNORECASE | re.UNICODE)


def match_filter(text: str, triggers) -> Optional[str]:
    """The trigger that fires for ``text``: a whole-word / whole-phrase match, longest trigger first.

    v2.10 used ``trigger in text``, so a filter on "hi" fired on "this" and "pi" on "spider", and
    which of several matching filters answered depended on dict insertion order."""
    hits = [t for t in triggers if t and _trigger_regex(t).search(text)]
    return max(hits, key=len) if hits else None


async def _save_filter(chat_id: int, title: str, chat_type: str, trigger: str, payload: dict) -> bool:
    """Store the filter. False when the chat is at ``MAX_FILTERS`` and ``trigger`` is new."""
    async with async_session() as session:
        chat = await mt.get_or_create_chat(session, chat_id, title=title, chat_type=chat_type)
        filters_map = _normalize_filters(chat.filters)
        if trigger not in filters_map and len(filters_map) >= MAX_FILTERS:
            return False
        filters_map[trigger] = payload
        chat.filters = filters_map
        await session.commit()
        return True


def _filter_payload(reply):
    if reply.photo:
        return {"type": "photo", "file_id": reply.photo[-1].file_id, "caption": reply.caption or ""}
    if reply.video:
        return {"type": "video", "file_id": reply.video.file_id, "caption": reply.caption or ""}
    if reply.animation:
        return {"type": "animation", "file_id": reply.animation.file_id, "caption": reply.caption or ""}
    if reply.sticker:
        return {"type": "sticker", "file_id": reply.sticker.file_id}
    if reply.text or reply.caption:
        return {"type": "text", "text": reply.text or reply.caption}
    return None


@mt.mod_command(None)
async def addfilter_cmd(update, context, ctx):
    """Add a keyword filter: reply to content with /filter <keyword>, or /filter <keyword> <text>"""
    msg = update.effective_message
    reply = None if ctx.remote else mt.real_reply(msg)
    args = list(context.args or [])
    payload, trigger = None, (args[0].lower() if args else "")
    if args and reply is not None:
        payload = _filter_payload(reply)
        if payload is None:
            await msg.reply_text("Unsupported media for filters.")
            return
    elif len(args) >= 2:
        payload = {"type": "text", "text": " ".join(args[1:])}
    if payload is None:
        await msg.reply_text(
            "Usage (admins):\n"
            "• Reply to a message with <code>/filter keyword</code>\n"
            "• <code>/filter keyword response text</code>\n"
            "• <code>/filters</code> · <code>/stopfilter keyword</code> · <code>/stopallfilters</code>",
            parse_mode="HTML")
        return
    if len(trigger) > MAX_TRIGGER:
        await msg.reply_text(f"Keywords are limited to {MAX_TRIGGER} characters.")
        return
    if not await _save_filter(ctx.chat_id, ctx.title, "supergroup", trigger, payload):
        await msg.reply_text(f"This chat already has {MAX_FILTERS} filters — remove some with /stopfilter first.")
        return
    await msg.reply_text(f"✅ Filter <code>{escape_html(trigger)}</code> {'saved (from replied message)' if reply is not None else 'added'}.", parse_mode="HTML")


@mt.mod_command(None)
async def listfilters_cmd(update, context, ctx):
    """List this chat's filters: /filters"""
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    fmap = _normalize_filters(snap.filters if snap else None)
    if not fmap:
        await update.effective_message.reply_text("No filters in this chat.")
        return
    lines = ["<b>Active filters</b>"]
    for k, v in sorted(fmap.items()):
        kind = v.get("type", "text")
        preview = (v.get("text") or v.get("caption") or kind)[:40]
        lines.append(f"• <code>{escape_html(k)}</code> → [{escape_html(kind)}] {escape_html(str(preview))}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


@mt.mod_command(None)
async def stopfilter_cmd(update, context, ctx):
    """Remove a keyword filter: /stopfilter <keyword>"""
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /stopfilter <keyword>")
        return
    trigger = context.args[0].lower()
    async with async_session() as session:
        chat = await session.get(Chat, ctx.chat_id)
        fmap = _normalize_filters(chat.filters if chat else None)
        if trigger not in fmap:
            await msg.reply_text(f"No filter named <code>{escape_html(trigger)}</code>.", parse_mode="HTML")
            return
        del fmap[trigger]
        chat.filters = fmap
        await session.commit()
    await msg.reply_text(f"🗑 Removed filter <code>{escape_html(trigger)}</code>.", parse_mode="HTML")


@mt.mod_command(None)
async def stopallfilters_cmd(update, context, ctx):
    """Remove every filter of this chat: /stopallfilters"""
    async with async_session() as session:
        chat = await session.get(Chat, ctx.chat_id)
        if chat is not None:
            chat.filters = {}
            await session.commit()
    await update.effective_message.reply_text("🗑 All filters removed.")


async def _send_filter_reply(message, payload: dict) -> None:
    kind = payload.get("type", "text")
    if kind == "text":
        await message.reply_text(payload.get("text") or "")
    elif kind == "photo":
        await message.reply_photo(payload["file_id"], caption=payload.get("caption") or None)
    elif kind == "video":
        await message.reply_video(payload["file_id"], caption=payload.get("caption") or None)
    elif kind == "animation":
        await message.reply_animation(payload["file_id"], caption=payload.get("caption") or None)
    elif kind == "sticker":
        await message.reply_sticker(payload["file_id"])


@safe_handler
async def filter_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answer keyword filters, and ``#name`` note shortcuts, in group text messages."""
    msg = update.message
    if not msg or not msg.text or update.effective_chat.type == "private":
        return
    text = msg.text
    chat_id = update.effective_chat.id
    if text.startswith("#") and len(text) > 1 and not text[1].isspace():
        note = await _find_note(chat_id, text[1:].split()[0])
        if note is not None:
            await _send_note(msg, note)
            return
    snap = await chatcache.get_chat_settings(chat_id)
    if snap is None or not snap.filters:
        return
    fmap = _normalize_filters(snap.filters)
    trigger = match_filter(text, fmap)
    if trigger is None:
        return
    try:
        await _send_filter_reply(msg, fmap[trigger])
    except (TelegramError, KeyError):
        logger.debug("filter_reply_failed trigger=%s", trigger, exc_info=True)


# ─── Locks ───
LOCK_TYPES = {
    "url": "lock_url",
    "forward": "lock_forward",
    "photo": "lock_photo",
    "video": "lock_video",
    "sticker": "lock_sticker",
    "gif": "lock_gif",
    "contact": "lock_contact",
    "location": "lock_location",
}
LOCK_NOTICE_COOLDOWN = 30.0
_lock_notice: Dict[Tuple[int, int], float] = {}


@mt.mod_command(None)
async def lock_cmd(update, context, ctx):
    """Lock a content type for non-admins: /lock <url|forward|photo|video|sticker|gif|contact|location>"""
    msg = update.effective_message
    lock_type = (context.args[0].lower() if context.args else "")
    if lock_type not in LOCK_TYPES:
        await msg.reply_text(f"Usage: /lock <type>\nTypes: {', '.join(LOCK_TYPES)}")
        return
    problem = await mt.check_bot(context, ctx.chat_id, "delete")
    if problem:     # a lock the bot cannot enforce would only look like protection
        await msg.reply_text(f"🚫 Locks work by deleting messages. {problem}")
        return
    await mt.update_chat(ctx.chat_id, **{LOCK_TYPES[lock_type]: True})
    await msg.reply_text(f"🔒 {lock_type.title()} locked.")


@mt.mod_command(None)
async def unlock_cmd(update, context, ctx):
    """Unlock a content type: /unlock <type>"""
    msg = update.effective_message
    lock_type = (context.args[0].lower() if context.args else "")
    if lock_type not in LOCK_TYPES:
        await msg.reply_text(f"Usage: /unlock <type>\nTypes: {', '.join(LOCK_TYPES)}")
        return
    await mt.update_chat(ctx.chat_id, **{LOCK_TYPES[lock_type]: False})
    await msg.reply_text(f"🔓 {lock_type.title()} unlocked.")


@safe_handler
async def locks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List which content types are locked: /locks"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    row = await chatcache.get_chat_settings(ctx.chat_id)
    locked = [name for name, col in LOCK_TYPES.items() if row and getattr(row, col)]
    open_ = [name for name in LOCK_TYPES if name not in locked]
    lines = [f"🔒 <b>Locked:</b> {', '.join(locked) if locked else 'nothing'}"]
    if open_:
        lines.append(f"🔓 <b>Open:</b> {', '.join(open_)}")
    lines.append("Admins: /lock &lt;type&gt; · /unlock &lt;type&gt;")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def locktypes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List the content types /lock understands: /locktypes"""
    await update.effective_message.reply_text("🔐 Lockable types: " + ", ".join(LOCK_TYPES) + "\nUse /lock <type> and /unlock <type>.")


def _is_service_message(msg) -> bool:
    return any(getattr(msg, attr, None) for attr in (
        "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo", "pinned_message"))


@safe_handler
async def lock_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_obj = update.effective_chat
    user = update.effective_user
    if not msg or not chat_obj or not user:
        return

    chat = await chatcache.get_chat_settings(chat_obj.id)
    if chat is None:
        return
    if chat.clean_service and _is_service_message(msg):
        # Deleting the service message does not hide the update from later handler groups, so the
        # welcome / goodbye / raid listeners still see who joined or left.
        try:
            await msg.delete()
        except TelegramError:
            logger.debug("clean_service_delete_failed", exc_info=True)
        return
    if not any(getattr(chat, col) for col in LOCK_TYPES.values()):
        return  # nothing is locked here

    # Evaluate the (cheap, local) lock rules first; only a violation justifies the admin lookup.
    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    violated = any((
        chat.lock_url and any(e.type in ("url", "text_link") for e in entities),
        chat.lock_forward and msg.forward_origin is not None,
        chat.lock_photo and msg.photo,
        chat.lock_video and msg.video,
        chat.lock_sticker and msg.sticker,
        chat.lock_gif and msg.animation,
        chat.lock_contact and msg.contact,
        chat.lock_location and (msg.location or msg.venue),
    ))
    if not violated:
        return
    # Admins, approved users, owners and anonymous-admin / channel identities are never locked out
    # (before v2.12 an anonymous admin's own post was deleted: @GroupAnonymousBot is not a member).
    if await mt.is_exempt(context.bot, chat_obj.id, user.id):
        return

    try:
        await msg.delete()
    except TelegramError:
        logger.debug("lock_listener could not delete", exc_info=True)
        return
    now, key = time.monotonic(), (chat_obj.id, user.id)
    if now - _lock_notice.get(key, -LOCK_NOTICE_COOLDOWN) >= LOCK_NOTICE_COOLDOWN:
        if len(_lock_notice) > 5000:
            _lock_notice.clear()
        _lock_notice[key] = now
        try:
            await context.bot.send_message(
                chat_obj.id, f"🚫 {mention_html(user.id, mt.display_name(user))}, this content is locked.", parse_mode="HTML")
        except TelegramError:
            logger.debug("lock_listener could not notify", exc_info=True)


# ─── Nightmode ───
# While the window is open the chat's default permissions are replaced by "nobody may post". The
# previous permissions are saved and restored at dawn. v2.10 instead rewrote the permissions *every
# minute* and, by day, granted all members can_change_info / can_pin_messages / can_invite_users -
# silently handing out admin-like rights - and a chat whose night mode was switched off during the
# night stayed muted forever.
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_NIGHT_PERMS = ChatPermissions(can_send_messages=False)
_SAFE_DAY_PERMS = ChatPermissions(can_send_messages=True, can_send_polls=True,
                                  can_send_other_messages=True, can_add_web_page_previews=True)
_night_failures: Dict[int, int] = {}


def _hhmm(value: str) -> str:
    h, m = value.split(":")
    return f"{int(h):02d}:{int(m):02d}"


def in_night_window(now: dt_time, start: str, end: str) -> bool:
    """Is ``now`` inside [start, end) - the window may wrap past midnight (23:00 -> 06:00)."""
    s, e = dt_time(*map(int, start.split(":"))), dt_time(*map(int, end.split(":")))
    if s == e:
        return False
    return s <= now < e if s < e else (now >= s or now < e)


async def _begin_night(context: ContextTypes.DEFAULT_TYPE, row) -> None:
    chat = await context.bot.get_chat(row.id)
    row.nightmode_prev_permissions = chat.permissions.to_dict() if chat.permissions else None
    await context.bot.set_chat_permissions(row.id, _NIGHT_PERMS)
    row.nightmode_active = True


async def _end_night(context: ContextTypes.DEFAULT_TYPE, row) -> None:
    prev = row.nightmode_prev_permissions
    perms = ChatPermissions.de_json(prev, context.bot) if prev else _SAFE_DAY_PERMS
    await context.bot.set_chat_permissions(row.id, perms or _SAFE_DAY_PERMS)
    row.nightmode_active = False
    row.nightmode_prev_permissions = None


@mt.mod_command("info")
async def nightmode_cmd(update, context, ctx):
    """Mute the chat during set hours (UTC): /nightmode [on|off|<start HH:MM> <end HH:MM>]"""
    msg = update.effective_message
    args = [a.lower() for a in (context.args or [])]
    async with async_session() as session:
        row = await mt.get_or_create_chat(session, ctx.chat_id, title=ctx.title)
        enable = None
        if not args:
            enable = not bool(row.nightmode_enabled)
        elif len(args) == 1 and args[0] in ("on", "off"):
            enable = args[0] == "on"
        elif len(args) == 2 and _HHMM.match(args[0]) and _HHMM.match(args[1]) and _hhmm(args[0]) != _hhmm(args[1]):
            row.nightmode_start, row.nightmode_end = _hhmm(args[0]), _hhmm(args[1])
        else:
            await msg.reply_text("Usage: /nightmode [on|off] or /nightmode <start HH:MM> <end HH:MM> (UTC, e.g. /nightmode 23:00 06:00)")
            return
        if enable is True:
            problem = await mt.check_bot(context, ctx.chat_id, "restrict")
            if problem:
                await msg.reply_text(f"🚫 Night mode changes the chat's permissions. {problem}")
                return
            row.nightmode_enabled = True
        elif enable is False:
            row.nightmode_enabled = False
            if row.nightmode_active:
                try:
                    await _end_night(context, row)
                except TelegramError as exc:
                    await msg.reply_text(f"❌ Couldn't lift the night restriction: {mt.friendly_telegram_error(exc)}")
                    return
        await session.commit()
        state = "enabled 🌙" if row.nightmode_enabled else "disabled ☀️"
        window = f"{row.nightmode_start or '23:00'}–{row.nightmode_end or '06:00'} UTC"
    await msg.reply_text(f"Nightmode {state} ({window}).")


def _night_retry_due(chat_id: int) -> bool:
    """After 5 failed attempts a chat (bot removed, rights revoked) is retried only every ~30 minutes."""
    failures = _night_failures.get(chat_id, 0)
    return failures < 5 or failures % 30 == 0


async def nightmode_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc).time().replace(second=0, microsecond=0)
    async with async_session() as session:
        rows = (await session.execute(
            select(Chat).where(or_(Chat.nightmode_enabled.is_(True), Chat.nightmode_active.is_(True)))
        )).scalars().all()
        for row in rows:
            try:
                want_night = bool(row.nightmode_enabled and row.nightmode_lock is not False
                                  and in_night_window(now, row.nightmode_start or "23:00", row.nightmode_end or "06:00"))
            except (ValueError, AttributeError):
                logger.warning("nightmode_bad_window chat_id=%s start=%r end=%r", row.id, row.nightmode_start, row.nightmode_end)
                continue
            active = bool(row.nightmode_active)
            if want_night == active:
                _night_failures.pop(row.id, None)
                continue
            if not _night_retry_due(row.id):
                _night_failures[row.id] += 1
                continue
            try:
                await (_begin_night if want_night else _end_night)(context, row)
                _night_failures.pop(row.id, None)
                try:
                    await context.bot.send_message(row.id, "🌙 Night mode: the chat is closed until morning." if want_night
                                                   else "☀️ Night mode is over — the chat is open again.")
                except TelegramError:
                    pass
            except TelegramError as exc:
                _night_failures[row.id] = _night_failures.get(row.id, 0) + 1
                logger.warning("nightmode_transition_failed chat_id=%s want_night=%s error=%s", row.id, want_night, exc)
        await session.commit()


# ─── CAPTCHA ───
@mt.mod_command(None)
async def captcha_cmd(update, context, ctx):
    """Toggle the join CAPTCHA: /captcha [on|off]"""
    msg = update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    async with async_session() as session:
        row = await mt.get_or_create_chat(session, ctx.chat_id, title=ctx.title)
        row.captcha_enabled = (arg == "on") if arg in ("on", "off") else not bool(row.captcha_enabled)
        await session.commit()
        status = "enabled ✅" if row.captcha_enabled else "disabled ❌"
    await msg.reply_text(f"CAPTCHA {status}")


_UNDISABLEABLE = frozenset({"start", "help", "enable", "disable", "disabled", "connect", "disconnect"})


# ─── Disable / Enable (from FallenRobot/YaeMiko) ───
# The actual gate is in bot/middleware/access_control.py, which checks
# Chat.disabled_commands early (before the disabled command's own
# handler would even run) and lets this chat's own admins through
# regardless — /disable is for quieting down regular members, not
# locking admins out of a command they chose to turn off. These three
# commands just manage that list.
@mt.mod_command(None)
async def disable_cmd(update, context, ctx):
    """Disable a command for non-admins: /disable <command>"""
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /disable <command name, without the />")
        return
    cmd_name = context.args[0].lstrip("/").lower().split("@")[0]
    if not re.fullmatch(r"[a-z0-9_]{1,32}", cmd_name):
        await msg.reply_text("That doesn't look like a command name.")
        return
    if cmd_name in _UNDISABLEABLE:
        await msg.reply_text(f"/{cmd_name} can't be disabled — it's needed to manage the bot.")
        return
    async with async_session() as session:
        chat = await mt.get_or_create_chat(session, ctx.chat_id, title=ctx.title)
        disabled = list(chat.disabled_commands or [])
        if cmd_name in disabled:
            await msg.reply_text(f"/{cmd_name} is already disabled here.")
            return
        disabled.append(cmd_name)
        chat.disabled_commands = disabled
        await session.commit()
    await msg.reply_text(f"🔇 /{cmd_name} is now disabled for non-admins in this chat.")


@mt.mod_command(None)
async def enable_cmd(update, context, ctx):
    """Re-enable a disabled command: /enable <command>"""
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /enable <command name>")
        return
    cmd_name = context.args[0].lstrip("/").lower().split("@")[0]
    async with async_session() as session:
        chat = await session.get(Chat, ctx.chat_id)
        if not chat or cmd_name not in (chat.disabled_commands or []):
            await msg.reply_text(f"/{cmd_name} isn't disabled here.")
            return
        chat.disabled_commands = [c for c in chat.disabled_commands if c != cmd_name]
        await session.commit()
    await msg.reply_text(f"🔊 /{cmd_name} is enabled again.")


@safe_handler
async def disabled_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List the commands disabled in this chat: /disabled"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    disabled = (snap.disabled_commands if snap else None) or []
    if not disabled:
        await update.effective_message.reply_text("No commands are disabled here.")
        return
    await update.effective_message.reply_text("🔇 Disabled here: " + ", ".join(f"/{c}" for c in disabled))


def register(app):
    app.add_handler(CommandHandler("welcome", welcome_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
    app.add_handler(CommandHandler("setgoodbye", setgoodbye_cmd))
    app.add_handler(CommandHandler("setrules", setrules_cmd))
    app.add_handler(CommandHandler("clearrules", clearrules_cmd))
    app.add_handler(CommandHandler("goodbye", goodbye_cmd))
    app.add_handler(CommandHandler("resetwelcome", resetwelcome_cmd))
    app.add_handler(CommandHandler("resetgoodbye", resetgoodbye_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("privaterules", privaterules_cmd))
    app.add_handler(CommandHandler("cleanwelcome", cleanwelcome_cmd))
    app.add_handler(CommandHandler("cleanservice", cleanservice_cmd))
    app.add_handler(CommandHandler("save", save_cmd))
    app.add_handler(CommandHandler("get", get_cmd))
    app.add_handler(CommandHandler(["notes", "saved"], notes_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler(["clearall", "removeallnotes"], clearall_cmd))
    app.add_handler(CallbackQueryHandler(clearall_callback, pattern=r"^clearnotes:"))
    app.add_handler(CommandHandler("addfilter", addfilter_cmd))
    app.add_handler(CommandHandler("filter", addfilter_cmd))
    app.add_handler(CommandHandler("filters", listfilters_cmd))
    app.add_handler(CommandHandler("stopfilter", stopfilter_cmd))
    app.add_handler(CommandHandler(["stopallfilters", "removeallfilters"], stopallfilters_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("locks", locks_cmd))
    app.add_handler(CommandHandler("locktypes", locktypes_cmd))
    app.add_handler(CommandHandler("nightmode", nightmode_cmd))
    app.add_handler(CommandHandler("captcha", captcha_cmd))
    app.add_handler(CommandHandler("disable", disable_cmd))
    app.add_handler(CommandHandler("enable", enable_cmd))
    app.add_handler(CommandHandler("disabled", disabled_cmd))

    # Listeners
    # python-telegram-bot invokes only the FIRST matching handler within a
    # group, so every catch-all handler needs a group of its own (a shared
    # group silently starves all but one — this is what once made this file's
    # filter_listener dead code). tests/test_hardening_contracts.py enforces
    # this. The complete map, so additions don't reintroduce the collision:
    #   -7  join_guard                (welcomemute.py, NEW_CHAT_MEMBERS in groups; /welcomemute + /captcha)
    #   -6  ban_enforcer              (fedtools.py, ALL & GROUPS; removes gbanned/fbanned users first)
    #   -5  access_control_middleware (core/bot.py, all updates)
    #   -4  force_sub_middleware      (core/bot.py, all updates)
    #   -3  log_middleware            (core/bot.py, all updates)
    #   -2  antiflood_middleware      (core/bot.py, groups only; also slow mode)
    #   -1  antispam_middleware       (core/bot.py, groups only)
    #    0  raid_join_listener        (automod.py, NEW_CHAT_MEMBERS)
    #    0  every CommandHandler / CallbackQueryHandler / InlineQueryHandler
    #                                 (mutually exclusive with the message handlers)
    #    0  compose_listener          (whisper.py, PRIVATE & ~COMMAND while a whisper is being
    #                                 composed; shares 0 with raid_join_listener: disjoint filters,
    #                                 and it stops later groups so banter never answers the secret)
    #    1  lock_listener             (this file, ALL messages in groups; also clean-service)
    #    2  filter_listener           (this file, TEXT & ~COMMAND; also #note shortcuts)
    #    3  afk_listener              (fun.py, TEXT & ~COMMAND)
    #    4  welcome_listener          (this file, NEW_CHAT_MEMBERS)
    #    5  handle_message            (fonts.py, TEXT & ~COMMAND — auto-style)
    #    6  xp_listener               (economy.py, TEXT & ~COMMAND)
    #    7  spawn_listener            (collector.py, TEXT & ~COMMAND)
    #    8  upload_cmd caption path   (collector.py, PHOTO|VIDEO & CAPTION)
    #    8  handle_message            (personality.py, TEXT & ~COMMAND — banter;
    #                                 shares 8 with the caption handler: disjoint filters)
    #    9  name_history_middleware   (name_history.py, ALL & GROUPS)
    #   10  games_text_listener       (games.py, TEXT & ~COMMAND)
    #   11  antichannel_listener      (automod.py, ALL & GROUPS)
    #   12  goodbye_listener          (this file, LEFT_CHAT_MEMBER)
    #   13  blacklist_listener        (blacklist.py, TEXT|CAPTION in groups)
    #   14  activity_counter          (chatfight.py, group messages that are not commands/service)
    #   15  policy_listener           (warnfilters.py, ALL & GROUPS; warn filters + sticker-pack blacklist)
    #   16  sed_listener              (toolbox.py, TEXT & REPLY & Regex ^s/)
    #   17  blue_listener             (cleanblue.py, COMMAND in groups; deletes made-up commands when /cleanblue is on)
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, lock_listener), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, filter_listener), group=2)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_listener), group=4)
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye_listener), group=12)

    # Jobs
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(nightmode_job, interval=60, first=10)
