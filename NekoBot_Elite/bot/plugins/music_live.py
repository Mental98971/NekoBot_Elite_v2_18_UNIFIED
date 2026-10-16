"""
Live voice-chat music — /play, /skip, /pause, /resume, /stop, /queue,
/nowplaying, /volume, /shuffle, /repeat, /effects, /loop, /seek,
/seekback, /channelplay, /toptracks, /resetqueue.

Built on the ported harmony-music-bot engine (bot/services/audio_engine.py
+ bot/services/queue_manager.py), the MTProto client/pool in
bot/core/music_client.py, multi-platform resolution in
bot/services/platforms.py (Spotify/Apple Music/Resso/SoundCloud), and
/seek + /toptracks ported from AnonXMusic (see engine.seek()'s
docstring for how AnonXMusic's core/call.py confirmed the underlying
ffmpeg-restart mechanism). Requires MUSIC_API_ID / MUSIC_API_HASH — if
they're not set, every command here replies with a short explanation
instead of failing. The original nova_guard_bot /music and /yt commands
(download a track and send it as a file) are untouched and keep working
with or without live music configured.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

import yt_dlp
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.decorators import admin_only, group_only, owner_only
from bot.models.queue import RepeatMode
from bot.models.track import Track, TrackSource
from bot.services.queue_manager import queue_manager
from bot.utils.helpers import escape_html
from bot.core.errors import safe_handler

_pool = None  # AssistantPool, set by bot.core.bot during startup once the music client(s) are ready


def set_pool(pool) -> None:
    global _pool
    _pool = pool


def _configured() -> bool:
    return settings.live_music_configured and _pool is not None and bool(_pool.pairs)


def _engine_for(chat_id: int):
    """The AudioEngine assigned to this chat — see AssistantPool.get_engine
    for the least-loaded, sticky assignment logic (matters once
    MUSIC_EXTRA_BOT_TOKENS has more than one assistant configured)."""
    return _pool.get_engine(chat_id) if _pool else None


def is_configured() -> bool:
    """Public wrapper for other plugins (playlists.py) that need to check
    whether live music is available before trying to start playback."""
    return _configured()


async def start_playback_if_idle(chat_id: int) -> Optional[Track]:
    """If nothing is currently playing in this chat, pop the next queued
    track and start it. Used by plugins/playlists.py after loading a
    playlist. Returns the track that started playing, if any."""
    if not _configured():
        return None
    engine = _engine_for(chat_id)
    if engine.is_active(chat_id):
        return None
    next_track = await queue_manager.next_track(chat_id)
    if next_track:
        await engine.play(chat_id, next_track, pipeline=_pipeline_for(chat_id))
    return next_track


async def _not_configured_reply(update: Update) -> None:
    await update.message.reply_text(
        "🎧 Live voice-chat music isn't configured on this bot.\n"
        "An admin needs to set MUSIC_API_ID and MUSIC_API_HASH (from "
        "my.telegram.org) — see the .env.example. /music and /yt (download "
        "& send as a file) work either way."
    )


def _pipeline_for(chat_id: int):
    """AudioEngine.play() starts a fresh, effect-less pipeline unless one is
    passed explicitly — reuse the chat's existing pipeline (volume, active
    effects) across /skip and auto-advance instead of silently resetting it
    on every track change."""
    engine = _pool.get_engine(chat_id) if _pool else None
    return engine.get_pipeline(chat_id) if engine else None


# ==================== PLAYBACK POSITION TRACKING ====================
# Computed from timestamps rather than AnonXMusic's seeker.py approach
# (a background task ticking every second for every active chat) —
# same information, no polling loop needed.
_play_started_at: dict = {}   # chat_id -> unix time the current run-segment began
_paused_elapsed: dict = {}    # chat_id -> accumulated seconds before the current segment


def _mark_started(chat_id: int, from_seconds: float = 0.0) -> None:
    _play_started_at[chat_id] = time.time()
    _paused_elapsed[chat_id] = from_seconds


def _mark_paused(chat_id: int) -> None:
    if chat_id in _play_started_at:
        _paused_elapsed[chat_id] = get_elapsed(chat_id)
        _play_started_at.pop(chat_id, None)


def _mark_resumed(chat_id: int) -> None:
    _play_started_at[chat_id] = time.time()


def _clear_position(chat_id: int) -> None:
    _play_started_at.pop(chat_id, None)
    _paused_elapsed.pop(chat_id, None)


def get_elapsed(chat_id: int) -> float:
    base = _paused_elapsed.get(chat_id, 0.0)
    if chat_id in _play_started_at:
        base += time.time() - _play_started_at[chat_id]
    return base


async def _record_play(chat_id: int, track: Track) -> None:
    """Logs a play for /toptracks (from AnonXMusic). Best-effort — a
    failure here shouldn't ever interrupt actual playback."""
    try:
        from bot.core.database import TrackPlay, async_session

        async with async_session() as session:
            session.add(TrackPlay(
                chat_id=chat_id, user_id=track.added_by, title=track.title,
                artist=track.artist, source_url=track.source_url,
            ))
            await session.commit()
    except Exception:
        pass


async def _is_music_controller(update: Update) -> bool:
    """True if this user may use control commands (skip/stop/pause/...)
    in this chat: a chat admin, an explicit /auth user (see auth_cmd),
    or a bot sudoer. Only actually consulted when the chat has opted
    into Chat.restrict_music_controls — see the decorator below."""
    from telegram.constants import ChatMemberStatus

    from bot.core.database import Chat, async_session

    user, chat = update.effective_user, update.effective_chat
    if settings.is_admin_id(user.id):
        return True
    try:
        member = await update.get_bot().get_chat_member(chat.id, user.id)
        if member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return True
    except Exception:
        pass

    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        if not row or not row.restrict_music_controls:
            return True  # restriction not enabled here — open to everyone

    from bot.services.economy_service import get_or_create_member

    async with async_session() as session:
        cm = await get_or_create_member(session, chat.id, user.id)
        await session.commit()
        return bool(cm.is_auth_user)


def music_control_only(func):
    """Decorator for control commands — see _is_music_controller."""
    import functools

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await _is_music_controller(update):
            await update.message.reply_text(
                "🚫 Music controls are restricted here — ask an admin, or get "
                "authorized with /auth (admins: reply to someone with /auth)."
            )
            return
        return await func(update, context)

    return wrapper


async def _resolve_track(query: str, requester_id: int, requester_name: str) -> Optional[Track]:
    """Resolve a search query or URL to a playable Track via yt-dlp,
    without downloading a file — pytgcalls streams directly from the URL.

    Spotify/Apple Music/Resso links are first resolved to a "title
    artist" search string (see bot/services/platforms.py) — their own
    audio is never used, only their public metadata to find the
    equivalent on YouTube. Everything else (YouTube links, SoundCloud
    links, plain search text) goes straight to yt-dlp as before, since
    it already handles those natively.
    """
    source_platform = None
    if query.startswith("http"):
        from bot.services.platforms import detect_platform, resolve_to_search_query

        source_platform = detect_platform(query)
        if source_platform:
            resolved_query = await resolve_to_search_query(query)
            if not resolved_query:
                return None  # couldn't resolve metadata (bad link, or platform creds not configured)
            query = resolved_query

    url = query if (query.startswith("http") and not source_platform) else f"ytsearch1:{query}"
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "default_search": "ytsearch",
        # Prefer a concrete audio URL; extractors refresh as needed by ffmpeg/yt-dlp
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }

    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info

    info = await loop.run_in_executor(None, _extract)
    if not info:
        return None

    return Track(
        id=str(uuid.uuid4()),
        source=TrackSource.YOUTUBE,
        title=info.get("title", "Unknown"),
        artist=info.get("uploader", "Unknown Artist"),
        duration=int(info.get("duration") or 0),
        thumbnail=info.get("thumbnail"),
        audio_url=info.get("url"),
        source_url=info.get("webpage_url"),
        added_by=requester_id,
        requester_name=requester_name,
        is_live=bool(info.get("is_live")),
    )


@group_only
@safe_handler
async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _configured():
        await _not_configured_reply(update)
        return

    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /play <song name or URL>")
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    msg = await update.message.reply_text("🔎 Searching...")

    try:
        track = await _resolve_track(query, user.id, user.first_name or "Someone")
    except Exception as e:
        from bot.core.errors import user_facing_error
        await msg.edit_text(user_facing_error(e, fallback="Couldn't find a playable track for that. Try another query or a direct YouTube link."))
        return

    if not track or not track.audio_url:
        await msg.edit_text("❌ No playable result found.")
        return

    queue = await queue_manager.get_queue(chat_id)
    engine = _engine_for(chat_id)
    was_empty = queue.is_empty or not engine.is_active(chat_id)

    await queue_manager.add_track(chat_id, track)

    if was_empty:
        from bot.core.database import Chat, async_session

        async with async_session() as session:
            chat_row = await session.get(Chat, chat_id)
            want_video = bool(chat_row and chat_row.video_enabled)

        next_track = await queue_manager.next_track(chat_id)
        if next_track:
            ok = await engine.play(chat_id, next_track, pipeline=_pipeline_for(chat_id), video=want_video)
            if ok:
                _mark_started(chat_id)
                await _record_play(chat_id, next_track)
                await msg.edit_text(
                    f"{'🎥' if want_video else '🎶'} Now playing: <b>{escape_html(next_track.display_name)}</b> "
                    f"[{next_track.duration_formatted}]",
                    parse_mode="HTML",
                )
            else:
                await msg.edit_text(
                    "❌ Couldn't start playback in the voice chat.\n"
                    "Check that:\n"
                    "• A voice chat is <b>already open</b> in this group\n"
                    "• The bot is a member (and usually an <b>admin</b>)\n"
                    "• MUSIC_API_ID / MUSIC_API_HASH match this bot account\n"
                    "• ffmpeg is installed on the host\n"
                    "Details are in the bot logs (playback_error).",
                    parse_mode="HTML",
                )
    else:
        position = len(queue.entries) + 1
        await msg.edit_text(
            f"➕ Queued at #{position}: <b>{escape_html(track.display_name)}</b> "
            f"[{track.duration_formatted}]",
            parse_mode="HTML",
        )


@group_only
@music_control_only
@safe_handler
async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _configured():
        await _not_configured_reply(update)
        return
    chat_id = update.effective_chat.id
    next_track = await queue_manager.next_track(chat_id)
    if next_track:
        await _engine_for(chat_id).play(chat_id, next_track, pipeline=_pipeline_for(chat_id))
        _mark_started(chat_id)
        await _record_play(chat_id, next_track)
        await update.message.reply_text(f"⏭ Skipped. Now playing: <b>{escape_html(next_track.display_name)}</b>", parse_mode="HTML")
    else:
        await _engine_for(chat_id).stop(chat_id)
        _clear_position(chat_id)
        await update.message.reply_text("⏭ Skipped. Queue is empty — left the voice chat.")


@group_only
@music_control_only
@safe_handler
async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _configured():
        await _not_configured_reply(update)
        return
    if await _engine_for(update.effective_chat.id).pause(update.effective_chat.id):
        _mark_paused(update.effective_chat.id)
        await update.message.reply_text("⏸ Paused.")
    else:
        await update.message.reply_text("❌ Nothing is playing.")


@group_only
@music_control_only
@safe_handler
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _configured():
        await _not_configured_reply(update)
        return
    if await _engine_for(update.effective_chat.id).resume(update.effective_chat.id):
        _mark_resumed(update.effective_chat.id)
        await update.message.reply_text("▶️ Resumed.")
    else:
        await update.message.reply_text("❌ Nothing is paused.")


@group_only
@music_control_only
@safe_handler
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _configured():
        await _not_configured_reply(update)
        return
    chat_id = update.effective_chat.id
    await _engine_for(chat_id).stop(chat_id)
    _clear_position(chat_id)
    from bot.core.music_client import get_assistant_pool
    get_assistant_pool().release(chat_id)
    await update.message.reply_text("⏹ Stopped and left the voice chat. The queue is still saved — /play to resume.")


@group_only
@safe_handler
async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    queue = await queue_manager.get_queue(chat_id)
    if queue.is_empty:
        await update.message.reply_text("📭 The queue is empty. /play something!")
        return

    lines = ["🎵 <b>Queue</b>\n"]
    for i, entry in enumerate(queue.entries[:15]):
        marker = "▶️" if i == queue.current_index else f"{i + 1}."
        lines.append(f"{marker} {escape_html(entry.track.display_name)} [{entry.track.duration_formatted}]")
    if len(queue.entries) > 15:
        lines.append(f"\n…and {len(queue.entries) - 15} more.")
    lines.append(f"\nRepeat: <code>{queue.repeat_mode.value}</code> · Shuffle: <code>{queue.shuffle_enabled}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@group_only
@safe_handler
async def nowplaying_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    queue = await queue_manager.get_queue(chat_id)
    track = queue.current_track
    if not track:
        await update.message.reply_text("📭 Nothing is playing right now.")
        return
    from bot.utils.helpers import format_duration, generate_progress_bar, reply_with_cleanup

    elapsed = int(get_elapsed(chat_id))
    duration = track.duration or 0
    progress_line = ""
    if duration:
        bar = generate_progress_bar(elapsed, duration, length=14)
        progress_line = f"\n{bar} {format_duration(elapsed)} / {format_duration(duration)}"

    live_tag = "🔴 LIVE\n" if getattr(track, "is_live", False) else ""

    await reply_with_cleanup(
        update, context,
        f"{live_tag}🎶 <b>{escape_html(track.display_name)}</b>{progress_line}\n"
        f"Requested by: {escape_html(track.requester_name)}\n"
        f"Source: {track.source.value}",
        parse_mode="HTML",
    )


@group_only
@music_control_only
@safe_handler
async def volume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _configured():
        await _not_configured_reply(update)
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Usage: /volume <0-{settings.max_group_volume}>")
        return
    vol = max(0, min(settings.max_group_volume, int(context.args[0])))
    if await _engine_for(update.effective_chat.id).change_volume(update.effective_chat.id, vol):
        await update.message.reply_text(f"🔊 Volume set to {vol}%.")
    else:
        await update.message.reply_text("❌ Nothing is playing.")


@group_only
@music_control_only
@safe_handler
async def shuffle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await queue_manager.shuffle(update.effective_chat.id)
    await update.message.reply_text("🔀 Queue shuffled.")


@group_only
@music_control_only
@safe_handler
async def repeat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mapping = {"off": RepeatMode.NONE, "none": RepeatMode.NONE, "one": RepeatMode.ONE, "all": RepeatMode.ALL}
    arg = (context.args[0].lower() if context.args else "")
    if arg not in mapping:
        await update.message.reply_text("Usage: /repeat off|one|all")
        return
    await queue_manager.set_repeat(update.effective_chat.id, mapping[arg])
    await update.message.reply_text(f"🔁 Repeat mode: {arg}")


@group_only
@music_control_only
@safe_handler
async def effects_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.services.audio_engine import AudioEffect, EffectConfig

    if not _configured():
        await _not_configured_reply(update)
        return

    if not context.args or context.args[0].lower() == "list":
        names = ", ".join(e.value for e in AudioEffect)
        await update.message.reply_text(f"🎛 Available effects:\n<code>{names}</code>\n\nUsage: /effects <name> on|off", parse_mode="HTML")
        return

    name = context.args[0].lower()
    toggle = (context.args[1].lower() if len(context.args) > 1 else "on")
    try:
        effect = AudioEffect(name)
    except ValueError:
        await update.message.reply_text(f"❌ Unknown effect: {name}. Try /effects list")
        return

    await _engine_for(update.effective_chat.id).set_effect(update.effective_chat.id, effect, EffectConfig(enabled=(toggle == "on")))
    await update.message.reply_text(f"🎛 {effect.value} is now {'ON' if toggle == 'on' else 'OFF'}. Effects apply from the next /play or /skip.")


async def on_track_end(chat_id: int) -> None:
    """Called by MusicClient when a stream finishes naturally — advances
    the queue and starts the next track, or leaves the call if empty."""
    if not _pool or not _pool.pairs:
        return
    next_track = await queue_manager.next_track(chat_id)
    if next_track:
        await _engine_for(chat_id).play(chat_id, next_track, pipeline=_pipeline_for(chat_id))
        _mark_started(chat_id)
        await _record_play(chat_id, next_track)
    else:
        await _engine_for(chat_id).stop(chat_id)
        _pool.release(chat_id)
        _clear_position(chat_id)


# ==================== LOOP (from YukkiMusicBot) ====================

@group_only
@music_control_only
@safe_handler
async def loop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/loop <1-10> adds that many repeats of the current track;
    /loop enable = 10, /loop disable = 0 (matches YukkiMusicBot's
    semantics, where repeated /loop N calls stack up to a max of 10)."""
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")

    if arg == "enable":
        new_val = await queue_manager.set_loop(chat_id, 10)
    elif arg == "disable":
        new_val = await queue_manager.set_loop(chat_id, 0)
    elif arg.isdigit() and 1 <= int(arg) <= 10:
        new_val = await queue_manager.add_loop(chat_id, int(arg))
    else:
        await update.message.reply_text("Usage: /loop <1-10>, /loop enable, or /loop disable")
        return

    if new_val == 0:
        await update.message.reply_text("🔁 Loop disabled.")
    else:
        await update.message.reply_text(f"🔁 Current track will repeat {new_val} more time(s).")


# ==================== AUTH USERS (from YukkiMusicBot) ====================

@group_only
@admin_only
@safe_handler
async def auth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grants a non-admin control over playback commands (only matters
    once a chat turns on /restrictcontrols — see plugins/admin.py-style
    chat settings). Reply to someone, or /auth @username."""
    from bot.core.database import async_session
    from bot.services.economy_service import get_or_create_member
    from bot.utils.helpers import resolve_target_user

    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /auth @username")
        return

    async with async_session() as session:
        member = await get_or_create_member(session, update.effective_chat.id, target_id)
        member.is_auth_user = True
        await session.commit()

    await update.message.reply_text(f"✅ {target_name} can now control music playback here.")


@group_only
@admin_only
@safe_handler
async def unauth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.core.database import async_session
    from bot.services.economy_service import get_or_create_member
    from bot.utils.helpers import resolve_target_user

    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /unauth @username")
        return

    async with async_session() as session:
        member = await get_or_create_member(session, update.effective_chat.id, target_id)
        member.is_auth_user = False
        await session.commit()

    await update.message.reply_text(f"❌ {target_name} can no longer control music playback here.")


@group_only
@safe_handler
async def authusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from sqlalchemy import select

    from bot.core.database import ChatMember, User, async_session

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .join(ChatMember, ChatMember.user_id == User.id)
            .where(ChatMember.chat_id == update.effective_chat.id, ChatMember.is_auth_user.is_(True))
        )
        users = result.scalars().all()

    if not users:
        await update.message.reply_text("No auth users set. Admins: /auth (reply to someone) to add one.")
        return

    lines = "\n".join(f"• {escape_html(str(u.first_name or u.username or u.id))}" for u in users)
    await update.message.reply_text(f"🎚 <b>Auth users here</b>\n{lines}", parse_mode="HTML")


# ==================== PLAYMODE (from YukkiMusicBot) ====================

@group_only
@admin_only
@safe_handler
async def playmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.core.database import Chat, async_session

    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("direct", "inline"):
        async with async_session() as session:
            row = await session.get(Chat, update.effective_chat.id)
            current = row.playmode if row else "direct"
        await update.message.reply_text(
            f"🎛 Current play mode: <b>{current}</b>\nUsage: /playmode direct|inline\n\n"
            f"<i>Direct plays the first search result immediately; inline "
            f"(reserved for a future results-picker UI) currently behaves like direct.</i>",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        row = await session.get(Chat, update.effective_chat.id)
        if row is None:
            row = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(row)
        row.playmode = arg
        await session.commit()
    await update.message.reply_text(f"🎛 Play mode set to: {arg}")


@group_only
@admin_only
@safe_handler
async def restrictcontrols_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.core.database import Chat, async_session

    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /restrictcontrols on|off — when on, /skip /stop /pause etc. need an admin or /auth user")
        return
    async with async_session() as session:
        row = await session.get(Chat, update.effective_chat.id)
        if row is None:
            row = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(row)
        row.restrict_music_controls = arg == "on"
        await session.commit()
    await update.message.reply_text(f"🎚 Music control restriction is now {'ON' if arg == 'on' else 'OFF'}.")


@group_only
@admin_only
@safe_handler
async def videomode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opt-in per chat: /play sends video, not just audio (from
    YukkiMusicBot's videomode/videolimit — the concurrent-stream cap is
    global, see settings.video_stream_limit)."""
    from bot.core.database import Chat, async_session

    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.message.reply_text(f"Usage: /videomode on|off (max {settings.video_stream_limit} concurrent video streams bot-wide)")
        return
    async with async_session() as session:
        row = await session.get(Chat, update.effective_chat.id)
        if row is None:
            row = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(row)
        row.video_enabled = arg == "on"
        await session.commit()
    await update.message.reply_text(f"🎥 Video mode is now {'ON' if arg == 'on' else 'OFF'} — takes effect on the next /play.")


@admin_only
@safe_handler
async def channelplay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stream into a *different* chat's voice chat (typically a linked
    channel) than the one this command was sent in — from
    YukkiMusicBot's play/channel.py. Usage: /channelplay <chat_id> <song>.
    The assistant account needs to already be a member/admin of that
    target chat; this doesn't add it there."""
    if not _configured():
        await _not_configured_reply(update)
        return
    if len(context.args) < 2 or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /channelplay <channel chat_id> <song name or URL>")
        return

    target_chat_id = int(context.args[0])
    query = " ".join(context.args[1:])
    user = update.effective_user
    msg = await update.message.reply_text(f"🔎 Searching (targeting chat {target_chat_id})...")

    try:
        track = await _resolve_track(query, user.id, user.first_name or "Someone")
    except Exception as e:
        from bot.core.errors import user_facing_error
        await msg.edit_text(user_facing_error(e, fallback="Couldn't find a playable track for that. Try another query or a direct YouTube link."))
        return
    if not track or not track.audio_url:
        await msg.edit_text("❌ No playable result found.")
        return

    engine = _engine_for(target_chat_id)
    await queue_manager.add_track(target_chat_id, track)
    next_track = await queue_manager.next_track(target_chat_id)
    if next_track and await engine.play(target_chat_id, next_track, pipeline=_pipeline_for(target_chat_id)):
        await msg.edit_text(f"📡 Now playing in <code>{target_chat_id}</code>: <b>{escape_html(next_track.display_name)}</b>", parse_mode="HTML")
    else:
        await msg.edit_text(
            "❌ Couldn't join that chat's voice chat. Make sure the assistant "
            "account is a member/admin there and a voice chat is active."
        )


@group_only
@music_control_only
@safe_handler
async def seek_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/seek <seconds> jumps forward to that absolute position (from
    AnonXMusic's /seek — confirmed the underlying mechanism there is an
    ffmpeg restart with -ss, which is what engine.seek() now does too)."""
    if not _configured():
        await _not_configured_reply(update)
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /seek <seconds> — jumps to that position in the current track")
        return

    chat_id = update.effective_chat.id
    queue = await queue_manager.get_queue(chat_id)
    track = queue.current_track
    if not track:
        await update.message.reply_text("❌ Nothing is playing.")
        return
    if track.is_live:
        await update.message.reply_text("❌ Can't seek a livestream.")
        return

    to_seconds = int(context.args[0])
    if await _engine_for(chat_id).seek(chat_id, track, to_seconds):
        _mark_started(chat_id, from_seconds=to_seconds)
        await update.message.reply_text(f"⏩ Seeked to {to_seconds}s.")
    else:
        await update.message.reply_text("❌ Seek failed — position may be out of range.")


@group_only
@music_control_only
@safe_handler
async def seekback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _configured():
        await _not_configured_reply(update)
        return
    amount = int(context.args[0]) if context.args and context.args[0].isdigit() else 10
    chat_id = update.effective_chat.id
    queue = await queue_manager.get_queue(chat_id)
    track = queue.current_track
    if not track:
        await update.message.reply_text("❌ Nothing is playing.")
        return
    if track.is_live:
        await update.message.reply_text("❌ Can't seek a livestream.")
        return

    to_seconds = max(0, int(get_elapsed(chat_id)) - amount)
    if await _engine_for(chat_id).seek(chat_id, track, to_seconds):
        _mark_started(chat_id, from_seconds=to_seconds)
        await update.message.reply_text(f"⏪ Rewound to {to_seconds}s.")
    else:
        await update.message.reply_text("❌ Seek failed.")


# ==================== RESET (from AnonXMusic's restart) ====================

@group_only
@admin_only
@safe_handler
async def resetqueue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force-stops playback and clears the queue for this chat — for
    when something's stuck. From AnonXMusic's restartbot (the per-chat
    "clear my state" kernel of it; the admin-cache-refresh half of that
    command doesn't apply here — this project's admin_only decorator
    checks fresh every time rather than caching)."""
    chat_id = update.effective_chat.id
    if _configured():
        await _engine_for(chat_id).stop(chat_id)
        from bot.core.music_client import get_assistant_pool
        get_assistant_pool().release(chat_id)
    await queue_manager.clear(chat_id)
    _clear_position(chat_id)
    await update.message.reply_text("🔄 Queue cleared and playback reset for this chat.")


@group_only
@safe_handler
async def toptracks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Most-played tracks — from AnonXMusic's /toptracks. Chat-scoped by
    default; /toptracks global for bot-wide."""
    from sqlalchemy import func as sa_func, select

    from bot.core.database import TrackPlay, async_session

    scope_global = bool(context.args and context.args[0].lower() == "global")

    async with async_session() as session:
        query = select(TrackPlay.title, TrackPlay.artist, sa_func.count().label("plays"))
        if not scope_global:
            query = query.where(TrackPlay.chat_id == update.effective_chat.id)
        query = query.group_by(TrackPlay.title, TrackPlay.artist).order_by(sa_func.count().desc()).limit(10)
        rows = (await session.execute(query)).all()

    if not rows:
        await update.message.reply_text("📭 No play history yet.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>Top Tracks</b> {'(global)' if scope_global else '(this chat)'}\n"]
    for i, (title, artist, plays) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {escape_html(title)} — {escape_html(artist or '')} ({plays} plays)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@owner_only
@safe_handler
async def musichealth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: live-music subsystem status (no secrets)."""
    import shutil
    lines = ["<b>🎧 Music health</b> (owner only)\n"]
    lines.append(f"Configured (API id/hash): <b>{'yes' if settings.live_music_configured else 'no'}</b>")
    lines.append(f"Pool ready: <b>{'yes' if _configured() else 'no'}</b>")
    ffmpeg = shutil.which(getattr(settings, "ffmpeg_path", None) or "") or shutil.which("ffmpeg")
    lines.append(f"ffmpeg: <code>{ffmpeg or 'NOT FOUND'}</code>")
    if _pool:
        lines.append(f"Assistants: <b>{len(_pool.pairs)}</b>")
        lines.append(f"Active chats: <b>{getattr(_pool, 'total_active_calls', len(getattr(_pool, 'active_chats', {}) or {}))}</b>")
    else:
        lines.append("Assistants: <b>0</b> (pool not started)")
    lines.append("\nOrdinary users never see this data.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler(["musichealth", "music_status", "musicstatus"], musichealth_cmd))
    if not settings.enable_live_music:
        return
    app.add_handler(CommandHandler("play", play_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("leave", stop_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("nowplaying", nowplaying_cmd))
    app.add_handler(CommandHandler("np", nowplaying_cmd))
    app.add_handler(CommandHandler("volume", volume_cmd))
    app.add_handler(CommandHandler("shuffle", shuffle_cmd))
    app.add_handler(CommandHandler("repeat", repeat_cmd))
    app.add_handler(CommandHandler("effects", effects_cmd))
    app.add_handler(CommandHandler("loop", loop_cmd))
    app.add_handler(CommandHandler("auth", auth_cmd))
    app.add_handler(CommandHandler("unauth", unauth_cmd))
    app.add_handler(CommandHandler("authusers", authusers_cmd))
    app.add_handler(CommandHandler("playmode", playmode_cmd))
    app.add_handler(CommandHandler("restrictcontrols", restrictcontrols_cmd))
    app.add_handler(CommandHandler("videomode", videomode_cmd))
    app.add_handler(CommandHandler("channelplay", channelplay_cmd))
    app.add_handler(CommandHandler("seek", seek_cmd))
    app.add_handler(CommandHandler("seekback", seekback_cmd))
    app.add_handler(CommandHandler("resetqueue", resetqueue_cmd))
    app.add_handler(CommandHandler("toptracks", toptracks_cmd))
