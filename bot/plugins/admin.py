"""Moderation commands — ban / mute / kick / warn (+ timed, silent and delete variants), unban, unmute,
warn bookkeeping, purge, delete, zombies, unbanall.

v2.12: rewritten on ``bot/utils/modtools.py``. Every command here
  * accepts a reply, a numeric id, a tapped mention or an ``@username`` the bot has seen;
  * checks that the *caller* holds the specific Telegram admin right (not merely "is an admin") and
    that the bot does too, before touching anything;
  * refuses to act on admins, the owner, the bot, anonymous-admin / channel identities;
  * works for anonymous admins and, from a private chat, for the group chosen with ``/connect``;
  * records bans/temp-actions so ``/bans``, ``/mutes`` and ``/unbanall`` have data to show.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Ban, Warn, async_session
from bot.core.errors import safe_handler
from bot.utils import modtools as mt
from bot.utils.helpers import escape_html, mention_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)


# ── shared flow ──────────────────────────────────────────────────────────────
_actor = mt.actor_of


async def _finish(update: Update, ctx: mt.ChatCtx, text: str, *, silent: bool) -> None:
    """Announce in the chat — or, for the silent variants, remove the command message instead.
    A private chat (``/connect``) is always answered: there is nothing to hide there."""
    msg = update.effective_message
    if silent and not ctx.remote:
        try:
            await msg.delete()
        except TelegramError:
            pass
        return
    await msg.reply_text(text, parse_mode="HTML")


async def _delete_replied(update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: mt.ChatCtx) -> None:
    reply = None if ctx.remote else mt.real_reply(update.effective_message)
    if reply is not None:
        try:
            await context.bot.delete_message(ctx.chat_id, reply.message_id)
        except TelegramError:
            pass


async def _delete_problem(update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: mt.ChatCtx) -> Optional[str]:
    """Why the *d* variants (which also delete the replied message) cannot run, or ``None``."""
    return await mt.check_caller(update, context, ctx.chat_id, "delete") or await mt.check_bot(context, ctx.chat_id, "delete")


def _usage(cmd: str, kind: str, require_time: bool) -> str:
    time_part = "" if kind in ("kick", "warn") else ("<time> " if require_time else "[time] ")
    return f"Usage: /{cmd} {time_part}[reason]\n{mt.NO_TARGET_HELP}"


_TITLES = {"ban": ("🔨", "Banned"), "mute": ("🔇", "Muted"), "kick": ("👢", "Kicked")}


async def _restrict(update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: mt.ChatCtx, kind: str, *, cmd: str,
                    require_time: bool = False, silent: bool = False, delete_reply: bool = False) -> None:
    msg = update.effective_message
    actor_id, actor = _actor(update)
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(_usage(cmd, kind, require_time))
        return

    seconds: Optional[int] = None
    reason_words = target.rest
    if kind != "kick":
        seconds = mt.parse_duration(target.rest[0]) if target.rest else None
        if seconds is not None:
            try:
                mt.validate_duration(seconds)
            except mt.DurationError as exc:
                await msg.reply_text(f"❌ {exc}")
                return
            reason_words = target.rest[1:]
        elif require_time:
            await msg.reply_text(f"Usage: /{cmd} <time> [reason] — time like 30m, 2h, 1d, 1w.")
            return
    reason = mt.clean_reason(reason_words)

    block = await mt.target_block_reason(context, ctx.chat_id, target, actor_id)
    if block:
        await msg.reply_text(f"🚫 {block}")
        return
    if delete_reply and not ctx.remote:
        problem = await _delete_problem(update, context, ctx)
        if problem:
            await msg.reply_text(f"🚫 {problem}")
            return

    action = {"ban": "tban" if seconds else "ban", "mute": "tmute" if seconds else "mute", "kick": "kick"}[kind]
    err = await mt.punish(context, ctx.chat_id, target.user_id, action, seconds=seconds, reason=reason, by=actor_id)
    if err:
        await msg.reply_text(f"❌ {err}")
        return

    icon, label = _TITLES[kind]
    span = f" for {mt.format_span(seconds)}" if seconds else (" permanently" if kind == "ban" else "")
    text = f"{icon} <b>{label}</b> {target.mention}{span}.\n<b>Reason:</b> {escape_html(reason)}"
    if delete_reply:
        await _delete_replied(update, context, ctx)
    await _finish(update, ctx, text, silent=silent)
    await mt.log_event(context, ctx.chat_id, label, actor=actor, target=(target.user_id, target.name),
                       reason=reason, extra=f"<b>Duration:</b> {mt.format_span(seconds)}" if seconds else None)


async def _lift(update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: mt.ChatCtx, kind: str, *, cmd: str) -> None:
    msg = update.effective_message
    actor_id, actor = _actor(update)
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(f"Usage: /{cmd}\n{mt.NO_TARGET_HELP}")
        return
    block = await mt.target_block_reason(context, ctx.chat_id, target, actor_id, allow_admin=(kind == "ban"), allow_self=True)
    if block:
        await msg.reply_text(f"🚫 {block}")
        return
    err = await mt.lift(context, ctx.chat_id, target.user_id, kind)
    if err:
        await msg.reply_text(f"❌ {err}")
        return
    if kind == "ban":
        await msg.reply_text(f"✅ {target.mention} was unbanned and can rejoin.", parse_mode="HTML")
        await mt.log_event(context, ctx.chat_id, "Unbanned", actor=actor, target=(target.user_id, target.name))
    else:
        await msg.reply_text(f"🔊 {target.mention} can speak again.", parse_mode="HTML")
        await mt.log_event(context, ctx.chat_id, "Unmuted", actor=actor, target=(target.user_id, target.name))


async def _warn(update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: mt.ChatCtx, *, cmd: str,
                silent: bool = False, delete_reply: bool = False) -> None:
    msg = update.effective_message
    actor_id, actor = _actor(update)
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(_usage(cmd, "warn", False))
        return
    reason = mt.clean_reason(target.rest)
    block = await mt.target_block_reason(context, ctx.chat_id, target, actor_id)
    if block:
        await msg.reply_text(f"🚫 {block}")
        return
    if delete_reply and not ctx.remote:
        problem = await _delete_problem(update, context, ctx)
        if problem:
            await msg.reply_text(f"🚫 {problem}")
            return
    outcome = await mt.add_warn(context, ctx.chat_id, target.user_id, by=actor_id, reason=reason, user_name=target.name)
    if delete_reply:
        await _delete_replied(update, context, ctx)
    await _finish(update, ctx, mt.describe_warn_outcome(target.mention, outcome, reason), silent=silent)
    await mt.log_event(context, ctx.chat_id, "Warned", actor=actor, target=(target.user_id, target.name), reason=reason,
                       extra=f"<b>Warns:</b> {outcome.count}/{outcome.limit}" + (f" → {outcome.action}" if outcome.punished else ""))


# ── ban / mute / kick families ───────────────────────────────────────────────
@mt.mod_command("restrict", bot_right="restrict")
async def ban_cmd(update, context, ctx):
    """Ban a user, optionally for a while: /ban [time] [reason]"""
    await _restrict(update, context, ctx, "ban", cmd="ban")


@mt.mod_command("restrict", bot_right="restrict")
async def tban_cmd(update, context, ctx):
    """Temporarily ban a user: /tban <time> [reason]"""
    await _restrict(update, context, ctx, "ban", cmd="tban", require_time=True)


@mt.mod_command("restrict", bot_right="restrict")
async def sban_cmd(update, context, ctx):
    """Ban silently — the command message is removed, nothing is announced"""
    await _restrict(update, context, ctx, "ban", cmd="sban", silent=True)


@mt.mod_command("restrict", bot_right="restrict")
async def dban_cmd(update, context, ctx):
    """Ban the replied-to user and delete the message they sent"""
    await _restrict(update, context, ctx, "ban", cmd="dban", delete_reply=True)


@mt.mod_command("restrict", bot_right="restrict")
async def dtban_cmd(update, context, ctx):
    """Temporarily ban the replied-to user and delete their message: /dtban <time> [reason]"""
    await _restrict(update, context, ctx, "ban", cmd="dtban", require_time=True, delete_reply=True)


@mt.mod_command("restrict", bot_right="restrict")
async def mute_cmd(update, context, ctx):
    """Mute a user, optionally for a while: /mute [time] [reason]"""
    await _restrict(update, context, ctx, "mute", cmd="mute")


@mt.mod_command("restrict", bot_right="restrict")
async def tmute_cmd(update, context, ctx):
    """Temporarily mute a user: /tmute <time> [reason]"""
    await _restrict(update, context, ctx, "mute", cmd="tmute", require_time=True)


@mt.mod_command("restrict", bot_right="restrict")
async def smute_cmd(update, context, ctx):
    """Mute silently — the command message is removed, nothing is announced"""
    await _restrict(update, context, ctx, "mute", cmd="smute", silent=True)


@mt.mod_command("restrict", bot_right="restrict")
async def dmute_cmd(update, context, ctx):
    """Mute the replied-to user and delete the message they sent"""
    await _restrict(update, context, ctx, "mute", cmd="dmute", delete_reply=True)


@mt.mod_command("restrict", bot_right="restrict")
async def dtmute_cmd(update, context, ctx):
    """Temporarily mute the replied-to user and delete their message: /dtmute <time> [reason]"""
    await _restrict(update, context, ctx, "mute", cmd="dtmute", require_time=True, delete_reply=True)


@mt.mod_command("restrict", bot_right="restrict")
async def kick_cmd(update, context, ctx):
    """Kick a user (they may rejoin): /kick [reason]"""
    await _restrict(update, context, ctx, "kick", cmd="kick")


@mt.mod_command("restrict", bot_right="restrict")
async def skick_cmd(update, context, ctx):
    """Kick silently — the command message is removed, nothing is announced"""
    await _restrict(update, context, ctx, "kick", cmd="skick", silent=True)


@mt.mod_command("restrict", bot_right="restrict")
async def dkick_cmd(update, context, ctx):
    """Kick the replied-to user and delete the message they sent"""
    await _restrict(update, context, ctx, "kick", cmd="dkick", delete_reply=True)


@mt.mod_command("restrict", bot_right="restrict")
async def unban_cmd(update, context, ctx):
    """Unban a user: /unban <reply|id|@user>"""
    await _lift(update, context, ctx, "ban", cmd="unban")


@mt.mod_command("restrict", bot_right="restrict")
async def unmute_cmd(update, context, ctx):
    """Let a muted user speak again: /unmute <reply|id|@user>"""
    await _lift(update, context, ctx, "mute", cmd="unmute")


@safe_handler
async def kickme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kick yourself out of the group: /kickme"""
    ctx = await mt.resolve_ctx(update, context, remote_ok=False)
    if ctx is None:
        return
    msg, user = update.effective_message, update.effective_user
    if mt.is_anonymous_admin(update, ctx.chat_id):
        await msg.reply_text("Anonymous admins can't be kicked.")
        return
    member = await mt.get_member(context.bot, ctx.chat_id, user.id)
    if member is not None and member.status in ("administrator", "creator"):
        await msg.reply_text("I won't kick an admin — demote yourself first if you really want out.")
        return
    problem = await mt.check_bot(context, ctx.chat_id, "restrict")
    if problem:
        await msg.reply_text(f"🚫 {problem}")
        return
    err = await mt.punish(context, ctx.chat_id, user.id, "kick", reason="/kickme", by=user.id)
    await msg.reply_text(f"❌ {err}" if err else f"👋 Bye, {mention_html(user.id, mt.display_name(user))}!", parse_mode="HTML")


# ── warns ────────────────────────────────────────────────────────────────────
@mt.mod_command("restrict")
async def warn_cmd(update, context, ctx):
    """Warn a user; at the chat's limit the warn action is applied: /warn [reason]"""
    await _warn(update, context, ctx, cmd="warn")


@mt.mod_command("restrict")
async def swarn_cmd(update, context, ctx):
    """Warn silently — the command message is removed"""
    await _warn(update, context, ctx, cmd="swarn", silent=True)


@mt.mod_command("restrict")
async def dwarn_cmd(update, context, ctx):
    """Warn the replied-to user and delete the message they sent"""
    await _warn(update, context, ctx, cmd="dwarn", delete_reply=True)


@safe_handler
async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a user's warnings (yours by default): /warns [reply|id]"""
    ctx = await mt.resolve_ctx(update, context)
    if ctx is None:
        return
    msg, me = update.effective_message, update.effective_user
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        if ctx.remote:
            await msg.reply_text("Usage: /warns <user id>")
            return
        target = mt.Target(me.id, mt.display_name(me), me)
    if ctx.remote:
        problem = await mt.check_caller(update, context, ctx.chat_id, None)
        if problem:
            await msg.reply_text(f"🚫 {problem}")
            return
    limit, _action = await mt.warn_settings(ctx.chat_id)
    async with async_session() as session:
        rows = (await session.execute(
            select(Warn).where(Warn.chat_id == ctx.chat_id, Warn.user_id == target.user_id).order_by(Warn.id.desc()).limit(10)
        )).scalars().all()
    count = await mt.count_warns(ctx.chat_id, target.user_id)
    lines = [f"⚠️ Warns for {target.mention}: <b>{count}/{limit}</b>"]
    lines += [f"• {escape_html(w.reason or 'no reason')}" for w in rows]
    if count > len(rows):
        lines.append(f"<i>…and {count - len(rows)} older</i>")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


@mt.mod_command("restrict")
async def rmwarn_cmd(update, context, ctx):
    """Remove a user's most recent warning: /rmwarn <reply|id>"""
    msg = update.effective_message
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(f"Usage: /rmwarn\n{mt.NO_TARGET_HELP}")
        return
    async with async_session() as session:
        row = (await session.execute(
            select(Warn).where(Warn.chat_id == ctx.chat_id, Warn.user_id == target.user_id).order_by(Warn.id.desc()).limit(1)
        )).scalar_one_or_none()
        if row is None:
            await msg.reply_text(f"{target.mention} has no warnings.", parse_mode="HTML")
            return
        await session.delete(row)
        await session.commit()
    limit, _action = await mt.warn_settings(ctx.chat_id)
    count = await mt.count_warns(ctx.chat_id, target.user_id)
    await msg.reply_text(f"✅ Removed the latest warning from {target.mention} — now <b>{count}/{limit}</b>.", parse_mode="HTML")
    await mt.log_event(context, ctx.chat_id, "Warn removed", actor=_actor(update)[1], target=(target.user_id, target.name))


@mt.mod_command("restrict")
async def resetwarns_cmd(update, context, ctx):
    """Clear all of a user's warnings: /resetwarns <reply|id>"""
    msg = update.effective_message
    target = await mt.resolve_target(update, context, allow_reply=not ctx.remote)
    if target is None:
        await msg.reply_text(f"Usage: /resetwarns\n{mt.NO_TARGET_HELP}")
        return
    removed = await mt.reset_warns(ctx.chat_id, target.user_id)
    await msg.reply_text(f"✅ Cleared {removed} warning(s) for {target.mention}.", parse_mode="HTML")
    await mt.log_event(context, ctx.chat_id, "Warns reset", actor=_actor(update)[1], target=(target.user_id, target.name))


# ── message cleanup ──────────────────────────────────────────────────────────
async def _purge(update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: mt.ChatCtx, *, silent: bool) -> None:
    msg = update.effective_message
    cap = settings.purge_max_messages
    reply = mt.real_reply(msg)
    note = ""
    if reply is not None:
        start_id = reply.message_id
    elif context.args and context.args[0].isascii() and context.args[0].isdigit() and int(context.args[0]) > 0:
        requested = int(context.args[0])
        start_id = msg.message_id - min(requested, cap)
        if requested > cap:
            note = f" (limited to the last {cap})"
    else:
        await msg.reply_text(f"Reply to the oldest message to delete, or use /purge <count> (max {cap}).")
        return
    if msg.message_id - start_id > cap:
        start_id, note = msg.message_id - cap, f" (limited to the last {cap})"
    ids = list(range(max(start_id, 1), msg.message_id + 1))
    deleted = await mt.delete_messages(context.bot, ctx.chat_id, ids)
    if silent:
        return
    notice = await context.bot.send_message(ctx.chat_id, f"🧹 Purged up to {max(deleted - 1, 0)} messages{note}.")
    mt.schedule_delete(context, ctx.chat_id, notice.message_id, 5)


@mt.mod_command("delete", remote_ok=False, bot_right="delete")
async def purge_cmd(update, context, ctx):
    """Delete everything from the replied-to message down, or the last N: /purge [count]"""
    await _purge(update, context, ctx, silent=False)


@mt.mod_command("delete", remote_ok=False, bot_right="delete")
async def spurge_cmd(update, context, ctx):
    """Purge without the confirmation message"""
    await _purge(update, context, ctx, silent=True)


@mt.mod_command("delete", remote_ok=False, bot_right="delete")
async def del_cmd(update, context, ctx):
    """Delete the replied-to message (and your command): reply + /del"""
    msg = update.effective_message
    reply = mt.real_reply(msg)
    if reply is None:
        await msg.reply_text("Reply to the message you want deleted.")
        return
    try:
        await context.bot.delete_message(ctx.chat_id, reply.message_id)
    except TelegramError as exc:
        await msg.reply_text(f"❌ {mt.friendly_telegram_error(exc)}")
        return
    try:
        await msg.delete()
    except TelegramError:
        pass


# ── zombies (needs the MTProto assistant account that live music uses) ───────
# Enumerating all members is not possible through the plain Bot API, so this reuses the assistant
# connection from bot/core/music_client.py and says so plainly when none is configured.
async def _get_deleted_accounts(chat_id: int) -> list[int]:
    from bot.core.music_client import get_assistant_pool

    pool = get_assistant_pool()
    if not pool.pairs:
        return []
    pyrogram_client = pool.pairs[0][0].pyrogram
    deleted = []
    async for member in pyrogram_client.get_chat_members(chat_id):
        if getattr(member.user, "is_deleted", False):
            deleted.append(member.user.id)
    return deleted


_ZOMBIE_NEEDS_ASSISTANT = (
    "❌ This needs MUSIC_API_ID/MUSIC_API_HASH configured — enumerating every group member isn't something "
    "the plain Bot API can do; it needs the same MTProto connection live music uses."
)


@mt.mod_command("restrict")
async def zombies_cmd(update, context, ctx):
    """Scan for deleted accounts in this chat"""
    msg = update.effective_message
    if not settings.live_music_configured:
        await msg.reply_text(_ZOMBIE_NEEDS_ASSISTANT)
        return
    status = await msg.reply_text("🔍 Scanning for deleted accounts...")
    try:
        deleted = await _get_deleted_accounts(ctx.chat_id)
    except Exception as exc:
        logger.warning("zombie_scan_failed", chat_id=ctx.chat_id, error=str(exc))
        await status.edit_text("❌ Scan failed — is the assistant account a member of this chat?")
        return
    await status.edit_text(f"🧟 Found {len(deleted)} deleted account(s). /rmzombies to remove them." if deleted
                           else "✅ No deleted accounts found.")


@mt.mod_command("restrict", bot_right="restrict")
async def rmzombies_cmd(update, context, ctx):
    """Remove deleted accounts from this chat"""
    msg = update.effective_message
    if not settings.live_music_configured:
        await msg.reply_text(_ZOMBIE_NEEDS_ASSISTANT)
        return
    status = await msg.reply_text("🔍 Scanning and removing deleted accounts...")
    try:
        deleted = await _get_deleted_accounts(ctx.chat_id)
    except Exception as exc:
        logger.warning("zombie_scan_failed", chat_id=ctx.chat_id, error=str(exc))
        await status.edit_text("❌ Scan failed — is the assistant account a member of this chat?")
        return
    removed = 0
    for user_id in deleted:
        if await mt.punish(context, ctx.chat_id, user_id, "kick", reason="deleted account", by=update.effective_user.id) is None:
            removed += 1
    await status.edit_text(f"🧟 Removed {removed}/{len(deleted)} deleted account(s).")


@mt.mod_command("restrict", bot_right="restrict")
async def unbanall_cmd(update, context, ctx):
    """Lift every ban this bot recorded in this chat"""
    msg = update.effective_message
    async with async_session() as session:
        user_ids = (await session.execute(select(Ban.user_id).where(Ban.chat_id == ctx.chat_id).distinct())).scalars().all()
    if not user_ids:
        await msg.reply_text("No bans recorded for this chat.")
        return
    status = await msg.reply_text(f"🔓 Unbanning {len(user_ids)} user(s)...")
    unbanned = 0
    for user_id in user_ids:
        if await mt.lift(context, ctx.chat_id, user_id, "ban") is None:
            unbanned += 1
    await status.edit_text(f"🔓 Unbanned {unbanned}/{len(user_ids)} user(s).")
    await mt.log_event(context, ctx.chat_id, "Unban all", actor=_actor(update)[1], extra=f"<b>Users:</b> {unbanned}")


def register(app):
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler(["tban", "tempban"], tban_cmd))
    app.add_handler(CommandHandler("dtban", dtban_cmd))
    app.add_handler(CommandHandler("sban", sban_cmd))
    app.add_handler(CommandHandler("dban", dban_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler(["tmute", "tempmute"], tmute_cmd))
    app.add_handler(CommandHandler("dtmute", dtmute_cmd))
    app.add_handler(CommandHandler("smute", smute_cmd))
    app.add_handler(CommandHandler("dmute", dmute_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("skick", skick_cmd))
    app.add_handler(CommandHandler("dkick", dkick_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("kickme", kickme_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("swarn", swarn_cmd))
    app.add_handler(CommandHandler("dwarn", dwarn_cmd))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler(["rmwarn", "unwarn"], rmwarn_cmd))
    app.add_handler(CommandHandler(["resetwarns", "resetwarn"], resetwarns_cmd))
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CommandHandler("spurge", spurge_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("zombies", zombies_cmd))
    app.add_handler(CommandHandler("rmzombies", rmzombies_cmd))
    app.add_handler(CommandHandler("unbanall", unbanall_cmd))
