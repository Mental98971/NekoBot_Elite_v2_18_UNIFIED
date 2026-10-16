"""Chat administration helpers: pins, promote / demote / titles, admin list, invite link, group info.

Ported from FallenRobot / Gojo_Satoru / tgbot and rebuilt on ``bot/utils/modtools.py`` (v2.12):
rights are checked for both the caller *and* the bot, nobody can be granted a right the caller does
not hold themselves, and ``/unpin`` no longer silently removes every pin (that is ``/unpinall``,
behind a confirmation button).
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.core.errors import safe_handler
from bot.utils import modtools as mt
from bot.utils.helpers import escape_html, mention_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_QUIET_WORDS = {"silent", "quiet", "mute", "nonotify"}
BASE_RIGHTS = ("can_delete_messages", "can_restrict_members", "can_pin_messages", "can_invite_users", "can_manage_video_chats")
FULL_RIGHTS = BASE_RIGHTS + ("can_change_info", "can_promote_members", "can_manage_topics")
ALL_FLAGS = FULL_RIGHTS
TITLE_MAX = 16
GROUP_TITLE_MAX = 128
DESCRIPTION_MAX = 255


def _has(member, flag: str) -> bool:
    """Does this member hold ``flag``? The owner holds everything."""
    if member is None:
        return False
    if member.status == "creator":
        return True
    return member.status == "administrator" and bool(getattr(member, flag, False))


# ── pins ─────────────────────────────────────────────────────────────────────
@mt.mod_command("pin", remote_ok=False, bot_right="pin")
async def pin_cmd(update, context, ctx):
    """Pin the replied-to message: /pin [silent]"""
    msg = update.effective_message
    reply = mt.real_reply(msg)
    if reply is None:
        await msg.reply_text("Reply to the message you want pinned. Add “silent” to pin without a notification.")
        return
    quiet = any(a.lower() in _QUIET_WORDS for a in (context.args or []))
    try:
        await context.bot.pin_chat_message(ctx.chat_id, reply.message_id, disable_notification=quiet)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    await msg.reply_text("📌 Pinned" + (" silently." if quiet else "."))
    await mt.log_event(context, ctx.chat_id, "Pinned", actor=mt.actor_of(update)[1])


@mt.mod_command("pin", remote_ok=False, bot_right="pin")
async def unpin_cmd(update, context, ctx):
    """Unpin the replied-to message, or the most recent pin: /unpin"""
    msg = update.effective_message
    reply = mt.real_reply(msg)
    try:
        if reply is not None:
            await context.bot.unpin_chat_message(ctx.chat_id, message_id=reply.message_id)
        else:
            await context.bot.unpin_chat_message(ctx.chat_id)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    await msg.reply_text("📌 Unpinned." if reply is not None else "📌 Unpinned the most recent pinned message. (/unpinall removes every pin.)")


@mt.mod_command("pin", remote_ok=False, bot_right="pin")
async def unpinall_cmd(update, context, ctx):
    """Unpin every pinned message (asks for confirmation)"""
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Unpin all", callback_data=f"unpinall:y:{ctx.chat_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="unpinall:n"),
    ]])
    await update.effective_message.reply_text("Remove <b>every</b> pinned message in this chat?", parse_mode="HTML", reply_markup=markup)


@safe_handler
async def unpinall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) < 2 or parts[1] != "y":
        await query.answer("Cancelled")
        await query.edit_message_text("Cancelled — nothing was unpinned.")
        return
    try:
        chat_id = int(parts[2])
    except (IndexError, ValueError):
        await query.answer("That button is no longer valid.", show_alert=True)
        return
    problem = await mt.check_caller(update, context, chat_id, "pin") or await mt.check_bot(context, chat_id, "pin")
    if problem:
        await query.answer(problem, show_alert=True)
        return
    try:
        await context.bot.unpin_all_chat_messages(chat_id)
    except TelegramError as exc:
        await query.answer(mt.friendly_telegram_error(exc), show_alert=True)
        return
    await query.answer("Done")
    await query.edit_message_text("📌 Every pinned message was removed.")
    await mt.log_event(context, chat_id, "Unpinned all", actor=(query.from_user.id, mt.display_name(query.from_user)))


@safe_handler
async def pinned_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link to the current pinned message: /pinned"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    msg = update.effective_message
    try:
        chat = await context.bot.get_chat(ctx.chat_id)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    pinned = getattr(chat, "pinned_message", None)
    if pinned is None:
        await msg.reply_text("Nothing is pinned here.")
        return
    link = mt.message_link(ctx.chat_id, pinned.message_id, getattr(chat, "username", None))
    if link is None:
        await msg.reply_text("📌 Something is pinned, but this chat type has no message links.")
        return
    await msg.reply_text(f'📌 <a href="{link}">Pinned message</a>', parse_mode="HTML", disable_web_page_preview=True)


# ── promote / demote / title ─────────────────────────────────────────────────
async def _promote(update, context, ctx, *, full: bool, cmd: str) -> None:
    msg = update.effective_message
    actor_id, actor = mt.actor_of(update)
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(f"Usage: /{cmd} [title]\n{mt.NO_TARGET_HELP}")
        return
    block = await mt.target_block_reason(context, ctx.chat_id, target, actor_id, allow_admin=True)
    if block:
        await msg.reply_text(f"🚫 {block}")
        return
    caller = await mt.get_member(context.bot, ctx.chat_id, actor_id)
    me = await mt.get_member(context.bot, ctx.chat_id, context.bot.id)
    # Nobody can hand out a right they do not hold: otherwise an admin who may add admins but cannot
    # ban could mint an admin who can.
    grant = {flag: True for flag in (FULL_RIGHTS if full else BASE_RIGHTS) if _has(caller, flag) and _has(me, flag)}
    if not grant:
        await msg.reply_text("🚫 There's nothing I can grant: both you and I need the rights being handed out.")
        return
    try:
        await context.bot.promote_chat_member(ctx.chat_id, target.user_id, **grant)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    mt.invalidate_admin(ctx.chat_id, target.user_id)
    text = f"⭐ {target.mention} is now an admin."
    title = " ".join(target.rest).strip()[:TITLE_MAX]
    if title:
        try:
            await context.bot.set_chat_administrator_custom_title(ctx.chat_id, target.user_id, title)
            text += f" Title: <b>{escape_html(title)}</b>"
        except TelegramError as exc:
            text += f"\n⚠️ Title not set: {escape_html(mt.friendly_telegram_error(exc))}"
    await msg.reply_text(text, parse_mode="HTML")
    await mt.log_event(context, ctx.chat_id, "Promoted", actor=actor, target=(target.user_id, target.name))


@mt.mod_command("promote", remote_ok=True, bot_right="promote")
async def promote_cmd(update, context, ctx):
    """Make someone an admin with everyday rights: /promote [title]"""
    await _promote(update, context, ctx, full=False, cmd="promote")


@mt.mod_command("promote", remote_ok=True, bot_right="promote")
async def fullpromote_cmd(update, context, ctx):
    """Make someone an admin with every right you hold: /fullpromote [title]"""
    await _promote(update, context, ctx, full=True, cmd="fullpromote")


@mt.mod_command("promote", remote_ok=True, bot_right="promote")
async def demote_cmd(update, context, ctx):
    """Remove someone's admin rights (only admins I promoted): /demote"""
    msg = update.effective_message
    actor_id, actor = mt.actor_of(update)
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(f"Usage: /demote\n{mt.NO_TARGET_HELP}")
        return
    block = await mt.target_block_reason(context, ctx.chat_id, target, actor_id, allow_admin=True, allow_self=True)
    if block:
        await msg.reply_text(f"🚫 {block}")
        return
    try:
        await context.bot.promote_chat_member(ctx.chat_id, target.user_id, **{flag: False for flag in ALL_FLAGS},
                                              can_manage_chat=False)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    mt.invalidate_admin(ctx.chat_id, target.user_id)
    await msg.reply_text(f"⬇️ {target.mention} is no longer an admin.", parse_mode="HTML")
    await mt.log_event(context, ctx.chat_id, "Demoted", actor=actor, target=(target.user_id, target.name))


@mt.mod_command("promote", remote_ok=True, bot_right="promote")
async def title_cmd(update, context, ctx):
    """Set an admin's custom title (admins I promoted): /title <text>"""
    msg = update.effective_message
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    title = " ".join(target.rest).strip() if target else ""
    if target is None or not title:
        await msg.reply_text(f"Usage: /title <text> (max {TITLE_MAX} characters, no emoji)\n{mt.NO_TARGET_HELP}")
        return
    if len(title) > TITLE_MAX:
        await msg.reply_text(f"❌ Titles are limited to {TITLE_MAX} characters.")
        return
    member = await target.load_member(context.bot, ctx.chat_id)
    if member is None or member.status not in ("administrator", "creator"):
        await msg.reply_text("🚫 That user isn't an admin — /promote them first.")
        return
    try:
        await context.bot.set_chat_administrator_custom_title(ctx.chat_id, target.user_id, title)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    await msg.reply_text(f"🏷 {target.mention}'s title is now <b>{escape_html(title)}</b>.", parse_mode="HTML")


# ── read-only: admin list, invite link ───────────────────────────────────────
@safe_handler
async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List this chat's admins: /admins"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    msg = update.effective_message
    try:
        admins = await context.bot.get_chat_administrators(ctx.chat_id)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    owner_lines, admin_lines, bots, hidden = [], [], 0, 0
    for member in admins:
        if member.user.is_bot:
            bots += 1
        elif getattr(member, "is_anonymous", False):
            hidden += 1
        else:
            label = mention_html(member.user.id, mt.display_name(member.user))
            title = getattr(member, "custom_title", None)
            line = f"• {label}" + (f" — <i>{escape_html(title)}</i>" if title else "")
            (owner_lines if member.status == "creator" else admin_lines).append(line)
    lines = [f"<b>Admins of {escape_html(ctx.title)}</b>"]
    if owner_lines:
        lines += ["👑 <b>Owner</b>", *owner_lines]
    if admin_lines:
        lines += ["👮 <b>Admins</b>", *admin_lines]
    extra = [f"{n} {what}" for n, what in ((hidden, "anonymous"), (bots, "bot(s)")) if n]
    if extra:
        lines.append("<i>+ " + ", ".join(extra) + "</i>")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


@mt.mod_command("invite", bot_right="invite")
async def invitelink_cmd(update, context, ctx):
    """Get this chat's invite link: /invitelink"""
    msg = update.effective_message
    try:
        chat = await context.bot.get_chat(ctx.chat_id)
        if getattr(chat, "username", None):
            link = f"https://t.me/{chat.username}"
        else:
            # getChat returns the bot's own link once it has one; exporting makes it the first time and
            # (unlike export-every-time) does not rotate links that other admins handed out.
            link = getattr(chat, "invite_link", None) or await context.bot.export_chat_invite_link(ctx.chat_id)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    await msg.reply_text(f"🔗 {escape_html(link)}", parse_mode="HTML", disable_web_page_preview=True)


# ── group info ───────────────────────────────────────────────────────────────
@mt.mod_command("info", bot_right="info")
async def setgtitle_cmd(update, context, ctx):
    """Change the group's title: /setgtitle <new title>"""
    msg = update.effective_message
    title = " ".join(context.args or []).strip()
    if not title or len(title) > GROUP_TITLE_MAX:
        await msg.reply_text(f"Usage: /setgtitle <new title> (1-{GROUP_TITLE_MAX} characters)")
        return
    try:
        await context.bot.set_chat_title(ctx.chat_id, title)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    await msg.reply_text("✅ Group title updated.")
    await mt.log_event(context, ctx.chat_id, "Title changed", actor=mt.actor_of(update)[1], extra=f"<b>New title:</b> {escape_html(title)}")


@mt.mod_command("info", bot_right="info")
async def setdesc_cmd(update, context, ctx):
    """Change the group's description (text or the replied-to message): /setdesc <text>"""
    msg = update.effective_message
    reply = None if ctx.remote else mt.real_reply(msg)
    text = " ".join(context.args or []).strip() or (getattr(reply, "text", None) or "").strip()
    if not text or len(text) > DESCRIPTION_MAX:
        await msg.reply_text(f"Usage: /setdesc <text> or reply to a message (1-{DESCRIPTION_MAX} characters)")
        return
    try:
        await context.bot.set_chat_description(ctx.chat_id, text)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    await msg.reply_text("✅ Group description updated.")


def register(app):
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("unpin", unpin_cmd))
    app.add_handler(CommandHandler("unpinall", unpinall_cmd))
    app.add_handler(CallbackQueryHandler(unpinall_callback, pattern=r"^unpinall:"))
    app.add_handler(CommandHandler("pinned", pinned_cmd))
    app.add_handler(CommandHandler(["promote", "lowpromote"], promote_cmd))
    app.add_handler(CommandHandler("fullpromote", fullpromote_cmd))
    app.add_handler(CommandHandler("demote", demote_cmd))
    app.add_handler(CommandHandler("title", title_cmd))
    app.add_handler(CommandHandler(["admins", "adminlist"], admins_cmd))
    app.add_handler(CommandHandler("invitelink", invitelink_cmd))
    app.add_handler(CommandHandler("setgtitle", setgtitle_cmd))
    app.add_handler(CommandHandler("setdesc", setdesc_cmd))
