"""
Welcome mute (v2.17): /welcomemute, /welcomemutehelp - and the join check behind /captcha.

From the Saitama / Masha ``welcome.py``:

  /welcomemute soft     a member who joins by themselves cannot send media, stickers, polls or links for 24 hours
  /welcomemute strong   they are muted until they tap "I'm not a bot" within VERIFY_SECONDS (120); if they don't
                        they are removed (kicked, not banned - they can rejoin). The welcome message is held back
                        until they verify, so bots never see it.
  /welcomemute off      nothing

Who is never challenged: admins, approved members, bots, people added by someone else, anyone who already passed
the check in this chat, and anyone gbanned/fbanned (the ban enforcer deals with them).

Before v2.17 ``/captcha on`` only stored a flag and nothing ever read it. It now means "strong" for chats that have
not chosen a /welcomemute setting (an explicit /welcomemute off wins).

Pending checks are rows in ``pending_verifications`` with a deadline, swept every SWEEP_SECONDS by a job, so a
restart can never leave someone muted forever. The join listener is handler group -7: it must run before the
welcome message (group 4) so the greeting can be deferred.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from sqlalchemy import delete, select
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.core.database import Chat, PendingVerification, VerifiedMember, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils import modtools as mt
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

VERIFY_SECONDS = 120
SOFT_MUTE_SECONDS = 24 * 3600
SWEEP_SECONDS = 30
MODES = ("off", "soft", "strong")

_SOFT = ChatPermissions(
    can_send_messages=True, can_send_audios=False, can_send_documents=False, can_send_photos=False, can_send_videos=False,
    can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False, can_change_info=False, can_invite_users=False, can_pin_messages=False)
_STRONG = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False, can_send_photos=False, can_send_videos=False,
    can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False, can_change_info=False, can_invite_users=False, can_pin_messages=False)


def effective_mode(welcome_mute: Optional[str], captcha_enabled: Optional[bool]) -> str:
    """The setting that applies: an explicit /welcomemute wins, otherwise /captcha on means strong."""
    if welcome_mute in MODES:
        return welcome_mute  # type: ignore[return-value]
    return "strong" if captcha_enabled else "off"


async def filter_deferred(chat_id: int, users: Iterable) -> List:
    """Drop the users whose welcome is held back until they verify (used by group_mgmt.welcome_listener)."""
    users = list(users)
    if not users:
        return users
    async with async_session() as session:
        pending = set((await session.execute(
            select(PendingVerification.user_id).where(PendingVerification.chat_id == chat_id))).scalars().all())
    return [u for u in users if u.id not in pending]


async def _is_verified(chat_id: int, user_id: int) -> bool:
    async with async_session() as session:
        return (await session.execute(select(VerifiedMember.id).where(
            VerifiedMember.chat_id == chat_id, VerifiedMember.user_id == user_id))).first() is not None


# ── join listener ───────────────────────────────────────────────────────────
@safe_handler
async def join_guard(update, context) -> None:
    """Apply the chat's welcome-mute setting to members who joined by themselves."""
    msg, chat = update.effective_message, update.effective_chat
    if msg is None or chat is None or chat.type not in ("group", "supergroup") or not msg.new_chat_members:
        return
    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        mode = effective_mode(getattr(row, "welcome_mute", None), getattr(row, "captcha_enabled", None)) if row else "off"
    if mode == "off":
        return
    sender = msg.from_user
    for member in msg.new_chat_members:
        if member.is_bot or sender is None or sender.id != member.id:
            continue                                   # bots and people added by someone else are not challenged
        if await mt.is_exempt(context.bot, chat.id, member.id) or await _is_verified(chat.id, member.id):
            continue
        from bot.plugins.fedtools import ban_reason_for
        if await ban_reason_for(chat.id, member.id):
            continue                                   # the ban enforcer removes them
        if mode == "soft":
            await _soft_mute(context, chat.id, member)
        else:
            await _challenge(context, chat, member)


async def _soft_mute(context, chat_id: int, member) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=SOFT_MUTE_SECONDS)
    try:
        await context.bot.restrict_chat_member(chat_id, member.id, _SOFT, until_date=until)
    except TelegramError as exc:
        logger.info("welcomemute_soft_failed chat=%s user=%s err=%s", chat_id, member.id, exc)


async def _challenge(context, chat, member) -> None:
    try:
        await context.bot.restrict_chat_member(chat.id, member.id, _STRONG)
    except TelegramError as exc:
        logger.info("welcomemute_strong_failed chat=%s user=%s err=%s", chat.id, member.id, exc)
        return                                          # not muted, so a prompt would be meaningless
    try:
        sent = await context.bot.send_message(
            chat.id,
            f"👋 {mt.mention_html(member.id, mt.display_name(member))}, tap the button within {VERIFY_SECONDS} seconds to "
            f"prove you're not a bot. Until then you can't send messages.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🙋 I'm not a bot", callback_data=f"wmv:{member.id}")]]))
        message_id = sent.message_id
    except TelegramError as exc:
        logger.info("welcomemute_prompt_failed chat=%s user=%s err=%s", chat.id, member.id, exc)
        message_id = None
    async with async_session() as session:
        await session.execute(delete(PendingVerification).where(
            PendingVerification.chat_id == chat.id, PendingVerification.user_id == member.id))
        session.add(PendingVerification(chat_id=chat.id, user_id=member.id, message_id=message_id,
                                        deadline=mt.utcnow() + timedelta(seconds=VERIFY_SECONDS)))
        await session.commit()


# ── verification button ─────────────────────────────────────────────────────
@safe_handler
async def verify_callback(update, context) -> None:
    query = update.callback_query
    try:
        target = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return
    if query.from_user.id != target:
        await query.answer("This button isn't for you.", show_alert=True)
        return
    chat = update.effective_chat
    async with async_session() as session:
        pending = (await session.execute(select(PendingVerification).where(
            PendingVerification.chat_id == chat.id, PendingVerification.user_id == target))).scalars().first()
        if pending is None:
            await query.answer("This verification has already finished or expired.", show_alert=True)
            return
        await session.execute(delete(PendingVerification).where(PendingVerification.id == pending.id))
        if not (await session.execute(select(VerifiedMember.id).where(
                VerifiedMember.chat_id == chat.id, VerifiedMember.user_id == target))).first():
            session.add(VerifiedMember(chat_id=chat.id, user_id=target))
        await session.commit()
    problem = await mt.lift(context, chat.id, target, "mute")
    if problem:
        await query.answer(f"I couldn't unmute you: {problem}", show_alert=True)
        return
    await query.answer("Verified - welcome! 🎉")
    try:
        await query.message.delete()
    except TelegramError:
        pass
    from bot.plugins import group_mgmt
    await group_mgmt._send_member_notice(update, context, users=[query.from_user], template_attr="welcome_text",
                                         flag_attr="welcome_enabled", default_enabled=True, track_welcome=True)


async def sweep(context) -> None:
    """Job: remove members who did not verify in time. Kick (ban + unban), so they may try again."""
    async with async_session() as session:
        due = (await session.execute(select(PendingVerification).where(PendingVerification.deadline <= mt.utcnow()))).scalars().all()
        jobs = [(p.id, p.chat_id, p.user_id, p.message_id) for p in due]
    for pid, chat_id, user_id, message_id in jobs:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        except TelegramError as exc:
            logger.info("welcomemute_kick_failed chat=%s user=%s err=%s", chat_id, user_id, exc)
        if message_id:
            try:
                await context.bot.delete_message(chat_id, message_id)
            except TelegramError:
                pass
        async with async_session() as session:
            await session.execute(delete(PendingVerification).where(PendingVerification.id == pid))
            await session.commit()


# ── commands ────────────────────────────────────────────────────────────────
_HELP = (
    "👋 <b>Welcome mute</b>\n"
    "• <code>/welcomemute soft</code> - new members can't send media, stickers, polls or links for 24 hours.\n"
    "• <code>/welcomemute strong</code> - new members are muted until they tap a button to prove they're human; "
    f"they have {VERIFY_SECONDS} seconds or they are removed (they can rejoin).\n"
    "• <code>/welcomemute off</code> - turn it off.\n\n"
    "Admins, approved members, bots and people added by someone else are never muted. Members who passed the check "
    "once aren't asked again. <code>/captcha on</code> acts as <i>strong</i> unless you pick a setting here."
)


@safe_handler
async def welcomemutehelp_cmd(update, context) -> None:
    """/welcomemutehelp - explain the welcome-mute modes."""
    await update.effective_message.reply_text(_HELP, parse_mode="HTML")


@admin_only
@group_only
@safe_handler
async def welcomemute_cmd(update, context) -> None:
    """/welcomemute [off|soft|strong] - what happens to members who join by themselves."""
    chat, msg = update.effective_chat, update.effective_message
    arg = (context.args[0].lower() if context.args else "")
    arg = {"no": "off", "false": "off"}.get(arg, arg)
    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        if row is None:
            row = Chat(id=chat.id, type=chat.type, title=chat.title)
            session.add(row)
        if arg in MODES:
            row.welcome_mute = arg
            await session.commit()
        current = effective_mode(row.welcome_mute, row.captcha_enabled)
    if not arg:
        await msg.reply_text(f"👋 Welcome mute: <b>{current}</b>\nChoose: <code>off</code>, <code>soft</code> or <code>strong</code> "
                             f"(see /welcomemutehelp).", parse_mode="HTML")
        return
    if arg not in MODES:
        await msg.reply_text("Please enter <code>off</code>, <code>soft</code> or <code>strong</code>!", parse_mode="HTML")
        return
    text = {"off": "I will no longer mute people when they join.",
            "soft": "I will stop new members sending media, stickers, polls and links for 24 hours.",
            "strong": f"I will mute new members until they prove they're human. They have {VERIFY_SECONDS} seconds or they are removed."}[arg]
    problem = await mt.check_bot(context, chat.id, "restrict") if arg != "off" else None
    await msg.reply_text(f"👋 {text}" + (f"\n⚠️ {escape_html(problem)} Until I can restrict members this does nothing." if problem else ""),
                         parse_mode="HTML")


def register(app) -> None:
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & filters.ChatType.GROUPS, join_guard), group=-7)
    app.add_handler(CommandHandler("welcomemute", welcomemute_cmd))
    app.add_handler(CommandHandler("welcomemutehelp", welcomemutehelp_cmd))
    app.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^wmv:"))
    job_queue = getattr(app, "job_queue", None)
    if job_queue is not None:
        job_queue.run_repeating(sweep, interval=SWEEP_SECONDS, first=45, name="welcomemute_sweep")
