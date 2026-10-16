"""Shared text safety utilities used across plugins and personality."""
from __future__ import annotations

import re

# Telegram message hard limit
TELEGRAM_MAX_MESSAGE = 4096
TELEGRAM_SAFE_MESSAGE = 4090

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def sanitize_plain(text: str, max_len: int = TELEGRAM_SAFE_MESSAGE) -> str:
    """Strip control chars, collapse excess whitespace, clamp length."""
    if not text:
        return ""
    text = _CONTROL.sub("", text)
    text = _MULTI_NL.sub("\n\n", text)
    text = text.strip()
    if len(text) <= max_len:
        return text
    # Prefer sentence break
    cut = text.rfind(".", 0, max_len)
    if cut > max_len // 2:
        return text[: cut + 1]
    space = text.rfind(" ", 0, max_len - 1)
    if space > max_len // 2:
        return text[:space].rstrip() + "…"
    return text[: max_len - 1] + "…"


def safe_html_message(text: str, max_len: int = TELEGRAM_SAFE_MESSAGE) -> str:
    """HTML-escape and sanitize together — see bot.utils.helpers.escape_html
    for the canonical HTML-escaping used across this codebase (this module
    doesn't redefine it, to avoid two slightly different implementations
    living side by side)."""
    from bot.utils.helpers import escape_html
    return escape_html(sanitize_plain(text, max_len=max_len))


_TEMPLATE_TOKEN = re.compile(r"\{([a-z_]+)\}")


def render_template(text: str, values: dict[str, str]) -> str:
    """Substitute only known ``{name}`` tokens; every other brace stays literal.

    Admin-authored welcome/goodbye text must never go through ``str.format``:
    a stray ``{`` raises, and ``{first.__class__}``-style lookups reach into
    Python objects. Unknown tokens are left exactly as typed.
    """
    return _TEMPLATE_TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text or "")


def truncate_middle(text: str, max_len: int = 200) -> str:
    if not text or len(text) <= max_len:
        return text or ""
    half = (max_len - 1) // 2
    return text[:half] + "…" + text[-half:]


def paginate_joined(items: list[str], sep: str = ", ", max_len: int = 3800) -> list[str]:
    """Group ``items`` into pages whose ``sep.join(page)`` stays under ``max_len``.

    For rendering a name list (presets, skins, …) that may be too long for a
    single Telegram message once the underlying catalog grows — join-then-clamp
    silently truncates mid-name and drops entries; this keeps every item and
    simply spreads them across more messages.
    """
    pages: list[str] = []
    page: list[str] = []
    page_len = 0
    for raw in items:
        it = raw if len(raw) <= max_len else truncate_middle(raw, max_len)
        added = len(it) + (len(sep) if page else 0)
        if page and page_len + added > max_len:
            pages.append(sep.join(page))
            page, page_len = [it], len(it)
        else:
            page.append(it)
            page_len += added
    if page:
        pages.append(sep.join(page))
    return pages
