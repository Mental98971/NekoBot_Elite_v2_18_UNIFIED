"""
Reaction GIFs — anime-style reaction images for group chats (hug, pat,
slap, and friends), via waifu.pics' SFW endpoint.

Ported from a Telethon + MongoDB "nekomode" plugin (Team-ProjectCodeX /
Mikobot lineage — one of the upstream sources this project draws on)
into this bot's python-telegram-bot + SQLAlchemy architecture. Notable
changes from the source:

- The on/off toggle is "/reactions", not "/nekomode" — this bot is
  itself named Neko and already has an unrelated "/personality"
  toggle for its own banter; a second, different feature also called
  "nekomode" would be a real everyday source of confusion.
- Per-chat on/off state lives on Chat.reactions_enabled (this project's
  existing settings table) instead of a separate MongoDB collection.
- "/wallpaper" (a different, unintegrated image API) and a stray
  "hTojiy" entry in the source's action list (mixed-case, so it could
  never match Telegram's lowercased commands — dead from the start)
  were dropped rather than carried over. So were "/avatar" and "/feed",
  which the source's own help text advertised but never actually
  registered as commands.
- Actions used as a reply to someone, or with a target mention, now
  render as "X pats Y" — completing what the source's own help text
  already promised ("hug: get hugged or hug a user") but its
  implementation never did; it only ever sent the file with no target
  logic at all.
- Outbound requests go through this project's existing httpx +
  with_retry pattern (bot/services/platforms.py) instead of adding a
  new synchronous `nekos` package dependency for a single command.
"""
from __future__ import annotations

import httpx
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Chat, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils.helpers import mention_html, resolve_target_user
from bot.utils.logger import get_logger
from bot.utils.retry import with_retry

logger = get_logger(__name__)

# Multi-source reaction GIF endpoints (SFW). Tried in shuffled order.
_REACTION_SOURCES = [
    {
        "name": "otakugifs",
        "url": "https://api.otakugifs.xyz/gif",
        "params": lambda action: {"reaction": action},
        "extract": lambda d: d.get("url") if isinstance(d, dict) else None,
        "supports": None,
    },
    {
        "name": "nekos.life",
        "url": "https://nekos.life/api/v2/img/{action}",
        "params": lambda action: {},
        "extract": lambda d: d.get("url") if isinstance(d, dict) else None,
        "supports": {
            "hug", "pat", "kiss", "slap", "cuddle", "tickle", "poke", "smug",
            "feed", "neko", "waifu", "cry", "smile", "wave", "wink", "dance",
        },
    },
    {
        "name": "waifu.pics",
        "url": "https://api.waifu.pics/sfw/{action}",
        "params": lambda action: {},
        "extract": lambda d: d.get("url") if isinstance(d, dict) else None,
        "supports": None,
    },
    {
        "name": "nekos.best",
        "url": "https://nekos.best/api/v2/{action}",
        "params": lambda action: {},
        "extract": lambda d: ((d.get("results") or [{}])[0] or {}).get("url") if isinstance(d, dict) else None,
        "supports": {
            "hug", "kiss", "pat", "slap", "cuddle", "laugh", "dance", "cry",
            "smile", "wave", "wink", "poke", "bite", "blush", "bored", "facepalm",
            "feed", "happy", "highfive", "pout", "shrug", "sleep", "stare", "think",
            "thumbsup", "tickle", "yeet",
        },
    },
]

# Interpersonal actions get an "X {verb}s Y" caption when used on a
# target (reply or @mention). Third-person singular, irregular ones
# spelled out; everything else in ACTIONS is a solo mood/vibe image
# sent as-is, since forcing e.g. "X sends neko vibes to Y" is more
# arbitrary than helpful.
ACTION_VERBS: dict[str, str] = {
    "hug": "hugs", "pat": "pats", "kiss": "kisses", "cuddle": "cuddles",
    "slap": "slaps", "bite": "bites", "poke": "pokes", "tickle": "tickles",
    "wave": "waves at", "highfive": "high-fives", "handhold": "holds hands with",
    "wink": "winks at", "bully": "bullies", "glomp": "glomps",
    "spank": "spanks", "lick": "licks", "nom": "noms on", "awoo": "awoos at",
}

# Solo mood/vibe images — sent standalone, no target caption.
SOLO_ACTIONS = {
    "neko", "waifu", "shinobu", "megumin", "cry", "dance", "smile",
    "blush", "cringe", "bonk", "yeet", "smug",
}

ACTIONS = sorted(set(ACTION_VERBS) | SOLO_ACTIONS)


async def _fetch_reaction_url(action: str) -> str | None:
    """Fetch a reaction GIF/image URL from multiple SFW anime sources."""
    import random
    sources = list(_REACTION_SOURCES)
    random.shuffle(sources)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NekoBot/2.4; +https://t.me)"}
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
            for src in sources:
                supports = src.get("supports")
                if supports is not None and action not in supports:
                    continue
                try:
                    url = src["url"].format(action=action)
                    params = src["params"](action) if callable(src["params"]) else src.get("params") or {}

                    @with_retry(max_attempts=2)
                    async def _get():
                        return await client.get(url, params=params)

                    resp = await _get()
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    found = src["extract"](data)
                    if found and isinstance(found, str) and found.startswith("http"):
                        return found
                except Exception:
                    logger.debug("reaction_source_fail", source=src["name"], action=action, exc_info=True)
                    continue
    except Exception:
        logger.error("reaction_fetch_failed", action=action, exc_info=True)
    return None


async def _reactions_enabled(chat_id: int) -> bool:
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        await session.commit()
        return bool(chat and chat.reactions_enabled)


def _build_caption(action: str, actor_name: str, actor_id: int, target_name: str | None, target_id: int | None) -> str | None:
    verb = ACTION_VERBS.get(action)
    if not verb or not target_name or target_id is None:
        return None
    if target_id == actor_id:
        return f"{mention_html(actor_id, actor_name)} {verb} themselves. 🥲"
    return f"{mention_html(actor_id, actor_name)} {verb} {mention_html(target_id, target_name)}!"


@safe_handler
async def reactions_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0].lower() if context.args else None)
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /reactions on | off")
        return

    chat_id = update.effective_chat.id
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        if not chat:
            chat = Chat(id=chat_id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.reactions_enabled = arg == "on"
        await session.commit()

    status = "enabled ✅" if arg == "on" else "disabled ❌"
    await update.message.reply_text(f"Reaction GIFs {status}.")


@safe_handler
async def reaction_action_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private" and not await _reactions_enabled(update.effective_chat.id):
        return  # feature not opted into in this chat — stay silent, not spammy

    action = update.message.text.lstrip("/").split("@")[0].split()[0].lower()
    if action not in ACTIONS:
        return

    url = await _fetch_reaction_url(action)
    if not url:
        await update.message.reply_text("❌ Couldn't fetch a reaction image right now — try again shortly.")
        return

    actor = update.effective_user
    target_id, target_name = await resolve_target_user(update, context)
    caption = _build_caption(action, actor.first_name, actor.id, target_name, target_id)

    send_kwargs = {"caption": caption, "parse_mode": "HTML"} if caption else {}
    try:
        if url.lower().endswith((".gif", ".webp")):
            await update.message.reply_animation(url, **send_kwargs)
        else:
            await update.message.reply_photo(url, **send_kwargs)
    except Exception:
        logger.warning("reaction_send_failed", action=action, url=url, exc_info=True)
        await update.message.reply_text("❌ Got the reaction but couldn't send it — try again.")


def register(app):
    if not settings.enable_reactions:
        return
    app.add_handler(CommandHandler("reactions", admin_only(group_only(reactions_toggle_cmd))))
    app.add_handler(CommandHandler(ACTIONS, reaction_action_cmd))
