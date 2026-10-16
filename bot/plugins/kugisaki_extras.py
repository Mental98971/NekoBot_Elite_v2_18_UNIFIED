"""
Small leftovers from the Saitama / Masha bots (v2.16):

  /gbanlist          sudo: export the global ban list (CSV, or ``/gbanlist json``)
  /whois             alias of /info
  /sin /cos /tan /asin /acos /atan /arcsin /arccos /arctan /abs /sqrt /ln   one-number math (radians; /log is the chat-log command)
  /weebify <text>    playful text transform (reply or inline)

Expressions that aren't plain numbers (``/sin pi/6``, ``/cos 2pi``) are handed to the sandboxed symbolic engine
for an exact answer; /simplify /factor /derive /integrate /zeroes /tangent /area live in symbolic_math.py.
"""
from __future__ import annotations

import io
import math
from typing import Callable, Dict, Optional

from sqlalchemy import select
from telegram import InputFile
from telegram.ext import CommandHandler

from bot.core.database import GlobalBan, async_session
from bot.core.errors import safe_handler
from bot.plugins.fedtools import render_ban_export
from bot.plugins.utilities import info_cmd
from bot.utils import symbolic
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_FUNCS: Dict[str, Callable[[float], float]] = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "arcsin": math.asin, "acos": math.acos, "arccos": math.acos,
    "atan": math.atan, "arctan": math.atan, "abs": abs, "sqrt": math.sqrt, "ln": math.log,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def parse_number(raw: str) -> Optional[float]:
    """A float, or a constant (pi, e, tau) optionally scaled: ``2*pi``, ``pi/2``. None if it isn't one."""
    text = (raw or "").strip().lower().replace(" ", "")
    if not text or len(text) > 40:
        return None
    for sep in ("*", "/"):
        if sep in text and text.count(sep) == 1:
            a, b = (parse_number(p) for p in text.split(sep))
            if a is None or b is None:
                return None
            if sep == "/" and b == 0:
                return None
            return a * b if sep == "*" else a / b
    if text in _CONSTANTS:
        return _CONSTANTS[text]
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def apply_function(name: str, value: float) -> float:
    """Raises ValueError for arguments outside the function's domain."""
    result = _FUNCS[name](value)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("result is not a finite number")
    return result


_WEEB = (("na", "nya"), ("ne", "nye"), ("no", "nyo"), ("ni", "nyi"), ("nu", "nyu"), ("r", "w"), ("l", "w"), ("th", "z"))


def weebify(text: str) -> str:
    out = text
    for a, b in _WEEB:
        out = out.replace(a, b).replace(a.capitalize(), b.capitalize())
    return (out.strip() + " ~uwu").strip()


def _make_math_cmd(name: str):
    @safe_handler
    async def _cmd(update, context) -> None:
        raw = " ".join(context.args or [])
        value = parse_number(raw)
        if value is None:
            # Not a plain number: let the symbolic engine try (exact results such as sin(pi/6) = 1/2, like Masha's).
            if raw.strip() and symbolic.sympy_available():
                try:
                    exact = await symbolic.get_sandbox().run("fn", raw, name)
                except symbolic.SymbolicError as exc:
                    await update.message.reply_text(f"❌ {exc}")
                    return
                await update.message.reply_text(f"{name}({escape_html(raw.strip())}) = <code>{escape_html(exact)}</code>", parse_mode="HTML")
                return
            await update.message.reply_text(f"Usage: /{name} <number>   (radians; pi, e, 2*pi and pi/2 work)")
            return
        try:
            result = apply_function(name, value)
        except (ValueError, OverflowError):
            await update.message.reply_text(f"❌ {name}({escape_html(raw)}) is undefined.")
            return
        await update.message.reply_text(f"{name}({escape_html(raw.strip())}) = <code>{result:.10g}</code>", parse_mode="HTML")
    _cmd.__name__ = f"{name}_cmd"
    return _cmd


_MATH = {name: _make_math_cmd(name) for name in _FUNCS}


@safe_handler
async def gbanlist_cmd(update, context) -> None:
    """/gbanlist [csv|json] - export the global ban list (sudo)."""
    from bot.config import settings
    if not settings.is_admin_id(update.effective_user.id):
        await update.message.reply_text("🚫 Sudo only.")
        return
    fmt = "json" if context.args and context.args[0].lower() == "json" else "csv"
    async with async_session() as session:
        rows = (await session.execute(select(GlobalBan.user_id, GlobalBan.reason, GlobalBan.banned_by).order_by(GlobalBan.user_id))).all()
    if not rows:
        await update.message.reply_text("The global ban list is empty.")
        return
    await update.message.reply_document(
        InputFile(io.BytesIO(render_ban_export([(u, r, b) for u, r, b in rows], fmt)), filename=f"gbanlist.{fmt}"),
        caption=f"🚫 {len(rows)} global ban(s)")


@safe_handler
async def weebify_cmd(update, context) -> None:
    """/weebify <text> (or reply to a message)."""
    reply = update.message.reply_to_message
    text = " ".join(context.args or []).strip() or ((reply.text or reply.caption or "") if reply else "")
    if not text:
        await update.message.reply_text("Usage: /weebify <text> (or reply to a message)")
        return
    await update.message.reply_text(escape_html(weebify(text[:1000])), parse_mode="HTML")


def register(app) -> None:
    app.add_handler(CommandHandler("gbanlist", gbanlist_cmd))
    app.add_handler(CommandHandler("whois", info_cmd))
    app.add_handler(CommandHandler("weebify", weebify_cmd))
    app.add_handler(CommandHandler("sin", _MATH["sin"]))
    app.add_handler(CommandHandler("cos", _MATH["cos"]))
    app.add_handler(CommandHandler("tan", _MATH["tan"]))
    app.add_handler(CommandHandler("asin", _MATH["asin"]))
    app.add_handler(CommandHandler("arcsin", _MATH["arcsin"]))
    app.add_handler(CommandHandler("acos", _MATH["acos"]))
    app.add_handler(CommandHandler("arccos", _MATH["arccos"]))
    app.add_handler(CommandHandler("atan", _MATH["atan"]))
    app.add_handler(CommandHandler("arctan", _MATH["arctan"]))
    app.add_handler(CommandHandler("abs", _MATH["abs"]))
    app.add_handler(CommandHandler("sqrt", _MATH["sqrt"]))
    app.add_handler(CommandHandler("ln", _MATH["ln"]))
