"""
Utility helpers: formatting, keyboards, time parsing, etc.
"""
import re
import html
import random
from datetime import datetime
from typing import List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def escape_html(text: str) -> str:
    return html.escape(str(text))


def mention_html(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape_html(name)}</a>'


def time_parser(time_str: str) -> Optional[int]:
    """Parse time strings like '10m', '2h', '1d' into seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    match = re.match(r"^(\d+)([smhdw])$", time_str.lower())
    if match:
        return int(match.group(1)) * units[match.group(2)]
    return None


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m"
    elif seconds < 86400:
        return f"{seconds//3600}h"
    else:
        return f"{seconds//86400}d"


def chunk_list(lst: List, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def build_menu(buttons: List[InlineKeyboardButton], n_cols: int = 3, header=None, footer=None) -> InlineKeyboardMarkup:
    menu = [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header:
        menu.insert(0, header)
    if footer:
        menu.append(footer)
    return InlineKeyboardMarkup(menu)


def paginate(items: List[str], page: int, per_page: int = 10):
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], total, (total + per_page - 1) // per_page


def random_couple(members: List[int]) -> tuple:
    if len(members) < 2:
        return None
    return tuple(random.sample(members, 2))


def generate_progress_bar(current: int, total: int, length: int = 10) -> str:
    if total == 0:
        return "▒" * length
    filled = int(length * current / total)
    return "█" * filled + "▒" * (length - filled)


def get_greeting() -> str:
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Good morning"
    elif 12 <= hour < 17:
        return "Good afternoon"
    elif 17 <= hour < 21:
        return "Good evening"
    else:
        return "Good night"


async def resolve_target_user(update, context) -> "tuple[Optional[int], Optional[str]]":
    """Resolve the user a command is targeting.

    Reply-to-message takes priority (always reliable). A bare @username is
    looked up against users NovaBot already knows about (has seen send a
    message) — the Bot API has no general username -> ID lookup, so a
    username the bot has never seen can't be resolved this way; a numeric
    ID always works.

    Returns (user_id, display_name), or (None, None) if nothing resolved.
    """
    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, (u.first_name or u.username or str(u.id))

    if context.args:
        arg = context.args[0]
        if arg.startswith("@"):
            from sqlalchemy import func as sa_func, select

            from bot.core.database import User, async_session

            username = arg[1:].lower()
            async with async_session() as session:
                result = await session.execute(
                    select(User).where(sa_func.lower(User.username) == username).limit(1)
                )
                row = result.scalar_one_or_none()
                if row:
                    return row.id, (row.first_name or row.username or str(row.id))
            return None, None
        if arg.lstrip("-").isdigit():
            return int(arg), arg

    return None, None


async def reply_with_cleanup(update, context, text: str, **kwargs):
    """Like update.message.reply_text, but if this chat has cleanmode on
    (from YukkiMusicBot's cleanmode — /cleanmode on in plugins/cleanmode.py),
    schedules the sent message for deletion after
    settings.cleanmode_delete_minutes. Scoped to the commands that opt
    into it (currently the highest-traffic live-music replies — see
    plugins/music_live.py) rather than a blanket auto-delete-everything
    system, which would need touching every reply_text call project-wide
    for a use case that's specifically about music-command clutter."""
    from bot.config import settings
    from bot.core.database import Chat, async_session

    sent = await update.message.reply_text(text, **kwargs)

    chat = update.effective_chat
    if not chat or chat.type == "private" or not context.job_queue:
        return sent

    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        enabled = bool(row and row.cleanmode_enabled)

    if enabled:
        async def _delete(ctx):
            try:
                await ctx.bot.delete_message(chat.id, sent.message_id)
            except Exception:
                pass

        context.job_queue.run_once(_delete, when=settings.cleanmode_delete_minutes * 60)

    return sent
