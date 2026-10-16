"""
Tests for bot/plugins/fun.py — specifically couples_cmd.

couples_cmd used to be a placeholder: it always replied with "(Feature
requires active user tracking — enable with /trackusers)" — a command
that doesn't exist anywhere in this bot, so the promise could never be
kept. This suite locks in the real, DB-backed replacement: it must
never reproduce that placeholder text, must pick from chat members
that are already tracked (the same ChatMember/User rows /tagall and
/info use), must degrade gracefully with fewer than 2 members, and
must be deterministic for a given chat + day so "Couple of the Day"
doesn't reshuffle on every call.

User IDs here are deliberately taken from a high, distinctive range
(920xxx) that doesn't overlap with any other test file's IDs — the
users table is global (not scoped per chat), and other suites in this
project share the same test-session database, so a colliding user_id
can make an unrelated test see data it didn't create (this file
originally collided with test_name_history.py's use of 601/602 and
was moved here after that surfaced as a real full-suite failure).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_fun_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.core.database import ChatMember, User, async_session, init_db  # noqa: E402
from bot.plugins.fun import couples_cmd  # noqa: E402

CHAT_ID = -100888


def _make_update(user_id: int, chat_id: int, chat_type: str = "supergroup"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []
    return update, context


async def _seed_member(chat_id: int, user_id: int, first_name: str, xp: int = 0):
    async with async_session() as session:
        session.add(User(id=user_id, first_name=first_name))
        session.add(ChatMember(chat_id=chat_id, user_id=user_id, xp=xp, personality_msg_count=0))
        await session.commit()


class TestCouplesCmd(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_private_chat_is_rejected(self):
        update, context = _make_update(user_id=920001, chat_id=1, chat_type="private")
        await couples_cmd(update, context)
        update.message.reply_text.assert_awaited_once()
        self.assertIn("group", update.message.reply_text.call_args[0][0].lower())

    async def test_fewer_than_two_members_gets_a_graceful_message_not_the_old_stub(self):
        chat_id = CHAT_ID - 1
        await _seed_member(chat_id, user_id=920010, first_name="Solo")
        update, context = _make_update(user_id=920010, chat_id=chat_id)
        await couples_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        # The regression this guards against: a placeholder promising a
        # command ("/trackusers") that never existed anywhere in the bot.
        self.assertNotIn("trackusers", sent.lower())
        self.assertIn("2", sent)

    async def test_two_or_more_members_produces_a_real_couple_message(self):
        chat_id = CHAT_ID - 2
        await _seed_member(chat_id, user_id=920020, first_name="Alice", xp=50)
        await _seed_member(chat_id, user_id=920021, first_name="Bob", xp=30)
        update, context = _make_update(user_id=920020, chat_id=chat_id)
        await couples_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("couple of the day", sent.lower())
        self.assertNotIn("trackusers", sent.lower())
        self.assertTrue(str(920020) in sent or "Alice" in sent)
        self.assertTrue(str(920021) in sent or "Bob" in sent)

    async def test_same_chat_same_day_picks_the_same_pair(self):
        chat_id = CHAT_ID - 3
        for i in range(920030, 920035):
            await _seed_member(chat_id, user_id=i, first_name=f"User{i}", xp=i)
        update1, context1 = _make_update(user_id=920030, chat_id=chat_id)
        await couples_cmd(update1, context1)
        first = update1.message.reply_text.call_args[0][0]

        update2, context2 = _make_update(user_id=920030, chat_id=chat_id)
        await couples_cmd(update2, context2)
        second = update2.message.reply_text.call_args[0][0]

        self.assertEqual(first, second)

    async def test_different_chats_can_get_different_pairs_from_the_same_day(self):
        # Not a strict assertion of inequality (a same-seed collision is
        # possible in principle) — just confirms each chat is scoped to
        # its own member pool and doesn't crash mixing chats.
        chat_a = CHAT_ID - 4
        chat_b = CHAT_ID - 5
        for i in range(920040, 920043):
            await _seed_member(chat_a, user_id=i, first_name=f"A{i}", xp=i)
        for i in range(920050, 920053):
            await _seed_member(chat_b, user_id=i, first_name=f"B{i}", xp=i)
        update_a, context_a = _make_update(user_id=920040, chat_id=chat_a)
        await couples_cmd(update_a, context_a)
        sent_a = update_a.message.reply_text.call_args[0][0]
        self.assertTrue(any(f"A{i}" in sent_a for i in range(920040, 920043)))
        self.assertFalse(any(f"B{i}" in sent_a for i in range(920050, 920053)))


if __name__ == "__main__":
    unittest.main()
