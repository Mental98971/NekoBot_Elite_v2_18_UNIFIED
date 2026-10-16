"""Lightweight input validation helpers used across plugins."""
from __future__ import annotations

import re

from bot.core.errors import ValidationError

_SAFE_NAME = re.compile(r"^[\w\s\-.'\"]{1,64}$", re.UNICODE)
_POSITIVE_INT = re.compile(r"^\d{1,12}$")


def require_text(args: list | tuple | None, *, name: str = "argument") -> str:
    if not args:
        raise ValidationError(f"Missing {name}.")
    text = " ".join(str(a) for a in args).strip()
    if not text:
        raise ValidationError(f"Missing {name}.")
    return text


def require_positive_int(raw: str | None, *, name: str = "number", maximum: int | None = None) -> int:
    if raw is None or not _POSITIVE_INT.match(str(raw).strip()):
        raise ValidationError(f"{name} must be a positive integer.")
    val = int(str(raw).strip())
    if val < 1:
        raise ValidationError(f"{name} must be at least 1.")
    if maximum is not None and val > maximum:
        raise ValidationError(f"{name} must be at most {maximum}.")
    return val


def sanitize_display_name(raw: str, *, fallback: str = "User") -> str:
    raw = (raw or "").strip()
    if not raw or not _SAFE_NAME.match(raw):
        return fallback
    return raw[:64]


def clamp_str(text: str, max_len: int = 4000) -> str:
    text = text or ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
