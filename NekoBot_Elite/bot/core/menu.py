"""Validation for the Telegram slash-command menu (``setMyCommands``).  Standard library only.

Telegram accepts at most 100 commands per scope; each name must be 1-32 characters of ``[a-z0-9_]`` and each
description 1-256 characters.  An invalid menu makes the API call fail, so the bot publishes whatever is valid
(first entries win) instead of letting a menu typo or a 101st entry abort startup.  Handlers are unaffected:
a command that is left out of the menu still works and is still listed in /help and docs/COMMANDS.md.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Tuple

MAX_MENU_COMMANDS = 100
_NAME = re.compile(r"^[a-z0-9_]{1,32}$")


def sanitize_menu(commands: Iterable[Tuple[str, str]], limit: int = MAX_MENU_COMMANDS) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return ``(kept, dropped)``: valid, de-duplicated entries in priority order, at most ``limit`` of them."""
    kept: List[Tuple[str, str]] = []
    dropped: List[str] = []
    seen = set()
    for name, description in commands:
        key = str(name or "").strip().lstrip("/").lower()
        text = " ".join(str(description or "").split())[:256]
        if not _NAME.match(key) or not text or key in seen:
            dropped.append(key or "<empty>")
            continue
        if len(kept) >= max(0, limit):
            dropped.append(key)
            continue
        seen.add(key)
        kept.append((key, text))
    return kept, dropped
