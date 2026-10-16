"""Whispers — private inline messages that only the chosen recipient can open.

Ported from PsstRobot (ALiwoto, github.com/ALiwoto/PsstRobot, Go; see NOTICE.md) and re-engineered for
this codebase. How it works:

  * Inline: type ``@bot @username your secret`` (or a numeric user id, or no recipient for "anyone")
    in any chat and pick the result. The chat receives a message with a 🔐 button; only the recipient
    gets the text, as a pop-up alert. Text over 199 characters and all media are read in the bot's PM.
  * Advanced: /whisper (/create) in the bot's PM composes a whisper of any length or media type
    (single files or whole albums), then hands you a "share" button that sends it inline.
  * Privacy: with /privacy on, reading a whisper does not edit the chat message to say who read it.

Requires inline mode and inline feedback (100%) to be enabled for the bot in @BotFather; without the
feedback setting Telegram never reports which result was sent and whispers stay "Generating...".

Differences from the Go original (full list in docs/WHISPERS.md): one database instead of ten sharded SQLite
files plus an in-memory copy; reads are atomic claims; ids are deep-link safe; photos send in full
resolution; albums keep per-item types; ``/list`` and ``/notifications`` (advertised by the original's
help but never implemented) exist; sending the same advanced whisper twice is refused.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InlineQueryResultsButton,
    InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo, InputTextMessageContent,
    LinkPreviewOptions, Message, Update, User as TgUser,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationHandlerStop, CallbackQueryHandler, ChosenInlineResultHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from bot.config import settings
from bot.core import inline as inline_router
from bot.core.database import BlockedUser, GlobalBan, User, async_session
from bot.core.errors import safe_handler
from bot.utils import whisper_store as store
from bot.utils.helpers import escape_html, mention_html
from bot.utils.logger import get_logger
from bot.utils.whisper_store import MediaItem, WhisperData
from bot.utils.whisper_text import (
    ADVANCED_RESULT_PREFIX, ALBUM_TYPES, HISTORY_RESULT_PREFIX, MAX_QUERY_LENGTH, PLAIN_RESULT_PREFIX,
    START_CREATE, START_HELP_INLINE, WhisperType, advanced_query, advanced_unique_id, clip_name,
    extract_recipient, is_advanced_inline, is_everyone_word, is_valid_username, new_unique_id,
    parse_start_payload, parse_toggle, start_payload,
)

logger = get_logger(__name__)

SHOW_PREFIX = "wh_"
CB_PREFIX = "ws:"
COMPOSE_TTL_SECONDS = 1800
ALBUM_SETTLE_SECONDS = 2.0   # Telegram delivers album items as separate updates; wait for the stragglers
READ_CACHE_SECONDS = 2500    # client-side cache of the callback answer, so a privacy-mode reader can re-open it
NAME_CACHE_SECONDS = 600
LIST_LIMIT = 10
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_TYPE_LABELS = {
    WhisperType.PLAIN_TEXT: "text", WhisperType.PHOTO: "photo", WhisperType.VIDEO: "video",
    WhisperType.AUDIO: "audio", WhisperType.VOICE: "voice", WhisperType.STICKER: "sticker",
    WhisperType.DOCUMENT: "file", WhisperType.VIDEO_NOTE: "video note", WhisperType.ANIMATION: "GIF",
    WhisperType.DICE: "dice",
}


# ─── State & small helpers ──────────────────────────────────────────

@dataclass
class Compose:
    """A whisper being composed in the PM. In memory by design: losing it on a restart costs the user one /whisper."""

    chat_id: int
    stage: str = "target"  # "target" -> "content"
    target_id: int = 0
    target_username: str = ""
    started: float = field(default_factory=time.monotonic)
    album: List[MediaItem] = field(default_factory=list)
    album_group: str = ""


_compose: Dict[int, Compose] = {}
_names: Dict[int, Tuple[float, str]] = {}


def reset_state_for_tests() -> None:
    _compose.clear()
    _names.clear()


def _get_compose(user_id: int) -> Optional[Compose]:
    state = _compose.get(user_id)
    if state is not None and time.monotonic() - state.started > COMPOSE_TTL_SECONDS:
        _compose.pop(user_id, None)
        return None
    return state


async def _name_for(bot, user_id: int) -> str:
    """Display name for a user id: cache -> our user table -> Telegram -> the number itself."""
    cached = _names.get(user_id)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    name = ""
    try:
        async with async_session() as session:
            row = await session.get(User, user_id)
            name = (row.first_name or "") if row else ""
    except Exception:
        logger.debug("whisper_name_db_lookup_failed", exc_info=True)
    if not name:
        try:
            chat = await bot.get_chat(user_id)
            name = getattr(chat, "first_name", None) or getattr(chat, "title", None) or ""
        except TelegramError:
            name = ""
    name = clip_name(name) or str(user_id)
    if len(_names) > 2000:
        _names.clear()
    _names[user_id] = (time.monotonic() + NAME_CACHE_SECONDS, name)
    return name


async def _target_html(bot, w: WhisperData) -> str:
    if w.recipient_username:
        return f"<b>{escape_html(w.recipient_username)}</b>"
    if w.recipient:
        return mention_html(w.recipient, await _name_for(bot, w.recipient))
    return "<b>anyone</b>"


async def _announce_html(bot, w: WhisperData) -> str:
    if w.is_for_everyone:
        return "📖 A whisper message to anyone.\nThe first person to open it can read it."
    return f"🔐 A whisper message to {await _target_html(bot, w)}.\nOnly they can read the message."


def _show_markup(unique_id: str, label: str = "🔐 Show message") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=SHOW_PREFIX + unique_id)]])


async def _is_barred(user_id: int) -> bool:
    """Whisper-banned (/whisperban) or banned from the whole bot (/gban, /block)."""
    if (await store.get_prefs(user_id)).banned:
        return True
    try:
        async with async_session() as session:
            if (await session.execute(select(GlobalBan.user_id).where(GlobalBan.user_id == user_id).limit(1))).first():
                return True
            if (await session.execute(select(BlockedUser.user_id).where(BlockedUser.user_id == user_id).limit(1))).first():
                return True
    except Exception:
        logger.debug("whisper_ban_lookup_failed", exc_info=True)
    return False


def _is_sudo(user_id: Optional[int]) -> bool:
    return bool(user_id and settings.is_admin_id(user_id))


def _age(ts: int) -> str:
    seconds = max(0, int(time.time()) - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "now"


def extract_media(message: Message) -> Tuple[WhisperType, str, str]:
    """``(type, file_id_or_emoji, caption_or_text)`` of a message's content."""
    if message.text:
        return WhisperType.PLAIN_TEXT, "", message.text
    caption = message.caption or ""
    if message.photo:
        return WhisperType.PHOTO, message.photo[-1].file_id, caption  # the original sent photo[0], the smallest size
    for attr, kind in (("video", WhisperType.VIDEO), ("audio", WhisperType.AUDIO), ("voice", WhisperType.VOICE),
                       ("sticker", WhisperType.STICKER), ("document", WhisperType.DOCUMENT),
                       ("video_note", WhisperType.VIDEO_NOTE), ("animation", WhisperType.ANIMATION)):
        obj = getattr(message, attr, None)
        if obj is not None:
            return kind, obj.file_id, caption
    if getattr(message, "dice", None) is not None:
        return WhisperType.DICE, message.dice.emoji, ""
    return WhisperType.UNKNOWN, "", caption


# ─── Inline mode ────────────────────────────────────────────────────

def _help_button(text: str, start_parameter: str) -> InlineQueryResultsButton:
    return InlineQueryResultsButton(text=text, start_parameter=start_parameter)


async def whisper_inline_provider(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Answer ``@bot <recipient> <text>``; registered with the shared inline router (bot/core/inline.py)."""
    if not settings.whisper_enabled:
        return None
    inline_query = update.inline_query
    user = inline_query.from_user
    contribution = inline_router.InlineContribution
    if await _is_barred(user.id):
        return contribution(button=_help_button("You can't send whispers", START_HELP_INLINE), exclusive=True, personal=True, cache_time=0)

    if len(query) > MAX_QUERY_LENGTH:
        return contribution(button=_help_button("Too long! Use an advanced whisper!", START_CREATE),
                            exclusive=True, personal=True, cache_time=0)

    if is_advanced_inline(query):
        unique_id = advanced_unique_id(query)
        w = await store.get_whisper(unique_id) if unique_id else None
        if w is None or not w.can_send_inline_advanced(user.id):
            return contribution(button=_help_button("Invalid advanced whisper! Please create another one!", START_CREATE),
                                exclusive=True, personal=True, cache_time=0)
        title_target = w.recipient_username or (await _name_for(context.bot, w.recipient) if w.recipient else "everyone")
        article = InlineQueryResultArticle(
            id=ADVANCED_RESULT_PREFIX + w.unique_id,
            title=f"An advanced whisper message to {title_target}.",
            description="Only they can read this advanced whisper." if not w.is_for_everyone else "Anyone can read this advanced whisper.",
            input_message_content=InputTextMessageContent(await _announce_html(context.bot, w), parse_mode="HTML",
                                                          link_preview_options=NO_PREVIEW),
            reply_markup=_show_markup(w.unique_id, "🔐 show message"),
        )
        return contribution(results=[article], exclusive=True, personal=True, cache_time=0, placement=inline_router.BEFORE)

    if not query:
        return contribution(button=_help_button("💬 Send a whisper", START_HELP_INLINE))

    rec = extract_recipient(query)
    placeholder = InputTextMessageContent("Generating whisper message...", link_preview_options=NO_PREVIEW)
    placeholder_markup = _show_markup("", "🔐 show message")

    if not rec.is_everyone:
        if not rec.text:
            return contribution(button=_help_button("Add your message after the recipient", START_HELP_INLINE),
                                exclusive=True, personal=True, cache_time=0)
        if rec.user_id:
            title = "🔐 A whisper message to " + await _name_for(context.bot, rec.user_id)
        else:
            title = "🔐 A whisper message to " + rec.username
        article = InlineQueryResultArticle(
            id=PLAIN_RESULT_PREFIX + inline_query.id, title=title, description="Only they can open this whisper.",
            input_message_content=placeholder, reply_markup=placeholder_markup,
        )
        return contribution(results=[article], exclusive=True, personal=True, cache_time=0, placement=inline_router.BEFORE)

    results = [InlineQueryResultArticle(
        id=PLAIN_RESULT_PREFIX + inline_query.id, title="📖 A whisper message to anyone!",
        description="The first person to open this whisper can read it.",
        input_message_content=placeholder, reply_markup=placeholder_markup,
    )]
    nonce = new_unique_id()[:6]
    for entry in await store.get_history(user.id, settings.whisper_history_size):
        results.append(InlineQueryResultArticle(
            id=f"{HISTORY_RESULT_PREFIX}{user.id}::{entry.target_id}::{nonce}",
            title="📩 A whisper message to " + (entry.target_name or str(entry.target_id)),
            description=f"Only {entry.target_name or entry.target_id} can open this whisper.",
            input_message_content=placeholder, reply_markup=placeholder_markup,
        ))
    # AFTER: for plain text the collector's character search comes first, the whisper options last.
    return contribution(results=results, personal=True, cache_time=0, placement=inline_router.AFTER)


def parse_history_result_id(result_id: str) -> Tuple[int, int]:
    """``wsh::<owner>::<target>::<nonce>`` -> ``(owner, target)``; ``(0, 0)`` if malformed."""
    parts = result_id.split("::")
    if len(parts) == 4 and parts[0] + "::" == HISTORY_RESULT_PREFIX and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[1]), int(parts[2])
    return 0, 0


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The user picked one of our results: create the whisper and replace the placeholder message."""
    try:
        result = update.chosen_inline_result
        rid, sender = result.result_id, result.from_user
        bot = context.bot

        if rid.startswith(ADVANCED_RESULT_PREFIX):
            w = await store.get_whisper(rid[len(ADVANCED_RESULT_PREFIX):])
            if w is None or w.sender != sender.id or not result.inline_message_id:
                return
            if w.inline_message_id and w.inline_message_id != result.inline_message_id:
                await _edit_inline(bot, result.inline_message_id, "This whisper has already been sent.")
                return
            await store.set_inline_message_id(w.unique_id, result.inline_message_id)
            return  # the message already carries the right text and button

        if not result.inline_message_id or await _is_barred(sender.id):
            return
        if rid.startswith(HISTORY_RESULT_PREFIX):
            owner, target = parse_history_result_id(rid)
            if owner != sender.id or not target or not (result.query or "").strip():
                return
            w = WhisperData(unique_id=new_unique_id(), sender=sender.id, text=result.query.strip(), recipient=target,
                            inline_message_id=result.inline_message_id)
        elif rid.startswith(PLAIN_RESULT_PREFIX):
            rec = extract_recipient(result.query)
            if rec is None or not rec.text:
                return
            w = WhisperData(unique_id=new_unique_id(), sender=sender.id, text=rec.text, recipient=rec.user_id,
                            recipient_username=rec.username, inline_message_id=result.inline_message_id)
        else:
            return  # another provider's result (e.g. a collector character card)

        await store.add_whisper(w)
        await _edit_inline(bot, result.inline_message_id, await _announce_html(bot, w), _show_markup(w.unique_id))
    except Exception:
        logger.exception("whisper_chosen_result_failed")


async def _edit_inline(bot, inline_message_id: str, html: str, markup: Optional[InlineKeyboardMarkup] = None) -> bool:
    try:
        await bot.edit_message_text(text=html, inline_message_id=inline_message_id, parse_mode="HTML",
                                    reply_markup=markup, link_preview_options=NO_PREVIEW)
        return True
    except TelegramError:
        logger.warning("whisper_edit_inline_failed", exc_info=True)
        return False


# ─── Reading ────────────────────────────────────────────────────────

async def _after_read(bot, w: WhisperData, reader: TgUser, inline_message_id: Optional[str]) -> None:
    """Everything that follows a successful first read by someone other than the sender."""
    prefs = await store.get_prefs(reader.id)
    private = (not w.is_for_everyone) and prefs.privacy_mode
    message_id = inline_message_id or w.inline_message_id
    if not private:
        who = mention_html(reader.id, clip_name(reader.first_name or str(reader.id)))
        if message_id:
            await _edit_inline(bot, message_id, f"{who} read the whisper")
        await store.save_history(w.sender, reader.id, clip_name(reader.first_name or str(reader.id)), settings.whisper_history_size)
        if (await store.get_prefs(w.sender)).notify_on_read:
            try:
                await bot.send_message(w.sender, f"👀 {who} read your whisper.", parse_mode="HTML")
            except TelegramError:
                logger.debug("whisper_notify_failed", exc_info=True)


async def show_whisper_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The 🔐 button under a whisper."""
    query = update.callback_query
    user = query.from_user
    bot = context.bot
    unique_id = (query.data or "")[len(SHOW_PREFIX):]
    try:
        if not unique_id:
            await query.answer("This whisper is not generated yet...", show_alert=True, cache_time=5)
            return
        w = await store.get_whisper(unique_id)
        if w is None:
            await query.answer("This whisper is too old or it has already been read...", show_alert=True, cache_time=READ_CACHE_SECONDS)
            return
        if await _is_barred(user.id):
            await query.answer("You can't open whispers.", show_alert=True, cache_time=READ_CACHE_SECONDS)
            return
        if not w.can_read(user.id, user.username):
            await query.answer("This psst is not for you!", show_alert=True, cache_time=READ_CACHE_SECONDS)
            return

        if w.should_redirect:
            # Media and long text are delivered in the PM; the whisper is consumed there.
            link = f"https://t.me/{bot.username}?start={start_payload(w.unique_id)}"
            await query.answer(url=link, cache_time=READ_CACHE_SECONDS)
            return

        if not w.should_mark_as_read(user.id):  # the sender re-reading their own whisper
            await query.answer(w.text, show_alert=True, cache_time=READ_CACHE_SECONDS)
            return

        if not await store.claim_whisper(w.unique_id):  # someone else got there first
            await query.answer("This whisper is too old or it has already been read...", show_alert=True, cache_time=READ_CACHE_SECONDS)
            return
        try:
            await query.answer(w.text, show_alert=True, cache_time=READ_CACHE_SECONDS)
        except TelegramError:
            await store.restore_whisper(w)  # the reader never saw it: do not burn it
            raise
        await _after_read(bot, w, user, query.inline_message_id)
    except Exception:
        logger.exception("whisper_show_failed", whisper=unique_id)
        try:
            await query.answer("Something went wrong. Try again.", show_alert=True, cache_time=0)
        except TelegramError:
            pass


async def _send_content(bot, chat_id: int, w: WhisperData) -> None:
    """Deliver a whisper's content (text, single media or album) to a chat."""
    if w.is_album:
        media: List[Any] = []
        for item in w.album:
            cls = {WhisperType.PHOTO: InputMediaPhoto, WhisperType.VIDEO: InputMediaVideo,
                   WhisperType.AUDIO: InputMediaAudio, WhisperType.DOCUMENT: InputMediaDocument}.get(WhisperType(item.type))
            if cls is not None:
                media.append(cls(media=item.file_id, caption=item.caption or None))
        if len(media) == 1:
            w = WhisperData(unique_id=w.unique_id, sender=w.sender, text=w.album[0].caption,
                            file_id=w.album[0].file_id, type=w.album[0].type)
        elif media:
            await bot.send_media_group(chat_id, media)
            return
    kind = WhisperType(w.type)
    caption = w.text or None
    if kind == WhisperType.PLAIN_TEXT:
        return  # already shown in the header message
    senders = {
        WhisperType.PHOTO: lambda: bot.send_photo(chat_id, w.file_id, caption=caption),
        WhisperType.VIDEO: lambda: bot.send_video(chat_id, w.file_id, caption=caption),
        WhisperType.AUDIO: lambda: bot.send_audio(chat_id, w.file_id, caption=caption),
        WhisperType.VOICE: lambda: bot.send_voice(chat_id, w.file_id, caption=caption),
        WhisperType.STICKER: lambda: bot.send_sticker(chat_id, w.file_id),
        WhisperType.DOCUMENT: lambda: bot.send_document(chat_id, w.file_id, caption=caption),
        WhisperType.VIDEO_NOTE: lambda: bot.send_video_note(chat_id, w.file_id),
        WhisperType.ANIMATION: lambda: bot.send_animation(chat_id, w.file_id, caption=caption),
        WhisperType.DICE: lambda: bot.send_dice(chat_id, emoji=w.file_id or None),
    }
    send = senders.get(kind)
    if send is not None:
        await send()


async def deliver_in_pm(update: Update, context: ContextTypes.DEFAULT_TYPE, unique_id: str) -> bool:
    """``/start w_<id>``: show a whisper in the PM. False if there is nothing this user may read."""
    user = update.effective_user
    message = update.effective_message
    bot = context.bot
    w = await store.get_whisper(unique_id)
    if w is None or not w.can_read(user.id, user.username) or await _is_barred(user.id):
        return False
    consume = w.should_mark_as_read(user.id)
    if consume and not await store.claim_whisper(w.unique_id):
        return False
    try:
        sender_name = await _name_for(bot, w.sender)
        stamp = datetime.fromtimestamp(w.created_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        header = (
            f"<b>🔹 This whisper has been sent from</b> {mention_html(w.sender, sender_name)} "
            f"<b>to</b> {await _target_html(bot, w)} <b>at</b> <code>{stamp}</code><b>:</b>"
        )
        if WhisperType(w.type) == WhisperType.PLAIN_TEXT:
            header += "\n\n" + escape_html(w.text)
        await message.reply_text(header, parse_mode="HTML", link_preview_options=NO_PREVIEW)
        await _send_content(bot, message.chat_id, w)
    except TelegramError:
        if consume:
            await store.restore_whisper(w)
        raise
    if consume:
        await _after_read(bot, w, user, None)
    return True


async def handle_start_payload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called by /start. True if the deep-link payload belonged to the whisper plugin (and was handled)."""
    if not settings.whisper_enabled or not context.args:
        return False
    payload = context.args[0]
    if update.effective_chat.type != ChatType.PRIVATE:
        return False
    if payload == START_CREATE:
        await create_cmd(update, context)
        return True
    if payload == START_HELP_INLINE:
        await whisperhelp_cmd(update, context)
        return True
    unique_id = parse_start_payload(payload)
    if not unique_id:
        return False
    if not await deliver_in_pm(update, context, unique_id):
        await update.effective_message.reply_text("This whisper is too old, has already been read, or is not for you.")
    return True


# ─── Composing advanced whispers (PM) ───────────────────────────────

async def _prompt_content(message: Message, state: Compose) -> None:
    if state.target_username:
        target = f"<b>{escape_html(state.target_username)}</b>"
    elif state.target_id:
        target = mention_html(state.target_id, await _name_for(message.get_bot(), state.target_id))
    else:
        target = "<b>everyone</b>"
    await message.reply_text(
        f"This whisper is going to be sent to {target}.\nPlease send the content to be whispered. "
        "It can be text, a photo, or any other type of media (albums work too).",
        parse_mode="HTML", link_preview_options=NO_PREVIEW,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💢 Cancel", callback_data=CB_PREFIX + "cancel")]]),
    )


@safe_handler
async def create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whisper (/create) [@user|id|everyone] — compose an advanced whisper in the bot's PM."""
    if not settings.whisper_enabled:
        await update.effective_message.reply_text("Whispers are disabled on this bot.")
        return
    user, message = update.effective_user, update.effective_message
    if update.effective_chat.type != ChatType.PRIVATE:
        username = getattr(context.bot, "username", None)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open in PM", url=f"https://t.me/{username}?start={START_CREATE}")]]) if username else None
        await message.reply_text("Advanced whispers are composed in my private chat.", reply_markup=markup)
        return
    if await _is_barred(user.id):
        await message.reply_text("You can't send whispers.")
        return
    state = Compose(chat_id=message.chat_id)
    _compose[user.id] = state
    arg = context.args[0] if context.args and context.args[0] != START_CREATE else ""
    if arg:
        if is_everyone_word(arg):
            state.stage = "content"
        elif is_valid_username(arg):
            state.stage, state.target_username = "content", arg
        elif arg.isdigit() and int(arg) > 0:
            state.stage, state.target_id = "content", int(arg)
    if state.stage == "content":
        await _prompt_content(message, state)
        return
    await message.reply_text(
        "Preparing an advanced whisper. To cancel, type /cancel.\n\n"
        "Send me the target user's username or id (or forward-mention them).\n"
        'You can also send "everyone" to let anyone in a chat read it.',
    )


async def _cancel_compose(user_id: int, message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _compose.pop(user_id, None)
    for job in (context.job_queue.get_jobs_by_name(f"wsalbum:{user_id}") if context.job_queue else ()):
        job.schedule_removal()
    if state is None:
        await message.reply_text("No active command to cancel. I wasn't doing anything anyway. Zzzzz...")
        return
    await message.reply_text("The latest operation has been cancelled. Anything else I can do for you?\n\n"
                             "Send /whisperhelp for a list of possible commands.")


@safe_handler
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — abandon the whisper being composed."""
    await _cancel_compose(update.effective_user.id, update.effective_message, context)


def _resolve_target(message: Message) -> Tuple[int, str]:
    """``(user_id, username)`` from the first message of the composer: @username, id, or a text mention."""
    for entity in message.entities or ():
        if entity.type == "text_mention" and entity.user is not None:
            return entity.user.id, ""
    token = (message.text or "").strip().split()[0] if (message.text or "").strip() else ""
    if is_valid_username(token):
        return 0, token
    if token.isdigit() and int(token) > 0:
        return int(token), ""
    return 0, ""


async def compose_listener(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private non-command messages while a whisper is being composed. Stops propagation so the
    personality layer does not also answer the secret the user just typed."""
    user, message = update.effective_user, update.effective_message
    if user is None or message is None:
        return
    state = _get_compose(user.id)
    if state is None:
        return
    try:
        if state.stage == "target":
            text = (message.text or "").strip()
            if is_everyone_word(text):
                state.stage = "content"
            else:
                target_id, username = _resolve_target(message)
                if not target_id and not username:
                    await message.reply_text(
                        "Invalid username or user-id provided.\nTry entering a valid and correct username or user-id.\n"
                        'You can also enter "everyone" if you want everyone to be able to see the whisper.')
                    raise ApplicationHandlerStop
                state.stage, state.target_id, state.target_username = "content", target_id, username
            await _prompt_content(message, state)
            raise ApplicationHandlerStop

        kind, file_id, text = extract_media(message)
        if kind == WhisperType.UNKNOWN:
            await message.reply_text("I can't whisper that kind of message. Send text, a photo, or another media file.")
            raise ApplicationHandlerStop
        if message.media_group_id and kind in ALBUM_TYPES:
            state.album_group = state.album_group or str(message.media_group_id)
            if str(message.media_group_id) == state.album_group and len(state.album) < 10:
                state.album.append(MediaItem(int(kind), file_id, text))
            if len(state.album) == 1 and context.job_queue is not None:
                context.job_queue.run_once(_finish_album, ALBUM_SETTLE_SECONDS, data=user.id, name=f"wsalbum:{user.id}")
            elif context.job_queue is None:
                await _finish_whisper(context.bot, user.id)
            raise ApplicationHandlerStop
        await _finish_whisper(context.bot, user.id, single=(kind, file_id, text))
        raise ApplicationHandlerStop
    except ApplicationHandlerStop:
        raise
    except Exception:
        logger.exception("whisper_compose_failed", user_id=user.id)
        _compose.pop(user.id, None)
        await message.reply_text("Something went wrong while preparing your whisper. Send /whisper to start again.")
        raise ApplicationHandlerStop


async def _finish_album(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _finish_whisper(context.bot, int(context.job.data))
    except Exception:
        logger.exception("whisper_album_finish_failed")
        _compose.pop(int(context.job.data), None)


async def _finish_whisper(bot, user_id: int, single: Optional[Tuple[WhisperType, str, str]] = None) -> None:
    state = _compose.pop(user_id, None)
    if state is None:
        return
    w = WhisperData(unique_id=new_unique_id(), sender=user_id, recipient=state.target_id, recipient_username=state.target_username)
    if state.album:
        w.type, w.album = state.album[0].type, state.album
        w.text = " ".join(i.caption for i in state.album if i.caption)[:1000]
    elif single is not None:
        kind, file_id, text = single
        w.type, w.file_id, w.text = int(kind), file_id, text.strip() if kind == WhisperType.PLAIN_TEXT else text
    else:
        return
    await store.add_whisper(w)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("📤 share whisper", switch_inline_query=advanced_query(w.unique_id))]])
    await bot.send_message(state.chat_id, "Done! The whisper is ready to be sent.\nClick the button below to share it.",
                           reply_markup=markup)


# ─── Preferences, listing, help ─────────────────────────────────────

@safe_handler
async def privacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/privacy [on|off] — hide yourself from "X read the whisper" messages."""
    user, message = update.effective_user, update.effective_message
    prefs = await store.get_prefs(user.id)
    wanted = parse_toggle(context.args[0]) if context.args else None
    state = lambda on: "enabled" if on else "disabled"  # noqa: E731
    if wanted is None:
        await message.reply_text(
            f"<b>· Usage:</b>\n    /privacy [on|off]\n\nCurrently privacy mode is {state(prefs.privacy_mode)} "
            "for every whisper you try to read.", parse_mode="HTML")
    elif wanted == prefs.privacy_mode:
        await message.reply_text(f"Privacy mode is already {state(wanted)}.")
    else:
        await store.set_pref(user.id, "privacy_mode", wanted)
        await message.reply_text(f"Privacy mode has been {state(wanted)} successfully.")


def _notifications_view(prefs) -> Tuple[str, InlineKeyboardMarkup]:
    on = bool(prefs.notify_on_read)
    text = ("<b>🔔 Whisper notifications</b>\n\nGet a private message when someone reads a whisper you sent "
            "(not when they have privacy mode on).\n\nCurrently: <b>" + ("on" if on else "off") + "</b>")
    button = InlineKeyboardButton(("🔕 Turn off" if on else "🔔 Turn on"), callback_data=CB_PREFIX + "notif")
    return text, InlineKeyboardMarkup([[button]])


@safe_handler
async def notifications_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/notifications — panel to toggle read receipts for your whispers."""
    text, markup = _notifications_view(await store.get_prefs(update.effective_user.id))
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def _list_view(user_id: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    items = await store.list_whispers(user_id, LIST_LIMIT)
    if not items:
        return "You have no pending whispers.", None
    total = await store.count_whispers(user_id)
    lines = ["<b>🗒 Your pending whispers</b>", ""]
    buttons: List[List[InlineKeyboardButton]] = []
    for index, w in enumerate(items, start=1):
        target = escape_html(w.recipient_username) if w.recipient_username else (str(w.recipient) if w.recipient else "anyone")
        kind = _TYPE_LABELS.get(WhisperType(w.type), "message")
        lines.append(f"{index}. → {target} · {kind} · {_age(w.created_ts)} ago")
        buttons.append([InlineKeyboardButton(f"🗑 Delete #{index}", callback_data=f"{CB_PREFIX}del:{w.unique_id}")])
    if total > len(items):
        lines.append(f"\n<i>…and {total - len(items)} more.</i>")
    lines.append(f"\n<i>Unread whispers are deleted after {settings.whisper_max_expiry_days} days.</i>")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


@safe_handler
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list (/whispers) — your unread whispers, with a delete button each."""
    text, markup = await _list_view(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)


def _help_view(bot_username: str, first_name: str, user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        f"Hey {mention_html(user_id, first_name)}!\nI'm @{escape_html(bot_username)} — I can send <b>whisper</b> messages "
        "that only the person you choose can open.\n"
        "Use me inline, just type:\n"
        f"<code>@{escape_html(bot_username)} @username your whisper text here</code>\n"
        "(a numeric user-id works instead of a username).\n\n"
        "Commands for my PM:\n"
        "• <code>/whisper</code> (<code>/create</code>): prepares an advanced whisper — any length, every media type, albums.\n"
        "• <code>/list</code>: your pending whispers, with a delete button each.\n"
        "• <code>/privacy</code>: if enabled, reading a whisper will not edit the chat message to say you read it.\n"
        "• <code>/notifications</code>: get a message when someone reads a whisper you sent.\n"
        "• <code>/cancel</code>: stop preparing a whisper."
    )
    rows = [
        [InlineKeyboardButton("📖 Privacy Policy", callback_data=CB_PREFIX + "privacy"),
         InlineKeyboardButton("📝 Whisper history", callback_data=CB_PREFIX + "hist")],
        [InlineKeyboardButton("🧾 Try inline", switch_inline_query="")],
    ]
    if settings.whisper_support_url:
        rows[1].append(InlineKeyboardButton("🚑 Support", url=settings.whisper_support_url))
    return text, InlineKeyboardMarkup(rows)


@safe_handler
async def whisperhelp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whisperhelp — how whispers work."""
    user = update.effective_user
    text, markup = _help_view(context.bot.username or "bot", user.first_name or "there", user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup, link_preview_options=NO_PREVIEW)


def _history_view(disabled: bool) -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        f"<b>🗒 User whisper history</b>\nI keep track of the {settings.whisper_history_size} most recent people who read your whispers, "
        "so you can pick them in one tap when you send a new one. You can clear the list or turn the feature off.\n\n"
        "Turning it on or off does not affect whispers you have already sent."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Clear whisper history", callback_data=CB_PREFIX + "histclear"),
         InlineKeyboardButton(("Enable" if disabled else "Disable") + " whisper history", callback_data=CB_PREFIX + "histtoggle")],
        [InlineKeyboardButton("🔙 Back", callback_data=CB_PREFIX + "help")],
    ])
    return text, markup


PRIVACY_POLICY = (
    "<b>📖 Privacy policy</b>\n\n"
    "• A whisper's content is stored until it is read by someone other than you, you delete it (/list), or "
    "{days} days pass. Then it is deleted.\n"
    "• Your preferences (privacy mode, history, notifications) are stored as on/off flags.\n"
    "• With whisper history on, the names of the last few people who read your whispers are stored; clear them any time.\n"
    "• Only the recipient (and you) can open a whisper; nobody else, including other members of the chat, sees its content."
)


async def ws_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buttons of the whisper PM panels (`ws:*`)."""
    query = update.callback_query
    user = query.from_user
    action = (query.data or "")[len(CB_PREFIX):]
    try:
        if action == "cancel":
            await query.answer()
            if query.message is not None:
                await _cancel_compose(user.id, query.message, context)
        elif action == "help":
            text, markup = _help_view(context.bot.username or "bot", user.first_name or "there", user.id)
            await query.answer()
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, link_preview_options=NO_PREVIEW)
        elif action == "privacy":
            await query.answer()
            await query.edit_message_text(
                PRIVACY_POLICY.format(days=settings.whisper_max_expiry_days), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=CB_PREFIX + "help")]]))
        elif action == "hist":
            text, markup = _history_view((await store.get_prefs(user.id)).history_disabled)
            await query.answer()
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        elif action == "histclear":
            cleared = await store.clear_history(user.id)
            await query.answer("User whisper history cleared successfully!" if cleared else "No user history to clear!", show_alert=True)
        elif action == "histtoggle":
            disabled = not (await store.get_prefs(user.id)).history_disabled
            await store.set_pref(user.id, "history_disabled", disabled)
            text, markup = _history_view(disabled)
            await query.answer("User whisper history has been " + ("disabled." if disabled else "enabled."), show_alert=True)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        elif action == "notif":
            prefs = await store.get_prefs(user.id)
            await store.set_pref(user.id, "notify_on_read", not prefs.notify_on_read)
            text, markup = _notifications_view(await store.get_prefs(user.id))
            await query.answer()
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        elif action.startswith("del:"):
            removed = await store.delete_whisper_of_sender(action[4:], user.id)
            await query.answer("Whisper deleted." if removed else "That whisper is already gone.")
            text, markup = await _list_view(user.id)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await query.answer()
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            logger.warning("whisper_panel_failed", action=action, exc_info=True)
    except Exception:
        logger.exception("whisper_panel_failed", action=action)


@safe_handler
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = update.callback_query.data or ""
    if data.startswith(SHOW_PREFIX):
        await show_whisper_callback(update, context)
    else:
        await ws_callback(update, context)


# ─── Owner moderation ───────────────────────────────────────────────

async def _set_ban(update: Update, context: ContextTypes.DEFAULT_TYPE, banned: bool) -> None:
    message = update.effective_message
    if not _is_sudo(update.effective_user.id):
        await message.reply_text("You are not allowed to use this command.")
        return
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target = int(context.args[0])
    if target is None:
        await message.reply_text("Usage: reply to a user or pass a numeric user id.")
        return
    await store.set_pref(target, "banned", banned)
    await message.reply_text(f"{'Banned' if banned else 'Unbanned'} {target} from whispers.")


@safe_handler
async def whisperban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whisperban (sudo) — stop a user sending or opening whispers."""
    await _set_ban(update, context, True)


@safe_handler
async def whisperunban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whisperunban (sudo) — lift a whisper ban."""
    await _set_ban(update, context, False)


# ─── Housekeeping & registration ────────────────────────────────────

async def purge_expired_whispers() -> int:
    removed = await store.purge_expired(settings.whisper_max_expiry_days * 86400)
    if removed:
        logger.info("whispers_expired", removed=removed)
    return removed


async def _cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await purge_expired_whispers()
        stale = [uid for uid, st in _compose.items() if time.monotonic() - st.started > COMPOSE_TTL_SECONDS]
        for uid in stale:
            _compose.pop(uid, None)
    except Exception:
        logger.exception("whisper_cleanup_failed")


async def on_startup(application) -> None:
    """Called from core/bot.py post_init: drop whispers that expired while the bot was down."""
    if settings.whisper_enabled:
        await purge_expired_whispers()


def register(app) -> None:
    if not settings.whisper_enabled:
        logger.info("whispers_disabled")
        return
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["whisper", "create"], create_cmd))
    app.add_handler(CommandHandler(["cancel", "whispercancel"], cancel_cmd, filters=private))
    app.add_handler(CommandHandler("privacy", privacy_cmd, filters=private))
    app.add_handler(CommandHandler(["list", "whispers"], list_cmd, filters=private))
    app.add_handler(CommandHandler("notifications", notifications_cmd, filters=private))
    app.add_handler(CommandHandler("whisperhelp", whisperhelp_cmd))
    app.add_handler(CommandHandler("whisperban", whisperban_cmd))
    app.add_handler(CommandHandler("whisperunban", whisperunban_cmd))
    app.add_handler(CallbackQueryHandler(callback_router, pattern=r"^(wh_|ws:)"))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    # Group 0, ahead of the personality layer's private-chat banter (group 8): its filter is disjoint from
    # every other group-0 message handler (commands are excluded; the raid listener only sees group joins).
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, compose_listener))
    inline_router.register_provider("whisper", whisper_inline_provider, priority=10)
    inline_router.install(app)
    if app.job_queue is not None:
        app.job_queue.run_repeating(_cleanup_job, interval=settings.whisper_cleanup_minutes * 60, first=60, name="whisper_cleanup")

