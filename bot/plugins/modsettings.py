"""Per-chat moderation settings: anti-flood, slow mode, anti-spam switch, warn policy, log channel, reports.

New in v2.12. Before this plugin the anti-flood limit, the report log and the warn policy were
documented but could only be changed through global environment variables (or not at all), and
``/slowmode`` called a Bot API method that does not exist. Every setting here lives in the ``Chat``
row, is read through ``bot/utils/chatcache.py`` and is shown together by ``/modsettings``.
"""
from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Report, async_session
from bot.core.errors import safe_handler
from bot.middleware.antiflood import FLOOD_ACTIONS, flood_config
from bot.utils import chatcache, modtools as mt
from bot.utils.helpers import escape_html, mention_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

FLOOD_MIN, FLOOD_MAX = 3, 100
WARN_LIMIT_MAX = 100
SLOWMODE_MAX = 3600
REPORT_COOLDOWN = 30.0
REPORT_EXCERPT = 300
REPORT_MENTION_CAP = 30
_OFF_WORDS = {"off", "no", "false", "disable", "disabled", "0"}
_ON_WORDS = {"on", "yes", "true", "enable", "enabled"}
_report_cooldown: Dict[Tuple[int, int], float] = {}


def _int_arg(token: Optional[str], low: int, high: int) -> Optional[int]:
    if token and token.isascii() and token.isdigit() and low <= int(token) <= high:
        return int(token)
    return None


def describe_flood(snap) -> str:
    enabled, limit, action, seconds = flood_config(snap)
    if not enabled:
        return "off"
    label = {"ban": "ban", "mute": "mute", "kick": "kick",
             "tban": f"temp-ban {mt.format_span(seconds)}", "tmute": f"temp-mute {mt.format_span(seconds)}"}.get(action, action)
    return f"on — warns at {limit} messages in {settings.antiflood_interval}s, {label} from {limit + 3}"


async def _rights_note(context: ContextTypes.DEFAULT_TYPE, chat_id: int, *rights: str) -> str:
    """A warning line for each admin right the bot lacks (empty when it has them all)."""
    missing = []
    for right in rights:
        problem = await mt.check_bot(context, chat_id, right)
        if problem:
            missing.append(problem)
    return ("\n⚠️ " + " ".join(dict.fromkeys(missing))) if missing else ""


# ── summary ──────────────────────────────────────────────────────────────────
@mt.mod_command(None)
async def modsettings_cmd(update, context, ctx):
    """Show every moderation setting of this chat at once: /modsettings"""
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    limit, action = await mt.warn_settings(ctx.chat_id)
    slow = int(getattr(snap, "slowmode_seconds", None) or 0)
    spam_on = snap is None or snap.antispam_enabled is None or bool(snap.antispam_enabled)
    reports_on = snap is None or snap.reports_enabled is None or bool(snap.reports_enabled)
    words = list(getattr(snap, "blacklist_words", None) or [])
    approved = len(await chatcache.get_approved_ids(ctx.chat_id))
    log = getattr(snap, "log_channel", None)
    lines = [
        f"<b>🛡 Moderation — {escape_html(ctx.title)}</b>",
        f"• Anti-flood: {describe_flood(snap)}",
        f"• Slow mode: {f'{slow}s between messages' if slow else 'off'}",
        f"• Anti-spam: {'on' if spam_on else 'off'}",
        f"• Warns: limit <b>{limit}</b>, then <b>{action}</b>",
        f"• Word blacklist: {len(words)} word(s), action <b>{escape_html(getattr(snap, 'blacklist_action', None) or 'warn')}</b>",
        f"• Approved users: {approved}",
        f"• Reports: {'on' if reports_on else 'off'}",
        f"• Log channel: {f'<code>{log}</code>' if log else 'not set'}",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ── anti-flood / slow mode / anti-spam ───────────────────────────────────────
@mt.mod_command(None)
async def antiflood_cmd(update, context, ctx):
    """Show the anti-flood settings: /antiflood"""
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    await update.effective_message.reply_text(
        f"🛡 <b>Anti-flood:</b> {describe_flood(snap)}\n"
        f"Change with /setflood &lt;{FLOOD_MIN}-{FLOOD_MAX}|off&gt; and /setfloodmode &lt;{'|'.join(FLOOD_ACTIONS)}&gt; [time].",
        parse_mode="HTML")


@mt.mod_command("info")
async def setflood_cmd(update, context, ctx):
    """Set how many messages in a few seconds count as flooding, or turn it off: /setflood <3-100|off>"""
    msg = update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    if arg in _OFF_WORDS:
        await mt.update_chat(ctx.chat_id, antiflood_enabled=False)
        await msg.reply_text("🛡 Anti-flood is now <b>off</b>.", parse_mode="HTML")
        return
    limit = _int_arg(arg, FLOOD_MIN, FLOOD_MAX)
    if limit is None:
        await msg.reply_text(f"Usage: /setflood <{FLOOD_MIN}-{FLOOD_MAX}|off>")
        return
    await mt.update_chat(ctx.chat_id, antiflood_enabled=True, antiflood_limit=limit)
    note = await _rights_note(context, ctx.chat_id, "delete", "restrict")
    await msg.reply_text(f"🛡 Anti-flood on: warning at <b>{limit}</b> messages in {settings.antiflood_interval}s.{escape_html(note)}",
                         parse_mode="HTML")


@mt.mod_command("info")
async def setfloodmode_cmd(update, context, ctx):
    """Choose what happens to flooders: /setfloodmode <ban|mute|kick|tban|tmute> [time]"""
    msg = update.effective_message
    args = context.args or []
    action = args[0].lower() if args else ""
    if action not in FLOOD_ACTIONS:
        await msg.reply_text(f"Usage: /setfloodmode <{'|'.join(FLOOD_ACTIONS)}> [time]  (time applies to tban / tmute)")
        return
    seconds = None
    if action in mt.TEMP_ACTIONS and len(args) > 1:
        seconds = mt.parse_duration(args[1])
        try:
            if seconds is None:
                raise mt.DurationError("Give the time like 30m, 2h or 1d.")
            mt.validate_duration(seconds)
        except mt.DurationError as exc:
            await msg.reply_text(f"❌ {exc}")
            return
    await mt.update_chat(ctx.chat_id, antiflood_action=action, antiflood_action_seconds=seconds)
    span = f" for {mt.format_span(seconds)}" if seconds else ""
    await msg.reply_text(f"🛡 Flooders will now get: <b>{action}</b>{span}.", parse_mode="HTML")


@mt.mod_command("info")
async def slowmode_cmd(update, context, ctx):
    """Bot-enforced slow mode (early messages are deleted): /slowmode <seconds|off>"""
    msg = update.effective_message
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    current = int(getattr(snap, "slowmode_seconds", None) or 0)
    arg = (context.args[0].lower() if context.args else "")
    if not arg:
        await msg.reply_text(f"🐢 Slow mode: {f'{current}s between messages' if current else 'off'}. Set with /slowmode <1-{SLOWMODE_MAX}|off>.")
        return
    if arg in _OFF_WORDS:
        await mt.update_chat(ctx.chat_id, slowmode_seconds=None)
        await msg.reply_text("🐇 Slow mode disabled.")
        return
    seconds = _int_arg(arg, 1, SLOWMODE_MAX)
    if seconds is None:
        await msg.reply_text(f"Usage: /slowmode <1-{SLOWMODE_MAX}|off>")
        return
    problem = await mt.check_bot(context, ctx.chat_id, "delete")
    if problem:     # without this right the setting would silently do nothing
        await msg.reply_text(f"🚫 Slow mode works by deleting early messages. {problem}")
        return
    await mt.update_chat(ctx.chat_id, slowmode_seconds=seconds)
    await msg.reply_text(f"🐢 Slow mode: members can post once every {seconds}s (admins and approved users are exempt).")


@mt.mod_command("info")
async def antispam_cmd(update, context, ctx):
    """Turn the heuristic spam filter on or off for this chat: /antispam [on|off]"""
    msg = update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    if arg in _ON_WORDS | _OFF_WORDS:
        await mt.update_chat(ctx.chat_id, antispam_enabled=arg in _ON_WORDS)
        await msg.reply_text(f"🚫 Anti-spam is now <b>{'on' if arg in _ON_WORDS else 'off'}</b>.", parse_mode="HTML")
        return
    on = snap is None or snap.antispam_enabled is None or bool(snap.antispam_enabled)
    await msg.reply_text(f"🚫 Anti-spam is <b>{'on' if on else 'off'}</b>. Use /antispam on|off.", parse_mode="HTML")


# ── warn policy ──────────────────────────────────────────────────────────────
@mt.mod_command(None)
async def setwarnlimit_cmd(update, context, ctx):
    """Set how many warnings trigger the warn action: /setwarnlimit <1-100>"""
    limit = _int_arg(context.args[0] if context.args else None, 1, WARN_LIMIT_MAX)
    if limit is None:
        await update.effective_message.reply_text(f"Usage: /setwarnlimit <1-{WARN_LIMIT_MAX}>")
        return
    await mt.update_chat(ctx.chat_id, warn_limit=limit)
    await update.effective_message.reply_text(f"⚠️ Warn limit set to {limit}.")


@mt.mod_command(None)
async def setwarnaction_cmd(update, context, ctx):
    """Set what happens at the warn limit: /setwarnaction <mute|kick|ban>"""
    action = (context.args[0].lower() if context.args else "")
    if action not in ("mute", "kick", "ban"):
        await update.effective_message.reply_text("Usage: /setwarnaction mute|kick|ban")
        return
    await mt.update_chat(ctx.chat_id, warn_action=action)
    await update.effective_message.reply_text(f"⚠️ Warn action set to: {action}")


# ── log channel ──────────────────────────────────────────────────────────────
@mt.mod_command(None)
async def setlog_cmd(update, context, ctx):
    """Send moderation logs to a channel or group: /setlog <chat id or @username>"""
    msg = update.effective_message
    arg = context.args[0] if context.args else ""
    if not arg:
        await msg.reply_text("Usage: /setlog <channel id or @username> — add me to that channel as an admin first.")
        return
    try:
        target = await context.bot.get_chat(int(arg) if arg.lstrip("-").isdigit() else arg)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    if target.id == ctx.chat_id:
        await msg.reply_text("🚫 Pick a different chat — logging into the chat being moderated defeats the purpose.")
        return
    try:   # prove the bot can really post there before promising anything
        await context.bot.send_message(target.id, f"✅ Moderation logs for <b>{escape_html(ctx.title)}</b> will be posted here.", parse_mode="HTML")
    except TelegramError:
        await msg.reply_text("❌ I can't post in that chat. Add me as an admin with permission to post messages, then try again.")
        return
    await mt.update_chat(ctx.chat_id, log_channel=target.id)
    await msg.reply_text(f"✅ Log channel set to <code>{target.id}</code>.", parse_mode="HTML")


@mt.mod_command("info")
async def unsetlog_cmd(update, context, ctx):
    """Stop sending moderation logs: /unsetlog"""
    await mt.update_chat(ctx.chat_id, log_channel=None)
    await update.effective_message.reply_text("✅ Log channel removed.")


@mt.mod_command(None)
async def logchannel_cmd(update, context, ctx):
    """Show where moderation logs go: /logchannel"""
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    log = getattr(snap, "log_channel", None)
    await update.effective_message.reply_text(f"📋 Log channel: <code>{log}</code>" if log else "📋 No log channel set — /setlog <id|@channel>.", parse_mode="HTML")


# ── reports ──────────────────────────────────────────────────────────────────
@safe_handler
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flag the replied-to message to the admins: reply + /report [reason]"""
    ctx = await mt.resolve_ctx(update, context, remote_ok=False)
    if ctx is None:
        return
    msg, reporter = update.effective_message, update.effective_user
    reply = mt.real_reply(msg)
    if reply is None:
        await msg.reply_text("Reply to the message you want to report.")
        return
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    if snap is not None and snap.reports_enabled is False:
        await msg.reply_text("Reports are turned off in this chat.")
        return
    reported = reply.from_user
    if reported is not None:
        if reported.id == reporter.id:
            await msg.reply_text("You can't report yourself.")
            return
        if reported.id == context.bot.id:
            await msg.reply_text("Nice try 😼")
            return
        if await mt.is_chat_admin(context.bot, ctx.chat_id, reported.id):
            await msg.reply_text("You can't report an admin — talk to the group owner instead.")
            return
    key, now = (ctx.chat_id, reporter.id), time.monotonic()
    wait = REPORT_COOLDOWN - (now - _report_cooldown.get(key, -REPORT_COOLDOWN))
    if wait > 0:
        await msg.reply_text(f"Please wait {int(wait) + 1}s before sending another report.")
        return
    if len(_report_cooldown) > 5000:
        _report_cooldown.clear()
    _report_cooldown[key] = now

    reason = mt.clean_reason(context.args or [], default="")
    admins = []
    try:
        admins = [a.user.id for a in await context.bot.get_chat_administrators(ctx.chat_id)
                  if not a.user.is_bot and not getattr(a, "is_anonymous", False)][:REPORT_MENTION_CAP]
    except TelegramError as exc:
        logger.debug("report_admin_lookup_failed", chat_id=ctx.chat_id, error=str(exc))
    hidden = "".join(f'<a href="tg://user?id={uid}">\u200b</a>' for uid in admins)
    reported_html = mention_html(reported.id, mt.display_name(reported)) if reported else "this message"
    text = f"🚨 {mention_html(reporter.id, mt.display_name(reporter))} reported {reported_html} to the admins.{hidden}"
    if reason:
        text += f"\n<b>Reason:</b> {escape_html(reason)}"
    try:
        await reply.reply_text(text, parse_mode="HTML")
    except TelegramError:
        await msg.reply_text(text, parse_mode="HTML")

    async with async_session() as session:
        session.add(Report(chat_id=ctx.chat_id, message_id=reply.message_id, reporter_id=reporter.id,
                           reported_user_id=reported.id if reported else None, reason=reason or None))
        await session.commit()
    link = mt.message_link(ctx.chat_id, reply.message_id, getattr(msg.chat, "username", None))
    excerpt = (reply.text or reply.caption or "[media]")[:REPORT_EXCERPT]
    extra = f"<b>Message:</b> {escape_html(excerpt)}" + (f'\n<a href="{link}">Go to message</a>' if link else "")
    await mt.log_event(context, ctx.chat_id, "Report", actor=(reporter.id, mt.display_name(reporter)),
                       target=(reported.id, mt.display_name(reported)) if reported else None, reason=reason or None, extra=extra)


@mt.mod_command("info")
async def reports_cmd(update, context, ctx):
    """Turn /report on or off for this chat: /reports [on|off]"""
    msg = update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    if arg in _ON_WORDS | _OFF_WORDS:
        await mt.update_chat(ctx.chat_id, reports_enabled=arg in _ON_WORDS)
        await msg.reply_text(f"🚨 Reports are now <b>{'on' if arg in _ON_WORDS else 'off'}</b>.", parse_mode="HTML")
        return
    snap = await chatcache.get_chat_settings(ctx.chat_id)
    on = snap is None or snap.reports_enabled is None or bool(snap.reports_enabled)
    await msg.reply_text(f"🚨 Reports are <b>{'on' if on else 'off'}</b>. Use /reports on|off.", parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("modsettings", modsettings_cmd))
    app.add_handler(CommandHandler(["antiflood", "flood"], antiflood_cmd))
    app.add_handler(CommandHandler("setflood", setflood_cmd))
    app.add_handler(CommandHandler(["setfloodmode", "floodaction", "actionflood"], setfloodmode_cmd))
    app.add_handler(CommandHandler("slowmode", slowmode_cmd))
    app.add_handler(CommandHandler("antispam", antispam_cmd))
    app.add_handler(CommandHandler(["setwarnlimit", "warnlimit"], setwarnlimit_cmd))
    app.add_handler(CommandHandler("setwarnaction", setwarnaction_cmd))
    app.add_handler(CommandHandler(["setlog", "log"], setlog_cmd))
    app.add_handler(CommandHandler("unsetlog", unsetlog_cmd))
    app.add_handler(CommandHandler("logchannel", logchannel_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("reports", reports_cmd))
