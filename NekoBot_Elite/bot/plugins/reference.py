"""
Reference-lookup plugin: Pokedex, currency conversion, Telegraph publishing, news.

Pokedex, currency and Telegraph use free, keyless public APIs (PokeAPI,
Frankfurter, telegra.ph), so they work out of the box. /news is the one
exception: it needs a NewsAPI key (NEWS_API_KEY) and says so when it is unset.
"""
from __future__ import annotations

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from bot.config import settings
from bot.utils.helpers import escape_html
from bot.core.errors import safe_handler, user_facing_error

POKEAPI_BASE = "https://pokeapi.co/api/v2"
NEWSAPI_BASE = "https://newsapi.org/v2"
FRANKFURTER_BASE = "https://api.frankfurter.app"
TELEGRAPH_BASE = "https://api.telegra.ph"

# Lazily created on first /telegraph use and cached for the process's
# lifetime — telegra.ph accounts are free and disposable, so there is
# nothing to configure and nothing sensitive to persist.
_telegraph_token: str | None = None


@safe_handler
async def pokedex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pokedex <name or number> — real Pokemon data via PokeAPI (free, no key)."""
    if not context.args:
        await update.message.reply_text("Usage: /pokedex <name or number>\nExample: /pokedex pikachu")
        return
    query = context.args[0].lower().strip()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{POKEAPI_BASE}/pokemon/{query}", timeout=aiohttp.ClientTimeout(total=12)
            ) as resp:
                if resp.status == 404:
                    await update.message.reply_text(f"❌ No Pokemon found named/numbered '{escape_html(query)}'.")
                    return
                if resp.status != 200:
                    await update.message.reply_text("❌ PokeAPI is unavailable right now. Try again shortly.")
                    return
                data = await resp.json()

            species_url = data.get("species", {}).get("url")
            flavor_text = ""
            if species_url:
                async with session.get(species_url, timeout=aiohttp.ClientTimeout(total=10)) as sresp:
                    if sresp.status == 200:
                        sdata = await sresp.json()
                        for entry in sdata.get("flavor_text_entries", []):
                            if entry.get("language", {}).get("name") == "en":
                                flavor_text = entry["flavor_text"].replace("\n", " ").replace("\f", " ")
                                break

        name = data.get("name", query).title()
        num = data.get("id", "?")
        types = ", ".join(t["type"]["name"].title() for t in data.get("types", []))
        height_m = (data.get("height", 0) or 0) / 10
        weight_kg = (data.get("weight", 0) or 0) / 10
        stats = data.get("stats", [])
        stat_line = " · ".join(f"{s['stat']['name'].upper()}: {s['base_stat']}" for s in stats[:3])
        sprite = (data.get("sprites") or {}).get("front_default")

        text = (
            f"📱 <b>#{num:04d} {escape_html(name)}</b>\n"
            f"Type: {escape_html(types) or '—'}\n"
            f"Height: {height_m}m · Weight: {weight_kg}kg\n"
        )
        if stat_line:
            text += f"{escape_html(stat_line)}\n"
        if flavor_text:
            text += f"\n<i>{escape_html(flavor_text)}</i>"

        if sprite:
            await update.message.reply_photo(sprite, caption=text, parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(user_facing_error(e, fallback="❌ Couldn't reach the Pokedex right now."))


@safe_handler
async def currency_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/currency <amount> <FROM> <TO> — live exchange rate via Frankfurter (free, no key, ECB-sourced)."""
    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "Usage: /currency <amount> <FROM> <TO>\nExample: /currency 100 USD EUR"
        )
        return
    try:
        amount = float(args[0])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a number.")
        return
    from_cur, to_cur = args[1].upper(), args[2].upper()

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{FRANKFURTER_BASE}/latest?amount={amount}&from={from_cur}&to={to_cur}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await update.message.reply_text(
                        f"❌ Couldn't convert {escape_html(from_cur)} → {escape_html(to_cur)}. "
                        "Check the currency codes (ISO 4217, e.g. USD, EUR, JPY, INR)."
                    )
                    return
                data = await resp.json()
        result = data.get("rates", {}).get(to_cur)
        if result is None:
            await update.message.reply_text(f"❌ Unknown currency code: {escape_html(to_cur)}.")
            return
        await update.message.reply_text(
            f"💱 {amount:,.2f} {escape_html(from_cur)} = <b>{result:,.2f} {escape_html(to_cur)}</b>\n"
            f"<i>Rate date: {data.get('date', '—')} · source: European Central Bank via frankfurter.app</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(user_facing_error(e, fallback="❌ Currency conversion failed. Try again shortly."))


async def _ensure_telegraph_token(session: aiohttp.ClientSession) -> str:
    global _telegraph_token
    if _telegraph_token:
        return _telegraph_token
    async with session.post(
        f"{TELEGRAPH_BASE}/createAccount",
        data={"short_name": "NekoBot"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError("telegraph account creation failed")
    _telegraph_token = data["result"]["access_token"]
    return _telegraph_token


@safe_handler
async def telegraph_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/telegraph <title> | <text>, or reply to a message with /telegraph <title>
    — publish to a free telegra.ph page and get back a real, public URL."""
    raw = " ".join(context.args) if context.args else ""
    reply_text = update.message.reply_to_message.text if update.message.reply_to_message else None

    if "|" in raw:
        title, _, body = raw.partition("|")
        title, body = title.strip(), body.strip()
    elif reply_text:
        title, body = (raw.strip() or "Untitled"), reply_text
    else:
        await update.message.reply_text(
            "Usage: /telegraph <title> | <text>\nOr reply to a message with: /telegraph <title>"
        )
        return

    if not body:
        await update.message.reply_text("❌ Nothing to publish — provide text after '|' or reply to a message.")
        return

    content = [{"tag": "p", "children": [line]} for line in body.split("\n") if line.strip()]
    try:
        async with aiohttp.ClientSession() as session:
            token = await _ensure_telegraph_token(session)
            async with session.post(
                f"{TELEGRAPH_BASE}/createPage",
                json={
                    "access_token": token,
                    "title": title[:256] or "Untitled",
                    "author_name": "NekoBot",
                    "content": content,
                    "return_content": False,
                },
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "unknown telegra.ph error"))
        url = data["result"]["url"]
        await update.message.reply_text(f"📰 Published: {url}")
    except Exception as e:
        await update.message.reply_text(user_facing_error(e, fallback="❌ Couldn't publish to Telegraph right now."))


@safe_handler
async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/news [topic] — five top headlines, or the latest stories about a topic (NewsAPI)."""
    key = settings.news_api_key
    if not key:
        await update.message.reply_text("📰 /news needs a NewsAPI key — the bot owner can set NEWS_API_KEY.")
        return
    topic = " ".join(context.args or []).strip()[:100]
    if topic:
        url = f"{NEWSAPI_BASE}/everything"
        params = {"q": topic, "language": "en", "sortBy": "publishedAt", "pageSize": 5}
        heading = f"📰 <b>News: {escape_html(topic)}</b>"
    else:
        url = f"{NEWSAPI_BASE}/top-headlines"
        params = {"country": "us", "pageSize": 5}
        heading = "📰 <b>Top headlines</b>"
    try:
        async with aiohttp.ClientSession() as session:
            # The key travels in a header, never in the URL, so it can't leak into logs.
            async with session.get(
                url, params=params, headers={"X-Api-Key": key}, timeout=aiohttp.ClientTimeout(total=12)
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
    except Exception as e:
        await update.message.reply_text(user_facing_error(e, fallback="❌ Couldn't reach the news service."))
        return
    if status != 200 or not isinstance(data, dict) or data.get("status") != "ok":
        await update.message.reply_text("❌ The news service didn't return results right now.")
        return
    articles = [
        a for a in (data.get("articles") or [])
        if a.get("title") and str(a.get("url") or "").startswith(("http://", "https://"))
    ][:5]
    if not articles:
        await update.message.reply_text("📰 No stories found.")
        return
    lines = [heading, ""]
    for a in articles:
        source = escape_html((a.get("source") or {}).get("name") or "")
        suffix = f" — {source}" if source else ""
        lines.append(f'• <a href="{escape_html(a["url"])}">{escape_html(a["title"])}</a>{suffix}')
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


def register(app):
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("pokedex", pokedex_cmd))
    app.add_handler(CommandHandler(["currency", "convert"], currency_cmd))
    app.add_handler(CommandHandler("telegraph", telegraph_cmd))
