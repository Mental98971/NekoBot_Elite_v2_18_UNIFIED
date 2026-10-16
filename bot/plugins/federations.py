"""
Federations — named groups of chats that share a ban list. A federation
owner (or admin) can /fedban a user once and have it apply across every
chat that's joined the federation, without each group's own admins
needing to separately ban them.

This closes a real gap rather than adding something new for its own
sake: /help has advertised /newfed /joinfed /fedban /fedinfo since the
very first NovaGuard round, and the Federation/FedBan tables have
existed just as long — but nothing ever actually implemented them.
Verified by grepping the whole plugins/ directory for any handler
registering those command names before writing this: there wasn't one.

Ported from YaeMiko/FallenRobot's feds.py + feds_sql.py (data model:
owner + JSON admin list + per-federation ban list + chats that have
joined), reusing the same propagate-to-every-relevant-chat pattern
already built for plugins/access_control.py's /gban.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.errors import safe_handler
from bot.core.database import Chat, FedBan, Federation, async_session
from bot.core.decorators import group_only
from bot.utils.helpers import escape_html, resolve_target_user


async def _get_chat_fed(session, chat_id: int) -> Federation | None:
    chat = await session.get(Chat, chat_id)
    if not chat or not chat.federation_id:
        return None
    return await session.get(Federation, chat.federation_id)


def _invalidate_enforcement() -> None:
    """Drop fedtools' cached ban answers so a new fban/unfban applies immediately (lazy import: fedtools imports this module)."""
    try:
        from bot.plugins.fedtools import invalidate_enforcement_cache
        invalidate_enforcement_cache()
    except Exception:  # never let cache housekeeping break a moderation command
        pass


def _is_fed_admin(fed: Federation, user_id: int) -> bool:
    return user_id == fed.owner_id or user_id in (fed.admins or []) or settings.is_admin_id(user_id)


@group_only
@safe_handler
async def newfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if member.status not in ("creator", "administrator") and not settings.is_admin_id(update.effective_user.id):
        await update.message.reply_text("🚫 Only this chat's creator can start a federation from it.")
        return

    name = " ".join(context.args) or f"{update.effective_chat.title}'s Federation"
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat and chat.federation_id:
            await update.message.reply_text("This chat is already in a federation. /leavefed first.")
            return

        fed_id = str(uuid.uuid4())
        session.add(Federation(id=fed_id, name=name, owner_id=update.effective_user.id, admins=[]))
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(chat)
        chat.federation_id = fed_id
        await session.commit()

    await update.message.reply_text(
        f"🌐 Federation <b>{escape_html(name)}</b> created and this chat joined.\n"
        f"Fed ID: <code>{fed_id}</code>", parse_mode="HTML",
    )


@group_only
@safe_handler
async def joinfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if member.status not in ("creator", "administrator") and not settings.is_admin_id(update.effective_user.id):
        await update.message.reply_text("🚫 Only this chat's creator can join a federation.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /joinfed <fed id>")
        return

    async with async_session() as session:
        fed = await session.get(Federation, context.args[0])
        if not fed:
            await update.message.reply_text("❌ No federation with that ID.")
            return
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(chat)
        elif chat.federation_id:
            await update.message.reply_text("This chat is already in a federation. /leavefed first.")
            return
        chat.federation_id = fed.id
        await session.commit()
        fed_name = fed.name

    await update.message.reply_text(f"🌐 Joined federation <b>{escape_html(fed_name)}</b>.", parse_mode="HTML")


@group_only
@safe_handler
async def leavefed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if member.status not in ("creator", "administrator") and not settings.is_admin_id(update.effective_user.id):
        await update.message.reply_text("🚫 Only this chat's creator can leave its federation.")
        return
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if not chat or not chat.federation_id:
            await update.message.reply_text("This chat isn't in a federation.")
            return
        chat.federation_id = None
        await session.commit()
    await update.message.reply_text("🌐 Left the federation.")


@group_only
@safe_handler
async def fedinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        fed_id = context.args[0] if context.args else None
        fed = await session.get(Federation, fed_id) if fed_id else await _get_chat_fed(session, update.effective_chat.id)
        if not fed:
            await update.message.reply_text("This chat isn't in a federation. /fedinfo <fed id> to look one up.")
            return
        chat_count = (await session.execute(
            select(Chat.id).where(Chat.federation_id == fed.id)
        )).scalars().all()
        ban_count = (await session.execute(
            select(FedBan.user_id).where(FedBan.federation_id == fed.id)
        )).scalars().all()

    await update.message.reply_text(
        f"🌐 <b>{escape_html(fed.name)}</b>\n"
        f"ID: <code>{fed.id}</code>\n"
        f"Owner: <code>{fed.owner_id}</code>\n"
        f"Admins: {len(fed.admins or [])}\n"
        f"Chats: {len(chat_count)}\n"
        f"Fbans: {len(ban_count)}",
        parse_mode="HTML",
    )


@group_only
@safe_handler
async def fedban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        fed = await _get_chat_fed(session, update.effective_chat.id)
        if not fed:
            await update.message.reply_text("This chat isn't in a federation.")
            return
        if not _is_fed_admin(fed, update.effective_user.id):
            await update.message.reply_text("🚫 Federation admins only.")
            return

        target_id, target_name = await resolve_target_user(update, context)
        if not target_id:
            await update.message.reply_text("Reply to a user, or /fedban @username [reason]")
            return
        if _is_fed_admin(fed, target_id):
            await update.message.reply_text("🚫 Can't fedban a federation admin.")
            return

        existing = (await session.execute(
            select(FedBan).where(FedBan.federation_id == fed.id, FedBan.user_id == target_id)
        )).scalar_one_or_none()
        if existing:
            await update.message.reply_text(f"{escape_html(target_name)} is already fbanned here.", parse_mode="HTML")
            return
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
        session.add(FedBan(federation_id=fed.id, user_id=target_id, reason=reason, banned_by=update.effective_user.id))
        await session.commit()
        _invalidate_enforcement()

        chat_ids = (await session.execute(select(Chat.id).where(Chat.federation_id == fed.id))).scalars().all()
        fed_name, log_channel = fed.name, fed.log_channel

    status = await update.message.reply_text(f"🌐 Fedbanning {escape_html(target_name)} across {len(chat_ids)} chats...", parse_mode="HTML")
    removed = 0
    for chat_id in chat_ids:
        try:
            await context.bot.ban_chat_member(chat_id, target_id)
            removed += 1
        except Exception:
            pass

    await status.edit_text(
        f"🌐 {escape_html(target_name)} fedbanned from <b>{escape_html(fed_name)}</b> — removed from {removed} chat(s).",
        parse_mode="HTML",
    )
    if log_channel:
        try:
            await context.bot.send_message(
                log_channel,
                f"🌐 #FEDBAN\nFed: {escape_html(fed_name)}\nUser: <code>{target_id}</code>\nBy: <code>{update.effective_user.id}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


@group_only
@safe_handler
async def unfedban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        fed = await _get_chat_fed(session, update.effective_chat.id)
        if not fed:
            await update.message.reply_text("This chat isn't in a federation.")
            return
        if not _is_fed_admin(fed, update.effective_user.id):
            await update.message.reply_text("🚫 Federation admins only.")
            return

        target_id, target_name = await resolve_target_user(update, context)
        if not target_id:
            await update.message.reply_text("Reply to a user, or /unfedban @username")
            return

        existing = (await session.execute(
            select(FedBan).where(FedBan.federation_id == fed.id, FedBan.user_id == target_id)
        )).scalar_one_or_none()
        if not existing:
            await update.message.reply_text(f"{escape_html(target_name)} isn't fbanned here.", parse_mode="HTML")
            return
        await session.delete(existing)
        await session.commit()
        _invalidate_enforcement()
        chat_ids = (await session.execute(select(Chat.id).where(Chat.federation_id == fed.id))).scalars().all()

    for chat_id in chat_ids:
        try:
            await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        except Exception:
            pass
    await update.message.reply_text(f"✅ {escape_html(target_name)} unfedbanned.", parse_mode="HTML")


@group_only
@safe_handler
async def fpromote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        fed = await _get_chat_fed(session, update.effective_chat.id)
        if not fed:
            await update.message.reply_text("This chat isn't in a federation.")
            return
        if update.effective_user.id != fed.owner_id and not settings.is_admin_id(update.effective_user.id):
            await update.message.reply_text("🚫 Only the federation owner can promote fed admins.")
            return
        target_id, target_name = await resolve_target_user(update, context)
        if not target_id:
            await update.message.reply_text("Reply to a user, or /fpromote @username")
            return
        admins = list(fed.admins or [])
        if target_id in admins:
            await update.message.reply_text(f"{escape_html(target_name)} is already a fed admin.", parse_mode="HTML")
            return
        admins.append(target_id)
        fed.admins = admins
        await session.commit()
    await update.message.reply_text(f"🌐 {escape_html(target_name)} is now a federation admin.", parse_mode="HTML")


@group_only
@safe_handler
async def fdemote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        fed = await _get_chat_fed(session, update.effective_chat.id)
        if not fed:
            await update.message.reply_text("This chat isn't in a federation.")
            return
        if update.effective_user.id != fed.owner_id and not settings.is_admin_id(update.effective_user.id):
            await update.message.reply_text("🚫 Only the federation owner can demote fed admins.")
            return
        target_id, target_name = await resolve_target_user(update, context)
        if not target_id:
            await update.message.reply_text("Reply to a user, or /fdemote @username")
            return
        admins = [a for a in (fed.admins or []) if a != target_id]
        fed.admins = admins
        await session.commit()
    await update.message.reply_text(f"🌐 {escape_html(target_name)} is no longer a federation admin.", parse_mode="HTML")


@group_only
@safe_handler
async def fedadmins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        fed = await _get_chat_fed(session, update.effective_chat.id)
        if not fed:
            await update.message.reply_text("This chat isn't in a federation.")
            return
        owner_id, admins = fed.owner_id, list(fed.admins or [])

    lines = [f"👑 Owner: <code>{owner_id}</code>"]
    lines += [f"• <code>{a}</code>" for a in admins] or ["(no additional admins)"]
    await update.message.reply_text("🌐 <b>Federation admins</b>\n" + "\n".join(lines), parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("newfed", newfed_cmd))
    app.add_handler(CommandHandler("joinfed", joinfed_cmd))
    app.add_handler(CommandHandler("leavefed", leavefed_cmd))
    app.add_handler(CommandHandler("fedinfo", fedinfo_cmd))
    app.add_handler(CommandHandler("fedban", fedban_cmd))
    app.add_handler(CommandHandler("unfedban", unfedban_cmd))
    app.add_handler(CommandHandler("fpromote", fpromote_cmd))
    app.add_handler(CommandHandler("fdemote", fdemote_cmd))
    app.add_handler(CommandHandler("fedadmins", fedadmins_cmd))
