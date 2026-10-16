"""Which language to answer in: the glue between the handlers, the chat row and bot/utils/i18n.py.

Order: the chat's explicit ``/lang`` setting, else the Telegram client language of the user who wrote, else ``DEFAULT_LANGUAGE``
(restricted to the languages that have a catalog and are allowed by ``SUPPORTED_LANGUAGES``).  Nothing here may break a handler:
every lookup failure falls back to the default language.
"""
from __future__ import annotations

from typing import Any, List, Optional

from bot.config import settings
from bot.core.database import Chat, async_session
from bot.utils import chatcache, i18n
from bot.utils.logger import get_logger

logger = get_logger(__name__)


def default_language() -> str:
    return i18n.normalize_language(getattr(settings, "default_language", None)) or i18n.DEFAULT_LANGUAGE


def supported() -> List[str]:
    """Catalog languages, limited by the operator's ``SUPPORTED_LANGUAGES`` when set (English is always kept)."""
    have = i18n.available_languages()
    allowed = {i18n.normalize_language(c) for c in (getattr(settings, "supported_languages", None) or [])} - {None}
    picked = [c for c in have if c in allowed or c == i18n.DEFAULT_LANGUAGE]
    return picked or have or [i18n.DEFAULT_LANGUAGE]


async def stored_language(chat_id: Optional[int]) -> Optional[str]:
    """The explicit ``/lang`` choice of a chat (None = automatic)."""
    if chat_id is None:
        return None
    try:
        snap = await chatcache.get_chat_settings(chat_id)
    except Exception as exc:  # a database hiccup must not take a reply down with it
        logger.info("lang_lookup_failed", error=type(exc).__name__)
        return None
    return getattr(snap, "language", None) if snap is not None else None


async def chat_language(update: Any) -> str:
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    stored = await stored_language(getattr(chat, "id", None))
    return i18n.resolve_language(stored, getattr(user, "language_code", None), default_language(), supported())


async def tr(update: Any, key: str, **values: object) -> str:
    """Translate ``key`` for the chat/user of ``update``."""
    try:
        lang = await chat_language(update)
    except Exception:
        lang = default_language()
    return i18n.t(key, lang, **values)


async def reply_language(update: Any) -> Optional[str]:
    """Language an LLM should answer in, or None for English/unknown (models then follow the user's own language)."""
    try:
        lang = await chat_language(update)
    except Exception:
        return None
    return None if lang == i18n.DEFAULT_LANGUAGE else lang


async def set_chat_language(chat_id: int, chat_type: Optional[str], title: Optional[str], code: Optional[str]) -> None:
    """Persist ``code`` (None = automatic) on the chat row, creating the row if the chat has none yet."""
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            chat = Chat(id=chat_id, type=chat_type or "private", title=((title or "")[:128] or None))
            session.add(chat)
        chat.language = code
        await session.commit()  # the commit hook invalidates chatcache for this chat
