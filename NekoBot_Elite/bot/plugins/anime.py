"""
Anime / manga / manhwa / manhua / novel / movie / character lookup
— AniList (GraphQL) primary + MyAnimeList via Jikan fallback.

Core:
  /anime /manga /manhwa /manhua /novel /movie /character (/char)
  /seasonal /topanime /topmanga /topcharacters /relations
  /animeid /malid

Discovery:
  /airing /upcoming /anischedule /browse /rec /top /watch /fillers /reverse

AniList user:
  /user /flex /alauth /logout /activity /favourites

Utility:
  /ping (anime latency check)

Both AniList and MAL links shown when available. No API keys required
for public data. /auth stores an AniList username against the Telegram user
(public profile only — no OAuth client secret needed).
"""
from __future__ import annotations

import re
import time as _time
from collections import OrderedDict
from typing import Any, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes

from bot.core.database import User, async_session
from bot.core.errors import safe_handler
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_TTL = 900.0
_CACHE_MAX = 256
_anilist_cache: OrderedDict = OrderedDict()
_jikan_cache: OrderedDict = OrderedDict()


def _cache_get(store: OrderedDict, key: str):
    item = store.get(key)
    if not item:
        return None
    val, exp = item
    if _time.time() > exp:
        store.pop(key, None)
        return None
    store.move_to_end(key)
    return val


def _cache_set(store: OrderedDict, key: str, val) -> None:
    store[key] = (val, _time.time() + _CACHE_TTL)
    store.move_to_end(key)
    while len(store) > _CACHE_MAX:
        store.popitem(last=False)


ANILIST_URL = "https://graphql.anilist.co"
JIKAN_BASE = "https://api.jikan.moe/v4"

ANIME_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id idMal
    title { romaji english native }
    description episodes duration status format averageScore
    genres coverImage { large } siteUrl
    relations {
      edges {
        relationType
        node { id idMal title { romaji english } type format status siteUrl }
      }
    }
  }
}
"""

MANGA_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
    id idMal
    title { romaji english native }
    description chapters volumes status format averageScore
    genres coverImage { large } siteUrl
    relations {
      edges {
        relationType
        node { id idMal title { romaji english } type format status siteUrl }
      }
    }
  }
}
"""

CHAR_QUERY = """
query ($search: String) {
  Character(search: $search) {
    id name { full native } description image { large } siteUrl
    media(perPage: 6) { nodes { title { romaji english } type format siteUrl } }
  }
}
"""

MEDIA_BY_ID_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id idMal title { romaji english native } description
    episodes chapters volumes duration status format type averageScore
    genres coverImage { large } siteUrl
    relations {
      edges {
        relationType
        node { id idMal title { romaji english } type format status siteUrl }
      }
    }
  }
}
"""

RELATIONS_ONLY_QUERY = """
query ($search: String, $type: MediaType) {
  Media(search: $search, type: $type) {
    id idMal title { romaji english native } siteUrl
    relations {
      edges {
        relationType
        node { id idMal title { romaji english } type format status siteUrl }
      }
    }
  }
}
"""


async def _anilist_request(query: str, variables: dict) -> Optional[dict]:
    cache_key = f"{hash(query)}:{sorted(variables.items())}"
    hit = _cache_get(_anilist_cache, cache_key)
    if hit is not None:
        return hit
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANILIST_URL,
                json={"query": query, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _cache_set(_anilist_cache, cache_key, data)
                    return data
                logger.warning("anilist_http", status=resp.status)
    except Exception as e:
        logger.warning("anilist_request_failed", error=str(e))
    return None


async def _jikan_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    cache_key = f"{path}:{sorted((params or {}).items())}"
    hit = _cache_get(_jikan_cache, cache_key)
    if hit is not None:
        return hit
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{JIKAN_BASE}{path}",
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _cache_set(_jikan_cache, cache_key, data)
                    return data
                logger.warning("jikan_http", status=resp.status, path=path)
    except Exception as e:
        logger.warning("jikan_request_failed", error=str(e))
    return None


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _title_from_media(media: dict) -> str:
    t = media.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or "Unknown"


def _mal_anime_url(mal_id) -> Optional[str]:
    return f"https://myanimelist.net/anime/{mal_id}" if mal_id else None


def _mal_manga_url(mal_id) -> Optional[str]:
    return f"https://myanimelist.net/manga/{mal_id}" if mal_id else None


def _mal_character_url(mal_id) -> Optional[str]:
    return f"https://myanimelist.net/character/{mal_id}" if mal_id else None


def _link_buttons(*, anilist_url: Optional[str] = None, mal_url: Optional[str] = None):
    row = []
    if anilist_url:
        row.append(InlineKeyboardButton("AniList", url=anilist_url))
    if mal_url:
        row.append(InlineKeyboardButton("MyAnimeList", url=mal_url))
    return InlineKeyboardMarkup([row]) if row else None


def _format_score(score) -> str:
    if score is None:
        return "—"
    try:
        return f"{int(score)}/100"
    except (TypeError, ValueError):
        return str(score)


def _format_status(status: Optional[str]) -> str:
    if not status:
        return "Unknown"
    return status.replace("_", " ").title()


async def _search_anime_anilist(search: str) -> Optional[dict]:
    data = await _anilist_request(ANIME_QUERY, {"search": search})
    return (data or {}).get("data", {}).get("Media") if data else None


async def _search_anime_mal(search: str, *, media_type: str = "tv") -> Optional[dict]:
    params: dict[str, Any] = {"q": search, "limit": 1}
    if media_type:
        params["type"] = media_type
    data = await _jikan_get("/anime", params)
    if not data or not data.get("data"):
        return None
    item = data["data"][0]
    return {
        "id": None, "idMal": item.get("mal_id"),
        "title": {"english": item.get("title_english") or item.get("title"), "romaji": item.get("title"), "native": item.get("title_japanese")},
        "description": item.get("synopsis"), "episodes": item.get("episodes"), "status": item.get("status"),
        "format": (item.get("type") or "").upper(),
        "averageScore": int(item["score"] * 10) if item.get("score") else None,
        "genres": [g["name"] for g in (item.get("genres") or [])],
        "coverImage": {"large": (item.get("images") or {}).get("jpg", {}).get("large_image_url")},
        "siteUrl": None, "source": "mal",
    }


async def _search_manga_anilist(search: str) -> Optional[dict]:
    data = await _anilist_request(MANGA_QUERY, {"search": search})
    return (data or {}).get("data", {}).get("Media") if data else None


async def _search_manga_mal(search: str, *, media_type: Optional[str] = None) -> Optional[dict]:
    params: dict[str, Any] = {"q": search, "limit": 1}
    if media_type:
        params["type"] = media_type
    data = await _jikan_get("/manga", params)
    if not data or not data.get("data"):
        return None
    item = data["data"][0]
    return {
        "id": None, "idMal": item.get("mal_id"),
        "title": {"english": item.get("title_english") or item.get("title"), "romaji": item.get("title"), "native": item.get("title_japanese")},
        "description": item.get("synopsis"), "chapters": item.get("chapters"), "volumes": item.get("volumes"),
        "status": item.get("status"), "format": (item.get("type") or "").upper(),
        "averageScore": int(item["score"] * 10) if item.get("score") else None,
        "genres": [g["name"] for g in (item.get("genres") or [])],
        "coverImage": {"large": (item.get("images") or {}).get("jpg", {}).get("large_image_url")},
        "siteUrl": None, "source": "mal",
    }


async def _search_character_anilist(search: str) -> Optional[dict]:
    data = await _anilist_request(CHAR_QUERY, {"search": search})
    return (data or {}).get("data", {}).get("Character") if data else None


async def _search_character_mal(search: str) -> Optional[dict]:
    data = await _jikan_get("/characters", {"q": search, "limit": 1})
    if not data or not data.get("data"):
        return None
    item = data["data"][0]
    return {
        "id": None, "idMal": item.get("mal_id"),
        "name": {"full": item.get("name"), "native": item.get("name_kanji")},
        "description": item.get("about"),
        "image": {"large": (item.get("images") or {}).get("jpg", {}).get("image_url")},
        "siteUrl": None, "source": "mal",
    }


def _build_media_caption(media: dict, *, kind: str = "Anime") -> str:
    title = _title_from_media(media)
    score = _format_score(media.get("averageScore"))
    status = _format_status(media.get("status"))
    genres = ", ".join((media.get("genres") or [])[:6]) or "—"
    fmt = media.get("format") or kind
    lines = [f"<b>🎌 {escape_html(title)}</b>", f"📊 Score: {score}"]
    if media.get("episodes") is not None:
        lines.append(f"📺 Episodes: {media['episodes']} | Status: {status}")
    elif media.get("chapters") is not None:
        vols = media.get("volumes")
        lines.append(f"📖 Chapters: {media['chapters']}" + (f" | Volumes: {vols}" if vols else "") + f" | Status: {status}")
    else:
        lines.append(f"📌 Status: {status}")
    if fmt:
        lines.append(f"🏷️ Format: {escape_html(str(fmt))}")
    lines.append(f"🎭 Genres: {escape_html(genres)}")
    desc_raw = _strip_html(media.get("description") or "")
    if desc_raw:
        desc = (desc_raw[:450] + "…") if len(desc_raw) > 450 else desc_raw
        lines.append("")
        lines.append(escape_html(desc))
    return "\n".join(lines)


async def _send_media(update: Update, media: dict, caption: str, buttons) -> None:
    """Send a media card: cover photo when available, plain text if the photo
    is missing, too large, or Telegram rejects the (remote) URL."""
    image = (media.get("coverImage") or {}).get("large")
    if image and len(caption) <= 1024:
        try:
            await update.message.reply_photo(image, caption=caption, parse_mode="HTML", reply_markup=buttons)
            return
        except Exception:
            pass
    await update.message.reply_text(caption[:4096], parse_mode="HTML", reply_markup=buttons)


@safe_handler
async def anime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /anime <title>")
        return
    search = " ".join(context.args)
    media = await _search_anime_anilist(search)
    from_mal = False
    if not media:
        media = await _search_anime_mal(search)
        from_mal = True
    if not media:
        await update.message.reply_text("❌ No results on AniList or MyAnimeList.")
        return
    caption = _build_media_caption(media, kind="Anime")
    buttons = _link_buttons(anilist_url=None if from_mal else media.get("siteUrl"), mal_url=_mal_anime_url(media.get("idMal")))
    await _send_media(update, media, caption, buttons)


@safe_handler
async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /manga <title>")
        return
    search = " ".join(context.args)
    media = await _search_manga_anilist(search)
    from_mal = False
    if not media:
        media = await _search_manga_mal(search)
        from_mal = True
    if not media:
        await update.message.reply_text("❌ No results on AniList or MyAnimeList.")
        return
    caption = _build_media_caption(media, kind="Manga")
    buttons = _link_buttons(anilist_url=None if from_mal else media.get("siteUrl"), mal_url=_mal_manga_url(media.get("idMal")))
    await _send_media(update, media, caption, buttons)


@safe_handler
async def manhwa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /manhwa <title>")
        return
    search = " ".join(context.args)
    media = await _search_manga_mal(search, media_type="manhwa") or await _search_manga_anilist(search)
    if not media:
        await update.message.reply_text("❌ No manhwa found.")
        return
    caption = _build_media_caption(media, kind="Manhwa")
    buttons = _link_buttons(anilist_url=media.get("siteUrl"), mal_url=_mal_manga_url(media.get("idMal")))
    await _send_media(update, media, caption, buttons)


@safe_handler
async def manhua_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /manhua <title>")
        return
    search = " ".join(context.args)
    media = await _search_manga_mal(search, media_type="manhua") or await _search_manga_anilist(search)
    if not media:
        await update.message.reply_text("❌ No manhua found.")
        return
    caption = _build_media_caption(media, kind="Manhua")
    buttons = _link_buttons(anilist_url=media.get("siteUrl"), mal_url=_mal_manga_url(media.get("idMal")))
    await _send_media(update, media, caption, buttons)


@safe_handler
async def novel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /novel <title>")
        return
    search = " ".join(context.args)
    media = await _search_manga_mal(search, media_type="novel") or await _search_manga_anilist(search)
    if not media:
        await update.message.reply_text("❌ No novel found.")
        return
    caption = _build_media_caption(media, kind="Novel")
    buttons = _link_buttons(anilist_url=media.get("siteUrl"), mal_url=_mal_manga_url(media.get("idMal")))
    await _send_media(update, media, caption, buttons)


@safe_handler
async def movie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /movie <title>")
        return
    search = " ".join(context.args)
    media = await _search_anime_mal(search, media_type="movie") or await _search_anime_anilist(search)
    if not media:
        await update.message.reply_text("❌ No movie found.")
        return
    caption = _build_media_caption(media, kind="Movie")
    buttons = _link_buttons(anilist_url=media.get("siteUrl"), mal_url=_mal_anime_url(media.get("idMal")))
    await _send_media(update, media, caption, buttons)


@safe_handler
async def character_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /character <name>")
        return
    search = " ".join(context.args)
    char = await _search_character_anilist(search)
    from_mal = False
    if not char:
        char = await _search_character_mal(search)
        from_mal = True
    if not char:
        await update.message.reply_text("❌ No results on AniList or MyAnimeList.")
        return
    name = (char.get("name") or {}).get("full") or search
    desc_raw = _strip_html(char.get("description") or "")
    desc = (desc_raw[:500] + "…") if len(desc_raw) > 500 else (desc_raw or "No info.")
    buttons = _link_buttons(anilist_url=None if from_mal else char.get("siteUrl"), mal_url=_mal_character_url(char.get("idMal")))
    image = (char.get("image") or {}).get("large")
    caption = f"<b>🎭 {escape_html(name)}</b>\n\n{escape_html(desc)}"
    await _send_media(update, {"coverImage": {"large": image}}, caption, buttons)


@safe_handler
async def seasonal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await _jikan_get("/seasons/now", {"limit": 10})
    if not data or not data.get("data"):
        await update.message.reply_text("🌸 Seasonal list unavailable. Try /anime <title>.")
        return
    lines = ["<b>🌸 Currently airing (MyAnimeList)</b>\n"]
    buttons_rows = []
    for item in data["data"][:10]:
        title = item.get("title_english") or item.get("title") or "?"
        score = item.get("score") or "—"
        mal_id = item.get("mal_id")
        lines.append(f"• <b>{escape_html(title)}</b> — {score}")
        if mal_id:
            buttons_rows.append([InlineKeyboardButton(f"MAL · {title[:28]}", url=f"https://myanimelist.net/anime/{mal_id}")])
    kb = InlineKeyboardMarkup(buttons_rows[:8]) if buttons_rows else None
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@safe_handler
async def topanime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args:
        try:
            page = max(1, int(context.args[0]))
        except ValueError:
            pass
    data = await _jikan_get("/top/anime", {"page": page, "limit": 10})
    if not data or not data.get("data"):
        await update.message.reply_text("❌ Top anime list unavailable.")
        return
    lines = [f"<b>🏆 Top Anime (page {page})</b>\n"]
    for i, item in enumerate(data["data"][:10], start=(page - 1) * 10 + 1):
        title = item.get("title_english") or item.get("title") or "?"
        score = item.get("score") or "—"
        lines.append(f"{i}. <b>{escape_html(title)}</b> — {score}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def topmanga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args:
        try:
            page = max(1, int(context.args[0]))
        except ValueError:
            pass
    data = await _jikan_get("/top/manga", {"page": page, "limit": 10})
    if not data or not data.get("data"):
        await update.message.reply_text("❌ Top manga list unavailable.")
        return
    lines = [f"<b>🏆 Top Manga (page {page})</b>\n"]
    for i, item in enumerate(data["data"][:10], start=(page - 1) * 10 + 1):
        title = item.get("title_english") or item.get("title") or "?"
        score = item.get("score") or "—"
        lines.append(f"{i}. <b>{escape_html(title)}</b> — {score}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def relations_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /relations <title>")
        return
    search = " ".join(context.args)
    data = await _anilist_request(RELATIONS_ONLY_QUERY, {"search": search, "type": "ANIME"})
    media = (data or {}).get("data", {}).get("Media") if data else None
    if not media:
        data = await _anilist_request(RELATIONS_ONLY_QUERY, {"search": search, "type": "MANGA"})
        media = (data or {}).get("data", {}).get("Media") if data else None
    if not media:
        await update.message.reply_text("❌ No relations found for that title.")
        return
    title = _title_from_media(media)
    edges = (media.get("relations") or {}).get("edges") or []
    if not edges:
        await update.message.reply_text(f"<b>Relations for {escape_html(title)}</b>\n\nNo related entries.", parse_mode="HTML")
        return
    groups: dict[str, list] = {}
    for edge in edges:
        rtype = (edge.get("relationType") or "OTHER").replace("_", " ").title()
        groups.setdefault(rtype, []).append(edge.get("node") or {})
    lines = [f"<b>🔗 Relations for {escape_html(title)}</b>\n"]
    buttons_rows = []
    idx = 1
    order = ["Prequel", "Sequel", "Side Story", "Parent", "Adaptation", "Alternative", "Character", "Summary", "Other"]
    for rtype in order + [k for k in groups if k not in order]:
        nodes = groups.get(rtype)
        if not nodes:
            continue
        lines.append(f"<b>{escape_html(rtype)}</b>")
        for node in nodes:
            ntitle = _title_from_media(node)
            ntype = node.get("type") or node.get("format") or ""
            status = _format_status(node.get("status"))
            lines.append(f"  {idx}. {escape_html(ntitle)} ({ntype}) — {status}")
            url = node.get("siteUrl")
            if url:
                buttons_rows.append([InlineKeyboardButton(f"{idx}. {ntitle[:30]}", url=url)])
            idx += 1
        lines.append("")
    kb = InlineKeyboardMarkup(buttons_rows[:20]) if buttons_rows else None
    text = "\n".join(lines).strip()
    if len(text) > 3900:
        text = text[:3900] + "…"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


@safe_handler
async def animeid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /animeid <anilist_id>")
        return
    aid = int(context.args[0])
    data = await _anilist_request(MEDIA_BY_ID_QUERY, {"id": aid})
    media = (data or {}).get("data", {}).get("Media") if data else None
    if not media:
        await update.message.reply_text("❌ Media not found.")
        return
    caption = _build_media_caption(media)
    buttons = _link_buttons(
        anilist_url=media.get("siteUrl"),
        mal_url=_mal_anime_url(media.get("idMal")) if media.get("type") == "ANIME" else _mal_manga_url(media.get("idMal")),
    )
    await _send_media(update, media, caption, buttons)


@safe_handler
async def malid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /malid <mal_id> [anime|manga]")
        return
    mid = int(context.args[0])
    kind = (context.args[1].lower() if len(context.args) > 1 else "anime")
    path = f"/anime/{mid}" if kind != "manga" else f"/manga/{mid}"
    data = await _jikan_get(path)
    if not data or not data.get("data"):
        await update.message.reply_text("❌ MAL entry not found.")
        return
    item = data["data"]
    media = {
        "id": None, "idMal": item.get("mal_id"),
        "title": {"english": item.get("title_english") or item.get("title"), "romaji": item.get("title"), "native": item.get("title_japanese")},
        "description": item.get("synopsis"), "episodes": item.get("episodes"), "chapters": item.get("chapters"),
        "volumes": item.get("volumes"), "status": item.get("status"), "format": (item.get("type") or "").upper(),
        "averageScore": int(item["score"] * 10) if item.get("score") else None,
        "genres": [g["name"] for g in (item.get("genres") or [])],
        "coverImage": {"large": (item.get("images") or {}).get("jpg", {}).get("large_image_url")},
        "siteUrl": None,
    }
    caption = _build_media_caption(media)
    url = _mal_anime_url(mid) if kind != "manga" else _mal_manga_url(mid)
    buttons = _link_buttons(mal_url=url)
    await _send_media(update, media, caption, buttons)



# ─── Extra AniList / Jikan queries ───────────────────────────────────────────

AIRING_QUERY = """
query ($page: Int) {
  Page(page: $page, perPage: 10) {
    media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC) {
      id idMal title { romaji english } episodes nextAiringEpisode { episode timeUntilAiring }
      averageScore coverImage { large } siteUrl
    }
  }
}
"""

UPCOMING_QUERY = """
query ($page: Int) {
  Page(page: $page, perPage: 10) {
    media(type: ANIME, status: NOT_YET_RELEASED, sort: POPULARITY_DESC) {
      id idMal title { romaji english } startDate { year month day }
      averageScore coverImage { large } siteUrl
    }
  }
}
"""

SCHEDULE_QUERY = """
query ($page: Int, $airingAt_greater: Int, $airingAt_lesser: Int) {
  Page(page: $page, perPage: 20) {
    airingSchedules(airingAt_greater: $airingAt_greater, airingAt_lesser: $airingAt_lesser, sort: TIME) {
      episode airingAt
      media { id idMal title { romaji english } siteUrl coverImage { large } }
    }
  }
}
"""

TOP_CHARS_QUERY = """
query ($page: Int) {
  Page(page: $page, perPage: 10) {
    characters(sort: FAVOURITES_DESC) {
      id name { full } favourites image { large } siteUrl
    }
  }
}
"""

RECS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id title { romaji english }
    recommendations(perPage: 10, sort: RATING_DESC) {
      nodes {
        mediaRecommendation {
          id idMal title { romaji english } averageScore siteUrl coverImage { large }
        }
      }
    }
  }
}
"""

USER_QUERY = """
query ($name: String) {
  User(name: $name) {
    id name about avatar { large } siteUrl
    statistics {
      anime { count meanScore minutesWatched episodesWatched }
      manga { count meanScore chaptersRead volumesRead }
    }
    favourites {
      anime(perPage: 5) { nodes { title { romaji english } siteUrl } }
      manga(perPage: 5) { nodes { title { romaji english } siteUrl } }
      characters(perPage: 5) { nodes { name { full } siteUrl } }
    }
  }
}
"""

USER_ACTIVITY_QUERY = """
query ($userId: Int) {
  Page(page: 1, perPage: 8) {
    activities(userId: $userId, sort: ID_DESC) {
      ... on ListActivity {
        status progress
        media { title { romaji english } type siteUrl }
        createdAt
      }
      ... on TextActivity { text createdAt }
    }
  }
}
"""

GENRE_TOP_QUERY = """
query ($genre: String, $page: Int) {
  Page(page: $page, perPage: 10) {
    media(type: ANIME, genre: $genre, sort: SCORE_DESC) {
      id idMal title { romaji english } averageScore episodes status siteUrl
    }
  }
}
"""

BROWSE_TRENDING_QUERY = """
query ($page: Int) {
  Page(page: $page, perPage: 10) {
    media(type: ANIME, sort: TRENDING_DESC) {
      id idMal title { romaji english } averageScore siteUrl coverImage { large }
    }
  }
}
"""


def _fmt_eta(seconds: int) -> str:
    if seconds is None or seconds < 0:
        return "?"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    return f"{h}h {m}m"


async def _get_linked_anilist_name(user_id: int) -> Optional[str]:
    async with async_session() as session:
        u = await session.get(User, user_id)
        if not u:
            return None
        notes = u.notes or {}
        if isinstance(notes, dict):
            return notes.get("anilist") or notes.get("anilist_username")
    return None


async def _set_linked_anilist_name(user_id: int, name: Optional[str], tg_user) -> None:
    async with async_session() as session:
        u = await session.get(User, user_id)
        if not u:
            u = User(
                id=user_id,
                username=getattr(tg_user, "username", None),
                first_name=getattr(tg_user, "first_name", None) or "",
            )
            session.add(u)
        notes = dict(u.notes or {}) if isinstance(u.notes, dict) else {}
        if name:
            notes["anilist"] = name
        else:
            notes.pop("anilist", None)
            notes.pop("anilist_username", None)
        u.notes = notes
        await session.commit()


# ─── New commands ────────────────────────────────────────────────────────────

@safe_handler
async def topcharacters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args and context.args[0].isdigit():
        page = max(1, min(10, int(context.args[0])))
    data = await _anilist_request(TOP_CHARS_QUERY, {"page": page})
    chars = ((data or {}).get("data") or {}).get("Page", {}).get("characters") or []
    if not chars:
        # Jikan fallback
        j = await _jikan_get("/top/characters", {"page": page, "limit": 10})
        items = (j or {}).get("data") or []
        if not items:
            await update.message.reply_text("❌ Couldn't load top characters.")
            return
        lines = [f"🏆 <b>Top Characters</b> (MAL · page {page})"]
        for i, c in enumerate(items, 1):
            name = escape_html(c.get("name") or "?")
            url = c.get("url") or ""
            lines.append(f"{i}. <a href=\"{url}\">{name}</a>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
        return
    lines = [f"🏆 <b>Top Characters</b> (AniList · page {page})"]
    for i, c in enumerate(chars, 1):
        name = escape_html((c.get("name") or {}).get("full") or "?")
        favs = c.get("favourites") or 0
        url = c.get("siteUrl") or ""
        lines.append(f"{i}. <a href=\"{url}\">{name}</a> · ❤ {favs:,}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def airing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args and context.args[0].isdigit():
        page = max(1, min(5, int(context.args[0])))
    data = await _anilist_request(AIRING_QUERY, {"page": page})
    media = ((data or {}).get("data") or {}).get("Page", {}).get("media") or []
    if not media:
        # Jikan currently airing
        j = await _jikan_get("/seasons/now", {"page": page, "limit": 10})
        items = (j or {}).get("data") or []
        if not items:
            await update.message.reply_text("❌ Airing list unavailable.")
            return
        lines = [f"📺 <b>Currently Airing</b> (MAL · page {page})"]
        for i, a in enumerate(items, 1):
            title = escape_html(a.get("title") or "?")
            score = a.get("score") or "—"
            url = a.get("url") or ""
            lines.append(f"{i}. <a href=\"{url}\">{title}</a> · ⭐ {score}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
        return
    lines = [f"📺 <b>Currently Airing</b> (AniList · page {page})"]
    for i, m in enumerate(media, 1):
        title = escape_html((m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji") or "?")
        nxt = m.get("nextAiringEpisode") or {}
        ep = nxt.get("episode")
        eta = _fmt_eta(nxt.get("timeUntilAiring"))
        score = _format_score(m.get("averageScore"))
        url = m.get("siteUrl") or ""
        extra = f" · ep {ep} in {eta}" if ep else ""
        lines.append(f"{i}. <a href=\"{url}\">{title}</a> · ⭐ {score}{extra}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def upcoming_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args and context.args[0].isdigit():
        page = max(1, min(5, int(context.args[0])))
    data = await _anilist_request(UPCOMING_QUERY, {"page": page})
    media = ((data or {}).get("data") or {}).get("Page", {}).get("media") or []
    if not media:
        j = await _jikan_get("/seasons/upcoming", {"page": page, "limit": 10})
        items = (j or {}).get("data") or []
        if not items:
            await update.message.reply_text("❌ Upcoming list unavailable.")
            return
        lines = [f"🗓️ <b>Upcoming Anime</b> (MAL · page {page})"]
        for i, a in enumerate(items, 1):
            title = escape_html(a.get("title") or "?")
            url = a.get("url") or ""
            lines.append(f"{i}. <a href=\"{url}\">{title}</a>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
        return
    lines = [f"🗓️ <b>Upcoming Anime</b> (AniList · page {page})"]
    for i, m in enumerate(media, 1):
        title = escape_html((m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji") or "?")
        sd = m.get("startDate") or {}
        date = f"{sd.get('year') or '?'}-{sd.get('month') or '?'}-{sd.get('day') or '?'}"
        url = m.get("siteUrl") or ""
        lines.append(f"{i}. <a href=\"{url}\">{title}</a> · {date}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Today's airing schedule (UTC window ±12h around now)."""
    import time
    now = int(time.time())
    start = now - 6 * 3600
    end = now + 18 * 3600
    data = await _anilist_request(
        SCHEDULE_QUERY,
        {"page": 1, "airingAt_greater": start, "airingAt_lesser": end},
    )
    schedules = ((data or {}).get("data") or {}).get("Page", {}).get("airingSchedules") or []
    if not schedules:
        await update.message.reply_text(
            "📅 No airing schedule entries in the next window.\nTry /airing for currently releasing shows."
        )
        return
    lines = ["📅 <b>Today's Airing Schedule</b>"]
    for s in schedules[:15]:
        media = s.get("media") or {}
        title = escape_html((media.get("title") or {}).get("english") or (media.get("title") or {}).get("romaji") or "?")
        ep = s.get("episode")
        airing_at = s.get("airingAt") or 0
        eta = _fmt_eta(airing_at - now)
        url = media.get("siteUrl") or ""
        lines.append(f"• <a href=\"{url}\">{title}</a> ep {ep} · in {eta}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def rec_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /rec <anime title>")
        return
    search = " ".join(context.args)
    media = await _search_anime_anilist(search)
    if not media:
        await update.message.reply_text("❌ Anime not found on AniList.")
        return
    data = await _anilist_request(RECS_QUERY, {"id": media["id"]})
    nodes = (
        ((data or {}).get("data") or {})
        .get("Media", {})
        .get("recommendations", {})
        .get("nodes")
        or []
    )
    recs = [n.get("mediaRecommendation") for n in nodes if n.get("mediaRecommendation")]
    if not recs:
        await update.message.reply_text("No recommendations found for that title.")
        return
    base = _title_from_media(media)
    lines = [f"✨ <b>Recommendations for {escape_html(base)}</b>"]
    for i, r in enumerate(recs[:10], 1):
        t = escape_html(_title_from_media(r))
        score = _format_score(r.get("averageScore"))
        url = r.get("siteUrl") or ""
        lines.append(f"{i}. <a href=\"{url}\">{t}</a> · ⭐ {score}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def fillers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Filler guidance via relations + status — links to animefillerlist when MAL id known."""
    if not context.args:
        await update.message.reply_text("Usage: /fillers <anime title>")
        return
    search = " ".join(context.args)
    media = await _search_anime_anilist(search) or await _search_anime_mal(search)
    if not media:
        await update.message.reply_text("❌ Anime not found.")
        return
    title = _title_from_media(media)
    mal_id = media.get("idMal")
    lines = [f"🎬 <b>Fillers — {escape_html(title)}</b>"]
    if mal_id:
        # Community filler list (slug approximation)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        lines.append(f'• <a href="https://www.animefillerlist.com/shows/{slug}">Anime Filler List</a> (search if slug mismatches)')
        lines.append(f'• <a href="https://myanimelist.net/anime/{mal_id}">MAL page</a> — check episodes / forums')
    else:
        lines.append("No MAL id available — search the title on animefillerlist.com")
    # Relations can hint canon arcs
    edges = ((media.get("relations") or {}).get("edges")) or []
    arcs = [e for e in edges if (e.get("relationType") or "") in ("SEQUEL", "PREQUEL", "SIDE_STORY", "PARENT")]
    if arcs:
        lines.append("")
        lines.append("<b>Related entries</b> (use /relations for full tree):")
        for e in arcs[:6]:
            node = e.get("node") or {}
            rt = (e.get("relationType") or "").replace("_", " ").title()
            nt = escape_html(_title_from_media(node))
            lines.append(f"• {rt}: {nt}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def reverse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reverse — reply to media for scene-search helpers (trace.moe / SauceNAO); with text, un-styles fancy fonts."""
    reply = update.message.reply_to_message if update.message else None
    photo = None
    if reply:
        if reply.photo:
            photo = reply.photo[-1]
        elif reply.animation:
            photo = reply.animation
        elif reply.video:
            photo = reply.video
        elif reply.document and (reply.document.mime_type or "").startswith("image/"):
            photo = reply.document
    if not photo and context.args:
        # /reverse <styled text> is the fonts feature (un-style fancy text); it owns this
        # command name when text is supplied, so both features coexist without a collision.
        from bot.plugins.fonts import reverse_cmd as _fonts_reverse_cmd
        await _fonts_reverse_cmd(update, context)
        return
    if not photo:
        await update.message.reply_text(
            "🔍 <b>/reverse</b>\n"
            "• Reply to a screenshot / scene → anime source search links\n"
            "• <code>/reverse &lt;styled text&gt;</code> → convert fancy fonts back to plain text\n"
            "Search sites:\n"
            "• <a href=\"https://trace.moe\">trace.moe</a> — anime scene search\n"
            "• <a href=\"https://saucenao.com\">SauceNAO</a> — general image source",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    # We cannot upload user media to third parties without explicit consent / file download complexity;
    # give deep links the user can open after saving the image.
    await update.message.reply_text(
        "🔍 Open the image and drop it here:\n"
        "• <a href=\"https://trace.moe\">trace.moe</a> (best for anime frames)\n"
        "• <a href=\"https://saucenao.com\">SauceNAO</a>\n"
        "• <a href=\"https://iqdb.org\">iqdb.org</a>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@safe_handler
async def top_genre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /top <genre> [page] — top anime by genre/tag via AniList. """
    if not context.args:
        await update.message.reply_text(
            "Usage: /top <genre> [page]\n"
            "Examples: /top Action · /top Romance 2 · /top \"Sci-Fi\""
        )
        return
    # last arg digit => page
    page = 1
    args = list(context.args)
    if args[-1].isdigit():
        page = max(1, min(10, int(args.pop())))
    genre = " ".join(args).strip().title()
    data = await _anilist_request(GENRE_TOP_QUERY, {"genre": genre, "page": page})
    media = ((data or {}).get("data") or {}).get("Page", {}).get("media") or []
    if not media:
        await update.message.reply_text(
            f"❌ No results for genre <b>{escape_html(genre)}</b>. Try Action, Comedy, Drama, Romance, Fantasy, …",
            parse_mode="HTML",
        )
        return
    lines = [f"🏅 <b>Top {escape_html(genre)}</b> (page {page})"]
    for i, m in enumerate(media, 1):
        t = escape_html(_title_from_media(m))
        score = _format_score(m.get("averageScore"))
        url = m.get("siteUrl") or ""
        lines.append(f"{i}. <a href=\"{url}\">{t}</a> · ⭐ {score}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Watch order from AniList relations (prequel chain + main + sequels)."""
    if not context.args:
        await update.message.reply_text("Usage: /watch <anime title>")
        return
    search = " ".join(context.args)
    media = await _search_anime_anilist(search)
    if not media:
        await update.message.reply_text("❌ Anime not found on AniList.")
        return
    # ANIME_QUERY already selects relations, so no second fetch is needed.
    edges = ((media.get("relations") or {}).get("edges")) or []
    prequels, sequels, side, other = [], [], [], []
    for e in edges:
        node = e.get("node") or {}
        if (node.get("type") or "") != "ANIME":
            continue
        rt = e.get("relationType") or ""
        entry = (rt, node)
        if rt == "PREQUEL":
            prequels.append(entry)
        elif rt == "SEQUEL":
            sequels.append(entry)
        elif rt in ("SIDE_STORY", "SPIN_OFF", "ALTERNATIVE"):
            side.append(entry)
        else:
            other.append(entry)
    base = _title_from_media(media)
    lines = [f"🧭 <b>Watch order — {escape_html(base)}</b>", ""]
    step = 1
    for rt, node in prequels:
        lines.append(f"{step}. {_title_from_media(node)} <i>(prequel)</i>")
        step += 1
    lines.append(f"{step}. <b>{escape_html(base)}</b> ← you are here")
    step += 1
    for rt, node in sequels:
        lines.append(f"{step}. {_title_from_media(node)} <i>(sequel)</i>")
        step += 1
    if side:
        lines.append("")
        lines.append("<b>Side / spin-off</b> (optional):")
        for rt, node in side[:8]:
            lines.append(f"• {_title_from_media(node)} ({rt.replace('_',' ').title()})")
    if other:
        lines.append("")
        lines.append("<b>Other related</b>:")
        for rt, node in other[:6]:
            lines.append(f"• {_title_from_media(node)} ({rt.replace('_',' ').title()})")
    lines.append("")
    lines.append("<i>Order is derived from AniList relation edges — verify for complex franchises.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /browse [trending|popular|upcoming|airing] """
    mode = (context.args[0].lower() if context.args else "trending")
    if mode in ("upcoming", "up"):
        return await upcoming_cmd(update, context)
    if mode in ("airing", "releasing", "now"):
        return await airing_cmd(update, context)
    if mode in ("popular", "pop", "top"):
        # reuse topanime
        return await topanime_cmd(update, context)
    # trending default
    data = await _anilist_request(BROWSE_TRENDING_QUERY, {"page": 1})
    media = ((data or {}).get("data") or {}).get("Page", {}).get("media") or []
    if not media:
        await update.message.reply_text("❌ Trending list unavailable. Try /topanime or /airing.")
        return
    lines = ["🔥 <b>Trending Anime</b>"]
    for i, m in enumerate(media, 1):
        t = escape_html(_title_from_media(m))
        score = _format_score(m.get("averageScore"))
        url = m.get("siteUrl") or ""
        lines.append(f"{i}. <a href=\"{url}\">{t}</a> · ⭐ {score}")
    lines.append("\nAlso: /browse airing · /browse upcoming · /browse popular")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip() if context.args else None
    if not name:
        # try linked
        name = await _get_linked_anilist_name(update.effective_user.id)
    if not name:
        await update.message.reply_text(
            "Usage: /user <anilist_username>\nOr /auth <username> first, then /user."
        )
        return
    data = await _anilist_request(USER_QUERY, {"name": name})
    user = ((data or {}).get("data") or {}).get("User")
    if not user:
        await update.message.reply_text(f"❌ AniList user <b>{escape_html(name)}</b> not found.", parse_mode="HTML")
        return
    stats = user.get("statistics") or {}
    anime = stats.get("anime") or {}
    manga = stats.get("manga") or {}
    about = _strip_html(user.get("about") or "")[:200]
    lines = [
        f"👤 <b>{escape_html(user.get('name') or name)}</b>",
        f'<a href="{user.get("siteUrl") or ""}">AniList profile</a>',
    ]
    if about:
        lines.append(escape_html(about))
    lines.append("")
    lines.append(
        f"📺 Anime: {anime.get('count', 0)} · mean {anime.get('meanScore') or '—'} · "
        f"{anime.get('episodesWatched', 0)} eps · {round((anime.get('minutesWatched') or 0)/60/24, 1)} days"
    )
    lines.append(
        f"📖 Manga: {manga.get('count', 0)} · mean {manga.get('meanScore') or '—'} · "
        f"{manga.get('chaptersRead', 0)} ch"
    )
    fav = user.get("favourites") or {}
    for kind, label in (("anime", "Anime favs"), ("manga", "Manga favs"), ("characters", "Char favs")):
        nodes = (fav.get(kind) or {}).get("nodes") or []
        if nodes:
            names = []
            for n in nodes[:5]:
                if kind == "characters":
                    names.append(escape_html((n.get("name") or {}).get("full") or "?"))
                else:
                    names.append(escape_html(_title_from_media(n)))
            lines.append(f"❤ {label}: " + ", ".join(names))
    avatar = (user.get("avatar") or {}).get("large")
    caption = "\n".join(lines)
    if avatar and len(caption) <= 1024:
        try:
            await update.message.reply_photo(avatar, caption=caption, parse_mode="HTML")
            return
        except Exception:
            pass
    await update.message.reply_text(caption, parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def flex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show linked AniList profile (or prompt to /auth)."""
    name = await _get_linked_anilist_name(update.effective_user.id)
    if not name:
        await update.message.reply_text(
            "No AniList linked yet.\nUse <code>/alauth YourAniListUsername</code> then /flex.",
            parse_mode="HTML",
        )
        return
    # reuse user_cmd by temporarily stuffing args
    context.args = [name]
    await user_cmd(update, context)


@safe_handler
async def auth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cur = await _get_linked_anilist_name(update.effective_user.id)
        if cur:
            await update.message.reply_text(
                f"Linked AniList: <b>{escape_html(cur)}</b>\n"
                "Use /logout to unlink, or /alauth &lt;new_username&gt; to change.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "Usage: /alauth &lt;anilist_username&gt;\n"
                "Links your public AniList profile (no password / OAuth required).",
                parse_mode="HTML",
            )
        return
    name = context.args[0].strip().lstrip("@")
    # Verify exists
    data = await _anilist_request(USER_QUERY, {"name": name})
    user = ((data or {}).get("data") or {}).get("User")
    if not user:
        await update.message.reply_text(f"❌ AniList user <b>{escape_html(name)}</b> not found.", parse_mode="HTML")
        return
    await _set_linked_anilist_name(update.effective_user.id, user.get("name") or name, update.effective_user)
    await update.message.reply_text(
        f"✅ Linked AniList <b>{escape_html(user.get('name') or name)}</b>.\n"
        "Try /flex · /activity · /favourites",
        parse_mode="HTML",
    )


@safe_handler
async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur = await _get_linked_anilist_name(update.effective_user.id)
    if not cur:
        await update.message.reply_text("No AniList account linked.")
        return
    await _set_linked_anilist_name(update.effective_user.id, None, update.effective_user)
    await update.message.reply_text(f"Unlinked AniList <b>{escape_html(cur)}</b>.", parse_mode="HTML")


@safe_handler
async def activity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip() if context.args else await _get_linked_anilist_name(update.effective_user.id)
    if not name:
        await update.message.reply_text("Usage: /activity [anilist_username] — or /alauth first.")
        return
    data = await _anilist_request(USER_QUERY, {"name": name})
    user = ((data or {}).get("data") or {}).get("User")
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    act_data = await _anilist_request(USER_ACTIVITY_QUERY, {"userId": user["id"]})
    acts = ((act_data or {}).get("data") or {}).get("Page", {}).get("activities") or []
    if not acts:
        await update.message.reply_text(f"No recent public activity for <b>{escape_html(name)}</b>.", parse_mode="HTML")
        return
    lines = [f"📝 <b>Recent activity — {escape_html(user.get('name') or name)}</b>"]
    for a in acts:
        if not a:
            continue
        if "media" in a and a.get("media"):
            media = a["media"]
            t = escape_html(_title_from_media(media))
            status = escape_html((a.get("status") or "").title())
            prog = a.get("progress") or ""
            lines.append(f"• {status} {prog} — {t}")
        elif a.get("text"):
            lines.append(f"• {escape_html((a.get('text') or '')[:120])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def favourites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip() if context.args else await _get_linked_anilist_name(update.effective_user.id)
    if not name:
        await update.message.reply_text("Usage: /favourites [anilist_username] — or /alauth first.")
        return
    data = await _anilist_request(USER_QUERY, {"name": name})
    user = ((data or {}).get("data") or {}).get("User")
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    fav = user.get("favourites") or {}
    lines = [f"❤ <b>Favourites — {escape_html(user.get('name') or name)}</b>"]
    for kind, label in (("anime", "Anime"), ("manga", "Manga"), ("characters", "Characters")):
        nodes = (fav.get(kind) or {}).get("nodes") or []
        lines.append(f"\n<b>{label}</b>")
        if not nodes:
            lines.append("—")
            continue
        for n in nodes:
            if kind == "characters":
                nm = escape_html((n.get("name") or {}).get("full") or "?")
                url = n.get("siteUrl") or ""
                lines.append(f'• <a href="{url}">{nm}</a>')
            else:
                nm = escape_html(_title_from_media(n))
                url = n.get("siteUrl") or ""
                lines.append(f'• <a href="{url}">{nm}</a>')
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def _probe(method: str, url: str, **kwargs) -> tuple[bool, int]:
    """One uncached request; returns (reachable, elapsed_ms). The response
    caches used by the lookup helpers would make repeated pings report ~0 ms."""
    import time

    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=8), **kwargs) as resp:
                ok = resp.status == 200
    except Exception:
        ok = False
    return ok, int((time.perf_counter() - t0) * 1000)


@safe_handler
async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ping — live reachability + latency of the anime data sources."""
    al_ok, al_ms = await _probe("POST", ANILIST_URL, json={"query": "query { Media(id: 1) { id } }"})
    jk_ok, jk_ms = await _probe("GET", f"{JIKAN_BASE}/anime/1")
    await update.message.reply_text(
        "🏓 <b>Pong</b>\n"
        f"AniList: {'ok' if al_ok else 'down'} ({al_ms} ms)\n"
        f"Jikan: {'ok' if jk_ok else 'down'} ({jk_ms} ms)",
        parse_mode="HTML",
    )



def register(app):
    # Core lookup
    app.add_handler(CommandHandler("anime", anime_cmd))
    app.add_handler(CommandHandler("manga", manga_cmd))
    app.add_handler(CommandHandler("manhwa", manhwa_cmd))
    app.add_handler(CommandHandler("manhua", manhua_cmd))
    app.add_handler(CommandHandler("novel", novel_cmd))
    app.add_handler(CommandHandler("movie", movie_cmd))
    app.add_handler(CommandHandler("character", character_cmd))
    app.add_handler(CommandHandler("char", character_cmd))  # alias
    app.add_handler(CommandHandler("seasonal", seasonal_cmd))
    app.add_handler(CommandHandler("topanime", topanime_cmd))
    app.add_handler(CommandHandler("topmanga", topmanga_cmd))
    app.add_handler(CommandHandler("topcharacters", topcharacters_cmd))
    app.add_handler(CommandHandler("relations", relations_cmd))
    app.add_handler(CommandHandler("animeid", animeid_cmd))
    app.add_handler(CommandHandler("malid", malid_cmd))
    # Discovery
    app.add_handler(CommandHandler("airing", airing_cmd))
    app.add_handler(CommandHandler("upcoming", upcoming_cmd))
    app.add_handler(CommandHandler("anischedule", schedule_cmd))
    app.add_handler(CommandHandler("aschedule", schedule_cmd))
    app.add_handler(CommandHandler("airingschedule", schedule_cmd))
    app.add_handler(CommandHandler("rec", rec_cmd))
    app.add_handler(CommandHandler("fillers", fillers_cmd))
    app.add_handler(CommandHandler("reverse", reverse_cmd))
    app.add_handler(CommandHandler("top", top_genre_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("browse", browse_cmd))
    # AniList user
    app.add_handler(CommandHandler("user", user_cmd))
    app.add_handler(CommandHandler("flex", flex_cmd))
    app.add_handler(CommandHandler("alauth", auth_cmd))
    app.add_handler(CommandHandler("anilist", auth_cmd))
    app.add_handler(CommandHandler("linkanilist", auth_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("activity", activity_cmd))
    app.add_handler(CommandHandler("favourites", favourites_cmd))
    app.add_handler(CommandHandler("favorites", favourites_cmd))  # US spelling
    # Utility
    app.add_handler(CommandHandler("ping", ping_cmd))
