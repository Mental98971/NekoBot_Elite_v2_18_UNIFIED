"""
/cosplay — random high-quality cosplay image delivery.

Multi-source with graceful degradation. SFW by default.
No captions — pure photo, matching the reference bot behaviour.
Aliases: /cosplay  /cp
"""
from __future__ import annotations

import random
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from bot.core.errors import safe_handler
from bot.utils.logger import get_logger
from bot.utils.ratelimit import hit

logger = get_logger(__name__)

_SOURCES = [
    # Dedicated cosplay sources first
    {
        "name": "xxapi-yscos",
        "url": "https://v2.xxapi.cn/api/yscos",
        "params": {"return": "json"},
        "extract": lambda d: d.get("data") if isinstance(d.get("data"), str) else None,
    },
    {
        "name": "xxapi-cosplay",
        "url": "https://v2.xxapi.cn/api/cosplay",
        "params": {"return": "json"},
        "extract": lambda d: d.get("data") if isinstance(d.get("data"), str) else None,
    },
    {
        "name": "waifu.im-cosplay",
        "url": "https://api.waifu.im/search",
        "params": {"included_tags": "cosplay", "is_nsfw": "false", "limit": "1"},
        "extract": lambda d: ((d.get("images") or [{}])[0] or {}).get("url"),
    },
    # High-quality anime character fallbacks (still thematically close)
    {
        "name": "waifu.pics",
        "url": "https://api.waifu.pics/sfw/waifu",
        "params": {},
        "extract": lambda d: d.get("url"),
    },
    {
        "name": "nekos.best",
        "url": "https://nekos.best/api/v2/neko",
        "params": {},
        "extract": lambda d: ((d.get("results") or [{}])[0] or {}).get("url"),
    },
    {
        "name": "waifu.im-waifu",
        "url": "https://api.waifu.im/search",
        "params": {"included_tags": "waifu", "is_nsfw": "false", "limit": "1"},
        "extract": lambda d: ((d.get("images") or [{}])[0] or {}).get("url"),
    },
]


@safe_handler
async def cosplay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a random cosplay image (no caption)."""
    user = update.effective_user
    if not user:
        return

    if not hit(f"cosplay:{user.id}", limit=1, window=4.0):
        await update.message.reply_text("⏳ Slow down a little — another cosplay is loading.")
        return

    status = await update.message.reply_text("🔍 Hunting for cosplay…")

    url = await _fetch_cosplay_url()
    if not url:
        await status.edit_text(
            "😿 Couldn't reach any cosplay source right now.\n"
            "Try again in a moment — the APIs sometimes nap."
        )
        return

    try:
        await update.message.reply_photo(photo=url)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.warning("cosplay_send_failed error=%s url=%s", e, (url or "")[:80])
        await status.edit_text(
            f"🖼️ Found one but Telegram rejected the photo.\n"
            f"<code>{(url or '')[:70]}…</code>",
            parse_mode="HTML",
        )


async def _fetch_cosplay_url() -> Optional[str]:
    timeout = aiohttp.ClientTimeout(total=9)
    headers = {"User-Agent": "NekoBot/1.0 (+telegram)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        sources = list(_SOURCES)
        random.shuffle(sources)
        for src in sources:
            try:
                async with session.get(src["url"], params=src.get("params") or {}) as resp:
                    if resp.status != 200:
                        continue
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "json" in ctype or "text" in ctype:
                        data = await resp.json(content_type=None)
                        url = src["extract"](data)
                        if url and isinstance(url, str) and url.startswith("http"):
                            return url
                    else:
                        return str(resp.url)
            except Exception as e:
                logger.debug("cosplay_source_fail source=%s err=%s", src["name"], e)
                continue
    return None


def register(app):
    app.add_handler(CommandHandler("cosplay", cosplay_cmd))
    app.add_handler(CommandHandler("cp", cosplay_cmd))
