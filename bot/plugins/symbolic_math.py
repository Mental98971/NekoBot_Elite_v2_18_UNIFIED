"""
Symbolic math commands (v2.17), from the Masha bot's ``math.py`` (which called the external Newton API):

  /simplify <expr>            /factor <expr>          /derive <expr>        /integrate <expr>
  /zeroes <expr>              /tangent <x>|<expr>     /area <from>:<to>|<expr>

Evaluated locally by sympy inside a sandboxed worker process (see bot/utils/symbolic.py): strict token
whitelist, restricted namespace, hard timeout, memory cap. ``^`` is power, ``2x`` means ``2*x``, ``pi`` and ``e``
are constants.  Each user may run one calculation every USER_COOLDOWN seconds and at most MAX_QUEUE calculations
may wait at once, so the worker cannot be used to starve the bot.
"""
from __future__ import annotations

import time
from typing import Dict

from telegram.ext import CommandHandler

from bot.core.errors import safe_handler
from bot.utils import symbolic
from bot.utils.helpers import escape_html

USER_COOLDOWN = 3.0
MAX_QUEUE = 6

_last_use: Dict[int, float] = {}
_waiting = 0

_USAGE = {
    "simplify": "/simplify <expression>   e.g. /simplify (x^2-1)/(x-1)",
    "factor": "/factor <expression>   e.g. /factor x^2-5x+6",
    "derive": "/derive <expression>   e.g. /derive x^3+2x",
    "integrate": "/integrate <expression>   e.g. /integrate x^2",
    "zeroes": "/zeroes <expression>   e.g. /zeroes x^2-4",
    "tangent": "/tangent <x value>|<expression>   e.g. /tangent 2|x^3",
    "area": "/area <from>:<to>|<expression>   e.g. /area 2:4|x^3",
}


def _expression(update, context) -> str:
    text = " ".join(context.args or []).strip()
    reply = getattr(update.effective_message, "reply_to_message", None)
    if not text and reply is not None:
        text = (getattr(reply, "text", None) or "").strip()
    return text


def _make(op: str):
    @safe_handler
    async def _cmd(update, context) -> None:
        global _waiting
        msg = update.effective_message
        text = _expression(update, context)
        if not text:
            await msg.reply_text("Usage: " + _USAGE[op])
            return
        if not symbolic.sympy_available():
            await msg.reply_text("Symbolic math isn't installed on this bot (it needs the sympy package).")
            return
        uid = update.effective_user.id
        now = time.monotonic()
        last = _last_use.get(uid)
        if last is not None and now - last < USER_COOLDOWN:
            await msg.reply_text(f"⏳ One calculation every {USER_COOLDOWN:.0f} seconds, please.")
            return
        if _waiting >= MAX_QUEUE:
            await msg.reply_text("⏳ I'm busy with other calculations - try again in a moment.")
            return
        _last_use[uid] = now
        if len(_last_use) > 5000:  # bound the dict: forget everything older than the cooldown
            for key in [k for k, v in _last_use.items() if now - v > USER_COOLDOWN]:
                _last_use.pop(key, None)
        _waiting += 1
        try:
            result = await symbolic.get_sandbox().run(op, text)
        except symbolic.SymbolicError as exc:
            await msg.reply_text(f"❌ {exc}")
            return
        finally:
            _waiting -= 1
        await msg.reply_text(f"<code>{escape_html(result)}</code>", parse_mode="HTML")

    _cmd.__name__ = f"{op}_cmd"
    _cmd.__doc__ = f"{_USAGE[op]} - symbolic math (sympy, sandboxed)."
    return _cmd


_CMDS = {op: _make(op) for op in _USAGE}


def register(app) -> None:
    app.add_handler(CommandHandler("simplify", _CMDS["simplify"]))
    app.add_handler(CommandHandler("factor", _CMDS["factor"]))
    app.add_handler(CommandHandler("derive", _CMDS["derive"]))
    app.add_handler(CommandHandler("integrate", _CMDS["integrate"]))
    app.add_handler(CommandHandler("zeroes", _CMDS["zeroes"]))
    app.add_handler(CommandHandler("tangent", _CMDS["tangent"]))
    app.add_handler(CommandHandler("area", _CMDS["area"]))
