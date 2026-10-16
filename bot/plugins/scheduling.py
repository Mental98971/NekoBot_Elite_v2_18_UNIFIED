"""
Scheduling — polls, reminders, announcements, and scheduled messages.
New plugin, not present in any of the original four projects.

Reminders and scheduled messages are persisted to the database *and*
scheduled on python-telegram-bot's JobQueue (APScheduler under the
hood) — the DB row is what survives a restart; rearm_jobs() (called
from core/bot.py's post_init) re-creates the in-memory JobQueue entries
for anything still pending so nothing is lost if the bot restarts.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Reminder, ScheduledMessage, async_session
from bot.core.decorators import admin_only, group_only
from bot.utils.helpers import escape_html, time_parser
from bot.core.errors import safe_handler


# ==================== POLLS ====================

@safe_handler
async def poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.split(maxsplit=1)
    from bot.plugins.pollsplus import apoll_cmd, looks_like_apoll  # Masha-style "/poll 12345 | question | ..." keeps working
    if len(raw) == 2 and looks_like_apoll(raw[1]):
        await apoll_cmd(update, context)
        return
    if len(raw) < 2 or "|" not in raw[1]:
        await update.message.reply_text(
            "Usage: <code>/poll Question? | Option 1 | Option 2 | ...</code>", parse_mode="HTML"
        )
        return

    parts = [p.strip() for p in raw[1].split("|")]
    question, options = parts[0], [p for p in parts[1:] if p]
    if len(options) < 2:
        await update.message.reply_text("❌ Need at least 2 options.")
        return
    if len(options) > 10:
        await update.message.reply_text("❌ Telegram polls support at most 10 options.")
        return

    await context.bot.send_poll(
        chat_id=update.effective_chat.id, question=question, options=options,
        is_anonymous=False, allows_multiple_answers=False,
    )


# ==================== REMINDERS ====================

async def _fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    reminder_id = job.data["reminder_id"]

    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if not reminder or reminder.fired:
            return
        reminder.fired = True
        await session.commit()
        text, chat_id = reminder.text, reminder.chat_id

    try:
        await context.bot.send_message(chat_id, f"⏰ <b>Reminder</b>\n{escape_html(text)}", parse_mode="HTML")
    except Exception:
        # chat unreachable (bot kicked, etc.) — nothing more to do
        pass


@safe_handler
async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /remind <duration e.g. 10m, 2h, 1d> <text>")
        return

    seconds = time_parser(context.args[0])
    if seconds is None:
        await update.message.reply_text("❌ Invalid duration. Use formats like 10m, 2h, 1d.")
        return

    text = " ".join(context.args[1:])
    remind_at = datetime.utcnow() + timedelta(seconds=seconds)

    async with async_session() as session:
        reminder = Reminder(
            user_id=update.effective_user.id, chat_id=update.effective_chat.id,
            text=text, remind_at=remind_at,
        )
        session.add(reminder)
        await session.commit()
        reminder_id = reminder.id

    context.job_queue.run_once(
        _fire_reminder, when=seconds, data={"reminder_id": reminder_id}, name=f"reminder:{reminder_id}",
    )
    await update.message.reply_text(f"⏰ Got it — I'll remind you in {context.args[0]}.")


@safe_handler
async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == update.effective_user.id, Reminder.fired.is_(False))
            .order_by(Reminder.remind_at)
            .limit(15)
        )
        rows = result.scalars().all()

    if not rows:
        await update.message.reply_text("⏰ You have no pending reminders.")
        return

    lines = ["⏰ <b>Your reminders</b>\n"]
    for r in rows:
        delta = r.remind_at - datetime.utcnow()
        hrs = max(0, int(delta.total_seconds() // 3600))
        mins = max(0, int((delta.total_seconds() % 3600) // 60))
        lines.append(f"• in {hrs}h {mins}m — {escape_html(r.text[:60])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ==================== ANNOUNCEMENTS & SCHEDULED MESSAGES ====================

@group_only
@admin_only
@safe_handler
async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /announce <message>")
        return
    sent = await update.message.reply_text(
        f"📢 <b>Announcement</b>\n\n{escape_html(text)}", parse_mode="HTML"
    )
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, sent.message_id, disable_notification=True)
    except Exception:
        pass  # bot may not have pin rights — the announcement still sent


async def _fire_scheduled_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    msg_id = job.data["scheduled_id"]

    async with async_session() as session:
        row = await session.get(ScheduledMessage, msg_id)
        if not row or row.sent:
            return
        row.sent = True
        await session.commit()
        text, chat_id = row.text, row.chat_id

    try:
        await context.bot.send_message(chat_id, f"📅 <b>Scheduled message</b>\n\n{escape_html(text)}", parse_mode="HTML")
    except Exception:
        pass


@group_only
@admin_only
@safe_handler
async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /schedule <duration e.g. 1h, 1d> <message>")
        return

    seconds = time_parser(context.args[0])
    if seconds is None:
        await update.message.reply_text("❌ Invalid duration. Use formats like 10m, 2h, 1d.")
        return

    text = " ".join(context.args[1:])
    send_at = datetime.utcnow() + timedelta(seconds=seconds)

    async with async_session() as session:
        row = ScheduledMessage(chat_id=update.effective_chat.id, text=text, send_at=send_at, created_by=update.effective_user.id)
        session.add(row)
        await session.commit()
        row_id = row.id

    context.job_queue.run_once(
        _fire_scheduled_message, when=seconds, data={"scheduled_id": row_id}, name=f"scheduled:{row_id}",
    )
    await update.message.reply_text(f"📅 Scheduled — I'll post it in {context.args[0]}.")


# ==================== STARTUP: RE-ARM PENDING JOBS ====================

async def rearm_jobs(app: Application) -> None:
    """Re-create JobQueue entries for reminders/scheduled messages that
    were still pending when the bot last stopped. Anything whose time
    already passed while the bot was down fires immediately (marked
    delayed) rather than being silently dropped."""
    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(select(Reminder).where(Reminder.fired.is_(False)))
        for reminder in result.scalars().all():
            delay = max(0, (reminder.remind_at - now).total_seconds())
            app.job_queue.run_once(
                _fire_reminder, when=delay, data={"reminder_id": reminder.id}, name=f"reminder:{reminder.id}",
            )

        result = await session.execute(select(ScheduledMessage).where(ScheduledMessage.sent.is_(False)))
        for row in result.scalars().all():
            delay = max(0, (row.send_at - now).total_seconds())
            app.job_queue.run_once(
                _fire_scheduled_message, when=delay, data={"scheduled_id": row.id}, name=f"scheduled:{row.id}",
            )


def register(app):
    if not settings.enable_scheduling:
        return
    app.add_handler(CommandHandler("poll", poll_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CommandHandler("announce", announce_cmd))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
