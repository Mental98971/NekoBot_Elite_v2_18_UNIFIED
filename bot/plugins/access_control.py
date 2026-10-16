"""
Access control & bot-wide ops — from YukkiMusicBot's sudo/blacklistchat.py,
sudo/private.py, sudo/maintenance.py, misc/globalstats.py, and
tools/active.py, adapted to this project's owner/admin model
(settings.is_admin_id) instead of Yukki's separate SUDOERS list. Also
now includes /gban and /block, ported from AnonXMusic's globalban.py
and block.py — two deliberately distinct mechanisms (see the GlobalBan
/ BlockedUser model comments in bot/core/database.py).

/stats, /mystats, and /botstats already mean other things in NovaBot
(chat-level moderation stats, personal economy stats, font usage) —
this is /globalstats: bot-wide totals across every chat it's in.
"""
from __future__ import annotations

import time

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.identity import bot_name
from bot.core.database import (
    Ban,
    BlockedUser,
    Chat,
    GlobalBan,
    Mute,
    User,
    Warn,
    async_session,
    get_bot_config,
)
from bot.middleware.access_control import invalidate_banned_users_cache, invalidate_config_cache
from bot.utils.helpers import escape_html, resolve_target_user
from bot.core.errors import safe_handler


def _invalidate_enforcement() -> None:
    """Also drop fedtools' per-chat ban answers so a new /gban or /ungban applies immediately (lazy import avoids a cycle)."""
    try:
        from bot.plugins.fedtools import invalidate_enforcement_cache
        invalidate_enforcement_cache()
    except Exception:  # cache housekeeping must never break a moderation command
        pass


def _owner_only(func_):
    """Stricter than admin_only — only the configured OWNER_ID, not the
    broader ADMIN_IDS list, since these commands change bot-wide
    behavior (maintenance, private mode) rather than one chat's."""
    import functools

    @functools.wraps(func_)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != settings.owner_id:
            await update.message.reply_text("🚫 Owner only.")
            return
        return await func_(update, context)

    return wrapper


def _sudo_only(func_):
    """Owner or ADMIN_IDS — for the less-sensitive bot-wide commands
    (blacklist/authorize a chat, view global stats)."""
    import functools

    @functools.wraps(func_)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not settings.is_admin_id(update.effective_user.id):
            await update.message.reply_text("🚫 Sudo only.")
            return
        return await func_(update, context)

    return wrapper


async def _target_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A chat ID arg if given, else the chat this command was sent in."""
    if context.args and context.args[0].lstrip("-").isdigit():
        return int(context.args[0])
    return update.effective_chat.id


# ==================== BLACKLIST ====================

@_sudo_only
@safe_handler
async def blacklistchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = await _target_chat_id(update, context)
    async with async_session() as session:
        row = await session.get(Chat, chat_id)
        if row is None:
            row = Chat(id=chat_id, type="unknown")
            session.add(row)
        row.bot_blacklisted = True
        await session.commit()
    await update.message.reply_text(f"🚫 Chat <code>{chat_id}</code> blacklisted — {bot_name()} will ignore it.", parse_mode="HTML")


@_sudo_only
@safe_handler
async def whitelistchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = await _target_chat_id(update, context)
    async with async_session() as session:
        row = await session.get(Chat, chat_id)
        if row:
            row.bot_blacklisted = False
            await session.commit()
    await update.message.reply_text(f"✅ Chat <code>{chat_id}</code> removed from the blacklist.", parse_mode="HTML")


@_sudo_only
@safe_handler
async def blacklistedchats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(select(Chat.id, Chat.title).where(Chat.bot_blacklisted.is_(True)))
        rows = result.all()
    if not rows:
        await update.message.reply_text("No blacklisted chats.")
        return
    lines = "\n".join(f"• <code>{cid}</code> {escape_html(title or '')}" for cid, title in rows)
    await update.message.reply_text(f"🚫 <b>Blacklisted chats</b>\n{lines}", parse_mode="HTML")


# ==================== PRIVATE-MODE AUTHORIZATION ====================

@_sudo_only
@safe_handler
async def authorize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = await _target_chat_id(update, context)
    async with async_session() as session:
        row = await session.get(Chat, chat_id)
        if row is None:
            row = Chat(id=chat_id, type="unknown")
            session.add(row)
        row.private_mode_authorized = True
        await session.commit()
    await update.message.reply_text(f"✅ Chat <code>{chat_id}</code> authorized.", parse_mode="HTML")


@_sudo_only
@safe_handler
async def unauthorize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = await _target_chat_id(update, context)
    async with async_session() as session:
        row = await session.get(Chat, chat_id)
        if row:
            row.private_mode_authorized = False
            await session.commit()
    await update.message.reply_text(f"❌ Chat <code>{chat_id}</code> de-authorized.", parse_mode="HTML")


@_sudo_only
@safe_handler
async def authorizedchats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(select(Chat.id, Chat.title).where(Chat.private_mode_authorized.is_(True)))
        rows = result.all()
    if not rows:
        await update.message.reply_text("No authorized chats.")
        return
    lines = "\n".join(f"• <code>{cid}</code> {escape_html(title or '')}" for cid, title in rows)
    await update.message.reply_text(f"✅ <b>Authorized chats</b>\n{lines}", parse_mode="HTML")


# ==================== GLOBAL TOGGLES ====================

@_owner_only
@safe_handler
async def privatemode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        async with async_session() as session:
            cfg = await get_bot_config(session)
            await session.commit()
            state = "on" if cfg.private_bot_mode else "off"
        await update.message.reply_text(
            f"🔒 Private mode is currently <b>{state}</b>.\nUsage: /privatemode on|off\n\n"
            f"When on, only /authorize'd chats (and DMs) get responses.",
            parse_mode="HTML",
        )
        return
    async with async_session() as session:
        cfg = await get_bot_config(session)
        cfg.private_bot_mode = arg == "on"
        await session.commit()
    invalidate_config_cache()
    await update.message.reply_text(f"🔒 Private mode is now {'ON' if arg == 'on' else 'OFF'}.")


@_owner_only
@safe_handler
async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off", "enable", "disable"):
        await update.message.reply_text("Usage: /maintenance on|off")
        return
    turn_on = arg in ("on", "enable")
    async with async_session() as session:
        cfg = await get_bot_config(session)
        cfg.maintenance_mode = turn_on
        await session.commit()
    invalidate_config_cache()
    await update.message.reply_text(f"🔧 Maintenance mode is now {'ON — only sudoers get responses' if turn_on else 'OFF'}.")


# ==================== VISIBILITY ====================

@_sudo_only
@safe_handler
async def globalstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        total_chats = (await session.execute(select(func.count()).select_from(Chat))).scalar() or 0
        total_users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
        total_warns = (await session.execute(select(func.count()).select_from(Warn))).scalar() or 0
        total_bans = (await session.execute(select(func.count()).select_from(Ban))).scalar() or 0
        total_mutes = (await session.execute(select(func.count()).select_from(Mute))).scalar() or 0
        blacklisted = (await session.execute(
            select(func.count()).select_from(Chat).where(Chat.bot_blacklisted.is_(True))
        )).scalar() or 0

    pool = context.application.bot_data.get("assistant_pool")
    active_calls = pool.total_active_calls if pool else 0
    assistants = len(pool.pairs) if pool else 0

    await update.message.reply_text(
        f"🌐 <b>Global Stats</b>\n\n"
        f"Chats: <b>{total_chats:,}</b> ({blacklisted} blacklisted)\n"
        f"Known users: <b>{total_users:,}</b>\n"
        f"Warns issued: <b>{total_warns:,}</b>\n"
        f"Bans issued: <b>{total_bans:,}</b>\n"
        f"Mutes issued: <b>{total_mutes:,}</b>\n"
        f"Active voice chats: <b>{active_calls}</b> across <b>{assistants}</b> assistant(s)",
        parse_mode="HTML",
    )


@_sudo_only
@safe_handler
async def activevc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool = context.application.bot_data.get("assistant_pool")
    active = pool.active_chats if pool else {}
    if not active:
        await update.message.reply_text("📭 No active voice chats.")
        return
    lines = [f"🎧 <b>Active voice chats</b> ({len(active)})\n"]
    for chat_id, idx in active.items():
        lines.append(f"• <code>{chat_id}</code> — assistant #{idx + 1}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_sudo_only
@safe_handler
async def speedtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import httpx

    msg = await update.message.reply_text("📡 Testing...")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.gstatic.com/generate_204")
        elapsed_ms = (time.monotonic() - start) * 1000
        await msg.edit_text(
            f"📡 <b>Speedtest</b>\nRound-trip to Google: <b>{elapsed_ms:.0f}ms</b>\nHTTP status: {resp.status_code}",
            parse_mode="HTML",
        )
    except Exception as e:
        await msg.edit_text(f"❌ Speedtest failed: {e}")


# ==================== GLOBAL BAN (from AnonXMusic) ====================
# Actually removes the user from every chat NovaBot moderates — distinct
# from /block below, which just makes NovaBot stop responding to them.

@_sudo_only
@safe_handler
async def gban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /gban @username [reason]")
        return
    if settings.is_admin_id(target_id):
        await update.message.reply_text("🚫 Can't gban a sudoer.")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else None

    async with async_session() as session:
        existing = await session.get(GlobalBan, target_id)
        if existing:
            await update.message.reply_text(f"{escape_html(target_name)} is already gbanned.", parse_mode="HTML")
            return
        session.add(GlobalBan(user_id=target_id, reason=reason, banned_by=update.effective_user.id))
        await session.commit()
        chat_ids = (await session.execute(select(Chat.id))).scalars().all()

    invalidate_banned_users_cache()
    _invalidate_enforcement()
    status = await update.message.reply_text(f"🌐 Gbanning {escape_html(target_name)} from {len(chat_ids)} chats...", parse_mode="HTML")

    removed = 0
    for chat_id in chat_ids:
        try:
            await context.bot.ban_chat_member(chat_id, target_id)
            removed += 1
        except Exception:
            pass

    await status.edit_text(f"🌐 {escape_html(target_name)} gbanned — removed from {removed} chat(s).", parse_mode="HTML")


@_sudo_only
@safe_handler
async def ungban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /ungban @username")
        return

    async with async_session() as session:
        existing = await session.get(GlobalBan, target_id)
        if not existing:
            await update.message.reply_text(f"{escape_html(target_name)} isn't gbanned.", parse_mode="HTML")
            return
        await session.delete(existing)
        await session.commit()
        chat_ids = (await session.execute(select(Chat.id))).scalars().all()

    invalidate_banned_users_cache()
    _invalidate_enforcement()
    for chat_id in chat_ids:
        try:
            await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        except Exception:
            pass
    await update.message.reply_text(f"✅ {escape_html(target_name)} ungbanned.", parse_mode="HTML")


@_sudo_only
@safe_handler
async def gbanned_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        rows = (await session.execute(select(GlobalBan))).scalars().all()
    if not rows:
        await update.message.reply_text("No gbanned users.")
        return
    lines = "\n".join(f"• <code>{r.user_id}</code> {escape_html(r.reason or '')}" for r in rows)
    await update.message.reply_text(f"🌐 <b>Gbanned users</b>\n{lines}", parse_mode="HTML")


# ==================== BLOCK (from AnonXMusic) — lighter than gban:
# NovaBot just ignores them everywhere, no chat-membership changes ====

@_sudo_only
@safe_handler
async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /block @username [reason]")
        return
    if settings.is_admin_id(target_id):
        await update.message.reply_text("🚫 Can't block a sudoer.")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else None

    async with async_session() as session:
        existing = await session.get(BlockedUser, target_id)
        if existing:
            await update.message.reply_text(f"{escape_html(target_name)} is already blocked.", parse_mode="HTML")
            return
        session.add(BlockedUser(user_id=target_id, reason=reason, blocked_by=update.effective_user.id))
        await session.commit()
    invalidate_banned_users_cache()
    _invalidate_enforcement()
    await update.message.reply_text(f"🚷 {escape_html(target_name)} blocked — {bot_name()} will ignore them everywhere.", parse_mode="HTML")


@_sudo_only
@safe_handler
async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /unblock @username")
        return
    async with async_session() as session:
        existing = await session.get(BlockedUser, target_id)
        if not existing:
            await update.message.reply_text(f"{escape_html(target_name)} isn't blocked.", parse_mode="HTML")
            return
        await session.delete(existing)
        await session.commit()
    invalidate_banned_users_cache()
    _invalidate_enforcement()
    await update.message.reply_text(f"✅ {escape_html(target_name)} unblocked.", parse_mode="HTML")


@_sudo_only
@safe_handler
async def blocked_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        rows = (await session.execute(select(BlockedUser))).scalars().all()
    if not rows:
        await update.message.reply_text("No blocked users.")
        return
    lines = "\n".join(f"• <code>{r.user_id}</code> {escape_html(r.reason or '')}" for r in rows)
    await update.message.reply_text(f"🚷 <b>Blocked users</b>\n{lines}", parse_mode="HTML")


@_owner_only
@safe_handler
async def autoend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-leave a voice chat once no real participants are left —
    from AnonXMusic's /autoend. Distinct from ASSISTANT_AUTO_LEAVE_SECONDS
    (ASSISTANT_AUTO_LEAVE_SECONDS is "no playback activity for N seconds";
    this is "no humans are actually in the call")."""
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        async with async_session() as session:
            cfg = await get_bot_config(session)
            await session.commit()
            state = "on" if cfg.autoend_enabled else "off"
        await update.message.reply_text(f"👋 Autoend is currently <b>{state}</b>.\nUsage: /autoend on|off", parse_mode="HTML")
        return
    async with async_session() as session:
        cfg = await get_bot_config(session)
        cfg.autoend_enabled = arg == "on"
        await session.commit()
    invalidate_config_cache()
    await update.message.reply_text(f"👋 Autoend is now {'ON' if arg == 'on' else 'OFF'}.")


def register(app):
    app.add_handler(CommandHandler("blacklistchat", blacklistchat_cmd))
    app.add_handler(CommandHandler("whitelistchat", whitelistchat_cmd))
    app.add_handler(CommandHandler("blacklistedchats", blacklistedchats_cmd))
    app.add_handler(CommandHandler("authorize", authorize_cmd))
    app.add_handler(CommandHandler("unauthorize", unauthorize_cmd))
    app.add_handler(CommandHandler("authorizedchats", authorizedchats_cmd))
    app.add_handler(CommandHandler("privatemode", privatemode_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    app.add_handler(CommandHandler("globalstats", globalstats_cmd))
    app.add_handler(CommandHandler("activevc", activevc_cmd))
    app.add_handler(CommandHandler("speedtest", speedtest_cmd))
    app.add_handler(CommandHandler("gban", gban_cmd))
    app.add_handler(CommandHandler("ungban", ungban_cmd))
    app.add_handler(CommandHandler("gbanned", gbanned_cmd))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("blocked", blocked_cmd))
    app.add_handler(CommandHandler("autoend", autoend_cmd))
