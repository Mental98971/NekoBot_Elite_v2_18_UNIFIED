"""Activity rankings — message counting, leaderboards and owner self-update.

Ported from ChatFight (Aadish Ranjan, github.com/Aadishranjan/chatfight; see NOTICE.md) and re-engineered
for this codebase: one SQLAlchemy database instead of MongoDB, python-telegram-bot handlers that follow
this project's conventions, and the fixes listed in docs/CHATFIGHT.md.

Commands
    /rankings (/ranking)  group leaderboard image + Overall / Today / Week buttons
    /topusers             top 10 members of this group
    /userstats            message count of a member (reply, @username or user id)
    /mytop                the top 5 groups where *you* chat the most
    /sysstats (/system)   host resource card (ChatFight's /ping; /ping stays the anime-source probe)
    /fightinfo            ChatFight's welcome card (ChatFight's /start; /start stays the bot menu)
    /update, /restart     sudo only: git pull + graceful restart / graceful restart

Design notes
  * Counting is cheap: each group message increments an in-memory counter. A job writes the batch
    every ACTIVITY_FLUSH_INTERVAL seconds, and ``flush_on_shutdown`` writes whatever is left on exit.
    A failed write puts the counts back in the buffer instead of dropping them.
  * "Today" and "this week" are never reset by a job. Each counter carries the key of the period it
    belongs to; readers treat a stale key as 0 and the next write resets it in the same UPDATE.
  * Every read adds the not-yet-flushed counts, so a leaderboard is never minutes behind the chat.
  * Image rendering (Pillow) runs in a worker thread so it cannot stall the event loop.
"""
from __future__ import annotations

import asyncio
import io
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError, TimedOut
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import settings
from bot.core import restart as restart_control
from bot.core.database import ActivityStat, Chat, async_session
from bot.core.errors import safe_handler
from bot.utils import sysstats
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger
from bot.utils.redact import redact_text

logger = get_logger(__name__)

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "chatfight"
REPO_DIR = Path(__file__).resolve().parents[2]

TOP_LIMIT = 10
MYTOP_LIMIT = 5
MODES = ("overall", "today", "week")
TEXT_CACHE_SECONDS = 30
IMAGE_CACHE_SECONDS = 15
CHAT_TITLE_CACHE_SECONDS = 3600
UPDATE_TIMEOUT_SECONDS = 60
_MAX_CACHE_ENTRIES = 512
_MAX_TRACKED_MEMBERS = 20_000  # bounds the in-memory spam-guard state

MODE_TITLES = {"overall": "LEADERBOARD", "today": "TODAY LEADERBOARD", "week": "WEEK LEADERBOARD"}


# ─── Time keys ──────────────────────────────────────────────────────

@lru_cache(maxsize=8)
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("activity_timezone_invalid", timezone=name, fallback="UTC")
        return ZoneInfo("UTC")


def period_keys(now: Optional[datetime] = None, tz_name: Optional[str] = None) -> Tuple[str, str]:
    """``(day_key, week_key)`` in the configured zone, e.g. ``("2026-10-10", "2026-W41")``.

    The week key is the ISO year-week. ChatFight used ``%Y-%W``, which splits the week that
    contains New Year into two buckets and numbers the days before the first Monday as week 00."""
    zone = _zone(tz_name or settings.activity_timezone)
    moment = (now or datetime.now(timezone.utc)).astimezone(zone)
    iso = moment.isocalendar()
    return moment.strftime("%Y-%m-%d"), f"{iso[0]}-W{iso[1]:02d}"


# ─── In-memory counting ─────────────────────────────────────────────

@dataclass
class _Pending:
    count: int
    name: str
    username: Optional[str]
    chat_title: Optional[str]


_buffer: Dict[Tuple[int, int], _Pending] = {}
_recent: Dict[Tuple[int, int], Deque[float]] = defaultdict(deque)
_blocked_until: Dict[Tuple[int, int], float] = {}

COUNTED, SKIPPED, FLOODING = "counted", "skipped", "flooding"


def _display_name(user: Any) -> str:
    return (getattr(user, "first_name", None) or getattr(user, "username", None) or "User")[:128]


def record_message(chat_id: int, user: Any, chat_title: Optional[str], now: Optional[float] = None) -> str:
    """Count one message. Returns COUNTED, SKIPPED (inside a flood block) or FLOODING (this message
    tripped the guard; it is not counted and the member is now blocked from counting)."""
    key = (chat_id, user.id)
    now = time.monotonic() if now is None else now

    blocked = _blocked_until.get(key)
    if blocked is not None:
        if now < blocked:
            return SKIPPED
        _blocked_until.pop(key, None)

    threshold = settings.activity_spam_threshold
    if threshold > 0:
        recent = _recent[key]
        cutoff = now - settings.activity_spam_window
        while recent and recent[0] < cutoff:
            recent.popleft()
        recent.append(now)
        if len(recent) >= threshold:
            _blocked_until[key] = now + settings.activity_spam_block_seconds
            recent.clear()
            return FLOODING

    username = getattr(user, "username", None)
    entry = _buffer.get(key)
    if entry is None:
        _buffer[key] = _Pending(1, _display_name(user), username, chat_title)
    else:
        entry.count += 1
        entry.name = _display_name(user)
        entry.username = username
        entry.chat_title = chat_title
    return COUNTED


def prune_guard_state(now: Optional[float] = None) -> int:
    """Drop expired flood blocks and idle windows so the guard's memory stays bounded."""
    now = time.monotonic() if now is None else now
    removed = 0
    for key in [k for k, until in _blocked_until.items() if until <= now]:
        del _blocked_until[key]
        removed += 1
    horizon = now - settings.activity_spam_window
    for key in [k for k, window in _recent.items() if not window or window[-1] < horizon]:
        del _recent[key]
        removed += 1
    if len(_recent) > _MAX_TRACKED_MEMBERS:  # a very large deployment: forget the oldest windows
        for key in sorted(_recent, key=lambda k: _recent[k][-1] if _recent[k] else 0)[: len(_recent) // 2]:
            del _recent[key]
            removed += 1
    return removed


def pending_for_chat(chat_id: int) -> Dict[int, _Pending]:
    return {user_id: p for (cid, user_id), p in _buffer.items() if cid == chat_id}


def pending_for_user(user_id: int) -> Dict[int, _Pending]:
    return {chat_id: p for (chat_id, uid), p in _buffer.items() if uid == user_id}


def reset_state_for_tests() -> None:
    _buffer.clear()
    _recent.clear()
    _blocked_until.clear()
    _text_cache.clear()
    _image_cache.clear()
    _title_cache.clear()


# ─── Persistence ────────────────────────────────────────────────────

async def flush_counts() -> int:
    """Write buffered counts to the database. Returns the number of (chat, member) rows written.

    Atomic per call: either every row is written or none is, and on failure the counts go back into
    the buffer (merged with anything counted meanwhile) for the next attempt."""
    global _buffer
    if not _buffer:
        return 0
    snapshot, _buffer = _buffer, {}
    day, week = period_keys()
    try:
        async with async_session() as session:
            for (chat_id, user_id), p in snapshot.items():
                if p.count <= 0:
                    continue
                values = dict(
                    name=p.name,
                    username=p.username,
                    username_lc=(p.username or "").lower() or None,
                    chat_title=p.chat_title,
                    overall=ActivityStat.overall + p.count,
                    day_count=case((ActivityStat.day_key == day, ActivityStat.day_count + p.count), else_=p.count),
                    day_key=day,
                    week_count=case((ActivityStat.week_key == week, ActivityStat.week_count + p.count), else_=p.count),
                    week_key=week,
                )
                where = (ActivityStat.chat_id == chat_id, ActivityStat.user_id == user_id)
                result = await session.execute(update(ActivityStat).where(*where).values(**values))
                if result.rowcount:
                    continue
                try:
                    async with session.begin_nested():
                        session.add(ActivityStat(
                            chat_id=chat_id, user_id=user_id, name=p.name, username=p.username,
                            username_lc=(p.username or "").lower() or None, chat_title=p.chat_title,
                            overall=p.count, day_key=day, day_count=p.count, week_key=week, week_count=p.count,
                        ))
                except IntegrityError:  # another writer inserted the row first: add to it instead
                    await session.execute(update(ActivityStat).where(*where).values(**values))
            await session.commit()
    except Exception:
        logger.exception("activity_flush_failed", rows=len(snapshot))
        for key, p in snapshot.items():
            current = _buffer.get(key)
            if current is None:
                _buffer[key] = p
            else:
                current.count += p.count
        return 0
    return len(snapshot)


async def flush_on_shutdown() -> None:
    """Called from core/bot.py's post_shutdown so a restart does not lose up to one flush interval."""
    flushed = await flush_counts()
    if flushed:
        logger.info("activity_flushed_on_shutdown", rows=flushed)


@dataclass
class Row:
    user_id: int
    name: str
    username: Optional[str]
    count: int


def _mode_columns(mode: str):
    """``(value_column, key_column | None, current_key | None)`` for a leaderboard mode."""
    day, week = period_keys()
    if mode == "today":
        return ActivityStat.day_count, ActivityStat.day_key, day
    if mode == "week":
        return ActivityStat.week_count, ActivityStat.week_key, week
    return ActivityStat.overall, None, None


async def leaderboard(chat_id: int, mode: str, limit: int = TOP_LIMIT) -> Tuple[List[Row], int]:
    """Top ``limit`` members of a chat for a mode, plus the chat's total for that mode.

    Not-yet-flushed counts are included, and a member whose only activity is still in the buffer can
    outrank members who are already stored."""
    value_col, key_col, key_value = _mode_columns(mode)
    pending = pending_for_chat(chat_id)
    merged: Dict[int, Row] = {}

    def _where():
        clauses = [ActivityStat.chat_id == chat_id]
        if key_col is not None:
            clauses.append(key_col == key_value)
        return clauses

    async with async_session() as session:
        top = await session.execute(
            select(ActivityStat.user_id, ActivityStat.name, ActivityStat.username, value_col)
            .where(*_where(), value_col > 0)
            .order_by(value_col.desc(), ActivityStat.user_id)
            .limit(limit)
        )
        for user_id, name, username, count in top.all():
            merged[user_id] = Row(user_id, name or "User", username, int(count))

        missing = [uid for uid in pending if uid not in merged]
        if missing:
            extra = await session.execute(
                select(ActivityStat.user_id, ActivityStat.name, ActivityStat.username, value_col, key_col
                       if key_col is not None else func.coalesce(value_col, 0))
                .where(ActivityStat.chat_id == chat_id, ActivityStat.user_id.in_(missing))
            )
            for user_id, name, username, count, key in extra.all():
                stored = int(count) if key_col is None or key == key_value else 0
                merged[user_id] = Row(user_id, name or "User", username, stored)

        total_stmt = select(func.coalesce(func.sum(value_col), 0)).where(*_where())
        total = int((await session.execute(total_stmt)).scalar() or 0)

    for user_id, p in pending.items():
        row = merged.get(user_id)
        if row is None:
            merged[user_id] = Row(user_id, p.name, p.username, p.count)
        else:
            row.count += p.count
            row.name = p.name or row.name
            row.username = p.username or row.username
    total += sum(p.count for p in pending.values())

    rows = sorted((r for r in merged.values() if r.count > 0), key=lambda r: (-r.count, r.user_id))[:limit]
    return rows, total


async def member_total(chat_id: int, user_id: int) -> Optional[Row]:
    """A member's overall count in a chat (stored + pending), or None if never seen."""
    async with async_session() as session:
        res = await session.execute(
            select(ActivityStat.user_id, ActivityStat.name, ActivityStat.username, ActivityStat.overall)
            .where(ActivityStat.chat_id == chat_id, ActivityStat.user_id == user_id)
        )
        found = res.first()
    p = _buffer.get((chat_id, user_id))
    if found is None and p is None:
        return None
    if found is None:
        return Row(user_id, p.name, p.username, p.count)
    user_id, name, username, overall = found
    row = Row(user_id, name or "User", username, int(overall))
    if p is not None:
        row.count += p.count
        row.name = p.name or row.name
        row.username = p.username or row.username
    return row


async def find_member_by_username(chat_id: int, username: str) -> Optional[Row]:
    wanted = username.lstrip("@").lower()
    async with async_session() as session:
        res = await session.execute(
            select(ActivityStat.user_id)
            .where(ActivityStat.chat_id == chat_id, ActivityStat.username_lc == wanted)
            .order_by(ActivityStat.overall.desc())
            .limit(1)
        )
        found = res.scalar()
    if found is None:  # the member may exist only in the buffer
        for user_id, p in pending_for_chat(chat_id).items():
            if (p.username or "").lower() == wanted:
                found = user_id
                break
    return await member_total(chat_id, found) if found is not None else None


async def my_top(user_id: int, mode: str, limit: int = MYTOP_LIMIT) -> List[Tuple[int, Optional[str], int]]:
    """``[(chat_id, stored_title, count)]`` for the groups where a user chats most (pending counts included)."""
    value_col, key_col, key_value = _mode_columns(mode)
    clauses = [ActivityStat.user_id == user_id]
    if key_col is not None:
        clauses.append(key_col == key_value)
    async with async_session() as session:
        res = await session.execute(
            select(ActivityStat.chat_id, ActivityStat.chat_title, value_col)
            .where(*clauses, value_col > 0)
            .order_by(value_col.desc(), ActivityStat.chat_id)
            .limit(limit)
        )
        rows = {cid: [title, int(count)] for cid, title, count in res.all()}
        pend = pending_for_user(user_id)
        missing = [cid for cid in pend if cid not in rows]
        if missing:
            extra = await session.execute(
                select(ActivityStat.chat_id, ActivityStat.chat_title, value_col,
                       key_col if key_col is not None else func.coalesce(value_col, 0))
                .where(ActivityStat.user_id == user_id, ActivityStat.chat_id.in_(missing))
            )
            for cid, title, count, key in extra.all():
                stored = int(count) if key_col is None or key == key_value else 0
                rows[cid] = [title, stored]
    for cid, p in pend.items():
        entry = rows.setdefault(cid, [p.chat_title, 0])
        entry[0] = entry[0] or p.chat_title
        entry[1] += p.count
    ranked = sorted(((cid, t, c) for cid, (t, c) in rows.items() if c > 0), key=lambda r: (-r[2], r[0]))
    return ranked[:limit]


# ─── Small TTL caches ───────────────────────────────────────────────

class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._items: Dict[Any, Tuple[float, Any]] = {}

    def get(self, key):
        item = self._items.get(key)
        if item is None:
            return None
        if time.monotonic() >= item[0]:
            self._items.pop(key, None)
            return None
        return item[1]

    def set(self, key, value) -> None:
        if len(self._items) >= _MAX_CACHE_ENTRIES:
            now = time.monotonic()
            for k in [k for k, (exp, _v) in self._items.items() if exp <= now] or list(self._items)[: _MAX_CACHE_ENTRIES // 4]:
                self._items.pop(k, None)
        self._items[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> None:
        self._items.clear()


_text_cache = _TTLCache(TEXT_CACHE_SECONDS)
_image_cache = _TTLCache(IMAGE_CACHE_SECONDS)
_title_cache = _TTLCache(CHAT_TITLE_CACHE_SECONDS)


# ─── Presentation ───────────────────────────────────────────────────

def format_member(name: str, username: Optional[str]) -> str:
    safe = escape_html(name or "User")
    return f"{safe} (@{escape_html(username)})" if username else safe


def format_leaderboard(mode: str, rows: List[Row], total: int) -> str:
    title = MODE_TITLES.get(mode, MODE_TITLES["overall"])
    if not rows:
        return f"<b>📈 {title}</b>\n\n<i>No data yet.</i>"
    lines = [f"<b>📈 {title}</b>", ""]
    for index, row in enumerate(rows, start=1):
        lines.append(f"<b>{index}. {escape_html(row.name)} • {row.count}</b>")
    lines += ["", f"<b>✉️ Total messages: {total}</b>"]
    return "\n".join(lines)


def format_top_users(rows: List[Row]) -> str:
    if not rows:
        return "<b>📈 ACTIVITY TRACKER</b>\n\n<i>No activity data yet.</i>"
    lines = ["<b>📈 ACTIVITY TRACKER</b>", "", f"<b>Top {TOP_LIMIT} active users:</b>", ""]
    for index, row in enumerate(rows, start=1):
        lines.append(f"<b>{index}. {format_member(row.name, row.username)} • {row.count}</b>")
    return "\n".join(lines)


def format_user_stats(row: Row) -> str:
    return f"<b>📈 USER STATS</b>\n\n<b>User:</b> {format_member(row.name, row.username)}\n<b>Messages:</b> {row.count}"


def mode_keyboard(prefix: str, mode: str) -> InlineKeyboardMarkup:
    def button(label: str, target: str) -> InlineKeyboardButton:
        text = ("✅ " if mode == target else "⏺️ ") + label
        return InlineKeyboardButton(text, callback_data="cf:noop" if mode == target else f"{prefix}:{target}")

    return InlineKeyboardMarkup([
        [button("Overall", "overall")],
        [button("Today", "today"), button("Week", "week")],
    ])


@lru_cache(maxsize=8)
def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(ASSET_DIR / "dejavu-sans.bold.ttf"), size)
    except OSError:
        logger.warning("activity_font_missing", path=str(ASSET_DIR))
        return ImageFont.load_default(size)


_W, _H = 1100, 650
_BG, _CARD, _BAR, _TEXT, _MUTED = (25, 5, 8), (45, 10, 15), (190, 70, 80), (255, 255, 255), (180, 180, 180)


def _canvas():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (_W, _H), _BG)
    try:
        background = Image.open(ASSET_DIR / "bg.png").resize((_W, _H)).convert("RGBA")
        image.paste(background, (0, 0), background)
    except (OSError, ValueError):
        pass  # the artwork is optional
    return image, ImageDraw.Draw(image)


def _centered_title(draw, title: str) -> None:
    box = draw.textbbox((0, 0), title, font=_font(56))
    draw.text(((_W - (box[2] - box[0])) // 2, 30), title, font=_font(56), fill=_TEXT)


def render_leaderboard_png(rows: List[Row], title: str = "LEADERBOARD") -> bytes:
    image, draw = _canvas()
    _centered_title(draw, title)
    x1, y1, x2, y2 = 50, 120, _W - 50, _H - 50
    draw.rounded_rectangle([x1, y1, x2, y2], radius=30, fill=_CARD, outline=(120, 40, 50), width=3)
    if not rows:
        draw.text((_W // 2 - 100, _H // 2), "No data available", font=_font(28), fill=_MUTED)
    else:
        top_score = max(max(r.count for r in rows), 1)
        bar_x = x1 + 260
        bar_max = x2 - bar_x - 40
        for index, row in enumerate(rows[:TOP_LIMIT]):
            y = y1 + 40 + index * 45
            draw.text((x1 + 30, y), (row.name or "User")[:16], font=_font(28), fill=_TEXT)
            width = int(row.count / top_score * bar_max)
            draw.rounded_rectangle([bar_x, y + 4, bar_x + width, y + 30], radius=15, fill=_BAR)
            label = str(row.count)
            box = draw.textbbox((0, 0), label, font=_font(26))
            draw.text((bar_x + width - (box[2] - box[0]) - 15, y + 6), label, font=_font(26), fill=_TEXT)
    return _png(image)


def render_stats_png(lines: Iterable[str], title: str = "SYSTEM STATS") -> bytes:
    image, draw = _canvas()
    _centered_title(draw, title)
    x1, y1, x2, y2 = 70, 140, _W - 70, _H - 70
    draw.rounded_rectangle([x1, y1, x2, y2], radius=30, fill=_CARD, outline=(120, 40, 50), width=3)
    y = y1 + 55
    for line in lines:
        draw.text((x1 + 40, y), line, font=_font(28), fill=_TEXT)
        y += 70
    return _png(image)


def _png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ─── Telegram helpers ───────────────────────────────────────────────

async def _groups_only(update: Update) -> bool:
    """True (after replying) when the command was used outside a group."""
    chat = update.effective_chat
    if chat is not None and chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("<b>This command only works in groups.</b>", parse_mode="HTML")
        return True
    return False


async def _send_photo_or_text(bot, chat_id: int, png: bytes, caption: str, markup=None, reply_to=None) -> None:
    """Send a photo, retrying one upload timeout; fall back to the caption as text."""
    for attempt in range(2):
        try:
            await bot.send_photo(
                chat_id=chat_id, photo=InputFile(io.BytesIO(png), filename="card.png"), caption=caption,
                reply_markup=markup, parse_mode="HTML",
                reply_to_message_id=reply_to, allow_sending_without_reply=True,
                connect_timeout=10, write_timeout=30, read_timeout=30, pool_timeout=10,
            )
            return
        except TimedOut:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            logger.warning("activity_send_photo_timed_out", chat_id=chat_id)
        except TelegramError:
            logger.warning("activity_send_photo_failed", chat_id=chat_id, exc_info=True)
            break
    await bot.send_message(chat_id=chat_id, text=caption, reply_markup=markup, parse_mode="HTML")


async def _ranking_text(chat_id: int, mode: str) -> str:
    cached = _text_cache.get((chat_id, mode))
    if cached is not None:
        return cached
    rows, total = await leaderboard(chat_id, mode)
    text = format_leaderboard(mode, rows, total)
    _text_cache.set((chat_id, mode), text)
    return text


# ─── Counter ────────────────────────────────────────────────────────

async def activity_counter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Count one group message. Never replies on error: a counter must not make noise."""
    try:
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if message is None or chat is None or user is None:
            return
        if user.is_bot and not settings.activity_count_bots:
            return
        outcome = record_message(chat.id, user, chat.title)
        if outcome == FLOODING:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"⛔️ {user.first_name or 'User'} is flooding: messages won't count toward the rankings "
                     f"for {max(1, settings.activity_spam_block_seconds // 60)} minutes.",
            )
    except Exception:
        logger.exception("activity_counter_failed")


async def _flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await flush_counts()
    prune_guard_state()


# ─── Commands ───────────────────────────────────────────────────────

@safe_handler
async def rankings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rankings — group leaderboard image with Overall / Today / Week buttons."""
    if await _groups_only(update):
        return
    chat = update.effective_chat
    message = update.effective_message
    markup = mode_keyboard("cfrank", "overall")
    cached = _image_cache.get(chat.id)
    if cached is None:
        rows, total = await leaderboard(chat.id, "overall")
        caption = format_leaderboard("overall", rows, total)
        png = await asyncio.to_thread(render_leaderboard_png, rows, MODE_TITLES["overall"]) if rows else None
        cached = (caption, png)
        _image_cache.set(chat.id, cached)
    caption, png = cached
    if png is None:
        await message.reply_text(caption, reply_markup=markup, parse_mode="HTML")
        return
    await _send_photo_or_text(context.bot, chat.id, png, caption, markup)


@safe_handler
async def rankings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mode = (query.data or "").split(":", 1)[-1]
    if mode not in MODES or query.message is None:
        return
    text = await _ranking_text(query.message.chat.id, mode)
    try:
        if query.message.caption is not None:
            await query.edit_message_caption(text, reply_markup=mode_keyboard("cfrank", mode), parse_mode="HTML")
        else:
            await query.edit_message_text(text, reply_markup=mode_keyboard("cfrank", mode), parse_mode="HTML")
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


@safe_handler
async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The already-selected mode button: acknowledge it so the client stops its loading spinner.
    (ChatFight sent a ``noop`` payload that nothing handled.)"""
    await update.callback_query.answer()


@safe_handler
async def topusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/topusers — top 10 members of this group by total messages."""
    if await _groups_only(update):
        return
    chat_id = update.effective_chat.id
    text = _text_cache.get(("topusers", chat_id))
    if text is None:
        rows, _total = await leaderboard(chat_id, "overall")
        text = format_top_users(rows)
        _text_cache.set(("topusers", chat_id), text)
    await update.effective_message.reply_text(text, parse_mode="HTML")


USERSTATS_USAGE = (
    "<b>Usage:</b> /userstats &lt;reply/@username/user_id&gt;\n"
    "Reply to a user's message or pass @username/user_id."
)


@safe_handler
async def userstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/userstats — message count of a member (reply, @username, or numeric user id)."""
    if await _groups_only(update):
        return
    message = update.effective_message
    chat_id = update.effective_chat.id
    row: Optional[Row] = None
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        target = reply.from_user
        row = await member_total(chat_id, target.id) or Row(target.id, _display_name(target), target.username, 0)
        row.name, row.username = _display_name(target), target.username or row.username
    elif context.args:
        arg = context.args[0].strip()
        row = await (member_total(chat_id, int(arg)) if arg.isdigit() else find_member_by_username(chat_id, arg))
        if row is None:
            await message.reply_text("<b>User not found.</b>\n" + USERSTATS_USAGE, parse_mode="HTML")
            return
    else:
        await message.reply_text(USERSTATS_USAGE, parse_mode="HTML")
        return
    await message.reply_text(format_user_stats(row), parse_mode="HTML")


async def _resolve_chat_title(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stored: Optional[str]) -> str:
    if stored:
        _title_cache.set(chat_id, stored)
        return stored
    cached = _title_cache.get(chat_id)
    if cached is not None:
        return cached
    title: Optional[str] = None
    try:
        async with async_session() as session:
            chat_row = await session.get(Chat, chat_id)
            title = getattr(chat_row, "title", None)
    except Exception:
        logger.debug("activity_chat_row_lookup_failed", exc_info=True)
    if not title:
        try:
            title = getattr(await context.bot.get_chat(chat_id), "title", None)
        except TelegramError:
            title = None
    if title:
        _title_cache.set(chat_id, title)
        try:
            async with async_session() as session:
                await session.execute(
                    update(ActivityStat)
                    .where(ActivityStat.chat_id == chat_id, (ActivityStat.chat_title.is_(None)) | (ActivityStat.chat_title == ""))
                    .values(chat_title=title)
                )
                await session.commit()
        except Exception:
            logger.debug("activity_title_backfill_failed", exc_info=True)
    return title or "Unknown Group"


async def _mytop_text(context: ContextTypes.DEFAULT_TYPE, user_id: int, first_name: str, mode: str) -> str:
    lines = [f"<b>📈 LEADERBOARD | {escape_html(first_name)}</b>", ""]
    ranked = await my_top(user_id, mode)
    for index, (chat_id, stored_title, count) in enumerate(ranked, start=1):
        title = await _resolve_chat_title(context, chat_id, stored_title)
        lines.append(f"<b>{index}. 👥 {escape_html(title)} • {count}</b>")
    if not ranked:
        lines.append("<b><i>No data found.</i></b>")
    return "\n".join(lines)


@safe_handler
async def mytop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mytop — the top 5 groups where you chat the most."""
    user = update.effective_user
    text = await _mytop_text(context, user.id, user.first_name or "User", "overall")
    await update.effective_message.reply_text(text, reply_markup=mode_keyboard("cfmytop", "overall"), parse_mode="HTML")


@safe_handler
async def mytop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mode = (query.data or "").split(":", 1)[-1]
    if mode not in MODES:
        return
    user = query.from_user
    text = await _mytop_text(context, user.id, user.first_name or "User", mode)
    try:
        await query.edit_message_text(text, reply_markup=mode_keyboard("cfmytop", mode), parse_mode="HTML")
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


@safe_handler
async def sysstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sysstats — uptime, RAM, CPU and disk usage of the host, as a card."""
    message = update.effective_message
    stats = await sysstats.collect()
    mention = f"@{context.bot.username}" if getattr(context.bot, "username", None) else "Bot"
    caption = (
        f"{escape_html(mention)} ꜱʏꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ :\n\n"
        f"↬ ʙᴏᴛ ᴜᴘᴛɪᴍᴇ : {stats['bot_uptime']}\n"
        f"↬ ʜᴏꜱᴛ ᴜᴘᴛɪᴍᴇ : {stats['uptime']}\n"
        f"↬ ʀᴀᴍ : {stats['ram_percent']}%\n"
        f"↬ ᴄᴘᴜ : {stats['cpu_percent']}%\n"
        f"↬ ᴅɪꜱᴋ : {stats['disk_percent']}%"
    )
    lines = [
        f"↬ bot uptime : {stats['bot_uptime']}",
        f"↬ host uptime : {stats['uptime']}",
        f"↬ ram : {stats['ram_percent']}%",
        f"↬ cpu : {stats['cpu_percent']}%",
        f"↬ disk : {stats['disk_percent']}%",
    ]
    png = await asyncio.to_thread(render_stats_png, lines, "SYSTEM STATS")
    await _send_photo_or_text(context.bot, message.chat_id, png, caption, reply_to=message.message_id)


@safe_handler
async def fightinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fightinfo — ChatFight's welcome card (its /start; here /start is the bot's main menu)."""
    chat = update.effective_chat
    private = chat.type == ChatType.PRIVATE
    text = (
        "🤖 Welcome, this bot will count group messages, create rankings and give prizes to users!"
        if private else
        "🤖 Welcome to the group chat fight! This bot will count group messages, "
        "create rankings and give prizes to users!"
    )
    rows = []
    username = getattr(context.bot, "username", None)
    if username:
        rows.append([InlineKeyboardButton("Add me to Group", url=f"https://t.me/{username}?startgroup=true")])
    rows.append([InlineKeyboardButton("Owner", url=f"tg://user?id={settings.owner_id}")])
    markup = InlineKeyboardMarkup(rows)
    logo = ASSET_DIR / "logo.png"
    if logo.exists():
        try:
            with open(logo, "rb") as handle:
                await context.bot.send_photo(chat_id=chat.id, photo=handle, caption=text, reply_markup=markup)
            return
        except TelegramError:
            logger.warning("fightinfo_photo_failed", exc_info=True)
    await update.effective_message.reply_text(text, reply_markup=markup)


# ─── Owner: /update and /restart ────────────────────────────────────

def _is_sudo(user_id: Optional[int]) -> bool:
    return bool(user_id and settings.is_admin_id(user_id))


async def _run_git_pull() -> Tuple[int, str]:
    """``git pull --ff-only`` in the checkout the bot runs from. The optional GIT_TOKEN travels in the
    environment (GIT_CONFIG_*), never in the remote URL or argv, so it cannot leak through ``ps``,
    ``.git/config`` or git's own output."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = settings.git_token.get_secret_value() if settings.git_token else ""
    if token:
        import base64

        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    args = ["git", "pull", "--ff-only"]
    if settings.upstream_repo:
        args += [settings.upstream_repo, settings.upstream_branch]
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(REPO_DIR), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=UPDATE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, f"Command timed out after {UPDATE_TIMEOUT_SECONDS}s."
    text = (output or b"").decode("utf-8", errors="replace").strip() or "(no output)"
    if token:
        text = text.replace(token, "***")
    return proc.returncode or 0, redact_text(text)


async def _restart(context: ContextTypes.DEFAULT_TYPE) -> bool:
    await flush_counts()
    return restart_control.request(context.application)


@safe_handler
async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/update (sudo) — git pull --ff-only, then restart."""
    message = update.effective_message
    user = update.effective_user
    if not _is_sudo(user.id if user else None):
        await message.reply_text("You are not allowed to use this command.")
        return
    if not (REPO_DIR / ".git").exists():
        await message.reply_text(
            "❌ This install is not a git checkout (a Docker image, for example), so there is nothing to pull. "
            "Rebuild or redeploy to update."
        )
        return
    status = await message.reply_text("Updating from upstream...")
    code, output = await _run_git_pull()
    trimmed = escape_html(output[:3500])
    if code != 0:
        await status.edit_text(f"❌ Update failed (exit {code}).\n\n<pre>{trimmed}</pre>", parse_mode="HTML")
        return
    if "Already up to date" in output:
        await status.edit_text(f"✅ Already up to date.\n\n<pre>{trimmed}</pre>", parse_mode="HTML")
        return
    await status.edit_text(
        f"✅ Update successful.\n\n<pre>{trimmed}</pre>\n\n♻️ Restarting bot...\n"
        "<i>If requirements.txt changed, run pip install -r requirements.txt first.</i>",
        parse_mode="HTML",
    )
    if not await _restart(context):
        await message.reply_text("⚠️ Updated, but this run mode cannot restart itself. Restart the bot manually.")


@safe_handler
async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/restart (sudo) — graceful restart: pending counts are flushed, shutdown hooks run."""
    user = update.effective_user
    if not _is_sudo(user.id if user else None):
        await update.effective_message.reply_text("You are not allowed to use this command.")
        return
    await update.effective_message.reply_text("♻️ Restarting bot...")
    if not await _restart(context):
        await update.effective_message.reply_text("⚠️ This run mode cannot restart itself. Restart the bot manually.")


# ─── Registration ───────────────────────────────────────────────────

def register(app) -> None:
    app.add_handler(CommandHandler(["rankings", "ranking"], rankings_cmd))
    app.add_handler(CommandHandler("topusers", topusers_cmd))
    app.add_handler(CommandHandler("userstats", userstats_cmd))
    app.add_handler(CommandHandler("mytop", mytop_cmd))
    app.add_handler(CommandHandler(["sysstats", "system"], sysstats_cmd))
    app.add_handler(CommandHandler(["fightinfo", "chatfight"], fightinfo_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))
    app.add_handler(CallbackQueryHandler(rankings_callback, pattern=r"^cfrank:"))
    app.add_handler(CallbackQueryHandler(mytop_callback, pattern=r"^cfmytop:"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^cf:noop$"))

    if not settings.activity_enabled:
        logger.info("activity_counting_disabled")
        return
    # Group 14: after every moderation/lock/filter listener (0-13), so a message the bot itself removed or
    # acted on is still a message the member sent; see the map in group_mgmt.register().
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL & ~filters.COMMAND, activity_counter),
        group=14,
    )
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            _flush_job, interval=settings.activity_flush_interval, first=settings.activity_flush_interval,
            name="activity_flush", job_kwargs={"coalesce": True, "misfire_grace_time": settings.activity_flush_interval * 2},
        )
