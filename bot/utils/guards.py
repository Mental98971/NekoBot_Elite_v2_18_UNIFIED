"""Shared request guards used by plugins."""
from __future__ import annotations

from telegram import Update

from bot.utils.textutil import TELEGRAM_SAFE_MESSAGE, sanitize_plain


def message_text(update: Update, max_len: int = TELEGRAM_SAFE_MESSAGE) -> str:
    """Extract and sanitize message text; empty string if none."""
    msg = update.effective_message
    if not msg or not msg.text:
        return ""
    return sanitize_plain(msg.text, max_len=max_len)


def args_joined(context, max_len: int = 2000) -> str:
    """Join context.args safely."""
    args = getattr(context, "args", None) or []
    return sanitize_plain(" ".join(str(a) for a in args), max_len=max_len)
