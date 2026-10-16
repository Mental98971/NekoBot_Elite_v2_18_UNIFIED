"""
Toolbox (v2.16) - utilities from the Saitama / Masha bots that had no counterpart here.

  /paste            text (reply or args) -> dpaste link
  /wiki <query>     Wikipedia summary
  /github <user>    GitHub profile card
  /country <name>   country facts            /covid [country]   COVID-19 totals
  /setme /me        a note about yourself    /setbio /bio       a note others write about a member
  s/old/new[/gi]    reply to a message to correct it (literal replacement, no regex - no ReDoS)
  /encrypt /decrypt authenticated encryption with a passphrase (Fernet + PBKDF2)
  /addrss <url> /removerss <url> /rss     follow feeds; new entries are posted every RSS_INTERVAL seconds

Network calls use short timeouts, never follow redirects blindly and refuse non-public hosts.
"""
from __future__ import annotations

import asyncio
import base64
import html as _html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import delete, func, select
from telegram.error import TelegramError
from telegram.ext import CommandHandler, MessageHandler, filters

from bot.core.database import RssFeed, User, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils import webtools
from bot.utils.helpers import escape_html, resolve_target_user
from bot.utils.logger import get_logger

logger = get_logger(__name__)

HTTP_TIMEOUT = 10.0
MAX_BODY = 1_500_000
RSS_INTERVAL = 600
MAX_FEEDS_PER_CHAT = 10
MAX_NEW_PER_POLL = 3
MAX_FAILURES = 12
NOTE_LIMIT = 300
PBKDF2_ITERATIONS = 200_000
_UA = {"User-Agent": "NekoBot-Toolbox/2.16 (+telegram bot)"}


# ── pure helpers ────────────────────────────────────────────────────────────
_SED = re.compile(r"^s/((?:\\.|[^/\\])+)/((?:\\.|[^/\\])*)(?:/([gi]{0,2}))?$", re.S)


def sed_substitute(command: str, text: str) -> Optional[str]:
    """Apply ``s/old/new/[gi]`` literally. None if ``command`` isn't a substitution or nothing changed."""
    m = _SED.match((command or "").strip())
    if not m or not text:
        return None
    old, new = (re.sub(r"\\(.)", r"\1", g) for g in m.group(1, 2))
    flags = m.group(3) or ""
    if len(old) > 200 or len(new) > 500:
        return None
    if "i" in flags:
        result, n = re.subn(re.escape(old), lambda _m: new, text, count=0 if "g" in flags else 1, flags=re.I)
    else:
        n = text.count(old) if "g" in flags else min(1, text.count(old))
        result = text.replace(old, new) if "g" in flags else text.replace(old, new, 1)
    return result if n and result != text else None


def encrypt_text(passphrase: str, plaintext: str) -> str:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import os
    salt = os.urandom(16)
    key = base64.urlsafe_b64encode(PBKDF2HMAC(hashes.SHA256(), 32, salt, PBKDF2_ITERATIONS).derive(passphrase.encode()))
    return base64.urlsafe_b64encode(salt + Fernet(key).encrypt(plaintext.encode())).decode()


def decrypt_text(passphrase: str, token: str) -> str:
    """Raises ValueError for a wrong passphrase or damaged token."""
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    try:
        raw = base64.urlsafe_b64decode(token.strip().encode())
        salt, body = raw[:16], raw[16:]
        key = base64.urlsafe_b64encode(PBKDF2HMAC(hashes.SHA256(), 32, salt, PBKDF2_ITERATIONS).derive(passphrase.encode()))
        return Fernet(key).decrypt(body).decode()
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise ValueError("wrong passphrase or damaged text") from exc


def parse_feed(xml_bytes: bytes) -> Tuple[str, List[Tuple[str, str, str]]]:
    """(title, [(entry_id, title, link)]) newest first, from RSS 2.0 or Atom. Raises ValueError on bad XML.
    DOCTYPE/entity declarations are rejected (billion-laughs / XXE)."""
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml_bytes[:65536].lower():
        raise ValueError("feeds with DTDs are not supported")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("not valid XML") from exc

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def text_of(el, name):
        for child in el:
            if local(child.tag) == name:
                return (child.text or "").strip()
        return ""

    entries: List[Tuple[str, str, str]] = []
    title = ""
    if local(root.tag) == "rss":
        channel = next((c for c in root if local(c.tag) == "channel"), None)
        if channel is None:
            raise ValueError("RSS without a channel")
        title = text_of(channel, "title")
        for item in channel:
            if local(item.tag) == "item":
                link = text_of(item, "link")
                entries.append((text_of(item, "guid") or link or text_of(item, "title"), text_of(item, "title"), link))
    elif local(root.tag) == "feed":
        title = text_of(root, "title")
        for entry in root:
            if local(entry.tag) == "entry":
                link = next((c.attrib.get("href", "") for c in entry if local(c.tag) == "link" and c.attrib.get("rel", "alternate") == "alternate"), "")
                entries.append((text_of(entry, "id") or link, text_of(entry, "title"), link))
    else:
        raise ValueError("not an RSS or Atom feed")
    return title, [e for e in entries if e[0]]


def new_entries(entries: List[Tuple[str, str, str]], last_entry: Optional[str]) -> List[Tuple[str, str, str]]:
    """Entries newer than ``last_entry`` (feed order is newest first), oldest first, capped at MAX_NEW_PER_POLL.
    A first poll (last_entry None) delivers nothing: it only records the baseline."""
    if last_entry is None:
        return []
    fresh = []
    for e in entries:
        if e[0] == last_entry:
            break
        fresh.append(e)
    else:
        fresh = fresh[:MAX_NEW_PER_POLL]  # last_entry fell off the feed: only announce the newest few
    return list(reversed(fresh[:MAX_NEW_PER_POLL]))


async def _public_url(raw: str) -> Optional[str]:
    """normalize_url + resolve the host and require every address to be public (SSRF guard)."""
    url = webtools.normalize_url(raw)
    host = urlsplit(url).hostname if url else None
    if not url or not host or not webtools.is_public_host(host):
        return None
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not infos or not all(webtools._ip_is_public(ipaddress.ip_address(i[4][0])) for i in infos):
            return None
    except (socket.gaierror, ValueError, OSError):
        return None
    return url


async def _get(url: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=_UA, follow_redirects=False) as client:
        return await client.get(url, **kw)


async def _get_json(url: str, **kw):
    r = await _get(url, **kw)
    if r.status_code != 200 or len(r.content) > MAX_BODY:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _arg_or_reply(update, context) -> str:
    msg = update.effective_message
    text = " ".join(context.args or []).strip()
    reply = getattr(msg, "reply_to_message", None)
    if not text and reply is not None:
        text = (getattr(reply, "text", None) or getattr(reply, "caption", None) or "").strip()
    return text


# ── network utilities ───────────────────────────────────────────────────────
@safe_handler
async def paste_cmd(update, context) -> None:
    """/paste - upload text (reply to a message, or give it inline) to dpaste.org."""
    text = _arg_or_reply(update, context)
    if not text:
        await update.message.reply_text("Reply to a message with /paste, or use /paste <text>.")
        return
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=_UA) as client:
            r = await client.post("https://dpaste.org/api/", data={"content": text[:200_000], "expiry_days": 7})
        link = r.text.strip().strip('"')
        ok = r.status_code in (200, 201) and link.startswith("https://dpaste.org/")
    except httpx.HTTPError:
        ok = False
    await update.message.reply_text(f"📋 {escape_html(link)}" if ok else "❌ The paste service isn't answering right now.", parse_mode="HTML")


@safe_handler
async def wiki_cmd(update, context) -> None:
    """/wiki <query> - Wikipedia summary."""
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("Usage: /wiki <topic>")
        return
    try:
        data = await _get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(query.replace(' ', '_'), safe='')}")
    except httpx.HTTPError:
        data = None
    if not data or data.get("type") == "https://mediawiki.org/wiki/HyperSwitch/errors/not_found" or not data.get("extract"):
        await update.message.reply_text("❌ I couldn't find that on Wikipedia.")
        return
    url = (data.get("content_urls") or {}).get("desktop", {}).get("page", "")
    await update.message.reply_text(
        f"📖 <b>{escape_html(data.get('title', query))}</b>\n{escape_html(webtools.clip(data['extract'], 900))}"
        + (f"\n{escape_html(url)}" if url else ""), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def github_cmd(update, context) -> None:
    """/github <username> - profile card."""
    name = (context.args[0] if context.args else "").strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", name):
        await update.message.reply_text("Usage: /github <username>")
        return
    try:
        d = await _get_json(f"https://api.github.com/users/{name}")
    except httpx.HTTPError:
        d = None
    if not d or "login" not in d:
        await update.message.reply_text("❌ No such GitHub user (or the API is rate-limited).")
        return
    lines = [f"🐙 <b>{escape_html(d.get('name') or d['login'])}</b> (@{escape_html(d['login'])})"]
    if d.get("bio"):
        lines.append(escape_html(webtools.clip(d["bio"], 200)))
    lines.append(f"Repos: {d.get('public_repos', 0)} · Followers: {d.get('followers', 0)} · Following: {d.get('following', 0)}")
    for label, key in (("Company", "company"), ("Location", "location")):
        if d.get(key):
            lines.append(f"{label}: {escape_html(str(d[key]))}")
    lines.append(escape_html(d.get("html_url", "")))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@safe_handler
async def country_cmd(update, context) -> None:
    """/country <name> - facts about a country."""
    name = " ".join(context.args or []).strip()
    if not name:
        await update.message.reply_text("Usage: /country <name>")
        return
    try:
        data = await _get_json(f"https://restcountries.com/v3.1/name/{quote(name, safe='')}", params={"fields": "name,capital,region,subregion,population,area,languages,currencies,flag,borders,cca2"})
    except httpx.HTTPError:
        data = None
    if not isinstance(data, list) or not data:
        await update.message.reply_text("❌ Country not found.")
        return
    c = data[0]
    langs = ", ".join((c.get("languages") or {}).values()) or "-"
    money = ", ".join(v.get("name", k) for k, v in (c.get("currencies") or {}).items()) or "-"
    await update.message.reply_text(
        f"{c.get('flag', '')} <b>{escape_html(c['name'].get('common', name))}</b> ({escape_html(c.get('cca2', ''))})\n"
        f"Capital: {escape_html(', '.join(c.get('capital') or ['-']))}\nRegion: {escape_html(c.get('region', '-'))} / {escape_html(c.get('subregion', '-'))}\n"
        f"Population: {c.get('population', 0):,}\nArea: {c.get('area', 0):,.0f} km²\nLanguages: {escape_html(langs)}\nCurrency: {escape_html(money)}\n"
        f"Borders: {escape_html(', '.join(c.get('borders') or ['-']))}", parse_mode="HTML")


@safe_handler
async def covid_cmd(update, context) -> None:
    """/covid [country] - COVID-19 totals (worldwide by default)."""
    name = " ".join(context.args or []).strip()
    path = f"countries/{quote(name, safe='')}" if name else "all"
    try:
        d = await _get_json(f"https://disease.sh/v3/covid-19/{path}")
    except httpx.HTTPError:
        d = None
    if not d or "cases" not in d:
        await update.message.reply_text("❌ No data for that country.")
        return
    await update.message.reply_text(
        f"🦠 <b>{escape_html(d.get('country', 'Worldwide'))}</b>\nCases: {d['cases']:,} (+{d.get('todayCases', 0):,} today)\n"
        f"Deaths: {d.get('deaths', 0):,} (+{d.get('todayDeaths', 0):,})\nRecovered: {d.get('recovered', 0):,}\nActive: {d.get('active', 0):,}", parse_mode="HTML")


# ── notes about people ──────────────────────────────────────────────────────
async def _get_user(session, user_id: int, first_name: Optional[str] = None) -> User:
    row = await session.get(User, user_id)
    if row is None:
        row = User(id=user_id, first_name=first_name or str(user_id))
        session.add(row)
    return row


async def _set_note(user_id: int, key: str, value: Optional[str], first_name: Optional[str] = None) -> None:
    async with async_session() as session:
        row = await _get_user(session, user_id, first_name)
        notes = dict(row.notes or {})
        if value:
            notes[key] = value
        else:
            notes.pop(key, None)
        row.notes = notes  # reassign: SQLAlchemy doesn't track in-place JSON mutation
        await session.commit()


async def _show_note(update, context, key: str, label: str) -> None:
    target_id, name = await resolve_target_user(update, context)
    if not target_id:
        target_id, name = update.effective_user.id, update.effective_user.first_name
    async with async_session() as session:
        row = await session.get(User, target_id)
    note = ((row.notes or {}).get(key) if row else None)
    who = escape_html(name or (row.first_name if row else None) or str(target_id))
    await update.effective_message.reply_text(
        f"<b>{who}</b>\n{label}: {escape_html(note)}" if note else f"No {label.lower()} set for {who}.", parse_mode="HTML")


@safe_handler
async def setme_cmd(update, context) -> None:
    """/setme <text> - write something about yourself (/setme alone clears it)."""
    text = " ".join(context.args or []).strip()
    if len(text) > NOTE_LIMIT:
        await update.message.reply_text(f"Keep it under {NOTE_LIMIT} characters (you used {len(text)}).")
        return
    await _set_note(update.effective_user.id, "me", text or None, update.effective_user.first_name)
    await update.message.reply_text("✅ Updated." if text else "🧹 Cleared.")


@safe_handler
async def me_cmd(update, context) -> None:
    """/me [user] - what someone wrote about themselves."""
    await _show_note(update, context, "me", "About")


@safe_handler
async def setbio_cmd(update, context) -> None:
    """/setbio <text> - reply to someone to write a bio for them (not for yourself - use /setme)."""
    reply = update.message.reply_to_message
    if reply is None or reply.from_user is None:
        await update.message.reply_text("Reply to the person you want to describe: /setbio <text>.")
        return
    target = reply.from_user
    if target.id == update.effective_user.id:
        await update.message.reply_text("You can't write your own bio - use /setme.")
        return
    if target.is_bot:
        await update.message.reply_text("Bots don't need bios.")
        return
    text = " ".join(context.args or []).strip()
    if len(text) > NOTE_LIMIT:
        await update.message.reply_text(f"Keep it under {NOTE_LIMIT} characters (you used {len(text)}).")
        return
    await _set_note(target.id, "bio", text or None, target.first_name)
    await update.message.reply_text("✅ Bio saved." if text else "🧹 Bio cleared.")


@safe_handler
async def bio_cmd(update, context) -> None:
    """/bio [user] - what others wrote about a member."""
    await _show_note(update, context, "bio", "Bio")


# ── s/old/new ───────────────────────────────────────────────────────────────
@safe_handler
async def sed_listener(update, context) -> None:
    msg = update.effective_message
    reply = getattr(msg, "reply_to_message", None) if msg else None
    if reply is None or not msg.text:
        return
    original = reply.text or reply.caption or ""
    fixed = sed_substitute(msg.text, original)
    if fixed is None:
        return
    who = escape_html(getattr(reply.from_user, "first_name", "") or "someone")
    await reply.reply_text(f"💬 <b>{who}</b> meant to say:\n{escape_html(fixed[:3500])}", parse_mode="HTML")


# ── encryption ──────────────────────────────────────────────────────────────
@safe_handler
async def encrypt_cmd(update, context) -> None:
    """/encrypt <passphrase> <text> (or reply to the text) - the result is safe to post."""
    args = list(context.args or [])
    reply = update.message.reply_to_message
    if not args:
        await update.message.reply_text("Usage: /encrypt <passphrase> <text>  (or reply to a message: /encrypt <passphrase>)")
        return
    passphrase, rest = args[0], " ".join(args[1:]).strip()
    text = rest or ((reply.text or reply.caption or "") if reply else "")
    if not text:
        await update.message.reply_text("Nothing to encrypt.")
        return
    if len(text) > 3000:
        await update.message.reply_text("Too long (max 3000 characters).")
        return
    token = await asyncio.to_thread(encrypt_text, passphrase, text)
    await update.message.reply_text(f"🔐 <code>{token}</code>\nDecrypt with /decrypt <i>passphrase</i> (reply to this).", parse_mode="HTML")
    try:
        await update.message.delete()  # the command message contains the passphrase
    except TelegramError:
        pass


@safe_handler
async def decrypt_cmd(update, context) -> None:
    """/decrypt <passphrase> [token] (or reply to the token)."""
    args = list(context.args or [])
    reply = update.message.reply_to_message
    if not args:
        await update.message.reply_text("Usage: /decrypt <passphrase> <token>  (or reply to the encrypted message)")
        return
    passphrase, rest = args[0], "".join(args[1:]).strip()
    token = rest or (((reply.text or "").split("\n")[0].lstrip("🔐 ").strip()) if reply else "")
    try:
        plain = await asyncio.to_thread(decrypt_text, passphrase, token)
    except ValueError:
        await update.message.reply_text("❌ Wrong passphrase, or that isn't encrypted text.")
        return
    await update.message.reply_text(f"🔓 {escape_html(plain)}", parse_mode="HTML")
    try:
        await update.message.delete()
    except TelegramError:
        pass


# ── RSS ─────────────────────────────────────────────────────────────────────
async def _fetch_feed(url: str) -> Tuple[str, List[Tuple[str, str, str]]]:
    safe = await _public_url(url)
    if safe is None:
        raise ValueError("that address isn't a public web address")
    r = await _get(safe)
    if r.status_code != 200:
        raise ValueError(f"the server answered {r.status_code}")
    if len(r.content) > MAX_BODY:
        raise ValueError("feed is too large")
    return parse_feed(r.content)


@admin_only
@group_only
@safe_handler
async def addrss_cmd(update, context) -> None:
    """/addrss <url> - follow an RSS/Atom feed in this chat."""
    if not context.args:
        await update.message.reply_text("Usage: /addrss <feed url>")
        return
    url = webtools.normalize_url(context.args[0])
    if url is None:
        await update.message.reply_text("❌ That isn't a valid web address.")
        return
    chat_id = update.effective_chat.id
    async with async_session() as session:
        count = (await session.execute(select(func.count()).select_from(RssFeed).where(RssFeed.chat_id == chat_id))).scalar() or 0
        dup = (await session.execute(select(RssFeed.id).where(RssFeed.chat_id == chat_id, RssFeed.url == url))).first()
    if dup:
        await update.message.reply_text("You already follow that feed.")
        return
    if count >= MAX_FEEDS_PER_CHAT:
        await update.message.reply_text(f"A chat can follow at most {MAX_FEEDS_PER_CHAT} feeds.")
        return
    try:
        title, entries = await _fetch_feed(url)
    except (ValueError, httpx.HTTPError) as exc:
        await update.message.reply_text(f"❌ Couldn't read that feed: {escape_html(str(exc))}", parse_mode="HTML")
        return
    async with async_session() as session:
        session.add(RssFeed(chat_id=chat_id, url=url, last_entry=entries[0][0] if entries else ""))
        await session.commit()
    await update.message.reply_text(f"✅ Following <b>{escape_html(title or url)}</b>. New entries will be posted here.", parse_mode="HTML")


@admin_only
@group_only
@safe_handler
async def removerss_cmd(update, context) -> None:
    """/removerss <url> - stop following a feed."""
    if not context.args:
        await update.message.reply_text("Usage: /removerss <feed url>")
        return
    url = webtools.normalize_url(context.args[0]) or context.args[0]
    async with async_session() as session:
        res = await session.execute(delete(RssFeed).where(RssFeed.chat_id == update.effective_chat.id, RssFeed.url == url))
        await session.commit()
    await update.message.reply_text("✅ Removed." if res.rowcount else "You don't follow that feed.")


@group_only
@safe_handler
async def rss_cmd(update, context) -> None:
    """/rss - feeds this chat follows."""
    async with async_session() as session:
        urls = (await session.execute(select(RssFeed.url).where(RssFeed.chat_id == update.effective_chat.id))).scalars().all()
    await update.message.reply_text(
        "📰 <b>Feeds</b>\n" + "\n".join(f"• {escape_html(u)}" for u in urls) if urls else "No feeds here. Add one with /addrss <url>.", parse_mode="HTML",
        disable_web_page_preview=True)


async def rss_poll(context) -> None:
    """Job: deliver new entries. A feed that fails MAX_FAILURES polls in a row is dropped."""
    async with async_session() as session:
        feeds = (await session.execute(select(RssFeed))).scalars().all()
        jobs = [(f.id, f.chat_id, f.url, f.last_entry) for f in feeds]
    for fid, chat_id, url, last in jobs:
        try:
            title, entries = await _fetch_feed(url)
            fresh = new_entries(entries, last)
            for _id, etitle, link in fresh:
                await context.bot.send_message(
                    chat_id, f"📰 <b>{escape_html(title or url)}</b>\n{escape_html(etitle)}\n{escape_html(link)}", parse_mode="HTML")
            async with async_session() as session:
                row = await session.get(RssFeed, fid)
                if row is not None:
                    row.failures = 0
                    if entries:
                        row.last_entry = entries[0][0]
                    await session.commit()
        except TelegramError as exc:
            logger.info("rss_send_failed", feed=fid, error=str(exc))
        except (ValueError, httpx.HTTPError) as exc:
            async with async_session() as session:
                row = await session.get(RssFeed, fid)
                if row is not None:
                    row.failures = (row.failures or 0) + 1
                    if row.failures >= MAX_FAILURES:
                        await session.delete(row)
                    await session.commit()
            logger.info("rss_fetch_failed", feed=fid, error=str(exc))
        await asyncio.sleep(0.2)


def register(app) -> None:
    app.add_handler(CommandHandler("paste", paste_cmd))
    app.add_handler(CommandHandler(["wiki", "wikipedia"], wiki_cmd))
    app.add_handler(CommandHandler("github", github_cmd))
    app.add_handler(CommandHandler("country", country_cmd))
    app.add_handler(CommandHandler("covid", covid_cmd))
    app.add_handler(CommandHandler("setme", setme_cmd))
    app.add_handler(CommandHandler("me", me_cmd))
    app.add_handler(CommandHandler("setbio", setbio_cmd))
    app.add_handler(CommandHandler("bio", bio_cmd))
    app.add_handler(CommandHandler("encrypt", encrypt_cmd))
    app.add_handler(CommandHandler("decrypt", decrypt_cmd))
    app.add_handler(CommandHandler("addrss", addrss_cmd))
    app.add_handler(CommandHandler("removerss", removerss_cmd))
    app.add_handler(CommandHandler("rss", rss_cmd))
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY & filters.Regex(r"^s/"), sed_listener), group=16)
    job_queue = getattr(app, "job_queue", None)
    if job_queue is not None:
        job_queue.run_repeating(rss_poll, interval=RSS_INTERVAL, first=90, name="rss_poll")
