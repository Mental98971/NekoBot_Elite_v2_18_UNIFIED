
"""
Reaction GIFs and images for Telegram group chats.

Providers:
- NEKOSBEST
- waifu.pics

Uses public SFW endpoints; no API key required.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Chat, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils.helpers import mention_html, resolve_target_user
from bot.utils.logger import get_logger

logger = get_logger(__name__)


# Actions that describe an interaction with another user.
ACTION_VERBS: dict[str, str] = {
    "hug": "hugs",
    "pat": "pats",
    "kiss": "kisses",
    "cuddle": "cuddles",
    "slap": "slaps",
    "bite": "bites",
    "poke": "pokes",
    "tickle": "tickles",
    "wave": "waves at",
    "highfive": "high-fives",
    "handhold": "holds hands with",
    "wink": "winks at",
    "bully": "bullies",
    "glomp": "glomps",
    "spank": "spanks",
    "lick": "licks",
    "nom": "noms on",
    "awoo": "awoos at",
}


# Solo mood/vibe commands.
SOLO_ACTIONS = {
    "neko",
    "waifu",
    "shinobu",
    "megumin",
    "cry",
    "dance",
    "smile",
    "blush",
    "cringe",
    "bonk",
    "yeet",
    "smug",
}

ACTIONS = sorted(set(ACTION_VERBS) | SOLO_ACTIONS)


# Common aliases for actions that some providers may not support.
_ACTION_FALLBACKS = {
    "spank": "slap",
    "bully": "slap",
    "glomp": "hug",
    "handhold": "highfive",
    "nom": "bite",
}


# Known SFW waifu.pics categories.
_WAIFU_CATEGORIES = {
    "waifu",
    "neko",
    "shinobu",
    "megumin",
    "bully",
    "cuddle",
    "cry",
    "hug",
    "awoo",
    "kiss",
    "lick",
    "pat",
    "smug",
    "bonk",
    "yeet",
    "blush",
    "smile",
    "wave",
    "highfive",
    "handhold",
    "nom",
    "bite",
    "glomp",
    "slap",
    "happy",
    "wink",
    "poke",
    "dance",
    "cringe",
}


def _valid_http_url(value: object) -> str | None:
    """Return a URL only if it is a valid HTTP(S) URL."""
    if not isinstance(value, str):
        return None

    parsed = urlparse(value)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    return value


async def _fetch_reaction_url(action: str) -> str | None:
    """
    Fetch an image URL using available public SFW providers.

    Provider failures are logged, and the next provider is tried.
    """
    headers = {
        "User-Agent": (
            "NekoBot (https://github.com/Mental98971/novabot-1)"
        ),
        "Accept": "application/json",
    }

    categories = [action]
    fallback = _ACTION_FALLBACKS.get(action)

    if fallback and fallback not in categories:
        categories.append(fallback)

    timeout = httpx.Timeout(15.0, connect=8.0)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
        ) as client:

            # Discover NEKOSBEST's currently advertised categories.
            nekos_categories: set[str] = set()

            try:
                response = await client.get(
                    "https://nekos.best/api/v2/endpoints"
                )

                logger.info(
                    "reaction_api_status",
                    source="nekos.best",
                    endpoint="endpoints",
                    status=response.status_code,
                )

                if response.status_code == 200:
                    data = response.json()

                    if isinstance(data, dict):
                        nekos_categories = {
                            str(category).lower()
                            for category in data.keys()
                        }

                    elif isinstance(data, list):
                        nekos_categories = {
                            str(category).lower()
                            for category in data
                        }

            except Exception:
                logger.warning(
                    "reaction_endpoint_discovery_failed",
                    exc_info=True,
                )

            # Try each requested action and its optional fallback.
            for category in categories:

                # Provider 1: NEKOSBEST.
                if category in nekos_categories:
                    try:
                        response = await client.get(
                            f"https://nekos.best/api/v2/{category}"
                        )

                        logger.info(
                            "reaction_api_status",
                            source="nekos.best",
                            action=category,
                            status=response.status_code,
                        )

                        if response.status_code == 200:
                            data = response.json()

                            if isinstance(data, dict):
                                results = data.get("results") or []

                                if results and isinstance(results[0], dict):
                                    image_url = _valid_http_url(
                                        results[0].get("url")
                                    )

                                    if image_url:
                                        logger.info(
                                            "reaction_image_found",
                                            source="nekos.best",
                                            action=category,
                                        )
                                        return image_url

                    except Exception:
                        logger.warning(
                            "reaction_source_failed",
                            source="nekos.best",
                            action=category,
                            exc_info=True,
                        )

                # Provider 2: waifu.pics.
                if category in _WAIFU_CATEGORIES:
                    try:
                        response = await client.get(
                            f"https://api.waifu.pics/sfw/{category}"
                        )

                        logger.info(
                            "reaction_api_status",
                            source="waifu.pics",
                            action=category,
                            status=response.status_code,
                        )

                        if response.status_code == 200:
                            data = response.json()

                            if isinstance(data, dict):
                                image_url = _valid_http_url(
                                    data.get("url")
                                )

                                if image_url:
                                    logger.info(
                                        "reaction_image_found",
                                        source="waifu.pics",
                                        action=category,
                                    )
                                    return image_url

                    except Exception:
                        logger.warning(
                            "reaction_source_failed",
                            source="waifu.pics",
                            action=category,
                            exc_info=True,
                        )

    except Exception:
        logger.error(
            "reaction_fetch_failed",
            action=action,
            exc_info=True,
        )

    logger.error(
        "all_reaction_sources_failed",
        action=action,
        attempted_categories=categories,
        nekos_categories_discovered=bool(nekos_categories)
        if "nekos_categories" in locals()
        else False,
    )

    return None


async def _reactions_enabled(chat_id: int) -> bool:
    """Check whether reactions are enabled for this chat."""
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        return bool(chat and chat.reactions_enabled)


def _build_caption(
    action: str,
    actor_name: str,
    actor_id: int,
    target_name: str | None,
    target_id: int | None,
) -> str | None:
    """Build an HTML caption when an action targets another user."""
    verb = ACTION_VERBS.get(action)

    if not verb or not target_name or target_id is None:
        return None

    actor = mention_html(actor_id, actor_name)

    if target_id == actor_id:
        return f"{actor} {verb} themselves. 🥲"

    target = mention_html(target_id, target_name)
    return f"{actor} {verb} {target}!"


@safe_handler
async def reactions_toggle_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Enable or disable reaction commands for a group."""
    message = update.effective_message
    chat_obj = update.effective_chat

    if not message or not chat_obj:
        return

    arg = context.args[0].lower() if context.args else None

    if arg not in ("on", "off"):
        await message.reply_text("Usage: /reactions on | off")
        return

    async with async_session() as session:
        chat = await session.get(Chat, chat_obj.id)

        if not chat:
            chat = Chat(
                id=chat_obj.id,
                title=chat_obj.title,
                type=chat_obj.type,
            )
            session.add(chat)

        chat.reactions_enabled = arg == "on"
        await session.commit()

    status = "enabled ✅" if arg == "on" else "disabled ❌"
    await message.reply_text(f"Reaction GIFs {status}.")


@safe_handler
async def reaction_action_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle reaction commands and send the resulting image."""
    message = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user

    if not message or not chat or not actor:
        return

    # Private chats do not require the group toggle.
    if (
        chat.type != "private"
        and not await _reactions_enabled(chat.id)
    ):
        return

    raw_text = message.text or message.caption or ""
    command = raw_text.split()[0].split("@")[0].lstrip("/")
    action = command.lower()

    if action not in ACTIONS:
        return

    url = await _fetch_reaction_url(action)

    if not url:
        await message.reply_text(
            "❌ Couldn't fetch a reaction image right now. "
            "Please try again later."
        )
        return

    try:
        target_id, target_name = await resolve_target_user(
            update,
            context,
        )

        caption = _build_caption(
            action,
            actor.first_name,
            actor.id,
            target_name,
            target_id,
        )

        send_kwargs = (
            {"caption": caption, "parse_mode": "HTML"}
            if caption
            else {}
        )

        # Check the URL path, not the entire URL including query parameters.
        path = urlparse(url).path.lower()

        if path.endswith((".gif", ".mp4")):
            await message.reply_animation(url, **send_kwargs)
        else:
            await message.reply_photo(url, **send_kwargs)

    except Exception:
        logger.warning(
            "reaction_send_failed",
            action=action,
            url=url,
            exc_info=True,
        )

        await message.reply_text(
            "❌ I found a reaction image but couldn't send it. "
            "Please try again."
        )


def register(app) -> None:
    """Register reaction commands with the application."""
    if not settings.enable_reactions:
        logger.info("reaction_plugin_disabled")
        return

    app.add_handler(
        CommandHandler(
            "reactions",
            admin_only(group_only(reactions_toggle_cmd)),
        )
    )

    app.add_handler(
        CommandHandler(
            ACTIONS,
            reaction_action_cmd,
        )
    )

    logger.info(
        "reaction_plugin_registered",
        actions_count=len(ACTIONS),
    )
