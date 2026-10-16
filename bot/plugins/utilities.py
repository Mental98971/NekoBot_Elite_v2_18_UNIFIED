"""
Utilities plugin — fills gaps advertised by /menu.

Implements (no API keys required unless noted):
  /id /info          — user & chat identity
  /tr                — translate text (LibreTranslate public + fallback)
  /weather           — current weather via wttr.in
  /imdb              — title search (OMDb if OMDB_API_KEY set, else Wikipedia)
  /calc              — safe arithmetic calculator
  /tagall            — mention active members (admin)
  /dice /fun         — dice roll + fun hub
  /stats             — alias hub for bot/user stats
"""
from __future__ import annotations

import ast
import logging
import operator
import re
from typing import Optional

from urllib.parse import quote

import aiohttp
from sqlalchemy import desc, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import ChatMember, User, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler, user_facing_error
from bot.utils.helpers import escape_html, mention_html

logger = logging.getLogger("bot.plugins.utilities")

# Safe calculator operators only
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_ast(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Num):  # py<3.8
        return float(node.n)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ZeroDivisionError("division by zero")
        if isinstance(node.op, ast.Pow) and (abs(left) > 1e6 or abs(right) > 100):
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_ast(node.operand))
    raise ValueError("unsupported expression")


def _safe_calc(expr: str) -> float:
    expr = expr.strip().replace("^", "**")
    if len(expr) > 120 or not re.fullmatch(r"[0-9+\-*/().%\s^]+", expr.replace("**", "^")):
        # allow ** after replace check using original with limited charset
        if not re.fullmatch(r"[0-9+\-*/().%\s*]+", expr):
            raise ValueError("invalid characters")
    tree = ast.parse(expr, mode="eval")
    return _eval_ast(tree)


# ─── Identity ───────────────────────────────────────────────────────────────

@safe_handler
async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show IDs for user / replied user / chat."""
    user = update.effective_user
    chat = update.effective_chat
    reply = update.message.reply_to_message if update.message else None

    lines = [
        f"👤 <b>You:</b> <code>{user.id}</code>",
        f"💬 <b>Chat:</b> <code>{chat.id}</code> ({chat.type})",
    ]
    if reply and reply.from_user:
        lines.append(
            f"↩️ <b>Replied:</b> <code>{reply.from_user.id}</code> "
            f"({escape_html(reply.from_user.first_name or '')})"
        )
    origin = getattr(reply, "forward_origin", None) if reply else None
    origin_user = getattr(origin, "sender_user", None)
    if origin_user:
        lines.append(f"➡️ <b>Forwarded from:</b> <code>{origin_user.id}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Profile card for yourself or a replied/mentioned user."""
    target = update.effective_user
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    elif context.args:
        # try numeric id
        try:
            uid = int(context.args[0])
            async with async_session() as session:
                u = await session.get(User, uid)
                if u:
                    name = u.first_name or u.username or str(uid)
                    await update.message.reply_text(
                        f"ℹ️ <b>{escape_html(name)}</b>\n"
                        f"ID: <code>{uid}</code>\n"
                        f"Username: @{escape_html(u.username or '—')}\n"
                        f"Karma: {u.karma or 0}",
                        parse_mode="HTML",
                    )
                    return
            target_id = uid
            await update.message.reply_text(f"ℹ️ User <code>{target_id}</code> (not in DB yet).", parse_mode="HTML")
            return
        except ValueError:
            pass

    async with async_session() as session:
        u = await session.get(User, target.id)
        karma = u.karma if u else 0
        # per-chat xp if in group
        xp = level = 0
        if update.effective_chat and update.effective_chat.type != "private":
            cm = (
                await session.execute(
                    select(ChatMember).where(
                        ChatMember.chat_id == update.effective_chat.id,
                        ChatMember.user_id == target.id,
                    )
                )
            ).scalar_one_or_none()
            if cm:
                xp, level = cm.xp or 0, cm.level or 0

    uname = f"@{target.username}" if target.username else "—"
    text = (
        f"ℹ️ <b>{escape_html(target.first_name or 'User')}</b>\n"
        f"ID: <code>{target.id}</code>\n"
        f"Username: {escape_html(uname)}\n"
        f"Karma: {karma}\n"
    )
    if update.effective_chat and update.effective_chat.type != "private":
        text += f"Level: {level} · XP: {xp:,}\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ─── Translate ──────────────────────────────────────────────────────────────

# ISO 639-1 / common BCP-47 codes we accept (case-insensitive). Extended list
# covers virtually every language supported by the free providers we use.
_VALID_LANG = re.compile(
    r"^(?:"
    r"[a-z]{2}"                                    # ISO 639-1
    r"|[a-z]{2}-[a-z]{2}"                          # e.g. zh-cn, pt-br
    r"|[a-z]{3}"                                   # rare 3-letter
    r")$",
    re.I,
)


def _normalize_lang(code: str) -> Optional[str]:
    """Normalize language code to lowercase ISO/BCP form. Returns None if invalid."""
    if not code:
        return None
    c = code.strip().lower().replace("_", "-")
    # Common aliases
    aliases = {
        "jp": "ja", "jpn": "ja", "japanese": "ja",
        "cn": "zh-cn", "zh": "zh-cn", "chinese": "zh-cn", "mandarin": "zh-cn",
        "tw": "zh-tw", "hk": "zh-hk",
        "kr": "ko", "kor": "ko", "korean": "ko",
        "sp": "es", "spa": "es", "spanish": "es",
        "ge": "de", "ger": "de", "german": "de",
        "fr": "fr", "fre": "fr", "french": "fr",
        "pt": "pt", "por": "pt", "portuguese": "pt",
        "ru": "ru", "rus": "ru", "russian": "ru",
        "ar": "ar", "ara": "ar", "arabic": "ar",
        "hi": "hi", "hin": "hi", "hindi": "hi",
        "en": "en", "eng": "en", "english": "en",
        "it": "it", "ita": "it", "italian": "it",
        "nl": "nl", "dut": "nl", "dutch": "nl",
        "pl": "pl", "pol": "pl", "polish": "pl",
        "tr": "tr", "tur": "tr", "turkish": "tr",
        "vi": "vi", "vie": "vi", "vietnamese": "vi",
        "th": "th", "tha": "th", "thai": "th",
        "id": "id", "ind": "id", "indonesian": "id",
        "ms": "ms", "may": "ms", "malay": "ms",
        "uk": "uk", "ukr": "uk", "ukrainian": "uk",
        "cs": "cs", "cze": "cs", "czech": "cs",
        "sv": "sv", "swe": "sv", "swedish": "sv",
        "fi": "fi", "fin": "fi", "finnish": "fi",
        "no": "no", "nor": "no", "norwegian": "no",
        "da": "da", "dan": "da", "danish": "da",
        "el": "el", "gre": "el", "greek": "el",
        "he": "he", "heb": "he", "hebrew": "he",
        "ro": "ro", "rum": "ro", "romanian": "ro",
        "hu": "hu", "hun": "hu", "hungarian": "hu",
        "bn": "bn", "ben": "bn", "bengali": "bn",
        "ta": "ta", "tam": "ta", "tamil": "ta",
        "te": "te", "tel": "te", "telugu": "te",
        "mr": "mr", "mar": "mr", "marathi": "mr",
        "gu": "gu", "guj": "gu", "gujarati": "gu",
        "kn": "kn", "kan": "kn", "kannada": "kn",
        "ml": "ml", "mal": "ml", "malayalam": "ml",
        "pa": "pa", "pan": "pa", "punjabi": "pa",
        "ur": "ur", "urd": "ur", "urdu": "ur",
        "fa": "fa", "per": "fa", "persian": "fa", "farsi": "fa",
        "auto": "auto",
    }
    if c in aliases:
        return aliases[c]
    if _VALID_LANG.match(c):
        return c
    return None


@safe_handler
async def tr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /tr <target> <text>              — auto-detect source → target
    /tr <source>|<target> <text>     — explicit language pair
    /tr <text>                       — translate to English
    Reply + /tr <target>             — translate replied message to target
    Reply + /tr <source>|<target>    — translate replied message with pair
    """
    text: Optional[str] = None
    source: str = "auto"
    dest: str = "en"
    args = list(context.args or [])

    # Reply-to-message mode
    if update.message and update.message.reply_to_message and (
        update.message.reply_to_message.text or update.message.reply_to_message.caption
    ):
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption
        if args:
            pair = args[0]
            if "|" in pair:
                parts = pair.split("|", 1)
                src_n = _normalize_lang(parts[0])
                dst_n = _normalize_lang(parts[1])
                if not dst_n:
                    await update.message.reply_text(
                        "❌ Invalid target language. Use ISO codes e.g. <code>en</code>, <code>ja</code>, <code>zh-CN</code>.",
                        parse_mode="HTML",
                    )
                    return
                source = src_n or "auto"
                dest = dst_n
            else:
                dst_n = _normalize_lang(pair)
                if not dst_n:
                    await update.message.reply_text(
                        "❌ Invalid language code. Examples: <code>en</code> <code>ja</code> <code>es</code> <code>zh-CN</code> <code>hi</code>",
                        parse_mode="HTML",
                    )
                    return
                dest = dst_n
    elif args:
        first = args[0]
        # Explicit pair: /tr en|ja Hello  or  /tr ja|en こんにちは
        if "|" in first:
            parts = first.split("|", 1)
            src_n = _normalize_lang(parts[0])
            dst_n = _normalize_lang(parts[1])
            if not dst_n:
                await update.message.reply_text(
                    "❌ Invalid target language in pair. Use e.g. <code>/tr en|ja Hello</code>",
                    parse_mode="HTML",
                )
                return
            source = src_n or "auto"
            dest = dst_n
            text = " ".join(args[1:]).strip() if len(args) > 1 else None
        # Target only: /tr ja Hello  or  /tr es ¿Cómo estás?
        elif _normalize_lang(first) and len(args) >= 2:
            dest = _normalize_lang(first)  # type: ignore
            text = " ".join(args[1:]).strip()
        # No lang → English
        else:
            text = " ".join(args).strip()
            dest = "en"
    else:
        await update.message.reply_text(
            "Usage:\n"
            "• <code>/tr &lt;lang&gt; &lt;text&gt;</code> — auto-detect → target\n"
            "• <code>/tr &lt;src&gt;|&lt;tgt&gt; &lt;text&gt;</code> — explicit pair\n"
            "• <code>/tr &lt;text&gt;</code> — translate to English\n"
            "• Reply to a message + <code>/tr &lt;lang&gt;</code>\n\n"
            "Examples:\n"
            "<code>/tr es Hello world</code>\n"
            "<code>/tr ja|en こんにちは</code>\n"
            "<code>/tr en|ja Good morning</code>\n"
            "<code>/tr zh-CN Hello</code>",
            parse_mode="HTML",
        )
        return

    if not text or not text.strip():
        await update.message.reply_text("Text missing. Provide text or reply to a message.")
        return
    if len(text) > 2500:
        await update.message.reply_text("Text too long (max 2500 characters).")
        return

    result = await _translate(text, source=source, dest=dest)
    if not result:
        await update.message.reply_text(
            "❌ Translation failed. Check the language codes and try again in a moment."
        )
        return

    translated, detected = result
    pair_label = f"{detected or source} → {dest}".upper()
    await update.message.reply_text(
        f"🌐 <b>{escape_html(pair_label)}</b>\n{escape_html(translated)}",
        parse_mode="HTML",
    )


async def _translate(
    text: str, source: str = "auto", dest: str = "en"
) -> Optional[tuple[str, Optional[str]]]:
    """
    Translate text. Returns (translated_text, detected_source_lang) or None.
    Primary: Google Translate free endpoint (client=gtx) — reliable auto-detect.
    Fallback 1: MyMemory with proper SOURCE|TARGET pair.
    Fallback 2: LibreTranslate public instances.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NekoBot/2.4; +https://t.me)",
        "Accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=14)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        # 1) Google Translate free endpoint (most reliable for auto + any pair)
        try:
            gurl = (
                "https://translate.googleapis.com/translate_a/single"
                f"?client=gtx&sl={quote(source)}&tl={quote(dest)}&dt=t&q={quote(text[:2000])}"
            )
            async with session.get(gurl) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data and isinstance(data, list) and data[0]:
                        pieces = []
                        for part in data[0]:
                            if part and part[0]:
                                pieces.append(part[0])
                        if pieces:
                            detected = None
                            if len(data) > 2 and isinstance(data[2], str):
                                detected = data[2]
                            return ("".join(pieces), detected)
        except Exception:
            logger.debug("gtx translate failed", exc_info=True)

        # 2) MyMemory — requires a complete SOURCE|TARGET pair
        try:
            # MyMemory does not accept "auto"; map to a sensible default or keep detected
            mm_src = source if source and source != "auto" else "en"
            # Prefer a short probe if we still have auto: try a tiny detect via gtx first already failed,
            # so fall back to en as source (MyMemory will still often work for common pairs).
            mm = (
                "https://api.mymemory.translated.net/get"
                f"?q={quote(text[:500])}&langpair={quote(mm_src)}|{quote(dest)}"
            )
            async with session.get(mm) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    t = (data.get("responseData") or {}).get("translatedText")
                    # MyMemory sometimes returns the same text or an error string
                    if t and isinstance(t, str) and "INVALID" not in t.upper() and t.strip():
                        # Match quality check
                        match = data.get("responseData", {}).get("match", 0)
                        if match is None or match >= 0.5 or len(t) > 0:
                            return (t, mm_src if source == "auto" else source)
        except Exception:
            logger.debug("mymemory failed", exc_info=True)

        # 3) LibreTranslate public instances
        payload = {"q": text[:2000], "source": source if source != "auto" else "auto", "target": dest, "format": "text"}
        for url in (
            "https://libretranslate.com/translate",
            "https://translate.argosopentech.com/translate",
            "https://translate.terraprint.co/translate",
        ):
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict) and data.get("translatedText"):
                            return (data["translatedText"], data.get("detectedLanguage") or source)
            except Exception:
                logger.debug("libretranslate failed: %s", url, exc_info=True)

    return None


# ─── Weather ────────────────────────────────────────────────────────────────

@safe_handler
async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /weather <city> — current conditions via wttr.in with Open-Meteo fallback. """
    if not context.args:
        await update.message.reply_text("Usage: /weather &lt;city&gt;", parse_mode="HTML")
        return
    city = " ".join(context.args).strip()
    if len(city) > 80:
        await update.message.reply_text("City name too long.")
        return

    headers = {"User-Agent": "Mozilla/5.0 (compatible; NekoBot/2.4; +https://t.me)"}
    timeout = aiohttp.ClientTimeout(total=14)

    # 1) wttr.in JSON
    try:
        url = f"https://wttr.in/{quote(city)}?format=j1"
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    cur = data["current_condition"][0]
                    area = data.get("nearest_area", [{}])[0]
                    place = area.get("areaName", [{}])[0].get("value", city)
                    country = area.get("country", [{}])[0].get("value", "")
                    desc = cur.get("weatherDesc", [{}])[0].get("value", "")
                    temp_c = cur.get("temp_C", "?")
                    feels = cur.get("FeelsLikeC", "?")
                    humidity = cur.get("humidity", "?")
                    wind = cur.get("windspeedKmph", "?")
                    await update.message.reply_text(
                        f"🌤 <b>{escape_html(place)}</b>, {escape_html(country)}\n"
                        f"{escape_html(desc)}\n"
                        f"🌡 {temp_c}°C (feels {feels}°C)\n"
                        f"💧 Humidity {humidity}% · 💨 Wind {wind} km/h",
                        parse_mode="HTML",
                    )
                    return
    except Exception:
        logger.debug("wttr failed for %s", city, exc_info=True)

    # 2) Open-Meteo geocoding + current weather (no key, reliable)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            geo_url = (
                "https://geocoding-api.open-meteo.com/v1/search"
                f"?name={quote(city)}&count=1&language=en&format=json"
            )
            async with session.get(geo_url) as resp:
                if resp.status != 200:
                    raise RuntimeError("geocode status")
                geo = await resp.json(content_type=None)
            results = geo.get("results") or []
            if not results:
                await update.message.reply_text(
                    f"❌ Couldn't find weather for <b>{escape_html(city)}</b>. Check the spelling.",
                    parse_mode="HTML",
                )
                return
            loc = results[0]
            lat, lon = loc["latitude"], loc["longitude"]
            place = loc.get("name", city)
            country = loc.get("country", "") or loc.get("country_code", "")
            wx_url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
                "&temperature_unit=celsius&wind_speed_unit=kmh"
            )
            async with session.get(wx_url) as resp:
                if resp.status != 200:
                    raise RuntimeError("forecast status")
                wx = await resp.json(content_type=None)
            cur = wx.get("current") or {}
            code = cur.get("weather_code", 0)
            # WMO weather interpretation codes (simplified)
            code_map = {
                0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
                45: "Fog", 48: "Depositing rime fog",
                51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
                61: "Slight rain", 63: "Rain", 65: "Heavy rain",
                71: "Slight snow", 73: "Snow", 75: "Heavy snow",
                80: "Slight rain showers", 81: "Rain showers", 82: "Violent rain showers",
                95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
            }
            desc = code_map.get(code, f"Code {code}")
            temp = cur.get("temperature_2m", "?")
            feels = cur.get("apparent_temperature", "?")
            humidity = cur.get("relative_humidity_2m", "?")
            wind = cur.get("wind_speed_10m", "?")
            await update.message.reply_text(
                f"🌤 <b>{escape_html(place)}</b>, {escape_html(str(country))}\n"
                f"{escape_html(desc)}\n"
                f"🌡 {temp}°C (feels {feels}°C)\n"
                f"💧 Humidity {humidity}% · 💨 Wind {wind} km/h",
                parse_mode="HTML",
            )
            return
    except Exception:
        logger.exception("weather_fallback_failed city=%s", city)

    await update.message.reply_text(
        "❌ Weather lookup failed right now. Try again in a moment or check the city name."
    )


# ─── IMDb / title search ────────────────────────────────────────────────────

@safe_handler
async def imdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /imdb <title> — movie/series lookup. TMDB (tappable multi-result search + poster)
    if TMDB_API_KEY is set, else OMDb if OMDB_API_KEY is set, else Wikipedia. """
    if not context.args:
        await update.message.reply_text("Usage: /imdb &lt;title&gt;", parse_mode="HTML")
        return
    title = " ".join(context.args).strip()
    key = getattr(settings, "omdb_api_key", None) or getattr(settings, "OMDB_API_KEY", None)
    # also check env-style on settings dict if present
    if not key and hasattr(settings, "model_dump"):
        key = settings.model_dump().get("omdb_api_key")

    tmdb_key = getattr(settings, "tmdb_api_key", None)

    async with aiohttp.ClientSession() as session:
        if tmdb_key:
            try:
                async with session.get(
                    "https://api.themoviedb.org/3/search/multi",
                    params={"api_key": tmdb_key, "query": title, "include_adult": "false"},
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    data = await resp.json()
                results = [
                    r for r in data.get("results", [])
                    if r.get("media_type") in ("movie", "tv") and (r.get("title") or r.get("name"))
                ][:8]
                if results:
                    buttons = []
                    for r in results:
                        name = r.get("title") or r.get("name") or "Untitled"
                        year = (r.get("release_date") or r.get("first_air_date") or "")[:4]
                        label = f"{name} ({year})" if year else name
                        buttons.append([InlineKeyboardButton(
                            label[:64], callback_data=f"imdbsel:{r['media_type']}:{r['id']}"
                        )])
                    await update.message.reply_text(
                        "🍿 Here is what I found:", reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return
            except Exception:
                logger.exception("tmdb_search_failed")

        if key:
            url = f"https://www.omdbapi.com/?t={quote(title)}&apikey={key}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    data = await resp.json()
                if data.get("Response") == "True":
                    await update.message.reply_text(
                        f"🎬 <b>{escape_html(data.get('Title',''))}</b> ({data.get('Year','')})\n"
                        f"{escape_html(data.get('Rated','') or '')} · {escape_html(data.get('Runtime','') or '')}\n"
                        f"⭐ IMDb {data.get('imdbRating','—')} · {escape_html(data.get('Genre','') or '')}\n\n"
                        f"{escape_html(data.get('Plot','') or '')}\n\n"
                        f"<i>{escape_html(data.get('Director','') or '')}</i>",
                        parse_mode="HTML",
                    )
                    return
            except Exception:
                logger.exception("omdb_failed")

        # Wikipedia fallback (opensearch + extract)
        try:
            api = (
                "https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&namespace=0"
                f"&format=json&search={quote(title)}"
            )
            async with session.get(api, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
            if len(data) >= 4 and data[1]:
                wiki_title = data[1][0]
                wiki_url = data[3][0] if data[3] else ""
                # Pull a short extract
                extract = ""
                try:
                    ext_api = (
                        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
                        f"&exintro=1&explaintext=1&exchars=400&format=json&titles={quote(wiki_title)}"
                    )
                    async with session.get(ext_api, timeout=aiohttp.ClientTimeout(total=8)) as er:
                        ed = await er.json()
                    pages = (ed.get("query") or {}).get("pages") or {}
                    for pg in pages.values():
                        extract = (pg.get("extract") or "").strip()
                        if extract:
                            break
                except Exception:
                    pass
                body = extract or (data[2][0] if data[2] else "")
                await update.message.reply_text(
                    f"🎬 <b>{escape_html(wiki_title)}</b>\n\n"
                    f"{escape_html(body[:500])}\n\n"
                    f"{wiki_url}",
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                return
        except Exception:
            logger.exception("wiki_imdb_fallback_failed")

    await update.message.reply_text(
        "❌ No results found.\n"
        "Tip: set <code>OMDB_API_KEY</code> in .env for richer IMDb data "
        "(free key at omdbapi.com)."
    )


@safe_handler
async def imdb_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a tap on a /imdb TMDB result: fetch that title's details and show a poster card."""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("movie", "tv") or not parts[2].isdigit():
        await query.edit_message_text("❌ Invalid selection.")
        return
    _, media_type, tmdb_id = parts
    tmdb_key = getattr(settings, "tmdb_api_key", None)
    if not tmdb_key:
        await query.edit_message_text("❌ TMDB is no longer configured.")
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                params={"api_key": tmdb_key},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                data = await resp.json()
        if not data.get("id"):
            await query.edit_message_text("❌ Couldn't load details for that result.")
            return

        name = data.get("title") or data.get("name") or "Untitled"
        year = (data.get("release_date") or data.get("first_air_date") or "—")[:4]
        runtime = data.get("runtime") or (data.get("episode_run_time") or [None])[0]
        genres = ", ".join(g["name"] for g in data.get("genres", []))
        rating = data.get("vote_average")
        overview = data.get("overview") or "No synopsis available."
        poster_path = data.get("poster_path")

        caption = (
            f"🎬 <b>{escape_html(name)}</b> ({year})\n"
            + (f"⏱ {runtime} min\n" if runtime else "")
            + (f"⭐ {rating:.1f}/10\n" if rating else "")
            + (f"🎭 {escape_html(genres)}\n" if genres else "")
            + f"\n{escape_html(overview)}"
        )
        if poster_path:
            await query.message.reply_photo(
                f"https://image.tmdb.org/t/p/w500{poster_path}", caption=caption[:1024], parse_mode="HTML"
            )
            await query.delete_message()
        else:
            await query.edit_message_text(caption[:4096], parse_mode="HTML")
    except Exception as e:
        logger.exception("imdb_callback_failed")
        await query.edit_message_text(user_facing_error(e, fallback="❌ Couldn't load that title's details."))


# ─── Calculator ─────────────────────────────────────────────────────────────

@safe_handler
async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /calc &lt;expression&gt;\nExample: /calc (3+5)*2", parse_mode="HTML")
        return
    expr = " ".join(context.args)
    try:
        result = _safe_calc(expr)
        if result == int(result):
            result = int(result)
        await update.message.reply_text(f"🧮 <code>{escape_html(expr)}</code> = <b>{result}</b>", parse_mode="HTML")
    except ZeroDivisionError:
        await update.message.reply_text("❌ Division by zero.")
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid expression: {escape_html(str(e))}", parse_mode="HTML")


# ─── Tag all ────────────────────────────────────────────────────────────────

@admin_only
@group_only
@safe_handler
async def tagall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mention active members of this chat (from ChatMember activity)."""
    chat = update.effective_chat
    note = " ".join(context.args) if context.args else "👋 Attention everyone!"

    async with async_session() as session:
        rows = (
            await session.execute(
                select(ChatMember.user_id, User.first_name, User.username)
                .join(User, User.id == ChatMember.user_id)
                .where(ChatMember.chat_id == chat.id)
                .order_by(desc(ChatMember.xp))
                .limit(40)
            )
        ).all()

    if not rows:
        await update.message.reply_text(
            "No active members tracked yet — chat a bit first so the bot learns who's here."
        )
        return

    mentions = []
    for uid, name, username in rows:
        label = name or username or str(uid)
        mentions.append(mention_html(uid, label))

    # Telegram message limit — chunk if needed
    header = f"📢 {escape_html(note)}\n\n"
    chunk = header
    messages = []
    for m in mentions:
        if len(chunk) + len(m) + 2 > 3500:
            messages.append(chunk)
            chunk = header
        chunk += m + " "
    if chunk.strip():
        messages.append(chunk)

    for msg in messages[:3]:  # hard cap 3 messages
        await update.message.reply_text(msg, parse_mode="HTML")


# ─── Dice / fun hub ─────────────────────────────────────────────────────────

@safe_handler
async def dice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_dice(emoji="🎲")


@safe_handler
async def fun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 <b>Fun commands</b>\n\n"
        "/meme · /quote · /wish · /couples · /dice\n"
        "/8ball · /joke · /karma · /afk\n"
        "/coinflip · /rps · /slots · /tictactoe · /trivia",
        parse_mode="HTML",
    )


@safe_handler
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hub pointing at existing stats commands."""
    await update.message.reply_text(
        "📊 <b>Stats</b>\n\n"
        "/mystats · /botstats · /globalstats\n"
        "/level · /leaderboard · /balance · /topcatchers\n"
        "/cstats — collector-wide",
        parse_mode="HTML",
    )


def register(app):
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler(["tr", "translate"], tr_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("imdb", imdb_cmd))
    app.add_handler(CallbackQueryHandler(imdb_callback, pattern=r"^imdbsel:"))
    app.add_handler(CommandHandler(["calc", "calculate"], calc_cmd))
    app.add_handler(CommandHandler(["tagall", "all", "mentionall"], tagall_cmd))
    app.add_handler(CommandHandler("dice", dice_cmd))
    app.add_handler(CommandHandler("fun", fun_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
