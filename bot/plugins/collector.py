"""
Character Collector — ambient "guess the name" catching game.

Originally sourced from four sibling projects (three single-file
anime_collector_bot variants plus the more production-shaped
anime_catcher_bot), cross-referenced against WAIFU-HUSBANDO-CATCHER.
This revision merges in a further round of improvements
("collector_enhanced.py", itself cross-referencing the same sources
plus this file) — weighted rarity spawning, several new commands, and
three real bugs this file had:

  - /topcatchers and the new /owners used to INNER JOIN UserCharacter
    against User. A user whose only interaction with the bot was ever
    catching a character (never anything that happened to create a
    User row first) was silently invisible on both — not an edge case,
    since grab_cmd never created that row itself. grab_cmd now ensures
    one exists before recording the catch.
  - upload_cmd stored context.bot.get_file(...).file_path as the
    character's permanent image_url. Telegram only guarantees that URL
    for a short window — Bot API docs: "It is guaranteed that the link
    will be valid for at least 1 hour." Spawns uploaded today could
    start showing broken images later. Now stores the photo's file_id
    directly, which Telegram guarantees stays valid indefinitely for
    re-sending via send_photo/send_video.
  - Storing file_id fixed the above but broke inline query results,
    which require an actual fetchable URL in photo_url/video_url —
    passing a file_id there doesn't render. inline_query now uses
    InlineQueryResultCachedPhoto/CachedVideo, which take a
    photo_file_id/video_file_id instead, exactly matching what's
    actually stored.

Two permission gates were also tightened. addmod/removemod and
upload/delchar were gated with @admin_only (chat-admin of wherever the
command happened to run) on top of an inline _is_collector_mod check —
meaning a legitimate collector mod who wasn't ALSO an admin of that
specific chat (e.g. curating via DM, the natural workflow) was blocked
before the intended check even ran, and inversely, any admin of any of
potentially many mutually-unrelated group chats could unilaterally
grant themselves bot-wide "collector moderator" status, a role this
file's own design explicitly scopes as "intentionally separate from
being an admin of any one chat." giveaway/endgiveaway had the same
@admin_only gate with no cap on the coin amount, so any chat admin
anywhere could mint unlimited coins via giveaways. All four now check
collector-mod/sudo status directly instead: addmod/removemod require
sudo (appointing a bot-wide trusted role should be more restrictive
than holding it), upload/delchar/giveaway/endgiveaway require
collector-mod. setspawnrate stays @admin_only — it's a genuinely
per-chat setting, so gating it on that chat's own admins is correct.

Architecture, unchanged from the original version of this file:
  - Catch rewards pay into the existing coin economy
    (bot/services/economy_service.py) instead of a second currency.
  - /topcatchers is a distinctly-named ranking — /leaderboard already
    means XP ranking (see plugins/economy.py).
  - No separate /daily, /broadcast, /stats, /botban, /disable: those
    already exist elsewhere in NovaBot (economy.py, admin.py,
    access_control.py, group_mgmt.py) and do the job.
  - Trade proposals use inline accept/decline (bot/plugins/games.py's
    "game:" callback pattern, mirrored here as "collector:").
  - Characters are shared bot-wide (upload once, catchable in every
    chat), curated by a small "collector moderator" role.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import case, delete, func, select, update as sa_update
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputTextMessageContent,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.config import settings
from bot.core.database import (
    Character,
    Chat,
    CollectorModerator,
    Giveaway,
    User,
    UserCharacter,
    async_session,
)
from bot.core import inline as inline_router
from bot.core.decorators import admin_only, group_only
from bot.services.economy_service import add_coins
from bot.utils.helpers import escape_html, paginate, resolve_target_user
from bot.utils.logger import get_logger
from bot.utils.ratelimit import hit
from bot.core.errors import safe_handler

logger = get_logger(__name__)

RARITY_EMOJIS = {
    "common": "⚪", "uncommon": "🟢", "rare": "🔵",
    "epic": "🟣", "legendary": "🟡", "divine": "🔴",
}
# Rarest first — listings read better leading with the impressive stuff.
RARITY_ORDER = ["divine", "legendary", "epic", "rare", "uncommon", "common"]
RARITY_WEIGHTS = {
    "common": 50, "uncommon": 30, "rare": 12,
    "epic": 5, "legendary": 2, "divine": 1,
}
# Coins refunded per duplicate smelted, by rarity.
SMELT_VALUES = {
    "common": 5, "uncommon": 12, "rare": 30,
    "epic": 80, "legendary": 200, "divine": 500,
}

ITEMS_PER_PAGE = 15
INLINE_LIMIT = 40
GRAB_COOLDOWN_SECONDS = 1.5
CLAIM_COOLDOWN_SECONDS = 2.0
CONFIRM_EXPIRY_SECONDS = 60

# Ephemeral, in-memory — same trade-off as games.py's trivia/tic-tac-toe
# state: a restart simply ends whatever was in progress.
_message_counters: Dict[int, int] = {}              # chat_id -> messages since last spawn
_current_spawns: Dict[int, dict] = {}                # chat_id -> {"id", "name", "anime", "rarity", "image_url"}
_pending_trades: Dict[tuple, dict] = {}              # (proposer_id, target_id) -> {char ids, expires}
_pending_confirms: Dict[str, dict] = {}              # confirm_id -> payload, for bulk transfer/copy confirmation
_grab_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)   # chat_id -> lock, belt-and-suspenders on top of the no-await guarantee below
_last_grab: Dict[int, float] = {}                    # user_id -> monotonic timestamp, anti-spam
_last_claim: Dict[int, float] = {}


# ==================== HELPERS ====================

def _rarity_emoji(rarity: str) -> str:
    return RARITY_EMOJIS.get((rarity or "common").lower(), "⚪")


def _is_video(url_or_id: Optional[str]) -> bool:
    if not url_or_id:
        return False
    lower = str(url_or_id).lower()
    return any(x in lower for x in (".mp4", ".webm", ".mov"))


def _rarity_order_expr():
    """Portable CASE expression for sorting by rarity — works on both
    SQLite and Postgres, unlike a MySQL-only ordering function."""
    return case(
        {r: i for i, r in enumerate(RARITY_ORDER)},
        value=Character.rarity,
        else_=len(RARITY_ORDER),
    )


def _cooldown_ok(store: Dict[int, float], user_id: int, seconds: float) -> bool:
    now = time.monotonic()
    if now - store.get(user_id, 0.0) < seconds:
        return False
    store[user_id] = now
    return True


async def _is_collector_mod(user_id: int) -> bool:
    if settings.is_admin_id(user_id):
        return True
    async with async_session() as session:
        return await session.get(CollectorModerator, user_id) is not None


async def _is_super_admin(user_id: int) -> bool:
    """Bot-wide sudo/owner only — deliberately stricter than
    _is_collector_mod. See module docstring: appointing new collector
    mods should be more restrictive than holding the role."""
    return settings.is_admin_id(user_id)


async def _ensure_user(session, user) -> None:
    """Make sure a User row exists for this Telegram user before
    recording anything that references them by id elsewhere (catches,
    favorites, ...). Without this, a user whose first-ever interaction
    with the bot is catching a character stays invisible to any query
    that joins against User — see module docstring."""
    row = await session.get(User, user.id)
    if row is None:
        session.add(User(
            id=user.id, username=user.username, first_name=user.first_name or "",
        ))
        await session.flush()


async def _pick_character(session, rarity: Optional[str] = None) -> Optional[Character]:
    """Weighted-random pick across rarity tiers, then uniform-random
    within the chosen tier. Previously this was a flat uniform pick
    over every character regardless of rarity — the "legendary"/
    "divine" labels were pure flavor text with zero effect on actual
    spawn odds. The `// 3 + 1` dampener keeps one over-populated tier
    (e.g. fifty "common" uploads) from swamping the weights beyond what
    RARITY_WEIGHTS itself already intends.
    """
    if rarity:
        result = await session.execute(
            select(Character).where(Character.rarity == rarity.lower()).order_by(func.random()).limit(1)
        )
        return result.scalar_one_or_none()

    counts: Dict[str, int] = {}
    for r in RARITY_ORDER:
        c = (await session.execute(
            select(func.count()).select_from(Character).where(Character.rarity == r)
        )).scalar() or 0
        if c:
            counts[r] = c
    if not counts:
        return None

    pool = list(counts.keys())
    weights = [RARITY_WEIGHTS.get(r, 10) * max(1, counts[r] // 3 + 1) for r in pool]
    chosen = random.choices(pool, weights=weights, k=1)[0]
    result = await session.execute(
        select(Character).where(Character.rarity == chosen).order_by(func.random()).limit(1)
    )
    return result.scalar_one_or_none()


async def _remove_one_copy(session, user_id: int, char_id: int, prefer_non_fav: bool = True) -> bool:
    """Delete one owned copy of a character, preferring a non-favorited
    copy first so smelting/gifting away duplicates doesn't eat the
    user's favorite if they happen to have it flagged among several."""
    q = select(UserCharacter).where(UserCharacter.user_id == user_id, UserCharacter.character_id == char_id)
    q = q.order_by(UserCharacter.is_favorite.asc(), UserCharacter.id.asc()) if prefer_non_fav else q.order_by(UserCharacter.id.asc())
    row = (await session.execute(q.limit(1))).scalar_one_or_none()
    if not row:
        return False
    await session.delete(row)
    return True


async def _is_private_collection(user_id: int) -> bool:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user or not user.notes:
            return False
        return bool(user.notes.get("collector_private"))


def _char_caption(char: Character, qty: int = 1, extra: str = "") -> str:
    qty_str = f" ×{qty}" if qty > 1 else ""
    return (
        f"{_rarity_emoji(char.rarity)} <b>{escape_html(char.name)}</b>{qty_str}\n"
        f"⛩️ <i>{escape_html(char.anime or 'Unknown')}</i>\n"
        f"🆔 <code>#{char.id}</code>  •  {char.rarity.title()}\n"
        f"{extra}"
    ).strip()


# ==================== SPAWN / GRAB ====================

async def _do_spawn(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        char = await _pick_character(session)
    if not char:
        return

    _current_spawns[chat_id] = {
        "id": char.id, "name": char.name, "anime": char.anime,
        "rarity": char.rarity, "image_url": char.image_url,
    }
    caption = (
        f"🎉 A wild {_rarity_emoji(char.rarity)} <b>{char.rarity.title()}</b> character appeared!\n\n"
        f"Series: {escape_html(char.anime or 'Unknown')}\n"
        f"Type <code>/grab &lt;name&gt;</code> to catch it!"
    )
    try:
        if char.image_url and _is_video(char.image_url):
            await context.bot.send_video(chat_id, video=char.image_url, caption=caption, parse_mode="HTML")
        elif char.image_url:
            await context.bot.send_photo(chat_id, photo=char.image_url, caption=caption, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id, caption, parse_mode="HTML")
    except Exception as e:
        logger.warning("spawn_send_failed, falling back to text", chat_id=chat_id, char_id=char.id, error=str(e))
        try:
            await context.bot.send_message(chat_id, caption, parse_mode="HTML")
        except Exception:
            logger.exception("spawn_send_failed_completely", chat_id=chat_id, char_id=char.id)


@safe_handler
async def spawn_listener(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ambient spawns based on chat activity — own handler group, see
    group_mgmt.py's register() for why every catch-all text handler
    needs one."""
    if not settings.enable_collector or not update.effective_chat or update.effective_chat.type == "private":
        return
    chat_id = update.effective_chat.id

    async with async_session() as session:
        chat_row = await session.get(Chat, chat_id)
        rate = (chat_row.collector_spawn_rate if chat_row else None) or settings.collector_default_spawn_rate

    _message_counters[chat_id] = _message_counters.get(chat_id, 0) + 1
    if _message_counters[chat_id] >= rate and chat_id not in _current_spawns:
        _message_counters[chat_id] = 0
        await _do_spawn(chat_id, context)


async def periodic_spawn_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Low-probability ambient spawn independent of message volume, so a
    quiet chat isn't starved of spawns just because message-count-based
    triggering needs activity to fire. Runs every 30 minutes (see
    register()); each tracked chat has a flat 15% chance per run,
    supplementing rather than replacing the activity-based trigger."""
    for chat_id in list(_message_counters.keys()):
        if chat_id in _current_spawns:
            continue
        if random.random() < 0.15:
            await _do_spawn(chat_id, context)


@group_only
@safe_handler
async def spawn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text("🚫 Collector moderators only. See /mods")
        return
    await _do_spawn(update.effective_chat.id, context)


@group_only
@safe_handler
async def grab_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _cooldown_ok(_last_grab, user.id, GRAB_COOLDOWN_SECONDS):
        return  # silent — no need to scold someone for mashing the button
    # _cooldown_ok is in-memory/per-process (fine for the tight 1.5s cadence
    # check above, but resets on restart and doesn't hold across workers if
    # this ever runs as more than one process). This second, looser check
    # goes through the shared cache, so it holds even then.
    ok, _ = await hit(f"grab:{user.id}", limit=40, window_seconds=60)
    if not ok:
        await update.effective_message.reply_text("Slow down on grabs.")
        return
    chat_id = update.effective_chat.id

    async with _grab_locks[chat_id]:
        spawn = _current_spawns.get(chat_id)
        if not spawn:
            await update.message.reply_text("❌ Nothing to grab right now — wait for a spawn.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /grab <character name>")
            return

        guess = " ".join(context.args).strip().lower()
        target = spawn["name"].lower()
        # Exact match, or a substantial (4+ char) fragment of the name —
        # forgiving enough for awkward romanizations without letting a
        # single short/common word trivially match everything.
        if guess != target and not (len(guess) >= 4 and guess in target):
            await update.message.reply_text("❌ Wrong name!")
            return

        # The lock above is defense in depth; the real guarantee is that
        # there's no `await` between reading _current_spawns and this
        # delete, so under asyncio's cooperative scheduling a second
        # near-simultaneous correct guess finds the spawn already gone
        # even without the lock. The lock protects this invariant from
        # quietly breaking if this block ever grows an await in between.
        del _current_spawns[chat_id]

        async with async_session() as session:
            await _ensure_user(session, user)
            session.add(UserCharacter(user_id=user.id, character_id=spawn["id"]))
            await session.commit()

    new_balance = await add_coins(chat_id, user.id, settings.collector_catch_reward_coins)
    await update.message.reply_text(
        f"🎉 <b>{escape_html(user.first_name)}</b> caught <b>{escape_html(spawn['name'])}</b> "
        f"({_rarity_emoji(spawn['rarity'])} {spawn['rarity'].title()})! "
        f"+{settings.collector_catch_reward_coins} 🪙 (balance: {new_balance:,})",
        parse_mode="HTML",
    )


# ==================== VIEWING ====================

@safe_handler
async def collection_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    target_id, target_name = update.effective_user.id, update.effective_user.first_name
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        target_name = update.message.reply_to_message.from_user.first_name

    if target_id != update.effective_user.id and await _is_private_collection(target_id) \
            and not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text(f"🔒 {escape_html(target_name)}'s collection is private.", parse_mode="HTML")
        return

    async with async_session() as session:
        result = await session.execute(
            select(UserCharacter, Character)
            .join(Character, Character.id == UserCharacter.character_id)
            .where(UserCharacter.user_id == target_id)
            .order_by(_rarity_order_expr(), Character.name)
        )
        rows = result.all()

    if not rows:
        await update.message.reply_text(f"📭 {escape_html(target_name)} hasn't caught anyone yet.", parse_mode="HTML")
        return

    lines_all = [
        f"{'⭐' if uc.is_favorite else _rarity_emoji(c.rarity)} <b>{escape_html(c.name)}</b> "
        f"({escape_html(c.anime or '?')}) — <code>{c.id}</code>"
        for uc, c in rows
    ]
    page_items, total, pages = paginate(lines_all, page, per_page=ITEMS_PER_PAGE)
    await update.message.reply_text(
        f"📚 <b>{escape_html(target_name)}'s Collection</b> ({total} total) — page {page}/{pages}\n\n"
        + "\n".join(page_items) + (f"\n\n<code>/collection {page + 1}</code> for more" if page < pages else ""),
        parse_mode="HTML",
    )


@safe_handler
async def cview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cview <character id> — full card for one character."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /cview <character id>")
        return
    async with async_session() as session:
        char = await session.get(Character, int(context.args[0]))
        if not char:
            await update.message.reply_text("❌ No character with that ID.")
            return
        qty = await session.execute(
            select(func.count()).select_from(UserCharacter).where(
                UserCharacter.user_id == update.effective_user.id, UserCharacter.character_id == char.id,
            )
        )
        owned_qty = qty.scalar() or 0

    extra = f"You own: {owned_qty}" if owned_qty else "You don't own this one yet."
    caption = _char_caption(char, extra=extra)
    try:
        if char.image_url and _is_video(char.image_url):
            await update.message.reply_video(video=char.image_url, caption=caption, parse_mode="HTML")
        elif char.image_url:
            await update.message.reply_photo(photo=char.image_url, caption=caption, parse_mode="HTML")
        else:
            await update.message.reply_text(caption, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(caption, parse_mode="HTML")


@safe_handler
async def crandom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/crandom [@user] — show a random character from your (or their) collection."""
    target_id, target_name = update.effective_user.id, update.effective_user.first_name
    if context.args or update.message.reply_to_message:
        resolved_id, resolved_name = await resolve_target_user(update, context)
        if resolved_id:
            target_id, target_name = resolved_id, resolved_name

    if target_id != update.effective_user.id and await _is_private_collection(target_id) \
            and not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text(f"🔒 {escape_html(target_name)}'s collection is private.", parse_mode="HTML")
        return

    async with async_session() as session:
        result = await session.execute(
            select(Character).join(UserCharacter, UserCharacter.character_id == Character.id)
            .where(UserCharacter.user_id == target_id).order_by(func.random()).limit(1)
        )
        char = result.scalar_one_or_none()
    if not char:
        await update.message.reply_text(f"📭 {escape_html(target_name)} hasn't caught anyone yet.", parse_mode="HTML")
        return

    caption = _char_caption(char, extra=f"From {escape_html(target_name)}'s collection")
    try:
        if _is_video(char.image_url):
            await update.message.reply_video(video=char.image_url, caption=caption, parse_mode="HTML")
        else:
            await update.message.reply_photo(photo=char.image_url, caption=caption, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(caption, parse_mode="HTML")


@safe_handler
async def characters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    async with async_session() as session:
        rows = (await session.execute(
            select(Character).order_by(_rarity_order_expr(), Character.anime, Character.name)
        )).scalars().all()
    if not rows:
        await update.message.reply_text("No characters uploaded yet. Collector mods: /upload")
        return
    lines_all = [f"{_rarity_emoji(c.rarity)} <b>{escape_html(c.name)}</b> ({escape_html(c.anime or '?')}) — <code>{c.id}</code>" for c in rows]
    page_items, total, pages = paginate(lines_all, page, per_page=20)
    await update.message.reply_text(
        f"📖 <b>All Characters</b> ({total}) — page {page}/{pages}\n\n"
        + "\n".join(page_items) + (f"\n\n<code>/characters {page + 1}</code> for more" if page < pages else ""),
        parse_mode="HTML",
    )


@safe_handler
async def owners_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/owners <character id> — who currently owns copies of this character."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /owners <character id>")
        return
    char_id = int(context.args[0])
    async with async_session() as session:
        char = await session.get(Character, char_id)
        if not char:
            await update.message.reply_text("❌ No character with that ID.")
            return
        result = await session.execute(
            select(UserCharacter.user_id, func.count().label("qty"), User)
            .join(User, User.id == UserCharacter.user_id)
            .where(UserCharacter.character_id == char_id)
            .group_by(UserCharacter.user_id, User.id)
            .order_by(func.count().desc())
            .limit(20)
        )
        rows = result.all()

    if not rows:
        await update.message.reply_text(f"Nobody owns <b>{escape_html(char.name)}</b> yet.", parse_mode="HTML")
        return

    is_mod = await _is_collector_mod(update.effective_user.id)
    lines = [f"👥 <b>Owners of {escape_html(char.name)}</b>\n"]
    for uid, qty, user in rows:
        if not is_mod and await _is_private_collection(uid):
            continue
        name = user.first_name or user.username or str(uid)
        lines.append(f"• {escape_html(name)} — ×{qty}")
    if len(lines) == 1:
        lines.append("(all owners have private collections)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ==================== PROFILE / STATS ====================

@safe_handler
async def fav_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /fav <character id> — see /collection for IDs")
        return
    char_id = int(context.args[0])
    async with async_session() as session:
        result = await session.execute(
            select(UserCharacter).where(UserCharacter.user_id == update.effective_user.id, UserCharacter.character_id == char_id)
        )
        owned = result.scalars().all()
        if not owned:
            await update.message.reply_text("❌ You don't own that character.")
            return
        # Clear any existing favorite, then set this one — only one
        # favorite at a time.
        existing_fav = await session.execute(
            select(UserCharacter).where(UserCharacter.user_id == update.effective_user.id, UserCharacter.is_favorite.is_(True))
        )
        for row in existing_fav.scalars().all():
            row.is_favorite = False
        owned[0].is_favorite = True
        await session.commit()
    await update.message.reply_text("⭐ Favorite set.")


@safe_handler
async def myprofile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with async_session() as session:
        total = (await session.execute(
            select(func.count()).select_from(UserCharacter).where(UserCharacter.user_id == user_id)
        )).scalar() or 0
        unique = (await session.execute(
            select(func.count(func.distinct(UserCharacter.character_id))).where(UserCharacter.user_id == user_id)
        )).scalar() or 0
        fav_result = await session.execute(
            select(Character).join(UserCharacter, UserCharacter.character_id == Character.id)
            .where(UserCharacter.user_id == user_id, UserCharacter.is_favorite.is_(True))
        )
        fav = fav_result.scalar_one_or_none()

        # Per-series completion — top 5 by % complete, matching the
        # spirit of a "gotta catch 'em all" progress view.
        series_totals = dict((await session.execute(
            select(Character.anime, func.count()).group_by(Character.anime)
        )).all())
        series_owned_rows = (await session.execute(
            select(Character.anime, func.count(func.distinct(Character.id)))
            .join(UserCharacter, UserCharacter.character_id == Character.id)
            .where(UserCharacter.user_id == user_id)
            .group_by(Character.anime)
        )).all()

    fav_line = f"⭐ Favorite: {escape_html(fav.name)}" if fav else "⭐ Favorite: none set (/fav <id>)"

    progress_lines = []
    scored = []
    for anime, owned_count in series_owned_rows:
        series_total = series_totals.get(anime) or 0
        if not anime or not series_total:
            continue
        pct = owned_count / series_total * 100
        scored.append((pct, anime, owned_count, series_total))
    scored.sort(reverse=True)
    for pct, anime, owned_count, series_total in scored[:5]:
        progress_lines.append(f"  {escape_html(anime)}: {owned_count}/{series_total} ({pct:.0f}%)")
    progress_block = ("\n\n📊 <b>Series progress</b>\n" + "\n".join(progress_lines)) if progress_lines else ""

    await update.message.reply_text(
        f"👤 <b>{escape_html(update.effective_user.first_name)}'s Collector Profile</b>\n\n"
        f"Total caught: <b>{total}</b>\nUnique characters: <b>{unique}</b>\n{fav_line}{progress_block}",
        parse_mode="HTML",
    )


@safe_handler
async def cstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cstats — bot-wide collector stats: pool size by rarity, total catches."""
    async with async_session() as session:
        by_rarity = dict((await session.execute(
            select(Character.rarity, func.count()).group_by(Character.rarity)
        )).all())
        total_chars = sum(by_rarity.values())
        total_catches = (await session.execute(select(func.count()).select_from(UserCharacter))).scalar() or 0
        total_catchers = (await session.execute(
            select(func.count(func.distinct(UserCharacter.user_id)))
        )).scalar() or 0

    lines = ["📊 <b>Collector Stats</b>\n", f"Characters in pool: <b>{total_chars}</b>"]
    for r in RARITY_ORDER:
        if by_rarity.get(r):
            lines.append(f"  {_rarity_emoji(r)} {r.title()}: {by_rarity[r]}")
    lines.append(f"\nTotal catches: <b>{total_catches:,}</b>")
    lines.append(f"Unique catchers: <b>{total_catchers:,}</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def cprivacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cprivacy on|off — hide your collection from /collection, /crandom, /owners for non-mods."""
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /cprivacy on|off")
        return
    want_private = context.args[0].lower() == "on"
    user_id = update.effective_user.id
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            user = User(id=user_id, username=update.effective_user.username, first_name=update.effective_user.first_name or "")
            session.add(user)
            await session.flush()
        notes = dict(user.notes or {})
        notes["collector_private"] = want_private
        user.notes = notes
        await session.commit()
    await update.message.reply_text(
        "🔒 Your collection is now private." if want_private else "🔓 Your collection is now public."
    )


@group_only
@safe_handler
async def topcatchers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(
            select(UserCharacter.user_id, func.count().label("total"), User)
            .join(User, User.id == UserCharacter.user_id)
            .group_by(UserCharacter.user_id, User.id)
            .order_by(func.count().desc())
            .limit(10)
        )
        rows = result.all()
    if not rows:
        await update.message.reply_text("Nobody has caught anyone yet.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Top Catchers</b>\n"]
    for i, (uid, total, user) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = user.first_name or user.username or str(uid)
        lines.append(f"{prefix} {escape_html(name)} — {total} caught")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ==================== ECONOMY (SMELT) ====================

@safe_handler
async def smelt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/smelt <character id> [qty] — trade duplicate copies for coins."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /smelt <character id> [quantity]")
        return
    char_id = int(context.args[0])
    qty = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 1
    qty = max(1, qty)
    user_id = update.effective_user.id

    async with async_session() as session:
        char = await session.get(Character, char_id)
        if not char:
            await update.message.reply_text("❌ No character with that ID.")
            return
        owned_count = (await session.execute(
            select(func.count()).select_from(UserCharacter).where(
                UserCharacter.user_id == user_id, UserCharacter.character_id == char_id,
            )
        )).scalar() or 0
        # Always keep at least one copy — smelting is for duplicates.
        smeltable = max(0, owned_count - 1)
        qty = min(qty, smeltable)
        if qty < 1:
            await update.message.reply_text("❌ You need at least 2 copies to smelt one — this keeps your last copy safe.")
            return
        for _ in range(qty):
            await _remove_one_copy(session, user_id, char_id)
        await session.commit()

    value_each = SMELT_VALUES.get(char.rarity, 5)
    total_value = value_each * qty
    new_balance = await add_coins(update.effective_chat.id, user_id, total_value)
    await update.message.reply_text(
        f"🔥 Smelted {qty}× <b>{escape_html(char.name)}</b> for {total_value:,} 🪙 (balance: {new_balance:,})",
        parse_mode="HTML",
    )


# ==================== TRADING & GIFTING ====================

@group_only
@safe_handler
async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("Reply to the user you want to trade with.")
        return
    if len(context.args) != 2 or not all(a.isdigit() for a in context.args):
        await update.message.reply_text("Usage: reply to them + <code>/trade &lt;your char id&gt; &lt;their char id&gt;</code>", parse_mode="HTML")
        return

    my_id, their_id = int(context.args[0]), int(context.args[1])
    sender, receiver = update.effective_user, update.message.reply_to_message.from_user
    if sender.id == receiver.id:
        await update.message.reply_text("You can't trade with yourself.")
        return

    async with async_session() as session:
        mine = (await session.execute(
            select(UserCharacter).where(UserCharacter.user_id == sender.id, UserCharacter.character_id == my_id)
        )).scalars().first()
        theirs = (await session.execute(
            select(UserCharacter).where(UserCharacter.user_id == receiver.id, UserCharacter.character_id == their_id)
        )).scalars().first()
        if not mine:
            await update.message.reply_text("❌ You don't own that character.")
            return
        if not theirs:
            await update.message.reply_text(f"❌ {escape_html(receiver.first_name)} doesn't own that character.", parse_mode="HTML")
            return
        my_char = await session.get(Character, my_id)
        their_char = await session.get(Character, their_id)

    _pending_trades[(sender.id, receiver.id)] = {
        "my_char": my_id, "their_char": their_id,
        "expires": datetime.utcnow() + timedelta(minutes=settings.collector_trade_expiry_minutes),
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"collector:trade_ok:{sender.id}:{receiver.id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"collector:trade_no:{sender.id}:{receiver.id}"),
    ]])
    await update.message.reply_text(
        f"🔄 {escape_html(sender.first_name)} offers <b>{escape_html(my_char.name)}</b> for "
        f"{escape_html(receiver.first_name)}'s <b>{escape_html(their_char.name)}</b>.\n"
        f"{escape_html(receiver.first_name)}, accept?",
        parse_mode="HTML", reply_markup=keyboard,
    )


async def _handle_trade_button(query, action: str, sender_id: int, receiver_id: int):
    if query.from_user.id != receiver_id:
        await query.answer("This trade isn't for you.", show_alert=False)
        return
    trade = _pending_trades.pop((sender_id, receiver_id), None)
    if not trade or datetime.utcnow() > trade["expires"]:
        await query.answer("This trade has expired.", show_alert=False)
        return

    if action == "trade_no":
        await query.answer("Declined.")
        await query.edit_message_text("❌ Trade declined.")
        return

    async with async_session() as session:
        mine = (await session.execute(
            select(UserCharacter).where(UserCharacter.user_id == sender_id, UserCharacter.character_id == trade["my_char"])
        )).scalars().first()
        theirs = (await session.execute(
            select(UserCharacter).where(UserCharacter.user_id == receiver_id, UserCharacter.character_id == trade["their_char"])
        )).scalars().first()
        if not mine or not theirs:
            await query.answer("One side no longer owns their character.", show_alert=True)
            await query.edit_message_text("❌ Trade fell through — a character changed hands since this was proposed.")
            return
        mine.user_id, theirs.user_id = receiver_id, sender_id
        await session.commit()

    await query.answer("Trade complete!")
    await query.edit_message_text("✅ Trade complete!")


@group_only
@safe_handler
async def gift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id, target_name = await resolve_target_user(update, context)
    char_id = next((int(a) for a in context.args if a.isdigit()), None)
    if not target_id or char_id is None:
        await update.message.reply_text("Usage: reply to someone (or /gift @username) + a character id, e.g. /gift 42")
        return
    if target_id == update.effective_user.id:
        await update.message.reply_text("❌ Can't gift to yourself.")
        return

    async with async_session() as session:
        owned = (await session.execute(
            select(UserCharacter).where(UserCharacter.user_id == update.effective_user.id, UserCharacter.character_id == char_id)
        )).scalars().first()
        if not owned:
            await update.message.reply_text("❌ You don't own that character.")
            return
        target_row = await session.get(User, target_id)
        if target_row is None:
            session.add(User(id=target_id, first_name=target_name or ""))
            await session.flush()
        owned.user_id = target_id
        owned.is_favorite = False
        char = await session.get(Character, char_id)
        await session.commit()

    await update.message.reply_text(f"🎁 Gifted <b>{escape_html(char.name)}</b> to {escape_html(target_name)}.", parse_mode="HTML")


# ==================== BULK ADMIN TOOLS ====================

@safe_handler
async def giveany_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/giveany <character id> — reply to a user to hand them a copy. Collector-mod only."""
    if not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text("🚫 Collector moderators only.")
        return
    target_id, target_name = await resolve_target_user(update, context)
    char_id = next((int(a) for a in context.args if a.isdigit()), None)
    if not target_id or char_id is None:
        await update.message.reply_text("Usage: reply to a user + /giveany <character id>")
        return
    async with async_session() as session:
        char = await session.get(Character, char_id)
        if not char:
            await update.message.reply_text("❌ No character with that ID.")
            return
        target_row = await session.get(User, target_id)
        if target_row is None:
            session.add(User(id=target_id, first_name=target_name or ""))
            await session.flush()
        session.add(UserCharacter(user_id=target_id, character_id=char_id))
        await session.commit()
    await update.message.reply_text(f"✅ Gave <b>{escape_html(char.name)}</b> to {escape_html(target_name)}.", parse_mode="HTML")


@safe_handler
async def takeany_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/takeany <character id> — reply to a user to remove one copy. Collector-mod only."""
    if not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text("🚫 Collector moderators only.")
        return
    target_id, target_name = await resolve_target_user(update, context)
    char_id = next((int(a) for a in context.args if a.isdigit()), None)
    if not target_id or char_id is None:
        await update.message.reply_text("Usage: reply to a user + /takeany <character id>")
        return
    async with async_session() as session:
        removed = await _remove_one_copy(session, target_id, char_id, prefer_non_fav=False)
        await session.commit()
    if removed:
        await update.message.reply_text(f"✅ Removed one copy from {escape_html(target_name)}.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ {escape_html(target_name)} doesn't own that character.", parse_mode="HTML")


def _make_confirm(kind: str, payload: dict, requested_by: int) -> str:
    confirm_id = f"{kind}:{requested_by}:{int(time.time() * 1000)}"
    _pending_confirms[confirm_id] = {
        **payload, "kind": kind, "requested_by": requested_by,
        "expires": datetime.utcnow() + timedelta(seconds=CONFIRM_EXPIRY_SECONDS),
    }
    return confirm_id


def _confirm_keyboard(confirm_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"collector:confirm:{confirm_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"collector:cancel:{confirm_id}"),
    ]])


@safe_handler
async def transferall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/transferall — reply to a user to MOVE their entire collection to
    another user. Sudo only, and requires confirmation — this is
    hard to reverse."""
    if not await _is_super_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo only — this moves an entire collection.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Usage: reply to the SOURCE user, then /transferall <destination user id>")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: reply to the SOURCE user, then /transferall <destination user id>")
        return
    from_user = update.message.reply_to_message.from_user
    to_id = int(context.args[0])
    if from_user.id == to_id:
        await update.message.reply_text("❌ Source and destination are the same user.")
        return

    async with async_session() as session:
        count = (await session.execute(
            select(func.count()).select_from(UserCharacter).where(UserCharacter.user_id == from_user.id)
        )).scalar() or 0
    if not count:
        await update.message.reply_text(f"{escape_html(from_user.first_name)} has nothing to transfer.", parse_mode="HTML")
        return

    confirm_id = _make_confirm("transferall", {"from_id": from_user.id, "to_id": to_id, "count": count}, update.effective_user.id)
    await update.message.reply_text(
        f"⚠️ Move all {count} characters from {escape_html(from_user.first_name)} to <code>{to_id}</code>?\n"
        f"This cannot be undone. Expires in {CONFIRM_EXPIRY_SECONDS}s.",
        parse_mode="HTML", reply_markup=_confirm_keyboard(confirm_id),
    )


@safe_handler
async def giftall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/giftall — reply to a user to COPY their entire collection to
    another user (source keeps theirs). Sudo only, with confirmation."""
    if not await _is_super_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo only — this duplicates an entire collection.")
        return
    if not update.message.reply_to_message or not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: reply to the SOURCE user, then /giftall <destination user id>")
        return
    from_user = update.message.reply_to_message.from_user
    to_id = int(context.args[0])
    if from_user.id == to_id:
        await update.message.reply_text("❌ Source and destination are the same user.")
        return

    async with async_session() as session:
        count = (await session.execute(
            select(func.count()).select_from(UserCharacter).where(UserCharacter.user_id == from_user.id)
        )).scalar() or 0
    if not count:
        await update.message.reply_text(f"{escape_html(from_user.first_name)} has nothing to copy.", parse_mode="HTML")
        return

    confirm_id = _make_confirm("giftall", {"from_id": from_user.id, "to_id": to_id, "count": count}, update.effective_user.id)
    await update.message.reply_text(
        f"⚠️ Copy all {count} characters from {escape_html(from_user.first_name)} to <code>{to_id}</code> "
        f"(they keep theirs too)?\nExpires in {CONFIRM_EXPIRY_SECONDS}s.",
        parse_mode="HTML", reply_markup=_confirm_keyboard(confirm_id),
    )


async def _run_confirm(query, confirm_id: str) -> None:
    payload = _pending_confirms.pop(confirm_id, None)
    if not payload:
        await query.answer("This confirmation has expired or was already used.", show_alert=True)
        return
    if query.from_user.id != payload["requested_by"]:
        await query.answer("Only the admin who requested this can confirm it.", show_alert=True)
        return
    if datetime.utcnow() > payload["expires"]:
        await query.answer("This confirmation has expired.", show_alert=True)
        return

    kind = payload["kind"]
    async with async_session() as session:
        if kind == "transferall":
            rows = (await session.execute(
                select(UserCharacter).where(UserCharacter.user_id == payload["from_id"])
            )).scalars().all()
            for row in rows:
                row.user_id = payload["to_id"]
            await session.commit()
            await query.answer("Transfer complete!")
            await query.edit_message_text(f"✅ Moved {len(rows)} characters to <code>{payload['to_id']}</code>.", parse_mode="HTML")
        elif kind == "giftall":
            rows = (await session.execute(
                select(UserCharacter).where(UserCharacter.user_id == payload["from_id"])
            )).scalars().all()
            dest_row = await session.get(User, payload["to_id"])
            if dest_row is None:
                session.add(User(id=payload["to_id"], first_name=""))
                await session.flush()
            for row in rows:
                session.add(UserCharacter(user_id=payload["to_id"], character_id=row.character_id))
            await session.commit()
            await query.answer("Copy complete!")
            await query.edit_message_text(f"✅ Copied {len(rows)} characters to <code>{payload['to_id']}</code>.", parse_mode="HTML")


# ==================== MODERATION ====================

@safe_handler
async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/upload Name | Anime | rarity — reply to a photo/video, attach one
    with the command as its caption, or (for collector mods only) just
    send a photo/video captioned "Name | Anime | rarity" with no /upload
    needed. Collector-mod only — not @admin_only, see module docstring."""
    if not await _is_collector_mod(update.effective_user.id):
        return  # silent for the no-command-text caption-only path; explicit commands still get a reply below

    media = update.message.photo[-1] if update.message.photo else (update.message.video if update.message.video else None)
    if not media and update.message.reply_to_message:
        reply = update.message.reply_to_message
        media = (reply.photo[-1] if reply.photo else reply.video) if (reply.photo or reply.video) else None
    is_video = bool(update.message.video) or bool(
        update.message.reply_to_message and update.message.reply_to_message.video
    )

    raw = (update.message.text or update.message.caption or "")
    # Strip a leading "/upload" if present; otherwise treat the whole
    # caption as the field list (the caption-only upload path).
    text = raw.split(maxsplit=1)[1] if raw.lower().startswith("/upload") and " " in raw else raw
    if raw.lower().startswith("/upload") and " " not in raw:
        text = ""

    if "|" not in text:
        if raw.lower().startswith("/upload"):
            await update.message.reply_text(
                "Usage: /upload Name | Anime | rarity (rarity required: "
                + ", ".join(RARITY_EMOJIS) + ")\nAttach a photo/video, or reply to one."
            )
        return  # caption-only path: not an upload attempt, ignore quietly

    if not media:
        await update.message.reply_text("Attach a photo/video (or reply to one) with caption: Name | Anime | rarity")
        return

    fields = [f.strip() for f in text.split("|")]
    if len(fields) < 3:
        await update.message.reply_text("Usage: Name | Anime | rarity — all three required.")
        return
    name, anime, rarity_raw = fields[0], fields[1], fields[2]
    rarity = rarity_raw.lower()
    if not name or not anime:
        await update.message.reply_text("Name and Anime can't be empty.")
        return
    # Accept both "Common" (legacy simple-bot style) and "common"
    if rarity not in RARITY_EMOJIS:
        await update.message.reply_text(
            f"❌ Invalid rarity '{escape_html(rarity_raw)}'. Choose: {', '.join(RARITY_EMOJIS)}",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        existing = (await session.execute(
            select(Character).where(Character.name == name, Character.anime == anime)
        )).scalars().first()
        if existing:
            await update.message.reply_text(f"⚠️ Already exists as <code>#{existing.id}</code>.", parse_mode="HTML")
            return

        # Store the stable file_id, not a fetched file_path — Telegram
        # only guarantees the latter for a short window. See module
        # docstring.
        char = Character(name=name, anime=anime, rarity=rarity, image_url=media.file_id, added_by=update.effective_user.id)
        session.add(char)
        await session.commit()
        char_id = char.id

    kind = "video" if is_video else "photo"
    await update.message.reply_text(
        f"✅ Uploaded <b>{escape_html(name)}</b> ({kind}, {_rarity_emoji(rarity)} {rarity.title()}) — ID <code>{char_id}</code>",
        parse_mode="HTML",
    )


@safe_handler
async def delchar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a character from the catalogue: /delchar <id> [force]

    v2.10 told moderators "existing copies in collections are kept", but ``user_characters`` has
    ``ON DELETE CASCADE``: on Postgres every player's copies were silently wiped, on SQLite (which
    does not enforce the key) they became orphan rows. Now a character that players own is only
    deleted with an explicit ``force`` - which removes those copies on every database - and one
    that an unfinished giveaway is handing out cannot be deleted at all."""
    if not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text("🚫 Collector moderators only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delchar <character id> [force]")
        return
    char_id = int(context.args[0])
    force = len(context.args) > 1 and context.args[1].lower() == "force"
    async with async_session() as session:
        char = await session.get(Character, char_id)
        if not char:
            await update.message.reply_text("❌ No character with that ID.")
            return
        live_giveaway = (await session.execute(
            select(Giveaway.id).where(Giveaway.character_id == char_id, Giveaway.ended.is_(False)).limit(1)
        )).first()
        if live_giveaway:
            await update.message.reply_text("🚫 A running giveaway is handing this character out — /endgiveaway it first.")
            return
        owned = (await session.execute(
            select(func.count()).select_from(UserCharacter).where(UserCharacter.character_id == char_id)
        )).scalar() or 0
        if owned and not force:
            await update.message.reply_text(
                f"⚠️ {owned} cop{'y is' if owned == 1 else 'ies are'} in players' collections. "
                f"<code>/delchar {char_id} force</code> deletes the character <b>and</b> those copies.",
                parse_mode="HTML")
            return
        if owned:
            await session.execute(delete(UserCharacter).where(UserCharacter.character_id == char_id))
        await session.execute(sa_update(Giveaway).where(Giveaway.character_id == char_id).values(character_id=None))
        await session.delete(char)
        await session.commit()
    await update.message.reply_text(f"🗑 Character deleted{f' along with {owned} player cop' + ('y' if owned == 1 else 'ies') if owned else ''}.")


_EDITABLE_FIELDS = {"name": "name", "anime": "anime", "rarity": "rarity"}


@safe_handler
async def editchar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit a catalogue character (ported from WAIFU-HUSBANDO-CATCHER /update): /editchar <id> <name|anime|rarity> <value>"""
    if not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text("🚫 Collector moderators only.")
        return
    args = list(context.args or [])
    if len(args) < 3 or not args[0].isdigit() or args[1].lower() not in _EDITABLE_FIELDS:
        await update.message.reply_text("Usage: /editchar <id> <name|anime|rarity> <new value>")
        return
    field, value = _EDITABLE_FIELDS[args[1].lower()], " ".join(args[2:]).strip()
    if field == "rarity":
        value = value.lower()
        if value not in RARITY_EMOJIS:
            await update.message.reply_text(f"❌ Invalid rarity. Choose: {', '.join(RARITY_EMOJIS)}")
            return
    elif not value or len(value) > 128:
        await update.message.reply_text("❌ The value must be 1-128 characters.")
        return
    async with async_session() as session:
        char = await session.get(Character, int(args[0]))
        if not char:
            await update.message.reply_text("❌ No character with that ID.")
            return
        if field in ("name", "anime"):    # /upload refuses duplicates; editing must not create one
            name, anime = (value, char.anime) if field == "name" else (char.name, value)
            clash = (await session.execute(
                select(Character.id).where(Character.name == name, Character.anime == anime, Character.id != char.id).limit(1)
            )).first()
            if clash:
                await update.message.reply_text(f"⚠️ That would duplicate <code>#{clash[0]}</code>.", parse_mode="HTML")
                return
        setattr(char, field, value)
        await session.commit()
        char_id = char.id
    await update.message.reply_text(f"✅ #{char_id}: {field} is now <b>{escape_html(value)}</b>.", parse_mode="HTML")


@safe_handler
async def addmod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sudo only — deliberately stricter than being a collector mod
    yourself. See module docstring."""
    if not await _is_super_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo only.")
        return
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /addmod @username")
        return
    async with async_session() as session:
        if await session.get(CollectorModerator, target_id):
            await update.message.reply_text(f"{escape_html(target_name)} is already a collector mod.", parse_mode="HTML")
            return
        session.add(CollectorModerator(user_id=target_id, added_by=update.effective_user.id))
        await session.commit()
    await update.message.reply_text(f"✅ {escape_html(target_name)} can now /upload, /delchar, /giveany, /takeany, /giveaway.", parse_mode="HTML")


@safe_handler
async def removemod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_super_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo only.")
        return
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        await update.message.reply_text("Reply to a user, or /removemod @username")
        return
    async with async_session() as session:
        mod = await session.get(CollectorModerator, target_id)
        if not mod:
            await update.message.reply_text(f"{escape_html(target_name)} isn't a collector mod.", parse_mode="HTML")
            return
        await session.delete(mod)
        await session.commit()
    await update.message.reply_text(f"❌ {escape_html(target_name)} is no longer a collector mod.", parse_mode="HTML")


@safe_handler
async def mods_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        rows = (await session.execute(select(CollectorModerator))).scalars().all()
    if not rows:
        await update.message.reply_text("No collector moderators yet (bot sudoers can always /upload).")
        return
    lines = "\n".join(f"• <code>{r.user_id}</code>" for r in rows)
    await update.message.reply_text(f"🛡 <b>Collector moderators</b>\n{lines}", parse_mode="HTML")


@group_only
@admin_only
@safe_handler
async def setspawnrate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Per-chat setting — @admin_only (this chat's admins) is the right
    gate here, unlike the bot-wide actions above."""
    if not context.args or not context.args[0].isdigit() or int(context.args[0]) < 5:
        await update.message.reply_text("Usage: /setspawnrate <messages ≥ 5>")
        return
    rate = int(context.args[0])
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(chat)
        chat.collector_spawn_rate = rate
        await session.commit()
    await update.message.reply_text(f"⚙️ A character will now spawn roughly every {rate} messages.")


# ==================== GIVEAWAYS ====================

@group_only
@safe_handler
async def giveaway_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collector-mod only, not @admin_only — an unlimited-amount coin
    giveaway shouldn't be runnable by any admin of any chat. See module
    docstring. Optionally give away a character instead of coins:
    /giveaway char <character id> <minutes>"""
    if not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text("🚫 Collector moderators only.")
        return

    if context.args and context.args[0].lower() == "char":
        if len(context.args) < 3 or not context.args[1].isdigit() or not context.args[2].isdigit():
            await update.message.reply_text("Usage: /giveaway char <character id> <minutes>")
            return
        char_id, minutes = int(context.args[1]), int(context.args[2])
        async with async_session() as session:
            char = await session.get(Character, char_id)
            if not char:
                await update.message.reply_text("❌ No character with that ID.")
                return
            ga = Giveaway(
                chat_id=update.effective_chat.id, character_id=char_id,
                created_by=update.effective_user.id, ends_at=datetime.utcnow() + timedelta(minutes=minutes),
            )
            session.add(ga)
            await session.commit()
            ga_id = ga.id
        await update.message.reply_text(
            f"🎁 <b>Character giveaway!</b> {_rarity_emoji(char.rarity)} <b>{escape_html(char.name)}</b> — "
            f"first to /claim {ga_id} wins. Ends in {minutes}m.",
            parse_mode="HTML",
        )
        if context.job_queue:
            context.job_queue.run_once(_end_giveaway_job, when=minutes * 60, data={"giveaway_id": ga_id})
        return

    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Usage: /giveaway <coins> <minutes>  (or: /giveaway char <character id> <minutes>)")
        return
    coins, minutes = int(context.args[0]), int(context.args[1])
    async with async_session() as session:
        ga = Giveaway(
            chat_id=update.effective_chat.id, prize_coins=coins,
            created_by=update.effective_user.id, ends_at=datetime.utcnow() + timedelta(minutes=minutes),
        )
        session.add(ga)
        await session.commit()
        ga_id = ga.id

    await update.message.reply_text(
        f"🎁 <b>Giveaway started!</b> {coins:,} 🪙 — first to /claim {ga_id} wins. Ends in {minutes}m.",
        parse_mode="HTML",
    )
    if context.job_queue:
        context.job_queue.run_once(_end_giveaway_job, when=minutes * 60, data={"giveaway_id": ga_id})


async def _end_giveaway_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    ga_id = context.job.data["giveaway_id"]
    async with async_session() as session:
        ga = await session.get(Giveaway, ga_id)
        if not ga or ga.ended:
            return
        ga.ended = True
        await session.commit()
        chat_id, claimed_by = ga.chat_id, ga.claimed_by
    if not claimed_by:
        try:
            await context.bot.send_message(chat_id, f"🎁 Giveaway #{ga_id} ended — nobody claimed it in time.")
        except Exception:
            pass


@group_only
@safe_handler
async def claim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _cooldown_ok(_last_claim, user.id, CLAIM_COOLDOWN_SECONDS):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /claim <giveaway id>")
        return
    ga_id = int(context.args[0])
    async with async_session() as session:
        ga = await session.get(Giveaway, ga_id)
        if not ga or ga.chat_id != update.effective_chat.id:
            await update.message.reply_text("❌ No such giveaway here.")
            return
        if ga.ended or datetime.utcnow() > ga.ends_at:
            await update.message.reply_text("❌ That giveaway has already ended.")
            return
        ga.ended = True
        ga.claimed_by = user.id
        char_id = ga.character_id
        coins = ga.prize_coins
        if char_id:
            await _ensure_user(session, user)
            session.add(UserCharacter(user_id=user.id, character_id=char_id))
        await session.commit()

    if char_id:
        async with async_session() as session:
            char = await session.get(Character, char_id)
        await update.message.reply_text(
            f"🎉 {escape_html(user.first_name)} claimed {_rarity_emoji(char.rarity)} <b>{escape_html(char.name)}</b>!",
            parse_mode="HTML",
        )
    else:
        new_balance = await add_coins(update.effective_chat.id, user.id, coins)
        await update.message.reply_text(
            f"🎉 {escape_html(user.first_name)} claimed {coins:,} 🪙! (balance: {new_balance:,})",
            parse_mode="HTML",
        )


@group_only
@safe_handler
async def endgiveaway_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_collector_mod(update.effective_user.id):
        await update.message.reply_text("🚫 Collector moderators only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /endgiveaway <giveaway id>")
        return
    async with async_session() as session:
        ga = await session.get(Giveaway, int(context.args[0]))
        if not ga or ga.chat_id != update.effective_chat.id or ga.ended:
            await update.message.reply_text("❌ No active giveaway with that ID here.")
            return
        ga.ended = True
        await session.commit()
    await update.message.reply_text("🛑 Giveaway ended early.")


# ==================== HELP ====================

@safe_handler
async def chelp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_mod = await _is_collector_mod(update.effective_user.id)
    lines = [
        "🎴 <b>Collector commands</b>\n",
        "<b>Playing</b>",
        "/grab &lt;name&gt; — catch the current spawn",
        "/collection [page] [reply to someone] — view a collection",
        "/cview &lt;id&gt; — full card for one character",
        "/crandom [@user] — a random character from a collection",
        "/characters [page] — every character in the pool",
        "/owners &lt;id&gt; — who owns a character",
        "/fav &lt;id&gt; — set your favorite",
        "/smelt &lt;id&gt; [qty] — trade duplicates for coins",
        "/myprofile — your stats",
        "/cstats — bot-wide stats",
        "/cprivacy on|off — hide your collection from others",
        "/topcatchers — leaderboard",
        "/trade &lt;your id&gt; &lt;their id&gt; (reply) — propose a trade",
        "/gift &lt;id&gt; (reply) — give a character away",
    ]
    if is_mod:
        lines += [
            "\n<b>Collector moderator</b>",
            "/upload Name | Anime | rarity (attach/reply photo or video)",
            "/delchar &lt;id&gt;",
            "/giveany &lt;id&gt; (reply) / /takeany &lt;id&gt; (reply)",
            "/giveaway &lt;coins&gt; &lt;minutes&gt; / /giveaway char &lt;id&gt; &lt;minutes&gt;",
            "/endgiveaway &lt;id&gt; / /claim &lt;id&gt;",
        ]
    if await _is_super_admin(update.effective_user.id):
        lines += [
            "\n<b>Sudo</b>",
            "/addmod, /removemod, /mods",
            "/transferall &lt;dest id&gt; (reply to source) — move a whole collection",
            "/giftall &lt;dest id&gt; (reply to source) — copy a whole collection",
        ]
    lines.append("\n<b>In groups:</b> /setspawnrate &lt;n&gt; (chat admins)")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ==================== INLINE MODE ====================

async def character_inline_provider(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    """Share a character card inline: @botname <search text>. Uses the
    Cached* result types because image_url stores a Telegram file_id,
    not a fetchable URL — InlineQueryResultPhoto/Video need an actual
    URL in photo_url/video_url and silently fail to render given a
    file_id there; InlineQueryResultCachedPhoto/CachedVideo take a
    photo_file_id/video_file_id instead, which is what's actually
    stored.

    Registered with bot/core/inline.py (the shared inline router) so the
    whisper plugin can answer the same inline queries without either
    plugin starving the other."""
    query_text = (query_text or "").strip()
    async with async_session() as session:
        stmt = select(Character)
        if query_text:
            stmt = stmt.where(Character.name.ilike(f"%{query_text}%"))
        stmt = stmt.order_by(_rarity_order_expr(), Character.name).limit(INLINE_LIMIT)
        rows = (await session.execute(stmt)).scalars().all()

    results = []
    for char in rows:
        caption = _char_caption(char)
        if char.image_url and _is_video(char.image_url):
            results.append(InlineQueryResultCachedVideo(
                id=str(char.id), video_file_id=char.image_url,
                title=char.name, description=f"{char.rarity.title()} — {char.anime or 'Unknown'}",
                caption=caption, parse_mode="HTML",
            ))
        elif char.image_url:
            results.append(InlineQueryResultCachedPhoto(
                id=str(char.id), photo_file_id=char.image_url,
                title=char.name, description=f"{char.rarity.title()} — {char.anime or 'Unknown'}",
                caption=caption, parse_mode="HTML",
            ))
        else:
            results.append(InlineQueryResultArticle(
                id=str(char.id), title=char.name,
                description=f"{char.rarity.title()} — {char.anime or 'Unknown'}",
                input_message_content=InputTextMessageContent(caption, parse_mode="HTML"),
            ))
    return inline_router.InlineContribution(results=results)


@safe_handler
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answer an inline query through the shared router (kept as the collector's public entry
    point; the provider is (re-)registered first so calling this directly always works)."""
    inline_router.register_provider("collector", character_inline_provider, priority=50)
    await inline_router.dispatch(update, context)


# ==================== CALLBACK ROUTER ====================

@safe_handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data[len("collector:"):]
    parts = data.split(":", 1)
    action = parts[0]

    if action in ("trade_ok", "trade_no"):
        ids = parts[1].split(":")
        await _handle_trade_button(query, action, int(ids[0]), int(ids[1]))
        return

    if action == "confirm":
        await _run_confirm(query, parts[1])
        return

    if action == "cancel":
        _pending_confirms.pop(parts[1], None)
        await query.answer("Cancelled.")
        try:
            await query.edit_message_text("❌ Cancelled.")
        except Exception:
            pass
        return

    await query.answer()


# ==================== REGISTRATION ====================

def register(app):
    if not settings.enable_collector:
        return
    app.add_handler(CommandHandler("spawn", spawn_cmd))
    app.add_handler(CommandHandler(["grab", "protecc", "collect", "hunt"], grab_cmd))  # last three: WAIFU-HUSBANDO-CATCHER names
    app.add_handler(CommandHandler(["collection", "harem"], collection_cmd))  # harem: WAIFU-HUSBANDO-CATCHER name
    app.add_handler(CommandHandler("cview", cview_cmd))
    app.add_handler(CommandHandler("crandom", crandom_cmd))
    app.add_handler(CommandHandler("characters", characters_cmd))
    app.add_handler(CommandHandler("listcharacters", characters_cmd))  # legacy alias from simple collectors
    app.add_handler(CommandHandler("owners", owners_cmd))
    app.add_handler(CommandHandler("fav", fav_cmd))
    app.add_handler(CommandHandler("smelt", smelt_cmd))
    app.add_handler(CommandHandler("myprofile", myprofile_cmd))
    app.add_handler(CommandHandler("cstats", cstats_cmd))
    app.add_handler(CommandHandler("cprivacy", cprivacy_cmd))
    app.add_handler(CommandHandler(["topcatchers", "ctop"], topcatchers_cmd))
    app.add_handler(CommandHandler("chelp", chelp_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("gift", gift_cmd))
    app.add_handler(CommandHandler("giveany", giveany_cmd))
    app.add_handler(CommandHandler("takeany", takeany_cmd))
    app.add_handler(CommandHandler("transferall", transferall_cmd))
    app.add_handler(CommandHandler("giftall", giftall_cmd))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler(["delchar", "delete"], delchar_cmd))
    app.add_handler(CommandHandler(["editchar", "updatechar"], editchar_cmd))
    app.add_handler(CommandHandler("addmod", addmod_cmd))
    app.add_handler(CommandHandler("removemod", removemod_cmd))
    app.add_handler(CommandHandler("mods", mods_cmd))
    app.add_handler(CommandHandler(["setspawnrate", "changetime"], setspawnrate_cmd))
    app.add_handler(CommandHandler("giveaway", giveaway_cmd))
    app.add_handler(CommandHandler("endgiveaway", endgiveaway_cmd))
    app.add_handler(CommandHandler("claim", claim_cmd))
    inline_router.register_provider("collector", character_inline_provider, priority=50)
    inline_router.install(app)
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^collector:"))
    # Ambient spawns from ordinary chat activity.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, spawn_listener), group=7)
    # Caption-only upload path for collector mods: a photo/video sent
    # with "Name | Anime | rarity" as its caption, no /upload text
    # needed. upload_cmd itself distinguishes this from an unrelated
    # captioned photo (see its docstring) and silently ignores anything
    # that isn't actually an upload attempt.
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO) & filters.CAPTION, upload_cmd), group=8)
    if app.job_queue:
        app.job_queue.run_repeating(periodic_spawn_job, interval=1800, first=1800)
