
"""
/cosplay — random SFW cosplay and anime images.

Multi-source fallback with graceful degradation.
Aliases: /cosplay, /cp
"""

from __future__ import annotations

import random
from urllib.parse import urlparse
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from bot.core.errors import safe_handler
from bot.utils.logger import get_logger
from bot.utils.ratelimit import hit

logger = get_logger(__name__)


_SOURCES = [
    {
        "name": "xxapi-yscos",
        "url": "https://v2.xxapi.cn/api/yscos",
        "params": {"return": "json"},
        "extract": lambda d: (
            d.get("data") if isinstance(d, dict)
            and isinstance(d.get("data"), str) else None
        ),
    },
    {
        "name": "xxapi-cosplay",
        "url": "https://v2.xxapi.cn/api/cosplay",
        "params": {"return": "json"},
        "extract": lambda d: (
            d.get("data") if isinstance(d, dict)
            and isinstance(d.get("data"), str) else None
        ),
    },
    {
        "name": "waifu.im-cosplay",
        "url": "https://api.waifu.im/search",
        "params": {
            "included_tags": "cosplay",
            "is_nsfw": "false",
            "limit": "1",
        },
        "extract": lambda d: (
            ((d.get("images") or [{}])[0] or {}).get("url")
            if isinstance(d, dict) else None
        ),
    },
    {
        "name": "waifu.pics",
        "url": "https://api.waifu.pics/sfw/waifu",
        "params": {},
        "extract": lambda d: (
            d.get("url") if isinstance(d, dict) else None
        ),
    },
    {
        "name": "nekos.best",
        "url": "https://nekos.best/api/v2/neko",
        "params": {},
        "extract": lambda d: (
            ((d.get("results") or [{}])[0] or {}).get("url")
            if isinstance(d, dict) else None
        ),
    },
    {
        "name": "waifu.im-waifu",
        "url": "https://api.waifu.im/search",
        "params": {
            "included_tags": "waifu",
            "is_nsfw": "false",
            "limit": "1",
        },
        "extract": lambda d: (
            ((d.get("images") or [{}])[0] or {}).get("url")
            if isinstance(d, dict) else None
        ),
    },
]


def _valid_url(value: object) -> Optional[str]:
    """Accept only valid HTTP(S) image URLs."""
    if not isinstance(value, str):
        return None

    parsed = urlparse(value)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    return value


@safe_handler
async def cosplay_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Send a random cosplay image without a caption."""
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    # FIX: hit() is async and expects window_seconds.
    allowed, _count = await hit(
        f"cosplay:{user.id}",
        limit=1,
        window_seconds=4,
    )

    if not allowed:
        await message.reply_text(
            "⏳ Slow down a little — another cosplay is loading."
        )
        return

    status = await message.reply_text("🔍 Hunting for cosplay…")

    url = await _fetch_cosplay_url()

    if not url:
        await status.edit_text(
            "😿 Couldn't reach any cosplay source right now.\n"
            "Please try again in a moment."
        )
        return

    try:
        # Telegram accepts GIF/MP4 animations through reply_animation.
        path = urlparse(url).path.lower()

        if path.endswith((".gif", ".mp4")):
            await message.reply_animation(animation=url)
        else:
            await message.reply_photo(photo=url)

        try:
            await status.delete()
        except Exception:
            logger.debug("Could not delete cosplay status message")

    except Exception:
        logger.warning(
            "cosplay_send_failed",
            url=url[:120],
            exc_info=True,
        )
        await status.edit_text(
            "🖼️ I found an image, but Telegram couldn't send it. "
            "Please try again."
        )


async def _fetch_cosplay_url() -> Optional[str]:
    """Try each provider until one returns a valid URL."""
    timeout = aiohttp.ClientTimeout(total=12)
    headers = {
        "User-Agent": "NekoBot/2.18 (Telegram bot)",
        "Accept": "application/json, image/*, */*",
    }

    sources = list(_SOURCES)
    random.shuffle(sources)

    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as session:
            for source in sources:
                try:
                    async with session.get(
                        source["url"],
                        params=source.get("params") or {},
                        allow_redirects=True,
                    ) as response:
                        logger.info(
                            "cosplay_api_status",
                            source=source["name"],
                            status=response.status,
                            content_type=response.headers.get(
                                "Content-Type", ""
                            ),
                        )

                        if response.status != 200:
                            continue

                        content_type = (
                            response.headers.get("Content-Type") or ""
                        ).lower()

                        # API returned JSON: extract the image URL.
                        if (
                            "json" in content_type
                            or "text" in content_type
                        ):
                            try:
                                data = await response.json(
                                    content_type=None
                                )
                            except Exception:
                                logger.warning(
                                    "cosplay_invalid_json",
                                    source=source["name"],
                                )
                                continue

                            try:
                                url = _valid_url(source["extract"](data))
                            except Exception:
                                logger.warning(
                                    "cosplay_extract_failed",
                                    source=source["name"],
                                    exc_info=True,
                                )
                                continue

                            if url:
                                logger.info(
                                    "cosplay_image_found",
                                    source=source["name"],
                                )
                                return url

                            continue

                        # Some endpoints return the image directly.
                        if content_type.startswith("image/"):
                            url = _valid_url(str(response.url))
                            if url:
                                logger.info(
                                    "cosplay_direct_image",
                                    source=source["name"],
                                )
                                return url

                        logger.warning(
                            "cosplay_unexpected_content_type",
                            source=source["name"],
                            content_type=content_type,
                        )

                except Exception:
                    logger.warning(
                        "cosplay_source_failed",
                        source=source["name"],
                        exc_info=True,
                    )
                    continue

    except Exception:
        logger.error(
            "cosplay_session_failed",
            exc_info=True,
        )

    logger.error("cosplay_all_sources_failed")
    return None


def register(app) -> None:
    """Register /cosplay and /cp."""
    app.add_handler(CommandHandler("cosplay", cosplay_cmd))
    app.add_handler(CommandHandler("cp", cosplay_cmd))
    logger.info("cosplay_plugin_registered")
