"""
Federation tools + ban enforcement (v2.16).

Folds the parts of the Saitama / Masha ``feds.py`` module (KugisakiNobara-master, NobaraKugisaki-prince) that
``bot/plugins/federations.py`` did not have, without touching that plugin's behaviour:

  * Enforcement - a user who is gbanned, or fbanned in this chat's federation (or in a federation it
    subscribes to), is removed the moment they join or speak.  Before v2.16 gbans/fedbans were only applied
    to the chats that existed when the ban was issued, so a banned user could simply rejoin.
    ``/banenforce on|off`` lets a chat's admins switch this off.
  * Federation management: /myfeds /fedchats /renamefed /delfed /subfed /unsubfed /fedsubs /frules /setfrules
    /fednotif /setfedlog /unsetfedlog /fbroadcast /fedstat /fbanlist /importfbans /chatfed and the
    /fedhelp family.  Aliases /fban and /unfban point at the existing /fedban and /unfedban handlers.

Handler groups: the enforcer sits at group -6, before every middleware, so a banned user is dealt with before
anything else sees the update (see the map in group_mgmt.py).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import settings
from bot.core.database import Chat, FedBan, Federation, GlobalBan, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.plugins import federations as _fed
from bot.utils.helpers import escape_html, resolve_target_user
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# ── tunables ────────────────────────────────────────────────────────────────
ENFORCE_CACHE_TTL = 60.0          # seconds a "is this user banned here?" answer is trusted
ENFORCE_CACHE_MAX = 20_000        # entries; the cache is cleared wholesale when it grows past this
ENFORCE_RETRY_SECONDS = 300.0     # after a failed ban attempt (no rights), don't retry for this long
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 50_000
MAX_SUBSCRIPTIONS = 5
BROADCAST_DELAY = 0.06            # seconds between sends: stays under Telegram's ~20 msgs/min per group limit overall
BROADCAST_MAX_LEN = 3500
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# (chat_id, user_id) -> (checked_at_monotonic, reason_or_None)
_ban_cache: Dict[Tuple[int, int], Tuple[float, Optional[str]]] = {}
_acted: Dict[Tuple[int, int], float] = {}


def invalidate_enforcement_cache() -> None:
    """Forget cached ban answers - called after any gban/ungban/fedban/unfedban/import."""
    _ban_cache.clear()
    _acted.clear()


# ── pure helpers (unit-tested without Telegram) ─────────────────────────────
def csv_safe(value: Optional[str]) -> str:
    """Neutralise spreadsheet formula injection in exported ban reasons."""
    text = (value or "").replace("\r", " ").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t") else text


def parse_ban_rows(raw: bytes, *, limit: int = MAX_IMPORT_ROWS) -> List[Tuple[int, Optional[str]]]:
    """Parse an fban export (CSV, JSON, or plain ``id reason`` lines) into ``[(user_id, reason)]``.

    Raises ValueError on content that is not text or has no usable rows.  Duplicate ids keep their first
    reason; non-positive and non-numeric ids are skipped; at most ``limit`` rows are returned."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not UTF-8 text") from exc
    text = text.strip()
    if not text:
        raise ValueError("file is empty")

    candidates: List[Tuple[object, Optional[str]]] = []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc.msg}") from exc
        if isinstance(data, dict):
            if isinstance(data.get("bans"), list):
                data = data["bans"]
            else:
                data = [{"user_id": k, "reason": v} for k, v in data.items()]
        if not isinstance(data, list):
            raise ValueError("JSON must be a list or an object")
        for item in data:
            if isinstance(item, dict):
                candidates.append((item.get("user_id", item.get("id")), item.get("reason")))
            else:
                candidates.append((item, None))
    else:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if "," in lines[0]:
            for row in csv.reader(lines):
                if row:
                    candidates.append((row[0], row[1] if len(row) > 1 else None))
        else:
            for ln in lines:
                parts = ln.strip().split(None, 1)
                candidates.append((parts[0], parts[1] if len(parts) > 1 else None))

    seen: set = set()
    rows: List[Tuple[int, Optional[str]]] = []
    for raw_id, reason in candidates:
        try:
            uid = int(str(raw_id).strip())
        except (TypeError, ValueError):
            continue  # header row, blank, or junk
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        reason = (str(reason).strip()[:500] if reason not in (None, "") else None)
        rows.append((uid, reason))
        if len(rows) >= limit:
            break
    if not rows:
        raise ValueError("no valid user ids found")
    return rows


def render_ban_export(rows: List[Tuple[int, Optional[str], Optional[int]]], fmt: str = "csv") -> bytes:
    """Serialise ``[(user_id, reason, banned_by)]`` as CSV (default) or JSON."""
    if fmt == "json":
        payload = [{"user_id": u, "reason": r, "banned_by": b} for u, r, b in rows]
        return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "reason", "banned_by"])
    for uid, reason, by in rows:
        writer.writerow([uid, csv_safe(reason), by if by is not None else ""])
    return buf.getvalue().encode("utf-8")


# ── enforcement ─────────────────────────────────────────────────────────────
async def ban_reason_for(chat_id: int, user_id: int, *, use_cache: bool = True) -> Optional[str]:
    """Why ``user_id`` must not be in ``chat_id`` (gban / fban text), or None.  Cached for ENFORCE_CACHE_TTL."""
    key = (chat_id, user_id)
    now = time.monotonic()
    if use_cache:
        hit = _ban_cache.get(key)
        if hit and now - hit[0] < ENFORCE_CACHE_TTL:
            return hit[1]

    reason: Optional[str] = None
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        if chat is not None and chat.ban_enforce is False:
            reason = None
        else:
            gban = await session.get(GlobalBan, user_id)
            if gban is not None:
                reason = "gbanned" + (f": {gban.reason}" if gban.reason else "")
            elif chat is not None and chat.federation_id:
                fed = await session.get(Federation, chat.federation_id)
                if fed is not None:
                    ids = [fed.id] + [f for f in (fed.subscribed or []) if f != fed.id]
                    row = (await session.execute(
                        select(FedBan.reason, Federation.name)
                        .join(Federation, Federation.id == FedBan.federation_id)
                        .where(FedBan.federation_id.in_(ids), FedBan.user_id == user_id)
                        .limit(1)
                    )).first()
                    if row is not None:
                        reason = f"fbanned in {row[1]}" + (f": {row[0]}" if row[0] else "")

    if len(_ban_cache) >= ENFORCE_CACHE_MAX:
        _ban_cache.clear()
    _ban_cache[key] = (now, reason)
    return reason


@safe_handler
async def ban_enforcer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban gbanned/fbanned users the moment they join or speak. Never blocks other handlers."""
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None or chat.type not in ("group", "supergroup"):
        return

    users = [u for u in (msg.new_chat_members or ()) if not u.is_bot] or []
    sender = update.effective_user
    if sender is not None and not sender.is_bot and sender not in users:
        users.append(sender)

    for user in users:
        if settings.is_admin_id(user.id):
            continue
        reason = await ban_reason_for(chat.id, user.id)
        if not reason:
            continue
        key = (chat.id, user.id)
        now = time.monotonic()
        acted_at = _acted.get(key)  # monotonic() can be tiny right after boot: use None, not 0.0, as 'never'
        if acted_at is not None and now - acted_at < ENFORCE_RETRY_SECONDS:
            try:
                await msg.delete()
            except TelegramError:
                pass
            continue
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
        except TelegramError as exc:
            _acted[key] = now  # no rights / user is an admin: don't hammer the API on every message
            logger.info("ban_enforce_failed chat=%s user=%s err=%s", chat.id, user.id, exc)
            continue
        if msg.from_user is not None and msg.from_user.id == user.id:
            try:
                await msg.delete()
            except TelegramError:
                pass
        notify = True
        async with async_session() as session:
            row = await session.get(Chat, chat.id)
            if row is not None and row.federation_id:
                fed = await session.get(Federation, row.federation_id)
                notify = fed is None or fed.notify is not False
        if notify:
            try:
                await context.bot.send_message(
                    chat.id,
                    f"⛔ <a href=\"tg://user?id={user.id}\">{escape_html(user.first_name or str(user.id))}</a> "
                    f"(<code>{user.id}</code>) was {escape_html(reason)} - removed.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass


@admin_only
@safe_handler
async def banenforce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/banenforce [on|off] - remove gbanned/fbanned users when they join or speak in this chat."""
    arg = (context.args[0].lower() if context.args else "")
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type, title=update.effective_chat.title)
            session.add(chat)
        if arg in ("on", "off"):
            chat.ban_enforce = arg == "on"
            await session.commit()
            invalidate_enforcement_cache()
        state = chat.ban_enforce is not False
    if arg in ("on", "off"):
        await update.message.reply_text(f"⛔ Ban enforcement is now <b>{'on' if state else 'off'}</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"⛔ Ban enforcement is <b>{'on' if state else 'off'}</b> here.\nUsage: /banenforce on|off", parse_mode="HTML")


# ── shared lookups ──────────────────────────────────────────────────────────
async def _owned_or_admin_feds(session, user_id: int) -> List[Federation]:
    rows = (await session.execute(select(Federation).order_by(Federation.name))).scalars().all()
    return [f for f in rows if f.owner_id == user_id or user_id in (f.admins or [])]


async def _require_chat_fed(update: Update, session, *, owner_only: bool = False) -> Optional[Federation]:
    fed = await _fed._get_chat_fed(session, update.effective_chat.id)
    if fed is None:
        await update.message.reply_text("This chat isn't in a federation.")
        return None
    uid = update.effective_user.id
    if owner_only:
        if uid != fed.owner_id and not settings.is_admin_id(uid):
            await update.message.reply_text("🚫 Only the federation owner can do that.")
            return None
    elif not _fed._is_fed_admin(fed, uid):
        await update.message.reply_text("🚫 Federation admins only.")
        return None
    return fed


# ── management commands ─────────────────────────────────────────────────────
@safe_handler
async def myfeds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myfeds - federations you own or administer."""
    async with async_session() as session:
        feds = await _owned_or_admin_feds(session, update.effective_user.id)
    if not feds:
        await update.message.reply_text("You don't own or administer any federation.")
        return
    lines = [f"• <b>{escape_html(f.name)}</b> {'👑' if f.owner_id == update.effective_user.id else '🛡'}\n  <code>{f.id}</code>" for f in feds]
    await update.message.reply_text("🌐 <b>Your federations</b>\n" + "\n".join(lines), parse_mode="HTML")


@group_only
@safe_handler
async def chatfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatfed - which federation this chat belongs to."""
    async with async_session() as session:
        fed = await _fed._get_chat_fed(session, update.effective_chat.id)
    if fed is None:
        await update.message.reply_text("This chat isn't in a federation.")
        return
    await update.message.reply_text(f"🌐 This chat is in <b>{escape_html(fed.name)}</b>\n<code>{fed.id}</code>", parse_mode="HTML")


@group_only
@safe_handler
async def fedchats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fedchats - list the chats in this chat's federation (fed admins)."""
    async with async_session() as session:
        fed = await _require_chat_fed(update, session)
        if fed is None:
            return
        rows = (await session.execute(select(Chat.id, Chat.title).where(Chat.federation_id == fed.id))).all()
    body = "\n".join(f"• {escape_html(t or 'untitled')} <code>{cid}</code>" for cid, t in rows) or "(none)"
    await update.message.reply_text(f"🌐 <b>{escape_html(fed.name)}</b> - {len(rows)} chat(s)\n{body}"[:4000], parse_mode="HTML")


@group_only
@safe_handler
async def renamefed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/renamefed <name> - rename this chat's federation (owner)."""
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usage: /renamefed <new name>")
        return
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True)
        if fed is None:
            return
        fed.name = name[:64]
        await session.commit()
        new = fed.name
    await update.message.reply_text(f"🌐 Federation renamed to <b>{escape_html(new)}</b>.", parse_mode="HTML")


@group_only
@safe_handler
async def delfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delfed - delete this chat's federation and its ban list (owner, asks to confirm)."""
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True)
        if fed is None:
            return
        fed_id, name = fed.id, fed.name
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Delete", callback_data=f"fedtools:del:yes:{fed_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"fedtools:del:no:{fed_id}"),
    ]])
    await update.message.reply_text(
        f"⚠️ Delete <b>{escape_html(name)}</b>? Its ban list is erased and every chat is detached. This cannot be undone.",
        parse_mode="HTML", reply_markup=kb)


@safe_handler
async def delfed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, choice, fed_id = (query.data or "").split(":", 3)
    async with async_session() as session:
        fed = await session.get(Federation, fed_id)
        if fed is None:
            await query.answer("That federation no longer exists.", show_alert=True)
            return
        if query.from_user.id != fed.owner_id and not settings.is_admin_id(query.from_user.id):
            await query.answer("Only the federation owner can do that.", show_alert=True)
            return
        if choice == "no":
            await query.answer()
            await query.edit_message_text("Cancelled.")
            return
        name = fed.name
        for other in (await session.execute(select(Federation))).scalars().all():
            if fed_id in (other.subscribed or []):
                other.subscribed = [f for f in other.subscribed if f != fed_id]
        for chat in (await session.execute(select(Chat).where(Chat.federation_id == fed_id))).scalars().all():
            chat.federation_id = None
        await session.execute(delete(FedBan).where(FedBan.federation_id == fed_id))
        await session.delete(fed)
        await session.commit()
    invalidate_enforcement_cache()
    await query.answer()
    await query.edit_message_text(f"🗑 Federation {name} deleted.")


@group_only
@safe_handler
async def subfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/subfed <fed id> - also enforce another federation's ban list (owner)."""
    if not context.args or not _UUID.match(context.args[0]):
        await update.message.reply_text("Usage: /subfed <fed id>")
        return
    target = context.args[0].lower()
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True)
        if fed is None:
            return
        if target == fed.id:
            await update.message.reply_text("A federation can't subscribe to itself.")
            return
        if await session.get(Federation, target) is None:
            await update.message.reply_text("❌ No federation with that ID.")
            return
        subs = list(fed.subscribed or [])
        if target in subs:
            await update.message.reply_text("Already subscribed.")
            return
        if len(subs) >= MAX_SUBSCRIPTIONS:
            await update.message.reply_text(f"A federation can subscribe to at most {MAX_SUBSCRIPTIONS} others.")
            return
        fed.subscribed = subs + [target]
        await session.commit()
    invalidate_enforcement_cache()
    await update.message.reply_text("✅ Subscribed. Its fbans are now enforced here too.")


@group_only
@safe_handler
async def unsubfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unsubfed <fed id> - stop enforcing another federation's ban list (owner)."""
    if not context.args:
        await update.message.reply_text("Usage: /unsubfed <fed id>")
        return
    target = context.args[0].lower()
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True)
        if fed is None:
            return
        subs = list(fed.subscribed or [])
        if target not in subs:
            await update.message.reply_text("You aren't subscribed to that federation.")
            return
        fed.subscribed = [f for f in subs if f != target]
        await session.commit()
    invalidate_enforcement_cache()
    await update.message.reply_text("✅ Unsubscribed.")


@group_only
@safe_handler
async def fedsubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fedsubs - federations this one subscribes to (fed admins)."""
    async with async_session() as session:
        fed = await _require_chat_fed(update, session)
        if fed is None:
            return
        subs = list(fed.subscribed or [])
        names = {}
        for sid in subs:
            other = await session.get(Federation, sid)
            names[sid] = other.name if other else "(deleted)"
    body = "\n".join(f"• {escape_html(names[s])}\n  <code>{s}</code>" for s in subs) or "(none)"
    await update.message.reply_text(f"🌐 <b>{escape_html(fed.name)}</b> subscriptions\n{body}", parse_mode="HTML")


@group_only
@safe_handler
async def setfrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setfrules <text> - set the federation's rules (fed admins)."""
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /setfrules <rules text>")
        return
    async with async_session() as session:
        fed = await _require_chat_fed(update, session)
        if fed is None:
            return
        fed.rules = text[:3500]
        await session.commit()
    await update.message.reply_text("✅ Federation rules saved. Members can read them with /frules.")


@group_only
@safe_handler
async def frules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/frules - show the federation's rules."""
    async with async_session() as session:
        fed = await _fed._get_chat_fed(session, update.effective_chat.id)
    if fed is None:
        await update.message.reply_text("This chat isn't in a federation.")
        return
    if not fed.rules:
        await update.message.reply_text("This federation hasn't set any rules.")
        return
    await update.message.reply_text(f"📜 <b>{escape_html(fed.name)} rules</b>\n\n{escape_html(fed.rules)}", parse_mode="HTML")


@group_only
@safe_handler
async def fednotif_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fednotif [on|off] - announce enforced bans in member chats (owner)."""
    arg = (context.args[0].lower() if context.args else "")
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=arg in ("on", "off"))
        if fed is None:
            return
        if arg in ("on", "off"):
            fed.notify = arg == "on"
            await session.commit()
        state = fed.notify is not False
    await update.message.reply_text(
        f"🔔 Fed notifications are <b>{'on' if state else 'off'}</b>." + ("" if arg in ("on", "off") else "\nUsage: /fednotif on|off"),
        parse_mode="HTML")


@safe_handler
async def setfedlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setfedlog [chat id] - send this federation's #FEDBAN log to this chat (owner)."""
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True) if update.effective_chat.type != "private" else None
        if fed is None:
            if update.effective_chat.type == "private":
                await update.message.reply_text("Run /setfedlog inside a federation chat; pass the log chat id: /setfedlog -100123...")
            return
        target = update.effective_chat.id
        if context.args:
            try:
                target = int(context.args[0])
            except ValueError:
                await update.message.reply_text("Usage: /setfedlog [chat id]")
                return
        try:
            await context.bot.send_message(target, f"✅ This chat is now the log for federation {fed.name}.")
        except TelegramError:
            await update.message.reply_text("❌ I can't post in that chat - add me there first.")
            return
        fed.log_channel = target
        await session.commit()
    await update.message.reply_text("✅ Federation log set.")


@group_only
@safe_handler
async def unsetfedlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unsetfedlog - stop logging fedbans to a chat (owner)."""
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True)
        if fed is None:
            return
        fed.log_channel = None
        await session.commit()
    await update.message.reply_text("✅ Federation log removed.")


@group_only
@safe_handler
async def fbroadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fbroadcast <text> - message every chat in the federation (owner)."""
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /fbroadcast <message>")
        return
    if len(text) > BROADCAST_MAX_LEN:
        await update.message.reply_text(f"Message too long (max {BROADCAST_MAX_LEN} characters).")
        return
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True)
        if fed is None:
            return
        chat_ids = (await session.execute(select(Chat.id).where(Chat.federation_id == fed.id))).scalars().all()
        fed_name = fed.name
    status = await update.message.reply_text(f"📣 Broadcasting to {len(chat_ids)} chat(s)...")
    sent = failed = 0
    for cid in chat_ids:
        try:
            await context.bot.send_message(cid, f"📣 <b>{escape_html(fed_name)}</b>\n\n{escape_html(text)}", parse_mode="HTML")
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)
    await status.edit_text(f"📣 Broadcast done: {sent} delivered, {failed} failed.")


@safe_handler
async def fedstat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fedstat [user] [fed id] - is a user fbanned, and why."""
    args = list(context.args or [])
    fed_id: Optional[str] = None
    if args and _UUID.match(args[-1]):
        fed_id = args.pop().lower()
    context.args = args
    target_id, target_name = await resolve_target_user(update, context)
    if not target_id:
        target_id, target_name = update.effective_user.id, update.effective_user.first_name

    async with async_session() as session:
        if fed_id:
            fed = await session.get(Federation, fed_id)
            if fed is None:
                await update.message.reply_text("❌ No federation with that ID.")
                return
            fed_ids = [fed.id]
        else:
            if update.effective_chat.type == "private":
                await update.message.reply_text("Usage in DM: /fedstat <user id> <fed id>")
                return
            fed = await _fed._get_chat_fed(session, update.effective_chat.id)
            if fed is None:
                await update.message.reply_text("This chat isn't in a federation. /fedstat <user> <fed id> works anywhere.")
                return
            fed_ids = [fed.id] + list(fed.subscribed or [])
        rows = (await session.execute(
            select(Federation.name, FedBan.reason)
            .join(Federation, Federation.id == FedBan.federation_id)
            .where(FedBan.federation_id.in_(fed_ids), FedBan.user_id == target_id)
        )).all()
    who = escape_html(target_name or str(target_id))
    if not rows:
        await update.message.reply_text(f"✅ {who} (<code>{target_id}</code>) is not fbanned there.", parse_mode="HTML")
        return
    lines = [f"• <b>{escape_html(n)}</b>" + (f" - {escape_html(r)}" if r else "") for n, r in rows]
    await update.message.reply_text(f"⛔ {who} (<code>{target_id}</code>) is fbanned in:\n" + "\n".join(lines), parse_mode="HTML")


@group_only
@safe_handler
async def fbanlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fbanlist [csv|json] - export the federation's ban list (fed admins)."""
    fmt = "json" if context.args and context.args[0].lower() == "json" else "csv"
    async with async_session() as session:
        fed = await _require_chat_fed(update, session)
        if fed is None:
            return
        rows = (await session.execute(
            select(FedBan.user_id, FedBan.reason, FedBan.banned_by).where(FedBan.federation_id == fed.id).order_by(FedBan.id)
        )).all()
        fed_name = fed.name
    if not rows:
        await update.message.reply_text("This federation has no bans.")
        return
    data = render_ban_export([(u, r, b) for u, r, b in rows], fmt)
    await update.message.reply_document(
        InputFile(io.BytesIO(data), filename=f"fbanlist.{fmt}"),
        caption=f"🌐 {fed_name}: {len(rows)} fban(s)")


@group_only
@safe_handler
async def importfbans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/importfbans - reply to a CSV/JSON/plain-text ban list to add it to this federation (owner)."""
    reply = update.message.reply_to_message
    doc = reply.document if reply else None
    if doc is None:
        await update.message.reply_text("Reply to a CSV / JSON / text file of user ids with /importfbans.")
        return
    if doc.file_size and doc.file_size > MAX_IMPORT_BYTES:
        await update.message.reply_text(f"File too large (max {MAX_IMPORT_BYTES // 1024 // 1024} MB).")
        return
    async with async_session() as session:
        fed = await _require_chat_fed(update, session, owner_only=True)
        if fed is None:
            return
        fed_id = fed.id
    try:
        raw = bytes(await (await doc.get_file()).download_as_bytearray())
        rows = parse_ban_rows(raw)
    except ValueError as exc:
        await update.message.reply_text(f"❌ Couldn't read that file: {escape_html(str(exc))}", parse_mode="HTML")
        return
    except TelegramError:
        await update.message.reply_text("❌ I couldn't download that file.")
        return

    me = update.effective_user.id
    added = skipped = 0
    async with async_session() as session:
        fed = await session.get(Federation, fed_id)
        protected = {fed.owner_id, *(fed.admins or [])}
        existing = set((await session.execute(select(FedBan.user_id).where(FedBan.federation_id == fed_id))).scalars().all())
        for uid, reason in rows:
            if uid in existing or uid in protected or settings.is_admin_id(uid) or uid == context.bot.id:
                skipped += 1
                continue
            session.add(FedBan(federation_id=fed_id, user_id=uid, reason=reason, banned_by=me))
            added += 1
        await session.commit()
    invalidate_enforcement_cache()
    await update.message.reply_text(
        f"✅ Imported {added} fban(s), skipped {skipped} (already banned or protected).\n"
        f"They are removed from member chats when they next join or speak.")


_HELP_USER = "🌐 <b>Federation - everyone</b>\n/chatfed /fedinfo /fedstat [user] /frules /fedadmins"
_HELP_ADMIN = ("🌐 <b>Federation - admins</b>\n/fban /unfban (= /fedban /unfedban) /fbanlist [csv|json] /fedchats /fedsubs "
               "/setfrules <text> /fedstat\nAlso: /banenforce on|off (chat admins)")
_HELP_OWNER = ("🌐 <b>Federation - owner</b>\n/newfed /renamefed /delfed /fpromote /fdemote /subfed /unsubfed /importfbans "
               "/fednotif on|off /setfedlog /unsetfedlog /fbroadcast /myfeds")


@safe_handler
async def fedhelp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fedhelp - federation command overview (also /feduserhelp /fedadminhelp /fedownerhelp)."""
    name = (update.effective_message.text or "").split()[0].lstrip("/").split("@")[0].lower()
    body = {"feduserhelp": _HELP_USER, "fedadminhelp": _HELP_ADMIN, "fedownerhelp": _HELP_OWNER}.get(
        name, "\n\n".join((_HELP_USER, _HELP_ADMIN, _HELP_OWNER)))
    await update.message.reply_text(body, parse_mode="HTML")


def register(app) -> None:
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, ban_enforcer), group=-6)
    app.add_handler(CommandHandler("banenforce", banenforce_cmd))
    # Aliases for the handlers federations.py already owns (Saitama/Masha command names).
    app.add_handler(CommandHandler("fban", _fed.fedban_cmd))
    app.add_handler(CommandHandler("unfban", _fed.unfedban_cmd))
    app.add_handler(CommandHandler("myfeds", myfeds_cmd))
    app.add_handler(CommandHandler("chatfed", chatfed_cmd))
    app.add_handler(CommandHandler("fedchats", fedchats_cmd))
    app.add_handler(CommandHandler("renamefed", renamefed_cmd))
    app.add_handler(CommandHandler("delfed", delfed_cmd))
    app.add_handler(CallbackQueryHandler(delfed_callback, pattern=r"^fedtools:del:(yes|no):"))
    app.add_handler(CommandHandler("subfed", subfed_cmd))
    app.add_handler(CommandHandler("unsubfed", unsubfed_cmd))
    app.add_handler(CommandHandler("fedsubs", fedsubs_cmd))
    app.add_handler(CommandHandler("setfrules", setfrules_cmd))
    app.add_handler(CommandHandler("frules", frules_cmd))
    app.add_handler(CommandHandler("fednotif", fednotif_cmd))
    app.add_handler(CommandHandler("setfedlog", setfedlog_cmd))
    app.add_handler(CommandHandler("unsetfedlog", unsetfedlog_cmd))
    app.add_handler(CommandHandler("fbroadcast", fbroadcast_cmd))
    app.add_handler(CommandHandler(["fedstat", "fbanstat"], fedstat_cmd))
    app.add_handler(CommandHandler("fbanlist", fbanlist_cmd))
    app.add_handler(CommandHandler("importfbans", importfbans_cmd))
    app.add_handler(CommandHandler(["fedhelp", "feduserhelp", "fedadminhelp", "fedownerhelp"], fedhelp_cmd))
