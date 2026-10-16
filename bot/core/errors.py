"""Central error handling for Neko.

Provides typed exceptions, a safe handler decorator, and consistent
user-facing error replies that never leak internal names or stack traces.
"""
from __future__ import annotations

import functools
import traceback
from typing import Any, Callable, TypeVar

from telegram import Update
from telegram.ext import ContextTypes

from bot.identity import bot_name
from bot.utils.logger import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class NekoError(Exception):
    """Base application error. Safe to surface a generic message."""

    user_message: str = "Something went sideways. Try again in a moment."

    def __init__(self, message: str | None = None, *, user_message: str | None = None):
        super().__init__(message or self.user_message)
        if user_message:
            self.user_message = user_message


class PermissionError(NekoError):
    user_message = "You don't have permission to do that."


class ValidationError(NekoError):
    user_message = "That input didn't look right. Check the format and try again."


class RateLimitError(NekoError):
    user_message = "Slow down a bit. You're hitting the rate limit."


class ServiceUnavailableError(NekoError):
    user_message = "A backend service is temporarily unavailable. Give it a minute."


class ConfigError(NekoError):
    user_message = "Bot configuration problem. The owner has been notified."


# Substrings that must never appear in ordinary user-facing error text.
_LEAK_MARKERS = (
    "gemini", "groq", "openrouter", "anthropic", "openai", "claude", "gpt-",
    "api key", "api_key", "traceback", "file \"/", "quota", "rate-limit",
    "429", "generativelanguage", "system prompt", "developer prompt",
)


def _looks_like_internal_leak(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _LEAK_MARKERS)


def _format_user_error(exc: BaseException) -> str:
    name = bot_name()
    if isinstance(exc, NekoError):
        msg = exc.user_message or ""
        if msg and not _looks_like_internal_leak(msg):
            return msg
    # Never leak internals (provider names, stack traces, quota text, etc.)
    return f"Something went sideways inside {name}. The error is logged; try again shortly."


def user_facing_error(exc: BaseException | None = None, *, fallback: str | None = None) -> str:
    """Public helper for plugins: map any exception to a safe user string.

    Logs the real error when provided. Prefer this over ``f"❌ {e}"``.
    """
    if exc is not None:
        logger.error(
            "user_facing_error",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=exc,
        )
        if isinstance(exc, NekoError) and exc.user_message and not _looks_like_internal_leak(exc.user_message):
            return exc.user_message
    if fallback and not _looks_like_internal_leak(fallback):
        return fallback
    return _format_user_error(exc or Exception("unknown"))


async def reply_safe(update: Update, text: str, **kwargs: Any) -> None:
    """Best-effort reply that never raises to the caller."""
    try:
        if update.effective_message:
            # Last line of defense against accidental internal leaks.
            if _looks_like_internal_leak(text):
                text = _format_user_error(Exception("redacted"))
            await update.effective_message.reply_text(text, **kwargs)
    except Exception as e:
        logger.warning("Failed to send error reply: %s", e)


def safe_handler(func: F) -> F:
    """Decorator for telegram handlers. Catches all exceptions, logs with
    context, and sends a branded non-leaking reply to the user.

    Safe for ambient MessageHandlers: does not change successful return
    behavior; only intercepts raised exceptions.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any):
        chat_id = None
        user_id = None
        try:
            chat_id = update.effective_chat.id if update.effective_chat else None
            user_id = update.effective_user.id if update.effective_user else None
            return await func(update, context, *args, **kwargs)
        except NekoError as e:
            logger.warning(
                "Handled NekoError in %s | chat=%s user=%s | %s",
                func.__name__, chat_id, user_id, e,
            )
            await reply_safe(update, _format_user_error(e))
        except Exception as e:
            logger.error(
                "Unhandled error in %s | chat=%s user=%s | %s\n%s",
                func.__name__, chat_id, user_id, e, traceback.format_exc(),
            )
            await reply_safe(update, _format_user_error(e))
        return None

    return wrapper  # type: ignore[return-value]
