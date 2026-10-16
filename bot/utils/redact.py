"""Secret redaction for logs — standard library only.

Used by both structlog (``redact_event`` processor) and stdlib ``logging``
(``RedactingFilter``). The stdlib side matters: python-telegram-bot talks to
Telegram through httpx, which logs every request URL at INFO level, and Bot API
URLs contain the bot token (``/bot<ID>:<SECRET>/getUpdates``). Without a filter
the token would be written to the container logs on every poll.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

# Key names whose *values* must never be logged in full.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|bot_token|session_string)",
    re.I,
)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.I)
# Telegram bot token: numeric bot id, colon, ~35 url-safe characters.
_TELEGRAM_TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")
# Common provider key shapes (OpenAI/Anthropic "sk-", Google "AIza", Groq "gsk_").
_PROVIDER_KEY_RE = re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gsk_[A-Za-z0-9]{20,})")


def redact_text(text: str) -> str:
    """Mask bearer tokens, Telegram bot tokens and well-known API-key shapes in free text."""
    if not text:
        return text
    text = _BEARER_RE.sub(r"\1***", text)
    text = _TELEGRAM_TOKEN_RE.sub("<redacted-bot-token>", text)
    return _PROVIDER_KEY_RE.sub("<redacted-api-key>", text)


def _mask(value: str) -> str:
    return "***" if len(value) <= 8 else value[:4] + "…" + value[-2:]


def redact_value(key: str, value: Any) -> Any:
    """Redact one logged value, using its key name as a hint."""
    if value is None:
        return value
    if isinstance(value, str):
        if _SECRET_KEY_RE.search(key or ""):
            return _mask(value)
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_value(key, v) for v in value)
    return value


def redact_event(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog processor: redact secrets by key name or by value shape."""
    return {k: redact_value(str(k), v) for k, v in event_dict.items()}


class RedactingFilter(logging.Filter):
    """Handler-level stdlib filter that scrubs secrets from the message and traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = redact_text(message)
            if redacted != message:
                record.msg, record.args = redacted, None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_text(record.exc_text)
        except Exception:  # a logging filter must never break logging itself
            pass
        return True


def install_stdlib_redaction(root: Optional[logging.Logger] = None) -> None:
    """Attach RedactingFilter to every handler of ``root`` (idempotent)."""
    root = root or logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
