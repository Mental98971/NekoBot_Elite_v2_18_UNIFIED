"""Word blacklist — /addblacklist /unblacklist /blacklist /blacklistmode (v2.12).

The ``blacklist_words`` / ``blacklist_action`` columns existed since v2.x but nothing read or wrote
them. Matching lives in ``bot/utils/blacklist.py``; enforcement runs in the anti-spam middleware
(``bot/middleware/antispam.py``) so it happens before any command, filter or XP handler sees the
message and also catches edited messages.
"""
from __future__ import annotations

from telegram.ext import CommandHandler

from bot.core.database import Chat, async_session
from bot.utils import blacklist as bl, chatcache, modtools as mt
from bot.utils.helpers import escape_html

LIST_LIMIT = 100
MODES = ("delete", "warn", "mute", "kick", "ban", "tmute", "tban")
_MODE_ALIASES = {"nothing": "delete"}


def _after_command(update) -> str:
    text = update.effective_message.text or ""
    parts = text.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


@mt.mod_command("info")
async def addblacklist_cmd(update, context, ctx):
    """Blacklist words or phrases (one per line); matching messages are deleted: /addblacklist <word>"""
    msg = update.effective_message
    raw = _after_command(update)
    wanted = bl.split_entries(raw)
    if not wanted:
        await msg.reply_text(
            "Usage: /addblacklist <word or phrase> — one entry per line for several.\n"
            f"Whole words only (no substring hits), case-insensitive, <code>*</code> is a wildcard (<code>spam*</code>); max {bl.MAX_ENTRY_LEN} characters each.",
            parse_mode="HTML")
        return
    async with async_session() as session:
        chat = await mt.get_or_create_chat(session, ctx.chat_id, title=ctx.title)
        current = [e for e in (chat.blacklist_words or []) if isinstance(e, str)]
        fresh = [w for w in wanted if w not in current]
        room = bl.MAX_ENTRIES - len(current)
        accepted, overflow = fresh[:max(room, 0)], fresh[max(room, 0):]
        if accepted:
            chat.blacklist_words = current + accepted      # a new list object, so the JSON change is detected
            await session.commit()
    lines = []
    if accepted:
        lines.append("✅ Blacklisted: " + ", ".join(f"<code>{escape_html(w)}</code>" for w in accepted))
    if len(fresh) < len(wanted):
        lines.append(f"{len(wanted) - len(fresh)} already on the list.")
    if overflow:
        lines.append(f"⚠️ The list is full ({bl.MAX_ENTRIES} entries) — {len(overflow)} not added.")
    problem = await mt.check_bot(context, ctx.chat_id, "delete")
    if accepted and problem:
        lines.append(f"⚠️ {escape_html(problem)} Until I can delete messages this does nothing.")
    await msg.reply_text("\n".join(lines) or "Nothing to add.", parse_mode="HTML")


@mt.mod_command("info")
async def unblacklist_cmd(update, context, ctx):
    """Remove blacklisted words (one per line): /unblacklist <word>"""
    msg = update.effective_message
    wanted = bl.split_entries(_after_command(update))
    if not wanted:
        await msg.reply_text("Usage: /unblacklist <word or phrase>")
        return
    async with async_session() as session:
        chat = await session.get(Chat, ctx.chat_id)
        current = [e for e in ((chat.blacklist_words if chat else None) or []) if isinstance(e, str)]
        removed = [w for w in wanted if w in current]
        if removed:
            chat.blacklist_words = [e for e in current if e not in removed]
            await session.commit()
    missing = len(wanted) - len(removed)
    text = ("🗑 Removed: " + ", ".join(f"<code>{escape_html(w)}</code>" for w in removed)) if removed else "None of those were blacklisted."
    if removed and missing:
        text += f"\n{missing} weren't on the list."
    await msg.reply_text(text, parse_mode="HTML")


@mt.mod_command(None)
async def blacklist_cmd(update, context, ctx):
    """List this chat's blacklisted words: /blacklist"""
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    words = [w for w in ((snap.blacklist_words if snap else None) or []) if isinstance(w, str)]
    if not words:
        await update.effective_message.reply_text("The blacklist is empty. Add words with /addblacklist.")
        return
    shown = sorted(words)[:LIST_LIMIT]
    lines = [f"🚫 <b>Blacklist</b> ({len(words)})"] + [f"• <code>{escape_html(w)}</code>" for w in shown]
    if len(words) > len(shown):
        lines.append(f"<i>…and {len(words) - len(shown)} more</i>")
    action = (snap.blacklist_action if snap else None) or "warn"
    lines.append(f"Action: <b>{escape_html(action)}</b> — change with /blacklistmode")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


@mt.mod_command("info")
async def blacklistmode_cmd(update, context, ctx):
    """Choose what happens to the author: /blacklistmode <delete|warn|mute|kick|ban|tmute|tban> [time]"""
    msg = update.effective_message
    args = [a.lower() for a in (context.args or [])]
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    if not args:
        action = (snap.blacklist_action if snap else None) or "warn"
        seconds = getattr(snap, "blacklist_duration", None)
        extra = f" for {mt.format_span(seconds)}" if seconds and action in mt.TEMP_ACTIONS else ""
        await msg.reply_text(f"🚫 Blacklist action: <b>{escape_html(action)}</b>{extra}.\nOptions: {', '.join(MODES)}", parse_mode="HTML")
        return
    mode = _MODE_ALIASES.get(args[0], args[0])
    if mode not in MODES:
        await msg.reply_text(f"Usage: /blacklistmode <{'|'.join(MODES)}> [time]  (time applies to tmute / tban)")
        return
    seconds = None
    if mode in mt.TEMP_ACTIONS and len(args) > 1:
        seconds = mt.parse_duration(args[1])
        try:
            if seconds is None:
                raise mt.DurationError("Give the time like 30m, 2h or 1d.")
            mt.validate_duration(seconds)
        except mt.DurationError as exc:
            await msg.reply_text(f"❌ {exc}")
            return
    await mt.update_chat(ctx.chat_id, blacklist_action=mode, blacklist_duration=seconds)
    span = f" for {mt.format_span(seconds)}" if seconds else ""
    await msg.reply_text(f"🚫 Blacklisted messages are deleted, and their author gets: <b>{mode}</b>{span}.", parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler(["addblacklist", "blacklistadd"], addblacklist_cmd))
    app.add_handler(CommandHandler(["unblacklist", "rmblacklist"], unblacklist_cmd))
    app.add_handler(CommandHandler("blacklist", blacklist_cmd))
    app.add_handler(CommandHandler(["blacklistmode", "blmode", "blacklistaction", "blaction"], blacklistmode_cmd))
