"""Structured logging helper for Neko.

All modules should use get_logger(__name__). Configuration (JSON vs
console, level) is done once at startup in bot.core.bot.setup_logging();
secret redaction lives in bot.utils.redact so structlog and stdlib logging
share one implementation.
"""
from __future__ import annotations

import structlog


def get_logger(name: str = __name__):
    return structlog.get_logger(name)
