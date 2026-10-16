"""Extras plugin - utilities absorbed from FallenRobot and related archives (v2.13), hardened in v2.14.

Commands
  /truth /dare             Truth or Dare (truthordarebot.xyz; built-in lists answer when the API is down)
  /carbon                  code or text (or a replied message) -> image
  /wall /wallpaper         wallpaper search: Wallhaven (SFW, no key) -> Unsplash (if UNSPLASH_ACCESS_KEY) -> Lorem Picsum
  /webss /ss /screenshot   website screenshot (public http(s) addresses only; ``full`` for the whole page)
  /gps                     place name or "lat, lon" -> location pin + map link (OpenStreetMap Nominatim, 1 request/second)
  /gsearch /google         DuckDuckGo instant answer + related topics

Every command that calls an outside service has a per-user cooldown, a timeout and a graceful failure message.
Pure helpers (URL validation, safe hrefs, stable seeds, coordinate parsing) live in bot/utils/webtools.py.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.errors import safe_handler, user_facing_error
from bot.utils import ratelimit
from bot.utils.helpers import escape_html
from bot.utils.langctx import tr
from bot.utils.logger import get_logger
from bot.utils.webtools import clip, ddg_search_url, normalize_url, parse_coordinates, safe_href, stable_seed

logger = get_logger(__name__)

USER_AGENT = "NekoBotElite/2.15 (Telegram bot; https://core.telegram.org/bots)"
_MAX_PHOTO_BYTES = 9_500_000      # Telegram rejects photos above 10 MB
_MAX_IMAGE_BYTES = 40_000_000     # never buffer more than this from an outside service
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# ── shared plumbing ─────────────────────────────────────────────────────────


async def _allowed(update: Update, name: str, *, limit: int = 3, window: int = 60) -> bool:
    """Per-user cooldown for commands that call outside services; answers the user when it refuses."""
    user, msg = update.effective_user, update.effective_message
    if user is None or msg is None:
        return False
    ok, _count = await ratelimit.hit(f"extras:{name}:{user.id}", limit=limit, window_seconds=window)
    if not ok:
        await msg.reply_text(await tr(update, "common.cooldown"))
    return ok


async def _get_json(url: str, *, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None,
                    timeout: float = 10.0) -> Any:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _mention(update: Update) -> str:
    user = update.effective_user
    return user.mention_html() if user is not None else ""


# ── Truth / Dare ────────────────────────────────────────────────────────────

_TOD_URL = "https://api.truthordarebot.xyz/v1/{kind}"
_TOD_FALLBACK: Dict[str, List[str]] = {
    "truth": [
        "What is the most embarrassing song on your playlist?",
        "What is a harmless habit you're a little ashamed of?",
        "What is the last thing you searched for on your phone?",
        "Which fictional character would you swap lives with for a day?",
        "What is the weirdest food combination you secretly enjoy?",
        "What was your most awkward moment in a group chat?",
        "What is one thing you pretend to understand but don't?",
        "Which app do you waste the most time on?",
        "What is the best compliment you have ever received?",
        "What is a small thing that instantly makes your day better?",
    ],
    "dare": [
        "Send the last emoji you used five times in a row.",
        "Change your profile picture to a potato until tomorrow.",
        "Write a two-line poem about the last thing you ate.",
        "Talk only in questions for the next five messages.",
        "Send a voice message of you doing your best cat impression.",
        "Type your next three messages with your eyes closed.",
        "Share the oldest photo in your gallery that you are happy to show.",
        "Say something nice about every person who replies in the next minute.",
        "Describe your day using only movie titles.",
        "Send a message in all capitals about how much you love Mondays.",
    ],
}


async def _truth_or_dare(update: Update, kind: str) -> None:
    msg = update.effective_message
    if msg is None or not await _allowed(update, kind, limit=6, window=60):
        return
    question: Optional[str] = None
    try:
        data = await _get_json(_TOD_URL.format(kind=kind), params={"rating": "pg13"}, timeout=8.0)
        question = str((data or {}).get("question") or (data or {}).get(kind) or "").strip() or None
    except Exception as exc:  # the API is a convenience, not a dependency
        logger.info("extras_tod_api_failed", kind=kind, error=type(exc).__name__)
    if not question:
        question = random.choice(_TOD_FALLBACK[kind])
    title = "🔵 <b>Truth</b>" if kind == "truth" else "🔴 <b>Dare</b>"
    await msg.reply_text(f"{title}\n\n{escape_html(clip(question, 500))}", parse_mode="HTML")


@safe_handler
async def truth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/truth - a random (PG-13) truth question."""
    await _truth_or_dare(update, "truth")


@safe_handler
async def dare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dare - a random (PG-13) dare."""
    await _truth_or_dare(update, "dare")


# ── Carbon ──────────────────────────────────────────────────────────────────

_CARBON_URL = "https://carbonara.solopov.dev/api/cook"


async def _make_carbon(text: str) -> Optional[io.BytesIO]:
    """Render ``text`` as a code image through the public carbonara service; None on any failure."""
    payload = {
        "code": text[:3000], "backgroundColor": "#1e1e2e", "theme": "one-dark", "language": "auto",
        "fontFamily": "Fira Code", "fontSize": "14px", "lineNumbers": True,
        "paddingVertical": "32px", "paddingHorizontal": "32px",
    }
    try:
        async with httpx.AsyncClient(timeout=25.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.post(_CARBON_URL, json=payload)
        body = resp.content
        if resp.status_code != 200 or not body or len(body) > _MAX_IMAGE_BYTES:
            return None
        if not body.startswith(_PNG_MAGIC) and "image" not in (resp.headers.get("content-type") or "").lower():
            return None
        bio = io.BytesIO(body)
        bio.name = "carbon.png"
        return bio
    except Exception as exc:
        logger.info("extras_carbon_failed", error=type(exc).__name__)
        return None


@safe_handler
async def carbon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/carbon <code or text> - turn code or a replied message into an image."""
    msg = update.effective_message
    if msg is None:
        return
    replied = msg.reply_to_message
    text = ((replied.text or replied.caption) if replied is not None else None) or (" ".join(context.args or []) or None)
    if not text or not text.strip():
        await msg.reply_text("Usage: /carbon <code or text>  (or reply to a message)")
        return
    if not await _allowed(update, "carbon", limit=3, window=60):
        return
    status = await msg.reply_text("🖼 Generating carbon…")
    img = await _make_carbon(text)
    if img is None:
        await status.edit_text("❌ Carbon generation failed. Try again later.")
        return
    try:
        await msg.reply_photo(photo=img, caption=f"Requested by {_mention(update)}", parse_mode="HTML")
    except Exception as exc:
        await status.edit_text(f"❌ Could not send the image: {escape_html(user_facing_error(exc))}", parse_mode="HTML")
        return
    await status.delete()


# ── Wallpaper ───────────────────────────────────────────────────────────────

_WALLHAVEN_URL = "https://wallhaven.cc/api/v1/search"
_UNSPLASH_URL = "https://api.unsplash.com/photos/random"


async def _wallhaven(query: str) -> Optional[Tuple[str, str, str, str]]:
    """(preview image, full image, page url, credit line) from Wallhaven's keyless SFW search."""
    data = await _get_json(_WALLHAVEN_URL, params={
        "q": query, "categories": "111", "purity": "100", "sorting": "random", "atleast": "1280x720"}, timeout=12.0)
    items = [it for it in ((data or {}).get("data") or []) if isinstance(it, dict) and it.get("path")]
    if not items:
        return None
    it = random.choice(items[:10])
    preview = (it.get("thumbs") or {}).get("large") or it["path"]
    return preview, it["path"], it.get("url") or "", f"Wallhaven · {it.get('resolution') or ''}".strip(" ·")


async def _unsplash(query: str) -> Optional[Tuple[str, str, str, str]]:
    key = str(getattr(settings, "unsplash_access_key", "") or "").strip()
    if not key:
        return None
    data = await _get_json(_UNSPLASH_URL, params={"query": query, "orientation": "landscape"},
                           headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"}, timeout=12.0)
    urls = (data or {}).get("urls") or {}
    link = urls.get("regular") or urls.get("full")
    if not link:
        return None
    who = ((data.get("user") or {}).get("name")) or "Unsplash"
    return link, urls.get("full") or link, ((data.get("links") or {}).get("html")) or "", f"Photo by {who} on Unsplash"


@safe_handler
async def wallpaper_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wallpaper <query> - search for a wallpaper (alias /wall)."""
    msg = update.effective_message
    if msg is None:
        return
    query = clip(" ".join(context.args or []), 80)
    if not query:
        await msg.reply_text("Usage: /wallpaper <query>\nExample: /wallpaper anime landscape")
        return
    if not await _allowed(update, "wallpaper", limit=4, window=60):
        return
    status = await msg.reply_text("🔍 Searching wallpapers…")
    found: Optional[Tuple[str, str, str, str]] = None
    for source in (_wallhaven, _unsplash):
        try:
            found = await source(query)
        except Exception as exc:
            logger.info("extras_wallpaper_source_failed", source=source.__name__, error=type(exc).__name__)
            found = None
        if found:
            break
    if found is None:  # last resort: a stable random image, labelled as such (never passed off as a search result)
        seed = stable_seed(query)
        found = (f"https://picsum.photos/seed/{seed}/1280/720", f"https://picsum.photos/seed/{seed}/1920/1080", "",
                 "random image (no wallpaper source answered)")
    preview, full, page, credit = found
    links = []
    if safe_href(full):
        links.append(f'<a href="{safe_href(full)}">full size</a>')
    if safe_href(page):
        links.append(f'<a href="{safe_href(page)}">source page</a>')
    caption = (f"🖼 <b>{escape_html(query)}</b>\n{escape_html(credit)}" + (f" · {' · '.join(links)}" if links else "")
               + f"\nRequested by {_mention(update)}")
    try:
        await msg.reply_photo(photo=preview, caption=caption, parse_mode="HTML")
        await status.delete()
    except Exception as exc:  # Telegram could not fetch the image: still hand over the links
        logger.info("extras_wallpaper_send_failed", error=type(exc).__name__)
        await status.edit_text(caption, parse_mode="HTML", disable_web_page_preview=False)


# ── Website screenshot ──────────────────────────────────────────────────────

_WEBSS_URL_DEFAULT = "https://webscreenshot.vercel.app/api"


def _decode_screenshot(data: Any) -> Optional[bytes]:
    raw = (data or {}).get("image") if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    b64 = raw.split("base64,", 1)[1] if "base64," in raw[:64] else raw
    if len(b64) > _MAX_IMAGE_BYTES * 4 // 3 + 16:
        return None
    try:
        return base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        return None


@safe_handler
async def webss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/webss <url> [full] - screenshot of a public website (aliases /ss, /screenshot)."""
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /webss <url> [full]\nExample: /webss example.com\n         /webss example.com full")
        return
    url = normalize_url(args[0])
    if url is None:
        await msg.reply_text("❌ I can only screenshot public http(s) websites, e.g. /webss example.com")
        return
    full = len(args) >= 2 and args[1].lower() in ("full", "yes", "y", "1", "true")
    if not await _allowed(update, "webss", limit=3, window=60):
        return
    status = await msg.reply_text("📸 Capturing screenshot…")
    endpoint = str(getattr(settings, "webss_api_url", "") or "").strip() or _WEBSS_URL_DEFAULT
    payload: Dict[str, Any] = {"url": url, "width": 1920, "height": 1080, "scale": 1, "format": "jpeg"}
    if full:
        payload["full"] = True
    try:
        async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.post(endpoint, json=payload)
        if resp.status_code != 200:
            await status.edit_text("❌ Screenshot service unavailable.")
            return
        blob = _decode_screenshot(resp.json())
        if not blob:
            await status.edit_text("❌ Could not capture that page.")
            return
        bio = io.BytesIO(blob)
        bio.name = "webss.jpg"
        caption = f"🌐 {escape_html(clip(url, 300))}" + (" (full page)" if full else "") + f"\nRequested by {_mention(update)}"
        if full or len(blob) > _MAX_PHOTO_BYTES:  # tall or large captures survive as a document
            await msg.reply_document(document=bio, caption=caption, parse_mode="HTML")
        else:
            await msg.reply_photo(photo=bio, caption=caption, parse_mode="HTML")
        await status.delete()
    except Exception as exc:
        await status.edit_text(f"❌ Screenshot failed: {escape_html(user_facing_error(exc))}", parse_mode="HTML")


# ── GPS ─────────────────────────────────────────────────────────────────────

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_geocode_lock: Optional[asyncio.Lock] = None
_last_geocode = 0.0


async def _geocode(query: str) -> Optional[Dict[str, Any]]:
    """First Nominatim hit for ``query``.  Nominatim's usage policy allows one request per second: serialise them."""
    global _geocode_lock, _last_geocode
    if _geocode_lock is None:  # created lazily inside the running loop
        _geocode_lock = asyncio.Lock()
    async with _geocode_lock:
        wait = 1.1 - (time.monotonic() - _last_geocode)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            results = await _get_json(_NOMINATIM_URL, params={"q": query, "format": "json", "limit": 1}, timeout=12.0)
        finally:
            _last_geocode = time.monotonic()
    return results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None


@safe_handler
async def gps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gps <place or "lat, lon"> - location pin plus a map link."""
    msg = update.effective_message
    if msg is None:
        return
    query = clip(" ".join(context.args or []), 200)
    if not query:
        await msg.reply_text("Usage: /gps <place or address>\nExample: /gps Tokyo Tower   or   /gps 35.6586, 139.7454")
        return
    if not await _allowed(update, "gps", limit=4, window=60):
        return
    status = await msg.reply_text("📍 Looking up location…")
    try:
        coords = parse_coordinates(query)
        if coords is not None:
            lat, lon = coords
            display = f"{lat:.5f}, {lon:.5f}"
        else:
            hit = await _geocode(query)
            if hit is None:
                await status.edit_text(f"❌ Could not find “{escape_html(query)}”.", parse_mode="HTML")
                return
            lat, lon = float(hit["lat"]), float(hit["lon"])
            display = str(hit.get("display_name") or query)
        maps_url = f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"
        await msg.reply_location(latitude=lat, longitude=lon)
        await msg.reply_text(f'📍 <b>{escape_html(clip(display, 300))}</b>\n<a href="{safe_href(maps_url)}">Open in Google Maps</a>',
                             parse_mode="HTML", disable_web_page_preview=True)
        await status.delete()
    except Exception as exc:
        await status.edit_text(f"❌ GPS lookup failed: {escape_html(user_facing_error(exc))}", parse_mode="HTML")


# ── Web search (instant answers) ────────────────────────────────────────────

_DDG_URL = "https://api.duckduckgo.com/"


def _related_lines(related: Any, limit: int = 6) -> List[str]:
    lines: List[str] = []

    def add(item: Any) -> None:
        if len(lines) >= limit or not isinstance(item, dict) or not item.get("Text"):
            return
        text = escape_html(clip(str(item["Text"]), 220))
        href = safe_href(item.get("FirstURL"))
        lines.append(f'• <a href="{href}">{text}</a>' if href else f"• {text}")

    for entry in related if isinstance(related, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("Topics"), list):
            for sub in entry["Topics"]:
                add(sub)
        else:
            add(entry)
        if len(lines) >= limit:
            break
    return lines


@safe_handler
async def gsearch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/gsearch <query> - DuckDuckGo instant answer and related topics (alias /google)."""
    msg = update.effective_message
    if msg is None:
        return
    query = clip(" ".join(context.args or []), 200)
    if not query:
        await msg.reply_text("Usage: /gsearch <query>\nExample: /gsearch python telegram bot")
        return
    if not await _allowed(update, "gsearch", limit=5, window=60):
        return
    status = await msg.reply_text("🔎 Searching…")
    footer = f'\n🔍 <a href="{safe_href(ddg_search_url(query))}">More results on DuckDuckGo</a>'
    try:
        data = await _get_json(_DDG_URL, params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}, timeout=15.0)
        data = data if isinstance(data, dict) else {}
        abstract = str(data.get("AbstractText") or data.get("Answer") or "")
        source = str(data.get("AbstractSource") or data.get("AbstractURL") or "")
        definition = str(data.get("Definition") or "")
        lines: List[str] = []
        if abstract:
            lines.append(f"📌 {escape_html(clip(abstract, 600))}")
            if source:
                lines.append(f"<i>Source: {escape_html(clip(source, 120))}</i>")
        if definition and definition != abstract:
            lines.append(f"📖 {escape_html(clip(definition, 400))}")
        lines.extend(_related_lines(data.get("RelatedTopics")))
        if not lines:
            await status.edit_text(f"No instant answer for <b>{escape_html(query)}</b>.{footer}", parse_mode="HTML",
                                   disable_web_page_preview=True)
            return
        await status.edit_text(f"🔎 <b>{escape_html(query)}</b>\n\n" + "\n".join(lines) + footer, parse_mode="HTML",
                               disable_web_page_preview=True)
    except Exception as exc:
        await status.edit_text(f"❌ Search failed: {escape_html(user_facing_error(exc))}", parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("truth", truth_cmd))
    app.add_handler(CommandHandler("dare", dare_cmd))
    app.add_handler(CommandHandler("carbon", carbon_cmd))
    app.add_handler(CommandHandler(["wall", "wallpaper"], wallpaper_cmd))
    app.add_handler(CommandHandler(["webss", "ss", "screenshot"], webss_cmd))
    app.add_handler(CommandHandler("gps", gps_cmd))
    app.add_handler(CommandHandler(["gsearch", "google"], gsearch_cmd))
