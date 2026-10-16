"""
MyAnimeList lookups (v2.17): /myanime, /mymanga, /mycharacter, /myupcoming.

From the Saitama bot's ``myanimelist.py`` (jikanpy on the retired Jikan v3), rebuilt on the current Jikan v4 REST
API with plain httpx.  They sit next to the AniList-based /anime /manga /character /upcoming already in the bot:
same questions, MyAnimeList's scores and data.

Improvements over the original: API text is HTML-escaped (the original injected it raw), a missing synopsis or
"about" no longer crashes, links are validated, results are cached for CACHE_TTL seconds (Jikan allows only 3
requests per second), a rate-limit or outage gets a clear message, and adult titles are filtered out (``sfw``)
because these commands run in group chats.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CommandHandler

from bot.core.errors import safe_handler
from bot.utils import webtools
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

JIKAN = "https://api.jikan.moe/v4"
HTTP_TIMEOUT = 12.0
CACHE_TTL = 600.0
CACHE_MAX = 300
SYNOPSIS_LIMIT = 1400
UPCOMING_LIMIT = 15
MAX_QUERY = 100

_cache: Dict[str, Tuple[float, Any]] = {}


class JikanUnavailable(Exception):
    """The API is rate-limiting us or down; message is user-facing."""


async def jikan_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """GET ``/v4{path}``; the ``data`` payload, or None when MyAnimeList has nothing (404). Cached."""
    key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": "NekoBot-MAL/2.17"}) as client:
            resp = await client.get(JIKAN + path, params=params)
    except httpx.HTTPError as exc:
        raise JikanUnavailable("Couldn't reach MyAnimeList right now. Please try again!") from exc
    if resp.status_code == 404:
        data = None
    elif resp.status_code == 429:
        raise JikanUnavailable("MyAnimeList is rate-limiting me - try again in a few seconds.")
    elif resp.status_code != 200:
        raise JikanUnavailable("Error connecting to the API. Please try again!")
    else:
        try:
            data = resp.json().get("data")
        except ValueError as exc:
            raise JikanUnavailable("MyAnimeList sent something unreadable. Please try again!") from exc
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[key] = (now, data)
    return data


# ── formatting (pure) ───────────────────────────────────────────────────────
def _names(items) -> str:
    return ", ".join(escape_html(i["name"]) for i in (items or []) if isinstance(i, dict) and i.get("name")) or "-"


def _val(value) -> str:
    return escape_html(str(value)) if value not in (None, "", []) else "-"


_URL = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def _safe_url(raw: Optional[str]) -> str:
    """The URL if it is a plain http(s) address (raw, for buttons; escape it yourself for HTML), else ''."""
    url = (raw or "").strip()
    return url if _URL.match(url) else ""


def _image(entry: dict) -> str:
    jpg = ((entry.get("images") or {}).get("jpg") or {})
    return jpg.get("large_image_url") or jpg.get("image_url") or ""


def _hidden_image(entry: dict) -> str:
    url = _safe_url(_image(entry))
    return f"<a href='{escape_html(url)}'>\u200c</a>" if url else ""


def _title(entry: dict) -> str:
    title = escape_html(entry.get("title") or entry.get("name") or "?")
    jp = entry.get("title_japanese") or entry.get("name_kanji")
    return f"<b>{title}{f' ({escape_html(jp)})' if jp else ''}</b>"


def _clip(text: Optional[str], limit: int = SYNOPSIS_LIMIT) -> str:
    return escape_html(webtools.clip((text or "No synopsis available.").strip(), limit))


def _buttons(entry: dict, trailer: bool = False) -> Optional[InlineKeyboardMarkup]:
    row = []
    info = _safe_url(entry.get("url"))
    if info:
        row.append(InlineKeyboardButton("More Information", url=info))
    if trailer:
        t = _safe_url((entry.get("trailer") or {}).get("url"))
        if t:
            row.append(InlineKeyboardButton("Trailer", url=t))
    return InlineKeyboardMarkup([row]) if row else None


def format_anime(a: dict) -> str:
    premiered = f"{(a.get('season') or '').capitalize()} {a.get('year')}".strip() if a.get("year") else None
    return (
        f"{_title(a)}\n"
        f"<b>Type:</b> <code>{_val(a.get('type'))}</code>\n<b>Source:</b> <code>{_val(a.get('source'))}</code>\n"
        f"<b>Status:</b> <code>{_val(a.get('status'))}</code>\n<b>Genres:</b> <code>{_names(a.get('genres'))}</code>\n"
        f"<b>Episodes:</b> <code>{_val(a.get('episodes'))}</code>\n<b>Duration:</b> <code>{_val(a.get('duration'))}</code>\n"
        f"<b>Score:</b> <code>{_val(a.get('score'))}</code>\n<b>Studio(s):</b> <code>{_names(a.get('studios'))}</code>\n"
        f"<b>Premiered:</b> <code>{_val(premiered)}</code>\n<b>Rating:</b> <code>{_val(a.get('rating'))}</code>\n\n"
        f"{_hidden_image(a)}<i>{_clip(a.get('synopsis'))}</i>")


def format_manga(m: dict) -> str:
    return (
        f"{_title(m)}\n"
        f"<b>Type:</b> <code>{_val(m.get('type'))}</code>\n<b>Status:</b> <code>{_val(m.get('status'))}</code>\n"
        f"<b>Genres:</b> <code>{_names(m.get('genres'))}</code>\n<b>Authors:</b> <code>{_names(m.get('authors'))}</code>\n"
        f"<b>Score:</b> <code>{_val(m.get('score'))}</code>\n<b>Volumes:</b> <code>{_val(m.get('volumes'))}</code>\n"
        f"<b>Chapters:</b> <code>{_val(m.get('chapters'))}</code>\n\n"
        f"{_hidden_image(m)}<i>{_clip(m.get('synopsis'))}</i>")


def format_character(c: dict) -> str:
    return f"{_title(c)}\n\n{_hidden_image(c)}<i>{_clip(c.get('about'), 2500)}</i>"


def format_upcoming(items) -> str:
    seen, lines = set(), []
    for it in items or []:
        title, url = it.get("title"), _safe_url(it.get("url"))
        if not title or title in seen:
            continue
        seen.add(title)
        lines.append(f"• <a href='{escape_html(url)}'>{escape_html(title)}</a>" if url else f"• {escape_html(title)}")
        if len(lines) >= UPCOMING_LIMIT:
            break
    return "<b>Upcoming anime</b>\n" + "\n".join(lines)


# ── commands ────────────────────────────────────────────────────────────────
def _query(context) -> str:
    return " ".join(context.args or []).strip()[:MAX_QUERY]


async def _first(kind: str, query: str) -> Optional[dict]:
    results = await jikan_get(f"/{kind}", {"q": query, "limit": 1, "sfw": "true"})
    return results[0] if isinstance(results, list) and results else None


async def _lookup(update, context, kind: str, formatter, usage: str, trailer: bool = False) -> None:
    msg = update.effective_message
    query = _query(context)
    if not query:
        await msg.reply_text(usage)
        return
    try:
        entry = await _first(kind, query)
        if entry is not None and kind == "characters" and not entry.get("about") and entry.get("mal_id"):
            entry = await jikan_get(f"/characters/{entry['mal_id']}") or entry
    except JikanUnavailable as exc:
        await msg.reply_text(str(exc))
        return
    if entry is None:
        await msg.reply_text("No results found!")
        return
    try:
        await msg.reply_text(formatter(entry), parse_mode="HTML", reply_markup=_buttons(entry, trailer))
    except TelegramError as exc:
        logger.info("mal_reply_failed kind=%s err=%s", kind, exc)
        await msg.reply_text("I found it, but Telegram wouldn't take the message. Try a different search.")


@safe_handler
async def myanime_cmd(update, context) -> None:
    """/myanime <title> - MyAnimeList entry for an anime."""
    await _lookup(update, context, "anime", format_anime, "Usage: /myanime <anime title>", trailer=True)


@safe_handler
async def mymanga_cmd(update, context) -> None:
    """/mymanga <title> - MyAnimeList entry for a manga."""
    await _lookup(update, context, "manga", format_manga, "Usage: /mymanga <manga title>")


@safe_handler
async def mycharacter_cmd(update, context) -> None:
    """/mycharacter <name> - MyAnimeList entry for a character."""
    await _lookup(update, context, "characters", format_character, "Usage: /mycharacter <character name>")


@safe_handler
async def myupcoming_cmd(update, context) -> None:
    """/myupcoming - anime coming in the next seasons."""
    msg = update.effective_message
    try:
        items = await jikan_get("/seasons/upcoming", {"limit": UPCOMING_LIMIT * 2, "sfw": "true"})
    except JikanUnavailable as exc:
        await msg.reply_text(str(exc))
        return
    if not items:
        await msg.reply_text("No upcoming anime found right now.")
        return
    await msg.reply_text(format_upcoming(items), parse_mode="HTML", disable_web_page_preview=True)


def register(app) -> None:
    app.add_handler(CommandHandler("myanime", myanime_cmd))
    app.add_handler(CommandHandler("mymanga", mymanga_cmd))
    app.add_handler(CommandHandler("mycharacter", mycharacter_cmd))
    app.add_handler(CommandHandler("myupcoming", myupcoming_cmd))
