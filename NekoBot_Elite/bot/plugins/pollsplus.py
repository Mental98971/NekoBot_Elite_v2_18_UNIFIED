"""
Masha-style polls (v2.17): /apoll, /stoppoll, /forgotpollid.

From the Masha bot's ``__polling.py`` (Telethon + MongoDB), rebuilt on the Bot API and this project's database:

  /apoll <id> | <question> | <quiz> <public> <multiple> <option1> <option2> ... (up to 10)
      <id>        a 5-digit code you pick; it is how you stop the poll later
      <quiz>      False, or True@<n> to make option n the correct answer
      <public>    True = votes are visible, False = anonymous
      <multiple>  True = several answers allowed (not for quizzes)
      Options are separated by spaces; use "double quotes" for an option with spaces.
  /stoppoll <id>      close the poll (reply to it, or just give the id) and show the result
  /forgotpollid       forget your active poll so you can start a new one (you may have deleted it)

The same syntax also works as /poll <5 digits> | ... (the existing /poll still handles the simple form).  The poll
is posted by the bot and the command message is removed, so the creator is not shown.  Each person can have one
active poll at a time (as in Masha).  Creating needs admin rights in groups (or approval); stopping needs the
creator or an admin.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from sqlalchemy import delete, select
from telegram.error import TelegramError
from telegram.ext import CommandHandler

from bot.core.database import PollRecord, async_session
from bot.core.errors import safe_handler
from bot.utils import modtools as mt
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

MAX_OPTIONS = 10
MAX_QUESTION = 300
MAX_OPTION_LEN = 100
_CODE = re.compile(r"^\d{5}$")
_TOKEN = re.compile(r'"([^"]+)"|(\S+)')
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0", "nothing"}


class PollSyntaxError(ValueError):
    """Message is shown to the user as is."""


def looks_like_apoll(text: str) -> bool:
    """True for ``12345 | question | ...`` (so /poll can hand the Masha form to this plugin)."""
    parts = (text or "").split("|")
    return len(parts) >= 3 and bool(_CODE.match(parts[0].strip()))


def parse_apoll(raw: str) -> dict:
    """Parse the text after /apoll into ``{code, question, options, quiz_answer, public, multiple}``."""
    parts = [p.strip() for p in (raw or "").split("|")]
    if len(parts) < 3:
        raise PollSyntaxError("Usage: /apoll <5-digit id> | <question> | <True@n/False> <True/False> <True/False> <option1> <option2> ...")
    code, question = parts[0], parts[1]
    if not code:
        raise PollSyntaxError("I need a poll id of 5 digits to make a poll.")
    if not _CODE.match(code):
        raise PollSyntaxError("Poll id should be an integer of 5 digits.")
    if not question:
        raise PollSyntaxError("Where is the question?")
    if len(question) > MAX_QUESTION:
        raise PollSyntaxError(f"The question can be at most {MAX_QUESTION} characters.")

    tokens = [m.group(1) or m.group(2) for m in _TOKEN.finditer("|".join(parts[2:]).replace("|", " "))]
    if len(tokens) < 3:
        raise PollSyntaxError("After the question give: <True@n/False> <True/False> <True/False> then the options.")
    quiz_tok, public_tok, multiple_tok, options = tokens[0].lower(), tokens[1].lower(), tokens[2].lower(), tokens[3:]

    quiz_answer: Optional[int] = None
    if quiz_tok.startswith("true"):
        if "@" not in quiz_tok:
            raise PollSyntaxError("You need to select the right answer with its number, like True@1 or True@3.")
        try:
            quiz_answer = int(quiz_tok.split("@", 1)[1])
        except ValueError:
            raise PollSyntaxError("The right answer must be a number, like True@2.") from None
    elif quiz_tok not in _FALSE:
        raise PollSyntaxError("The first setting must be False or True@<option number>.")

    for label, tok in (("public votes", public_tok), ("multiple choice", multiple_tok)):
        if tok not in _TRUE | _FALSE:
            raise PollSyntaxError(f"The {label} setting must be True or False.")
    public, multiple = public_tok in _TRUE, multiple_tok in _TRUE

    if len(options) < 2:
        raise PollSyntaxError("At least two options are needed to create a poll.")
    if len(options) > MAX_OPTIONS:
        raise PollSyntaxError(f"Telegram polls support at most {MAX_OPTIONS} options.")
    if any(len(o) > MAX_OPTION_LEN for o in options):
        raise PollSyntaxError(f"Each option can be at most {MAX_OPTION_LEN} characters.")
    if len({o.lower() for o in options}) != len(options):
        raise PollSyntaxError("Options must all be different.")
    if quiz_answer is not None:
        if multiple:
            raise PollSyntaxError("A quiz can't allow multiple answers.")
        if not 1 <= quiz_answer <= len(options):
            raise PollSyntaxError(f"The right answer must be between 1 and {len(options)}.")
    return {"code": code, "question": question, "options": options, "quiz_answer": quiz_answer,
            "public": public, "multiple": multiple}


def _after_command(update) -> str:
    parts = (update.effective_message.text or "").split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def format_results(poll) -> str:
    total = max(int(getattr(poll, "total_voter_count", 0) or 0), 0)
    lines = [f"📊 <b>{escape_html(poll.question)}</b> - closed ({total} voter{'s' if total != 1 else ''})"]
    correct = getattr(poll, "correct_option_id", None)
    for i, opt in enumerate(poll.options):
        votes = int(getattr(opt, "voter_count", 0) or 0)
        pct = f" ({round(100 * votes / total)}%)" if total else ""
        mark = " ✅" if correct is not None and i == correct else ""
        lines.append(f"• {escape_html(opt.text)}: {votes}{pct}{mark}")
    return "\n".join(lines)


@safe_handler
async def apoll_cmd(update, context) -> None:
    """/apoll <id> | <question> | <True@n/False> <True/False> <True/False> <options...>"""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    try:
        spec = parse_apoll(_after_command(update))
    except PollSyntaxError as exc:
        await msg.reply_text(f"❌ {exc}")
        return
    if chat.type != "private" and not await mt.is_exempt(context.bot, chat.id, user.id):
        await msg.reply_text("🚫 Only admins can create polls here.")
        return
    async with async_session() as session:
        mine = (await session.execute(select(PollRecord.id).where(PollRecord.creator_id == user.id))).first()
        taken = (await session.execute(select(PollRecord.id).where(PollRecord.poll_code == spec["code"], PollRecord.chat_id == chat.id))).first()
    if mine:
        await msg.reply_text("Please stop your previous poll before creating a new one! (/stoppoll <id>, or /forgotpollid if it was deleted)")
        return
    if taken:
        await msg.reply_text("That poll id is already used in this chat - pick another 5-digit id.")
        return
    try:
        sent = await context.bot.send_poll(
            chat.id, spec["question"], spec["options"], is_anonymous=not spec["public"],
            allows_multiple_answers=spec["multiple"] and spec["quiz_answer"] is None,
            type="quiz" if spec["quiz_answer"] is not None else "regular",
            correct_option_id=(spec["quiz_answer"] - 1) if spec["quiz_answer"] is not None else None)
    except TelegramError as exc:
        await msg.reply_text(f"❌ Telegram refused the poll: {escape_html(mt.friendly_telegram_error(exc))}", parse_mode="HTML")
        return
    async with async_session() as session:
        session.add(PollRecord(poll_code=spec["code"], creator_id=user.id, chat_id=chat.id, message_id=sent.message_id))
        await session.commit()
    if chat.type != "private":
        try:
            await msg.delete()  # keep the creator anonymous, as Masha did
        except TelegramError:
            pass


@safe_handler
async def stoppoll_cmd(update, context) -> None:
    """/stoppoll <id> - close the poll with that id (reply to it optional)."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    code = (context.args[0] if context.args else "").strip()
    if not code:
        await msg.reply_text("Where is the poll id? Usage: /stoppoll <5-digit id>")
        return
    if not _CODE.match(code):
        await msg.reply_text("Poll id should be an integer of 5 digits.")
        return
    async with async_session() as session:
        rec = (await session.execute(select(PollRecord).where(PollRecord.poll_code == code, PollRecord.chat_id == chat.id))).scalars().first()
    if rec is None:
        await msg.reply_text("I don't have an active poll with that id in this chat.")
        return
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None and getattr(reply, "message_id", rec.message_id) != rec.message_id:
        await msg.reply_text("That id belongs to a different poll than the one you replied to.")
        return
    if rec.creator_id != user.id and not (chat.type != "private" and await mt.is_exempt(context.bot, chat.id, user.id)):
        await msg.reply_text("🚫 Only the poll's creator or an admin can stop it.")
        return
    try:
        poll = await context.bot.stop_poll(chat.id, rec.message_id)
    except TelegramError as exc:
        # The poll was deleted or is already closed: the record is dead either way, free the id.
        logger.info("stoppoll_failed chat=%s err=%s", chat.id, exc)
        async with async_session() as session:
            await session.execute(delete(PollRecord).where(PollRecord.id == rec.id))
            await session.commit()
        await msg.reply_text("That poll was already closed or deleted - its id is free again.")
        return
    async with async_session() as session:
        await session.execute(delete(PollRecord).where(PollRecord.id == rec.id))
        await session.commit()
    await msg.reply_text(format_results(poll), parse_mode="HTML")


@safe_handler
async def forgotpollid_cmd(update, context) -> None:
    """/forgotpollid - forget your active poll so you can create a new one."""
    async with async_session() as session:
        res = await session.execute(delete(PollRecord).where(PollRecord.creator_id == update.effective_user.id))
        await session.commit()
    await update.effective_message.reply_text(
        "Done - you can now create a new poll." if res.rowcount else "Seems like you haven't created any poll yet!")


def register(app) -> None:
    app.add_handler(CommandHandler("apoll", apoll_cmd))
    app.add_handler(CommandHandler("stoppoll", stoppoll_cmd))
    app.add_handler(CommandHandler("forgotpollid", forgotpollid_cmd))
