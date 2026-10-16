"""Tests for bot/plugins/kugisaki_extras.py (v2.16)."""
from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_extras_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP.name}")

from bot.core.database import GlobalBan, async_session, init_db  # noqa: E402
import bot.plugins.kugisaki_extras as ex  # noqa: E402
from tests._fakes import FakeBot, make_context, make_update  # noqa: E402


class TestMath(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(parse := ex.parse_number("2*pi"), 2 * math.pi)
        self.assertAlmostEqual(ex.parse_number("pi / 2"), math.pi / 2)
        self.assertEqual(ex.parse_number("3.5"), 3.5)
        for bad in ("", "abc", "1/0", "1*2*3", "nan", "inf", "x" * 50, "pi*"):
            self.assertIsNone(ex.parse_number(bad), bad)

    def test_functions_and_domains(self):
        self.assertAlmostEqual(ex.apply_function("sin", math.pi / 2), 1.0)
        self.assertEqual(ex.apply_function("abs", -3), 3)
        self.assertEqual(ex.apply_function("arcsin", 1), ex.apply_function("asin", 1))
        for name, arg in (("asin", 2), ("sqrt", -1), ("ln", 0)):
            with self.assertRaises(ValueError):
                ex.apply_function(name, arg)

    def test_weebify(self):
        self.assertEqual(ex.weebify("hello world"), "hewwo wowwd ~uwu")


class TestCommands(unittest.IsolatedAsyncioTestCase):
    async def test_math_command_replies_and_rejects(self):
        cmd = ex._make_math_cmd("cos")
        upd = make_update("/cos", user_id=1, chat_id=-1)
        await cmd(upd, make_context(FakeBot(), ["0"]))
        self.assertIn("cos(0) = <code>1</code>", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/cos", user_id=1, chat_id=-1)
        await cmd(upd, make_context(FakeBot(), []))
        self.assertIn("Usage", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/cos", user_id=1, chat_id=-1)
        await cmd(upd, make_context(FakeBot(), ["0;"]))                       # symbolic path rejects junk characters
        self.assertIn("❌", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/sin", user_id=1, chat_id=-1)
        await ex._make_math_cmd("sin")(upd, make_context(FakeBot(), ["pi/6+0*x"]))   # not a plain number -> exact via sympy
        self.assertIn("1/2", upd.effective_message.reply_text.await_args.args[0])

    async def test_gbanlist_is_sudo_only_and_exports(self):
        await init_db()
        async with async_session() as s:
            if await s.get(GlobalBan, 931_001) is None:
                s.add(GlobalBan(user_id=931_001, reason="=bad", banned_by=1))
                await s.commit()
        upd = make_update("/gbanlist", user_id=999, chat_id=-1)
        upd.effective_message.reply_document = AsyncMock()
        await ex.gbanlist_cmd(upd, make_context(FakeBot()))
        upd.effective_message.reply_document.assert_not_awaited()
        self.assertIn("Sudo only", upd.effective_message.reply_text.await_args.args[0])
        from bot.config import settings
        upd = make_update("/gbanlist", user_id=settings.owner_id, chat_id=-1)
        upd.effective_message.reply_document = AsyncMock()
        await ex.gbanlist_cmd(upd, make_context(FakeBot()))
        upd.effective_message.reply_document.assert_awaited()

    def test_registration(self):
        added = []
        ex.register(SimpleNamespace(add_handler=lambda h, group=0: added.append(h)))
        names = {c for h in added for c in (getattr(h, "commands", ()) or ())}
        for c in ("gbanlist", "whois", "weebify", "sin", "arccos", "sqrt", "ln"):
            self.assertIn(c, names)


if __name__ == "__main__":
    unittest.main()
