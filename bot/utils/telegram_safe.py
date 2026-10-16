"""Safe Telegram API wrappers that never raise to callers."""
from __future__ import annotations

from typing import Any, Optional

from telegram import Message, Update
from telegram.error import BadRequest, Forbidden, TelegramError

from bot.utils.logger import get_logger
from bot.utils.textutil import sanitize_plain

logger = get_logger(__name__)


async def safe_reply(
    update: Update,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    **kwargs: Any,
) -> Optional[Message]:
    """Reply to effective_message; swallow Telegram errors."""
    msg = update.effective_message
    if not msg:
        return None
    text = sanitize_plain(text)
    if not text:
        return None
    try:
        return await msg.reply_text(text, parse_mode=parse_mode, **kwargs)
    except (BadRequest, Forbidden) as e:
        logger.debug("safe_reply suppressed: %s", e)
        return None
    except TelegramError as e:
        logger.warning("safe_reply failed: %s", e)
        return None


async def html_or_plain(send, text: str, *, plain: Optional[str] = None, **kwargs: Any) -> Optional[Message]:
    """Send admin-authored text as HTML; if Telegram rejects the markup (a stray
    ``<`` in a rules/welcome text), resend it as plain text instead of failing.

    ``plain`` is the body for that fallback; pass it when ``text`` embeds values
    that were HTML-escaped (``&lt;``), which would otherwise show up literally.
    ``send`` is any ``(text, **kwargs)`` coroutine function, e.g.
    ``message.reply_text`` or ``functools.partial(bot.send_message, chat_id)``."""
    try:
        return await send(text, parse_mode="HTML", **kwargs)
    except BadRequest as e:
        if "parse entities" not in str(e).lower():
            raise
        logger.debug("html_or_plain: markup rejected, falling back to plain text")
        return await send(text if plain is None else plain, **kwargs)


async def safe_edit(
    message: Message,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    **kwargs: Any,
) -> bool:
    text = sanitize_plain(text)
    try:
        await message.edit_text(text, parse_mode=parse_mode, **kwargs)
        return True
    except BadRequest as e:
        # "message is not modified" is fine
        if "not modified" in str(e).lower():
            return True
        logger.debug("safe_edit suppressed: %s", e)
        return False
    except TelegramError as e:
        logger.warning("safe_edit failed: %s", e)
        return False
