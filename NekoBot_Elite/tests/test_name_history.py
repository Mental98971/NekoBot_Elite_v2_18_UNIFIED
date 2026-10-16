"""
Tests for bot/plugins/name_history.py — sangmata-style name/username
change tracking, cross-referenced from Mikobot during a feature-gap
review (NovaBot had no equivalent; unlike AFK/karma it wasn't already
covered elsewhere).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_names_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.core.database import User, async_session, init_db  # noqa: E402
import bot.plugins.name_history as name_history  # noqa: E402


def _make_update(user_id, username, first_name, last_name=None, chat_type="supergroup"):
    u = MagicMock()
    u.effective_chat.type = chat_type
    u.effective_chat.id = -4242
    u.effective_user.id = user_id
    u.effective_user.username = username
    u.effective_user.first_name = first_name
    u.effective_user.last_name = last_name
    u.effective_user.is_bot = False
    return u


class TestNameHistory(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        import asyncio
        asyncio.run(init_db())

    async def test_first_sighting_seeds_without_fake_history(self):
        name_history._last_checked.pop(601, None)
        await name_history.name_history_middleware(_make_update(601, "bob1", "Bob"), MagicMock())
        async with async_session() as session:
            row = await session.get(User, 601)
        self.assertIn(row.name_history, (None, []))
        self.assertEqual(row.username, "bob1")

    async def test_detects_and_records_a_real_change(self):
        name_history._last_checked.pop(602, None)
        await name_history.name_history_middleware(_make_update(602, "carol_old", "Carol"), MagicMock())
        name_history._last_checked.pop(602, None)  # simulate the throttle window elapsing
        await name_history.name_history_middleware(_make_update(602, "carol_new", "Caroline"), MagicMock())

        async with async_session() as session:
            row = await session.get(User, 602)
        self.assertEqual(row.username, "carol_new")
        self.assertEqual(row.first_name, "Caroline")
        fields = {e["field"] for e in row.name_history}
        self.assertEqual(fields, {"username", "first_name"})

    async def test_throttle_skips_db_when_unchanged_and_recently_checked(self):
        name_history._last_checked.pop(603, None)
        await name_history.name_history_middleware(_make_update(603, "dave", "Dave"), MagicMock())
        with patch("bot.plugins.name_history.async_session") as mock_session:
            await name_history.name_history_middleware(_make_update(603, "dave", "Dave"), MagicMock())
            mock_session.assert_not_called()

    async def test_private_chats_and_bots_are_ignored(self):
        with patch("bot.plugins.name_history.async_session") as mock_session:
            await name_history.name_history_middleware(_make_update(604, "eve", "Eve", chat_type="private"), MagicMock())
            mock_session.assert_not_called()
            bot_update = _make_update(605, "somebot", "Bot")
            bot_update.effective_user.is_bot = True
            await name_history.name_history_middleware(bot_update, MagicMock())
            mock_session.assert_not_called()

    async def test_names_cmd_renders_history(self):
        name_history._last_checked.pop(606, None)
        await name_history.name_history_middleware(_make_update(606, "frank_old", "Frank"), MagicMock())
        name_history._last_checked.pop(606, None)
        await name_history.name_history_middleware(_make_update(606, "frank_new", "Frank"), MagicMock())

        update = MagicMock()
        update.effective_user.id = 606
        update.effective_user.first_name = "Frank"
        update.message.reply_to_message = None
        update.effective_message.reply_to_message = None
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = []
        await name_history.names_cmd(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("username", reply)
        self.assertIn("frank_old", reply)
        self.assertIn("frank_new", reply)


    async def test_lookup_by_numeric_id_and_forgetnames(self):
        name_history._last_checked.pop(607, None)
        await name_history.name_history_middleware(_make_update(607, "gina_old", "Gina"), MagicMock())
        name_history._last_checked.pop(607, None)
        await name_history.name_history_middleware(_make_update(607, "gina_new", "Gina"), MagicMock())

        update = MagicMock()
        update.effective_user.id = 1
        update.effective_user.first_name = "Someone"
        update.effective_message.reply_to_message = None
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = ["607"]
        await name_history.names_cmd(update, ctx)
        self.assertIn("gina_old", update.message.reply_text.call_args[0][0])

        me = MagicMock()
        me.effective_user.id = 607
        me.message.reply_text = AsyncMock()
        await name_history.forgetnames_cmd(me, MagicMock())
        self.assertIn("erased", me.message.reply_text.call_args[0][0])
        await name_history.names_cmd(update, ctx)
        self.assertIn("No name changes", update.message.reply_text.call_args[0][0])

    async def test_alert_is_sent_only_when_the_chat_enabled_it(self):
        from bot.core.database import Chat
        async with async_session() as session:
            session.add(Chat(id=-4343, type="supergroup", title="t", name_alerts=True))
            await session.commit()
        name_history._last_checked.pop(608, None)
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        for uname in ("hal_old", "hal_new"):
            u = _make_update(608, uname, "Hal")
            u.effective_chat.id = -4343
            name_history._last_checked.pop(608, None)
            await name_history.name_history_middleware(u, ctx)
        ctx.bot.send_message.assert_awaited_once()
        ctx2 = MagicMock()
        ctx2.bot.send_message = AsyncMock()
        name_history._last_checked.pop(608, None)
        await name_history.name_history_middleware(_make_update(608, "hal_third", "Hal"), ctx2)   # chat -4242: alerts off
        ctx2.bot.send_message.assert_not_awaited()


def _cleanup_tmp_db():
    try:
        os.unlink(_TMP_DB.name)
    except OSError:
        pass


import atexit  # noqa: E402
atexit.register(_cleanup_tmp_db)


if __name__ == "__main__":
    unittest.main()
