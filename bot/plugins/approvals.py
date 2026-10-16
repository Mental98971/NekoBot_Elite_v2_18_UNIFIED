"""Approved users — ported from FallenRobot's approve module (v2.12).

An approved user is exempt from automatic moderation in *this chat*: locks, the word blacklist,
anti-flood, anti-spam and slow mode skip them (``modtools.is_exempt`` is the single place that
decides). They are still subject to manual moderation by admins. Admins never need approval.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.core.database import Approval, async_session
from bot.core.errors import safe_handler
from bot.utils import chatcache, modtools as mt
from bot.utils.helpers import escape_html, mention_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

LIST_LIMIT = 50


@mt.mod_command("restrict")
async def approve_cmd(update, context, ctx):
    """Exempt a user from locks, flood, spam and the blacklist: /approve <reply|id>"""
    msg = update.effective_message
    actor_id, actor = mt.actor_of(update)
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(f"Usage: /approve\n{mt.NO_TARGET_HELP}")
        return
    block = await mt.target_block_reason(context, ctx.chat_id, target, actor_id, allow_admin=True, allow_self=True)
    if block:
        await msg.reply_text(f"🚫 {block}")
        return
    member = await target.load_member(context.bot, ctx.chat_id)
    if member is None or member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        await msg.reply_text("🚫 That user isn't in this chat.")
        return
    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await msg.reply_text("Admins are always exempt — nothing to approve.")
        return
    try:
        async with async_session() as session:
            await mt.ensure_rows(session, ctx.chat_id, target.user_id, first_name=target.name)
            exists = (await session.execute(select(Approval.id).where(
                Approval.chat_id == ctx.chat_id, Approval.user_id == target.user_id))).first()
            if exists:
                await msg.reply_text(f"{target.mention} is already approved.", parse_mode="HTML")
                return
            session.add(Approval(chat_id=ctx.chat_id, user_id=target.user_id, first_name=target.name[:128], approved_by=actor_id))
            await session.commit()
    except IntegrityError:   # another admin approved them at the same moment
        await msg.reply_text(f"{target.mention} is already approved.", parse_mode="HTML")
        return
    await msg.reply_text(f"✅ {target.mention} is approved: locks, flood, spam and blacklist checks skip them in this chat.", parse_mode="HTML")
    await mt.log_event(context, ctx.chat_id, "Approved", actor=actor, target=(target.user_id, target.name))


@mt.mod_command("restrict")
async def unapprove_cmd(update, context, ctx):
    """Remove a user's approval: /unapprove <reply|id>"""
    msg = update.effective_message
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(f"Usage: /unapprove\n{mt.NO_TARGET_HELP}")
        return
    async with async_session() as session:
        # ORM delete (not a bulk DELETE) so the commit hook in bot/utils/chatcache.py drops the cached
        # approval list and the exemption ends on the very next message.
        row = (await session.execute(select(Approval).where(
            Approval.chat_id == ctx.chat_id, Approval.user_id == target.user_id))).scalars().first()
        if row is not None:
            await session.delete(row)
            await session.commit()
    if row is None:
        await msg.reply_text(f"{target.mention} wasn't approved.", parse_mode="HTML")
        return
    await msg.reply_text(f"✅ {target.mention} is no longer approved.", parse_mode="HTML")
    await mt.log_event(context, ctx.chat_id, "Unapproved", actor=mt.actor_of(update)[1], target=(target.user_id, target.name))


@mt.mod_command(None)
async def approved_cmd(update, context, ctx):
    """List this chat's approved users: /approved"""
    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(Approval).where(Approval.chat_id == ctx.chat_id))).scalar() or 0
        rows = (await session.execute(select(Approval).where(Approval.chat_id == ctx.chat_id)
                                      .order_by(Approval.id).limit(LIST_LIMIT))).scalars().all()
    if not rows:
        await update.effective_message.reply_text("Nobody is approved in this chat.")
        return
    lines = [f"✅ <b>Approved in {escape_html(ctx.title)}</b> ({total})"]
    lines += [f"• {mention_html(r.user_id, r.first_name or str(r.user_id))}" for r in rows]
    if total > len(rows):
        lines.append(f"<i>…and {total - len(rows)} more</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def approval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check whether you (or the replied-to user) are approved: /approval"""
    ctx = await mt.resolve_ctx(update, context, remote_ok=False)
    if ctx is None:
        return
    msg, me = update.effective_message, update.effective_user
    target = await mt.resolve_target(update, context)
    if target is None:
        target = mt.Target(me.id, mt.display_name(me), me)
    approved = await chatcache.is_approved(ctx.chat_id, target.user_id)
    await msg.reply_text(f"{target.mention} is {'✅ approved' if approved else 'not approved'} in this chat.", parse_mode="HTML")


@safe_handler
async def unapproveall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove every approval in this chat (owner only, asks first): /unapproveall"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    msg = update.effective_message
    if mt.is_anonymous_admin(update, ctx.chat_id) or not await mt.is_chat_owner(context.bot, ctx.chat_id, update.effective_user.id):
        await msg.reply_text("🚫 Only the chat owner can remove every approval at once.")
        return
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Remove all", callback_data=f"unapproveall:y:{ctx.chat_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="unapproveall:n"),
    ]])
    await msg.reply_text("Remove <b>every</b> approved user in this chat?", parse_mode="HTML", reply_markup=markup)


@safe_handler
async def unapproveall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) < 3 or parts[1] != "y":
        await query.answer("Cancelled")
        await query.edit_message_text("Cancelled — nobody was un-approved.")
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
        res = await session.execute(delete(Approval).where(Approval.chat_id == chat_id))
        await session.commit()
    chatcache.invalidate_chat(chat_id)       # bulk DML bypasses the commit hook
    await query.answer("Done")
    await query.edit_message_text(f"🗑 Removed {res.rowcount or 0} approval(s).")


def register(app):
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler(["unapprove", "disapprove"], unapprove_cmd))
    app.add_handler(CommandHandler("approved", approved_cmd))
    app.add_handler(CommandHandler("approval", approval_cmd))
    app.add_handler(CommandHandler("unapproveall", unapproveall_cmd))
    app.add_handler(CallbackQueryHandler(unapproveall_callback, pattern=r"^unapproveall:"))
