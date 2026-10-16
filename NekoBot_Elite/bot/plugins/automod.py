"""
Auto-Moderation — new plugin, not present in any of the original four
projects. Anti-raid detection and a
queryable view of active temp-mutes/bans.

Note on temp-action expiry: Telegram's Bot API already lifts a
restrict/ban automatically once `until_date` passes — that part doesn't
need this bot to be running. What Telegram *doesn't* provide is a way to
list who's currently restricted or to announce when it lifts, which is
what TempAction + the JobQueue notification here actually add.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

from sqlalchemy import select
from telegram import ChatPermissions, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import settings
from bot.core.database import Chat, TempAction, async_session
from bot.core.decorators import admin_only, group_only
from bot.utils.helpers import format_duration, mention_html
from bot.core.errors import safe_handler

# Recent-join timestamps per chat, for anti-raid detection — ephemeral by
# design, doesn't need to survive a restart the way lockdown state does.
_recent_joins: Dict[int, List[datetime]] = {}


# ==================== ANTI-RAID ====================

@group_only
@admin_only
@safe_handler
async def antiraid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /antiraid on|off")
        return
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(chat)
        chat.antiraid_enabled = arg == "on"
        await session.commit()
    await update.message.reply_text(f"🛡️ Anti-raid is now {'ON' if arg == 'on' else 'OFF'}.")


@group_only
@admin_only
@safe_handler
async def lockdown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "on")
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(chat)
        if arg == "off":
            chat.lockdown_until = None
            await session.commit()
            await update.message.reply_text("🔓 Lockdown lifted — new members can chat normally again.")
        else:
            minutes = int(arg) if arg.isdigit() else 15
            chat.lockdown_until = datetime.utcnow() + timedelta(minutes=minutes)
            await session.commit()
            await update.message.reply_text(
                f"🔒 Lockdown active for {minutes}m — new members will be restricted on join."
            )


@admin_only
@group_only
@safe_handler
async def antichannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/antichannel on|off — when on, messages posted via a linked
    channel's anonymous identity (a common way spammers dodge normal
    per-user bans) are deleted and that channel is banned from posting
    here again. Genuine admins posting as themselves are unaffected —
    this only ever looks at message.sender_chat."""
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(chat)
        chat.antichannel_enabled = not chat.antichannel_enabled
        await session.commit()
        status = "enabled 🚫" if chat.antichannel_enabled else "disabled"
    await update.message.reply_text(f"AntiChannel {status}")


@safe_handler
async def antichannel_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes messages posted via a channel identity and bans that
    channel from posting again, when /antichannel is enabled for this
    chat. Registered in its own listener group (11) — this codebase
    reserves group numbers per passive listener (0-10 already assigned
    across automod/group_mgmt/fun/fonts/economy/collector/personality/
    name_history) since only the first matching handler in a shared
    group runs and a collision silently drops the others."""
    message = update.effective_message
    if message is None or message.sender_chat is None:
        return
    chat_id = update.effective_chat.id

    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None or not chat.antichannel_enabled:
            return

    # A group's own linked discussion channel posts as that same
    # channel identity legitimately (e.g. channel posts mirrored into
    # its discussion group) — don't treat that as spam.
    linked_id = getattr(update.effective_chat, "linked_chat_id", None)
    if linked_id is not None and message.sender_chat.id == linked_id:
        return

    try:
        await message.delete()
    except Exception:
        pass
    try:
        await context.bot.ban_chat_sender_chat(chat_id=chat_id, sender_chat_id=message.sender_chat.id)
    except Exception:
        pass


@safe_handler
async def raid_join_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks join velocity per chat and auto-triggers a lockdown if it
    spikes. Also restricts new joiners while a lockdown (manual or
    auto-triggered) is active."""
    chat = update.effective_chat
    new_members = update.message.new_chat_members if update.message else []
    if not new_members:
        return

    now = datetime.utcnow()

    async with async_session() as session:
        chat_row = await session.get(Chat, chat.id)
        antiraid_on = bool(chat_row and chat_row.antiraid_enabled)
        threshold = (chat_row.antiraid_join_threshold if chat_row else None) or settings.antiraid_default_join_threshold
        window = (chat_row.antiraid_join_window if chat_row else None) or settings.antiraid_default_join_window
        lockdown_active = bool(chat_row and chat_row.lockdown_until and chat_row.lockdown_until > now)

        if antiraid_on and not lockdown_active:
            joins = _recent_joins.setdefault(chat.id, [])
            joins.extend([now] * len(new_members))
            cutoff = now - timedelta(seconds=window)
            _recent_joins[chat.id] = [t for t in joins if t > cutoff]

            if len(_recent_joins[chat.id]) >= threshold:
                if chat_row is None:
                    chat_row = Chat(id=chat.id, type=chat.type)
                    session.add(chat_row)
                chat_row.lockdown_until = now + timedelta(minutes=15)
                lockdown_active = True
                await session.commit()
                await context.bot.send_message(
                    chat.id,
                    f"🚨 <b>Raid detected</b> — {len(_recent_joins[chat.id])} joins in {window}s. "
                    f"Auto-lockdown enabled for 15m. New members will be restricted until an admin lifts it "
                    f"with <code>/lockdown off</code>.",
                    parse_mode="HTML",
                )
        else:
            await session.commit()

    if lockdown_active:
        perms = ChatPermissions(can_send_messages=False)
        for member in new_members:
            try:
                await context.bot.restrict_chat_member(chat.id, member.id, perms)
            except Exception:
                pass


# ==================== TEMP-ACTION VISIBILITY ====================

async def _list_temp_actions(update: Update, action: str, label: str):
    async with async_session() as session:
        result = await session.execute(
            select(TempAction).where(
                TempAction.chat_id == update.effective_chat.id,
                TempAction.action == action,
                TempAction.reversed.is_(False),
                TempAction.expires_at > datetime.utcnow(),
            ).order_by(TempAction.expires_at)
        )
        rows = result.scalars().all()

    if not rows:
        await update.message.reply_text(f"No active {label}.")
        return

    lines = [f"<b>Active {label}</b>\n"]
    for row in rows:
        remaining = row.expires_at - datetime.utcnow()
        lines.append(f"• {mention_html(row.user_id, str(row.user_id))} — {format_duration(int(remaining.total_seconds()))} left")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@group_only
@safe_handler
async def mutes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _list_temp_actions(update, "mute", "temp-mutes")


@group_only
@safe_handler
async def bans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _list_temp_actions(update, "ban", "temp-bans")


async def _notify_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    action_id = job.data["action_id"]

    async with async_session() as session:
        row = await session.get(TempAction, action_id)
        if not row or row.reversed:
            return
        row.reversed = True
        await session.commit()
        chat_id, user_id, action = row.chat_id, row.user_id, row.action

    verb = "mute" if action == "mute" else "ban"
    try:
        await context.bot.send_message(
            chat_id, f"⏰ {mention_html(user_id, str(user_id))}'s {verb} has expired.", parse_mode="HTML"
        )
    except Exception:
        pass


def schedule_temp_action_notice(app: Application, action_id: int, seconds: float) -> None:
    """Called by plugins/admin.py after creating a TempAction row for a
    timed mute/ban, so the expiry gets announced."""
    app.job_queue.run_once(_notify_expiry, when=max(0, seconds), data={"action_id": action_id}, name=f"tempaction:{action_id}")


async def rearm_jobs(app: Application) -> None:
    """Re-create expiry-notification jobs for anything still active after
    a restart."""
    now = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(select(TempAction).where(TempAction.reversed.is_(False)))
        for row in result.scalars().all():
            delay = max(0, (row.expires_at - now).total_seconds())
            schedule_temp_action_notice(app, row.id, delay)


def register(app):
    if not settings.enable_automod:
        return
    app.add_handler(CommandHandler("antiraid", antiraid_cmd))
    app.add_handler(CommandHandler("lockdown", lockdown_cmd))
    app.add_handler(CommandHandler("antichannel", antichannel_cmd))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, antichannel_listener), group=11)
    app.add_handler(CommandHandler("mutes", mutes_cmd))
    app.add_handler(CommandHandler("bans", bans_cmd))
    # Own group — see group_mgmt.py's register() for the full explanation;
    # this listens for the same NEW_CHAT_MEMBERS update type as
    # welcome_listener there, so it needs its own slot too.
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, raid_join_listener), group=0)
