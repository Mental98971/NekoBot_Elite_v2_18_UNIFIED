"""
Multi-platform music source resolution — ported from YukkiMusicBot's
platforms/ directory (Spotify.py, Apple.py, Resso.py), adapted to reuse
this project's existing HTTP client (httpx) and HTML parser
(BeautifulSoup, already a declared dependency that nothing previously
used) instead of adding aiohttp/youtubesearchpython as new ones.

Spotify/Apple Music/Resso links resolve to metadata (title + artist) via
their own public API or page metadata, then get searched on YouTube —
their actual audio is never fetched or streamed, only used to identify
the track. This is the same approach the original harmony-music-bot
engine already used for plain-text search, just extended to more entry
points. SoundCloud needs no special-casing here: yt-dlp streams it
directly, and music_live.py's _resolve_track() already passes any
http(s) URL straight through to yt-dlp.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import httpx

from bot.config import settings
from bot.utils.logger import get_logger
from bot.utils.retry import with_retry

logger = get_logger(__name__)

SPOTIFY_RE = re.compile(r"^https?://open\.spotify\.com/")
APPLE_RE = re.compile(r"^https?://music\.apple\.com/")
RESSO_RE = re.compile(r"^https?://m\.resso\.com/")

_spotify_token_cache: dict = {"token": None, "expires_at": 0.0}


def detect_platform(url: str) -> Optional[str]:
    """Returns 'spotify' / 'apple' / 'resso' for links this module
    resolves specially, or None for anything yt-dlp already handles
    natively (YouTube, SoundCloud, direct audio URLs, ...)."""
    if SPOTIFY_RE.match(url):
        return "spotify"
    if APPLE_RE.match(url):
        return "apple"
    if RESSO_RE.match(url):
        return "resso"
    return None


async def _spotify_token() -> Optional[str]:
    """Client-credentials OAuth flow — app-level access only, no user
    login, and no access to anyone's actual Spotify audio/library."""
    if not (settings.spotify_client_id and settings.spotify_client_secret):
        return None
    if _spotify_token_cache["token"] and time.time() < _spotify_token_cache["expires_at"]:
        return _spotify_token_cache["token"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:

            @with_retry()
            async def _post():
                return await client.post(
                    "https://accounts.spotify.com/api/token",
                    data={"grant_type": "client_credentials"},
                    auth=(settings.spotify_client_id, settings.spotify_client_secret.get_secret_value()),
                )

            resp = await _post()
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.error("spotify_token_failed", exc_info=True)
        return None

    _spotify_token_cache["token"] = data["access_token"]
    _spotify_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    return _spotify_token_cache["token"]


async def resolve_spotify(url: str) -> Optional[str]:
    """Returns a YouTube-searchable 'title artist' string for a Spotify
    track link, or None if it can't be resolved (missing credentials,
    a non-track link like an album/playlist, or a lookup failure)."""
    if "/track/" not in url:
        return None  # playlists/albums aren't handled by this single-track resolver
    token = await _spotify_token()
    if not token:
        return None

    track_id = url.split("/track/")[-1].split("?")[0]
    try:
        async with httpx.AsyncClient(timeout=10) as client:

            @with_retry()
            async def _get():
                return await client.get(
                    f"https://api.spotify.com/v1/tracks/{track_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )

            resp = await _get()
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        logger.error("spotify_track_lookup_failed", exc_info=True)
        return None

    artist = data["artists"][0]["name"] if data.get("artists") else ""
    query = f"{data.get('name', '')} {artist}".strip()
    return query or None


async def _resolve_via_og_title(url: str) -> Optional[str]:
    """Shared helper for Apple Music / Resso: both just need the page's
    og:title meta tag as a YouTube search query."""
    try:
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:

            @with_retry()
            async def _get():
                return await client.get(url, headers={"User-Agent": "Mozilla/5.0"})

            resp = await _get()
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        logger.error("og_title_fetch_failed", url=url, exc_info=True)
        return None

    tag = soup.find("meta", property="og:title")
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


async def resolve_apple_music(url: str) -> Optional[str]:
    return await _resolve_via_og_title(url)


async def resolve_resso(url: str) -> Optional[str]:
    return await _resolve_via_og_title(url)


async def resolve_to_search_query(url: str) -> Optional[str]:
    """Single entry point for music_live.py: given any URL, return a
    YouTube-searchable string if it's a platform this module resolves
    specially, else None (meaning: hand the URL to yt-dlp directly —
    covers YouTube, SoundCloud, and anything else yt-dlp supports)."""
    platform = detect_platform(url)
    if platform == "spotify":
        return await resolve_spotify(url)
    if platform == "apple":
        return await resolve_apple_music(url)
    if platform == "resso":
        return await resolve_resso(url)
    return None
