"""
Warn filters and the sticker-pack blacklist (v2.16).

From the Saitama / Masha ``warns.py`` and ``blacklist_stickers.py`` modules (KugisakiNobara-master,
NobaraKugisaki-prince), rebuilt on this project's moderation core (``bot/utils/modtools.py``):

  * /addwarn <keyword> <reason>   - a message containing the keyword (whole word, case-insensitive,
                                     ``*`` wildcard) warns its author with that reason
  * /nowarn /stopwarn <keyword>   - remove a warn filter;  /warnlist /warnfilters - list them
  * /strongwarn [on|off]          - what the warn limit does: on = ban, off = kick (sets /setwarnaction)
  * /addblsticker [pack]          - blacklist a sticker pack (reply to one of its stickers, or give the name)
  * /unblsticker /rmblsticker     - remove one;  /blsticker /blstickers - list;  /blstickermode - the action

Admins, approved users and the bot's owners are never touched (``mt.is_exempt``).  Rules are cached per chat
for RULES_TTL seconds and the cache is dropped on every change.  The listener is group 15 (see the map in
group_mgmt.py).
"""
from __future__ import annotations

import fnmatch
import re
import time
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select
from telegram.error import TelegramError
from telegram.ext import CommandHandler, MessageHandler, filters

from bot.core.database import StickerBlacklist, WarnFilter, async_session
from bot.core.errors import safe_handler
from bot.utils import blacklist as bl, modtools as mt
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

RULES_TTL = 30.0
MAX_WARN_FILTERS = 100
MAX_STICKER_RULES = 100
MAX_REASON_LEN = 300
MAX_TRIGGER_LEN = 64
STICKER_MODES = ("delete", "warn", "mute", "kick", "ban", "tmute", "tban")

_QUOTED = re.compile(r'^\s*(?:"([^"]+)"|\'([^\']+)\')\s*(.*)$', re.S)
_cache: Dict[Tuple[str, int], Tuple[float, object]] = {}


def _invalidate(chat_id: int) -> None:
    _cache.pop(("warn", chat_id), None)
    _cache.pop(("sticker", chat_id), None)


# ── pure helpers ────────────────────────────────────────────────────────────
def split_keyword_and_reason(raw: str) -> Tuple[Optional[str], str]:
    """``'"free money" scam'`` -> ``('free money', 'scam')``; ``'spam no ads'`` -> ``('spam', 'no ads')``."""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    m = _QUOTED.match(raw)
    if m:
        keyword, reason = (m.group(1) or m.group(2)), m.group(3)
    else:
        parts = raw.split(None, 1)
        keyword, reason = parts[0], (parts[1] if len(parts) > 1 else "")
    return bl.clean_entry(keyword), reason.strip()[:MAX_REASON_LEN]


def clean_trigger(raw: str) -> Optional[str]:
    """Normalise a sticker-pack trigger (pack names are ASCII ``[A-Za-z0-9_]``; ``*`` is a wildcard)."""
    trig = (raw or "").strip().lower()
    if not trig or len(trig) > MAX_TRIGGER_LEN or not re.fullmatch(r"[a-z0-9_*]+", trig):
        return None
    if trig.count("*") > 2 or not trig.replace("*", ""):
        return None
    return trig


def sticker_match(set_name: Optional[str], triggers: List[str]) -> Optional[str]:
    """The trigger that matches this sticker's pack name, or None."""
    if not set_name:
        return None
    name = set_name.lower()
    for trig in triggers:
        if fnmatch.fnmatchcase(name, trig):
            return trig
    return None


# ── cached lookups ──────────────────────────────────────────────────────────
async def _warn_rules(chat_id: int) -> Dict[str, str]:
    hit = _cache.get(("warn", chat_id))
    if hit and time.monotonic() - hit[0] < RULES_TTL:
        return hit[1]  # type: ignore[return-value]
    async with async_session() as session:
        rows = (await session.execute(select(WarnFilter.keyword, WarnFilter.reason).where(WarnFilter.chat_id == chat_id))).all()
    rules = {k: (r or "Triggered a warn filter") for k, r in rows}
    _cache[("warn", chat_id)] = (time.monotonic(), rules)
    return rules


async def _sticker_rules(chat_id: int) -> Tuple[List[str], Optional[str], Optional[int]]:
    """(triggers, action, seconds) for the chat."""
    hit = _cache.get(("sticker", chat_id))
    if hit and time.monotonic() - hit[0] < RULES_TTL:
        return hit[1]  # type: ignore[return-value]
    from bot.core.database import Chat
    async with async_session() as session:
        triggers = list((await session.execute(select(StickerBlacklist.trigger).where(StickerBlacklist.chat_id == chat_id))).scalars().all())
        chat = await session.get(Chat, chat_id)
        action = chat.blsticker_action if chat is not None else None
        seconds = chat.blsticker_duration if chat is not None else None
    value = (triggers, action, seconds)
    _cache[("sticker", chat_id)] = (time.monotonic(), value)
    return value


# ── listener ────────────────────────────────────────────────────────────────
@safe_handler
async def policy_listener(update, context) -> None:
    """Enforce warn filters (text/captions) and the sticker-pack blacklist. Never blocks later handlers."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if msg is None or chat is None or user is None or chat.type == "private" or user.is_bot:
        return

    sticker = getattr(msg, "sticker", None)
    if sticker is not None:
        triggers, action, seconds = await _sticker_rules(chat.id)
        trig = sticker_match(getattr(sticker, "set_name", None), triggers) if triggers else None
        if trig is not None and not await mt.is_exempt(context.bot, chat.id, user.id):
            try:
                await msg.delete()
            except TelegramError as exc:
                logger.debug("blsticker_delete_failed chat=%s err=%s", chat.id, exc)
            notice = await mt.enforce_action(context, chat.id, user.id, mt.display_name(user), action, seconds=seconds,
                                             reason=f"Blacklisted sticker pack: {trig}", by=context.bot.id)
            if notice:
                try:
                    await context.bot.send_message(chat.id, f"🚫 {notice}", parse_mode="HTML")
                except TelegramError:
                    pass
            return

    text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
    if not text or text.startswith("/"):
        return
    rules = await _warn_rules(chat.id)
    if not rules:
        return
    entry = bl.find_match(text, rules.keys())
    if entry is None or await mt.is_exempt(context.bot, chat.id, user.id):
        return
    reason = rules[entry]
    outcome = await mt.add_warn(context, chat.id, user.id, by=context.bot.id, reason=reason, user_name=mt.display_name(user))
    try:
        await msg.reply_text(mt.describe_warn_outcome(mt.mention_html(user.id, mt.display_name(user)), outcome, reason), parse_mode="HTML")
    except TelegramError:
        pass


# ── warn-filter commands ────────────────────────────────────────────────────
def _after_command(update) -> str:
    text = update.effective_message.text or ""
    parts = text.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


@mt.mod_command("info")
async def addwarn_cmd(update, context, ctx):
    """/addwarn <keyword> <reason> - warn anyone whose message contains the keyword."""
    msg = update.effective_message
    keyword, reason = split_keyword_and_reason(_after_command(update))
    if keyword is None:
        await msg.reply_text('Usage: /addwarn <keyword> <reason>  (quote a phrase: /addwarn "free money" scam). '
                             "Whole words, case-insensitive, <code>*</code> is a wildcard.", parse_mode="HTML")
        return
    async with async_session() as session:
        await mt.get_or_create_chat(session, ctx.chat_id, title=ctx.title)
        count = (await session.execute(select(func.count()).select_from(WarnFilter).where(WarnFilter.chat_id == ctx.chat_id))).scalar() or 0
        row = (await session.execute(select(WarnFilter).where(WarnFilter.chat_id == ctx.chat_id, WarnFilter.keyword == keyword))).scalar_one_or_none()
        if row is None and count >= MAX_WARN_FILTERS:
            await msg.reply_text(f"⚠️ A chat can have at most {MAX_WARN_FILTERS} warn filters.")
            return
        if row is None:
            session.add(WarnFilter(chat_id=ctx.chat_id, keyword=keyword, reason=reason or None))
        else:
            row.reason = reason or None
        await session.commit()
    _invalidate(ctx.chat_id)
    await msg.reply_text(f"✅ Warn filter set: <code>{escape_html(keyword)}</code>"
                         + (f" - {escape_html(reason)}" if reason else ""), parse_mode="HTML")


@mt.mod_command("info")
async def nowarn_cmd(update, context, ctx):
    """/nowarn <keyword> - remove a warn filter (also /stopwarn)."""
    msg = update.effective_message
    keyword, _ = split_keyword_and_reason(_after_command(update))
    if keyword is None:
        await msg.reply_text("Usage: /nowarn <keyword>")
        return
    async with async_session() as session:
        res = await session.execute(delete(WarnFilter).where(WarnFilter.chat_id == ctx.chat_id, WarnFilter.keyword == keyword))
        await session.commit()
    _invalidate(ctx.chat_id)
    await msg.reply_text("✅ Removed." if res.rowcount else "That isn't a warn filter here.")


@safe_handler
async def warnlist_cmd(update, context) -> None:
    """/warnlist - the chat's warn filters (also /warnfilters)."""
    chat = update.effective_chat
    if chat.type == "private":
        await update.effective_message.reply_text("Use this in a group.")
        return
    rules = await _warn_rules(chat.id)
    if not rules:
        await update.effective_message.reply_text("No warn filters here. Add one with /addwarn <keyword> <reason>.")
        return
    lines = [f"• <code>{escape_html(k)}</code> - {escape_html(r)}" for k, r in sorted(rules.items())]
    await update.effective_message.reply_text("⚠️ <b>Warn filters</b>\n" + "\n".join(lines)[:3900], parse_mode="HTML")


@mt.mod_command("info")
async def strongwarn_cmd(update, context, ctx):
    """/strongwarn [on|off] - on: reaching the warn limit bans; off: it kicks."""
    msg = update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    limit, action = await mt.warn_settings(ctx.chat_id)
    if arg in ("on", "yes", "true"):
        await mt.update_chat(ctx.chat_id, warn_action="ban")
        action = "ban"
    elif arg in ("off", "no", "false"):
        await mt.update_chat(ctx.chat_id, warn_action="kick")
        action = "kick"
    elif arg:
        await msg.reply_text("Usage: /strongwarn on|off")
        return
    await msg.reply_text(f"⚠️ At {limit} warns a member is <b>{'banned' if action == 'ban' else 'kicked' if action == 'kick' else action}</b>."
                         + ("" if arg else "\nChange it with /strongwarn on|off (or /setwarnaction)."), parse_mode="HTML")


# ── sticker blacklist commands ──────────────────────────────────────────────
@mt.mod_command("info")
async def addblsticker_cmd(update, context, ctx):
    """/addblsticker [pack] - blacklist a sticker pack (reply to one of its stickers, or give the pack name)."""
    msg = update.effective_message
    reply = mt.real_reply(msg)
    raw = ""
    if reply is not None and getattr(reply, "sticker", None) is not None:
        raw = getattr(reply.sticker, "set_name", None) or ""
        if not raw:
            await msg.reply_text("That sticker doesn't belong to a pack, so there's nothing to blacklist.")
            return
    else:
        raw = (context.args[0] if context.args else "")
    trig = clean_trigger(raw)
    if trig is None:
        await msg.reply_text("Usage: reply to a sticker with /addblsticker, or /addblsticker <pack name> (letters, digits, _ and *).")
        return
    async with async_session() as session:
        await mt.get_or_create_chat(session, ctx.chat_id, title=ctx.title)
        count = (await session.execute(select(func.count()).select_from(StickerBlacklist).where(StickerBlacklist.chat_id == ctx.chat_id))).scalar() or 0
        exists = (await session.execute(select(StickerBlacklist.id).where(StickerBlacklist.chat_id == ctx.chat_id, StickerBlacklist.trigger == trig))).first()
        if exists:
            await msg.reply_text("That pack is already blacklisted.")
            return
        if count >= MAX_STICKER_RULES:
            await msg.reply_text(f"⚠️ A chat can blacklist at most {MAX_STICKER_RULES} sticker packs.")
            return
        session.add(StickerBlacklist(chat_id=ctx.chat_id, trigger=trig))
        await session.commit()
    _invalidate(ctx.chat_id)
    problem = await mt.check_bot(context, ctx.chat_id, "delete")
    await msg.reply_text(f"✅ Sticker pack <code>{escape_html(trig)}</code> blacklisted."
                         + (f"\n⚠️ {escape_html(problem)} Until I can delete messages this does nothing." if problem else ""), parse_mode="HTML")


@mt.mod_command("info")
async def unblsticker_cmd(update, context, ctx):
    """/unblsticker <pack> - remove a blacklisted sticker pack (also /rmblsticker)."""
    msg = update.effective_message
    reply = mt.real_reply(msg)
    raw = (getattr(getattr(reply, "sticker", None), "set_name", None) if reply is not None else None) or (context.args[0] if context.args else "")
    trig = clean_trigger(raw)
    if trig is None:
        await msg.reply_text("Usage: reply to a sticker with /unblsticker, or /unblsticker <pack name>.")
        return
    async with async_session() as session:
        res = await session.execute(delete(StickerBlacklist).where(StickerBlacklist.chat_id == ctx.chat_id, StickerBlacklist.trigger == trig))
        await session.commit()
    _invalidate(ctx.chat_id)
    await msg.reply_text("✅ Removed." if res.rowcount else "That pack isn't blacklisted here.")


@safe_handler
async def blsticker_cmd(update, context) -> None:
    """/blsticker - list the blacklisted sticker packs (also /blstickers)."""
    chat = update.effective_chat
    if chat.type == "private":
        await update.effective_message.reply_text("Use this in a group.")
        return
    triggers, action, seconds = await _sticker_rules(chat.id)
    if not triggers:
        await update.effective_message.reply_text("No sticker packs are blacklisted here.")
        return
    extra = f" for {mt.format_span(seconds)}" if seconds and action in mt.TEMP_ACTIONS else ""
    body = "\n".join(f"• <code>{escape_html(t)}</code>" for t in sorted(triggers))
    await update.effective_message.reply_text(
        f"🚫 <b>Blacklisted sticker packs</b> (action: {escape_html(action or 'delete')}{extra})\n{body}"[:3900], parse_mode="HTML")


@mt.mod_command("info")
async def blstickermode_cmd(update, context, ctx):
    """/blstickermode <delete|warn|mute|kick|ban|tmute|tban> [time] - what happens to the sender."""
    msg = update.effective_message
    args = [a.lower() for a in (context.args or [])]
    if not args:
        _, action, seconds = await _sticker_rules(ctx.chat_id)
        extra = f" for {mt.format_span(seconds)}" if seconds and action in mt.TEMP_ACTIONS else ""
        await msg.reply_text(f"🚫 Sticker blacklist action: <b>{escape_html(action or 'delete')}</b>{extra}.\nOptions: {', '.join(STICKER_MODES)}", parse_mode="HTML")
        return
    mode = {"nothing": "delete"}.get(args[0], args[0])
    if mode not in STICKER_MODES:
        await msg.reply_text(f"Usage: /blstickermode <{'|'.join(STICKER_MODES)}> [time]")
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
    await mt.update_chat(ctx.chat_id, blsticker_action=mode, blsticker_duration=seconds)
    _invalidate(ctx.chat_id)
    span = f" for {mt.format_span(seconds)}" if seconds else ""
    await msg.reply_text(f"🚫 Blacklisted stickers are deleted, and their sender gets: <b>{mode}</b>{span}.", parse_mode="HTML")


def register(app) -> None:
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, policy_listener), group=15)
    app.add_handler(CommandHandler("addwarn", addwarn_cmd))
    app.add_handler(CommandHandler(["nowarn", "stopwarn"], nowarn_cmd))
    app.add_handler(CommandHandler(["warnlist", "warnfilters"], warnlist_cmd))
    app.add_handler(CommandHandler("strongwarn", strongwarn_cmd))
    app.add_handler(CommandHandler("addblsticker", addblsticker_cmd))
    app.add_handler(CommandHandler(["unblsticker", "rmblsticker"], unblsticker_cmd))
    app.add_handler(CommandHandler(["blsticker", "blstickers"], blsticker_cmd))
    app.add_handler(CommandHandler("blstickermode", blstickermode_cmd))
