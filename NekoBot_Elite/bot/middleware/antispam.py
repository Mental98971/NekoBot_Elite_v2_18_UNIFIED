"""
Heuristic Anti-Spam Engine with multi-factor scoring.
Analyzes: repetition, caps ratio, link density, emoji spam, forward chains, join date.

v2.12: the per-chat word blacklist (plugins/blacklist.py) is enforced here too, ahead of the spam
heuristics and independent of the /antispam switch, so a blacklisted message never reaches a command,
filter or XP handler. Exemptions come from ``modtools.is_exempt`` (admins, approved users, owners, Telegram's own
account - which previously let a linked channel's auto-forwarded posts be scored as spam), the engine
can be switched off per chat (``/antispam off``), a deleted message stops further processing so
filters / XP / personality never react to spam, and the public notice is rate-limited per user (a
spammer posting 20 messages used to get 20 notices).
"""
import re
import time
from typing import Dict, Tuple

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot.config import settings
from bot.utils import blacklist, chatcache, modtools as mt
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

NOTICE_COOLDOWN = 60.0
_last_notice: Dict[Tuple[int, int], float] = {}


def _calculate_spam_score(text: str, is_forward: bool, user_join_date) -> float:
    if not text:
        return 0.0

    score = 0.0
    text_len = len(text)

    # 1. Caps ratio
    caps = sum(1 for c in text if c.isupper())
    caps_ratio = caps / text_len if text_len > 0 else 0
    if caps_ratio > 0.7 and text_len > 10:
        score += 0.25

    # 2. Link density
    links = len(re.findall(r"http[s]?://|t\.me/|@\w+", text))
    if links > 2:
        score += 0.30
    elif links > 0:
        score += 0.10

    # 3. Emoji spam
    emojis = len(re.findall(r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251]", text))
    if emojis > 5:
        score += 0.20

    # 4. Repetition
    words = text.lower().split()
    if len(words) > 3:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.3:
            score += 0.20

    # 5. Forward penalty
    if is_forward:
        score += 0.15

    # 6. Suspicious keywords
    spam_keywords = ["free", "click here", "join now", "limited", "urgent", "winner", "prize", "crypto", "invest"]
    if any(kw in text.lower() for kw in spam_keywords):
        score += 0.15

    return min(score, 1.0)


async def _enforce_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE, snap) -> bool:
    """Delete a message containing a blacklisted word and apply the chat's blacklist action.
    True when the message was handled (deleted and/or its author punished)."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    text = msg.text or msg.caption or ""
    entry = blacklist.find_match(text, snap.blacklist_words) if text else None
    if entry is None:
        return False
    if await mt.is_exempt(context.bot, chat.id, user.id):   # looked up only after a hit: no API call per message
        return False
    deleted = False
    try:
        await msg.delete()
        deleted = True
    except Exception as exc:
        logger.debug("blacklist_delete_failed", chat_id=chat.id, error=str(exc))
    action = snap.blacklist_action or "warn"
    notice = await mt.enforce_action(context, chat.id, user.id, mt.display_name(user), action,
                                     seconds=snap.blacklist_duration, reason=f"Blacklisted word: {entry}", by=context.bot.id)
    if notice:
        try:
            await context.bot.send_message(chat.id, f"🚫 {notice}", parse_mode="HTML")
        except Exception as exc:
            logger.debug("blacklist_notice_failed", chat_id=chat.id, error=str(exc))
    if deleted or notice:
        await mt.log_event(context, chat.id, "Blacklist", target=(user.id, mt.display_name(user)),
                           reason=entry, extra=f"<b>Action:</b> {escape_html(action)}")
    return deleted or bool(notice)


async def antispam_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if msg is None or chat is None or user is None or chat.type == "private" or user.is_bot:
        return

    snap = await chatcache.get_chat_settings(chat.id)
    if snap is not None and snap.blacklist_words and await _enforce_blacklist(update, context, snap):
        raise ApplicationHandlerStop
    if snap is not None and snap.antispam_enabled is False:
        return
    if await mt.is_exempt(context.bot, chat.id, user.id):
        return

    text = msg.text or msg.caption or ""
    is_forward = getattr(msg, "forward_origin", None) is not None
    score = _calculate_spam_score(text, is_forward, user)
    if score < settings.spam_ml_threshold:
        return

    try:
        await msg.delete()
    except Exception as exc:
        logger.debug("antispam_delete_failed", chat_id=chat.id, error=str(exc))
        return                          # could not remove it: do not announce a deletion that did not happen
    now = time.monotonic()
    key = (chat.id, user.id)
    if now - _last_notice.get(key, -NOTICE_COOLDOWN) >= NOTICE_COOLDOWN:
        if len(_last_notice) > 5000:
            _last_notice.clear()
        _last_notice[key] = now
        try:
            await context.bot.send_message(
                chat.id,
                f"🛡️ <b>Spam detected & deleted</b>\n"
                f"User: {mt.mention_html(user.id, mt.display_name(user))}\n"
                f"Confidence: <code>{score:.0%}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.debug("antispam_notice_failed", chat_id=chat.id, error=str(exc))
    raise ApplicationHandlerStop
