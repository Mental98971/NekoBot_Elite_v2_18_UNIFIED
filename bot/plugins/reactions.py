"""Expanded SFW reaction media commands for NekoBot.

~650 commands cover affection, comfort, playful gestures, greetings, emotions, comedy, everyday
gestures, social interactions and anime/fantasy actions. Each command requests its own media
category first, then sensible fallbacks (see :mod:`bot.utils.reaction_catalog`, the single source of
truth for names, captions, fallbacks and the ``/kick`` -> ``/reactionkick`` style aliases).

* ``/reactions on|off`` — per-chat switch (group admins). Private chats always work.
* ``/reactionlist [filter]`` (``/reactionhelp``, ``/rxlist``) — list the commands.
* Reply to someone or @mention them and the caption reads "X pats Y!".

Media lookup is :mod:`bot.utils.anime_media` (waifu.pics, nekos.life, nekos.best, otakugifs; concurrent,
cached, bounded); delivery is :mod:`bot.utils.reaction_media` (URL first, upload fallback).

History: ported from a Telethon + MongoDB "nekomode" plugin (Team-ProjectCodeX / Mikobot lineage).
The toggle is ``/reactions`` (this bot already has an unrelated ``/personality`` toggle), the state lives
on ``Chat.reactions_enabled``, and ``/wallpaper`` plus a dead mixed-case ``hTojiy`` entry from the
source's action list were deliberately not carried over.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Chat, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils.anime_media import fetch_anime_media
from bot.utils.helpers import mention_html, resolve_target_user
from bot.utils.logger import get_logger
from bot.utils.ratelimit import hit
from bot.utils.reaction_catalog import (  # noqa: F401  (re-exported for callers and tests)
    ACTION_FALLBACKS,
    ACTION_VERBS,
    ACTIONS,
    COMMAND_ALIASES as _COMMAND_ALIASES,
    COMMAND_CONFLICTS as _COMMAND_CONFLICTS,
    REACTION_COMMANDS as _REACTION_COMMANDS,
    SOLO_ACTIONS,
    fallbacks_for as _fallbacks_for,
)
from bot.utils.reaction_media import reply_media

logger = get_logger(__name__)

_REACTION_COMMAND_SET = frozenset(_REACTION_COMMANDS)
_RATE_LIMIT = 8        # reaction commands per user ...
_RATE_WINDOW = 10      # ... per this many seconds (protects the public media APIs)
_LIST_CHUNK = 3500     # Telegram's limit is 4096 characters; leave a margin


async def _fetch_reaction_url(action: str) -> str | None:
    """Try the requested category and its fallbacks through the shared multi-provider lookup."""
    try:
        url = await fetch_anime_media(action, extra_fallbacks=_fallbacks_for(action))
        if url:
            logger.info("reaction_media_found action=%s", action)
            return url
    except Exception:
        logger.warning("reaction_media_lookup_failed action=%s", action, exc_info=True)
    logger.warning("reaction_media_unavailable action=%s", action)
    return None


async def _reactions_enabled(chat_id: int) -> bool:
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        return bool(chat and chat.reactions_enabled)


def _build_caption(action: str, actor_name: str, actor_id: int, target_name: str | None, target_id: int | None) -> str | None:
    verb = ACTION_VERBS.get(action)
    if not verb or not target_name or target_id is None:
        return None
    actor = mention_html(actor_id, actor_name)
    if target_id == actor_id:
        return f"{actor} {verb} themselves. 🥲"
    return f"{actor} {verb} {mention_html(target_id, target_name)}!"


async def _send_media(message, url: str, caption: str | None = None) -> None:
    """Reply with the media: Telegram URL delivery first, then a bounded download/upload fallback."""
    await reply_media(message, url, caption=caption, parse_mode="HTML" if caption else None)


@safe_handler
async def reactions_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reactions on|off — switch reaction GIFs on or off for this chat."""
    message = update.message or update.effective_message
    chat_obj = update.effective_chat
    if not message or not chat_obj:
        return

    arg = context.args[0].lower() if context.args else None
    if arg not in ("on", "off"):
        state = ""
        if arg is None:
            state = f"Currently {'on ✅' if await _reactions_enabled(chat_obj.id) else 'off ❌'}.\n"
        await message.reply_text(f"{state}Usage: /reactions on | off")
        return

    async with async_session() as session:
        chat = await session.get(Chat, chat_obj.id)
        if not chat:
            chat = Chat(id=chat_obj.id, title=chat_obj.title, type=chat_obj.type)
            session.add(chat)
        chat.reactions_enabled = arg == "on"
        await session.commit()

    await message.reply_text(f"Reaction GIFs {'enabled ✅' if arg == 'on' else 'disabled ❌'}.")


@safe_handler
async def reaction_action_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the reaction image/GIF for the command that was used (captioned when someone is targeted)."""
    message = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if not message or not chat or not actor:
        return
    if chat.type != "private" and not await _reactions_enabled(chat.id):
        return
    raw_text = message.text or message.caption or ""
    if not raw_text.split():
        return
    command = raw_text.split()[0].split("@")[0].lstrip("/").lower()
    if command not in _REACTION_COMMAND_SET:
        return

    allowed, count = await hit(f"reaction:{actor.id}", limit=_RATE_LIMIT, window_seconds=_RATE_WINDOW)
    if not allowed:
        if count == _RATE_LIMIT + 1:   # say it once per window, then stay quiet
            await message.reply_text("⏳ Easy there — reactions are rate-limited for a few seconds.")
        return

    action = _COMMAND_ALIASES.get(command, command)
    url = await _fetch_reaction_url(action)
    if not url:
        await message.reply_text("😿 I couldn't find a reaction image right now. Please try again shortly.")
        return
    target_id, target_name = await resolve_target_user(update, context)
    caption = _build_caption(action, actor.first_name, actor.id, target_name, target_id)
    try:
        await _send_media(message, url, caption)
    except Exception:
        logger.warning("reaction_send_failed action=%s", action, exc_info=True)
        await message.reply_text("❌ I found a reaction, but Telegram couldn't send it. Please try again.")


def _chunk_commands(commands: list[str], header: str, limit: int = _LIST_CHUNK) -> list[str]:
    """Pack ``/command`` tokens into messages of at most ``limit`` characters; ``header`` leads the first."""
    chunks: list[str] = []
    current = header
    for command in commands:
        token = f"/{command}"
        if len(current) + len(token) + 1 > limit:
            chunks.append(current.rstrip())
            current = ""
        current += (" " if current and not current.endswith("\n") else "") + token
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


@safe_handler
async def reaction_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the reaction commands (optionally filtered: /reactionlist hug)."""
    message = update.effective_message
    if not message:
        return
    needle = " ".join(context.args).strip().lower().lstrip("/") if context.args else ""
    commands = [c for c in sorted(_REACTION_COMMANDS) if needle in c]
    if not commands:
        await message.reply_text(f"No reaction command contains “{needle[:40]}”.")
        return
    header = (
        f"✨ <b>NekoBot reaction commands</b> ({len(commands)}"
        f"{f' matching “{needle[:40]}”' if needle else ''})\n"
        "Groups: an admin turns them on with /reactions on. Reply to or @mention someone to target them.\n"
        "Filter the list with /reactionlist &lt;text&gt;.\n\n"
    )
    for chunk in _chunk_commands(commands, header):
        await message.reply_text(chunk, parse_mode="HTML")


def register(app) -> None:
    if not settings.enable_reactions:
        logger.info("reaction_plugin_disabled")
        return
    app.add_handler(CommandHandler("reactions", admin_only(group_only(reactions_toggle_cmd))))
    app.add_handler(CommandHandler(_REACTION_COMMANDS, reaction_action_cmd))
    app.add_handler(CommandHandler(["reactionlist", "reactionhelp", "rxlist"], reaction_list_cmd))
    logger.info("reaction_plugin_registered actions=%s", len(_REACTION_COMMANDS))
