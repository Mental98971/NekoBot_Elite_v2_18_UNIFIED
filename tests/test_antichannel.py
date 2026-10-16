"""
Tests for /antichannel in bot/plugins/automod.py.

Covers: the toggle command persisting per-chat, the listener being a
no-op when disabled or when there's no sender_chat, deleting+banning a
spamming channel identity when enabled, and NOT touching the group's
own linked discussion channel (a legitimate use of sender_chat, not
spam).
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

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_antichannel_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.core.database import Chat, async_session, init_db  # noqa: E402
from bot.plugins.automod import antichannel_cmd, antichannel_listener  # noqa: E402

CHAT_ID = -100999111


def _make_command_update(chat_id, user_id=42):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "supergroup"
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
    return update, context


def _make_channel_message_update(chat_id, sender_chat_id, linked_chat_id=None):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.linked_chat_id = linked_chat_id
    update.effective_message.sender_chat.id = sender_chat_id
    update.effective_message.delete = AsyncMock()
    context = MagicMock()
    context.bot.ban_chat_sender_chat = AsyncMock()
    return update, context


def _make_user_message_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_message.sender_chat = None
    update.effective_message.delete = AsyncMock()
    context = MagicMock()
    context.bot.ban_chat_sender_chat = AsyncMock()
    return update, context


async def _set_antichannel(chat_id, enabled):
    async with async_session() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            chat = Chat(id=chat_id, type="supergroup")
            session.add(chat)
        chat.antichannel_enabled = enabled
        await session.commit()


class TestAntichannelCmd(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_toggle_persists_per_chat(self):
        chat_id = CHAT_ID - 1
        update, context = _make_command_update(chat_id)
        await antichannel_cmd(update, context)
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            self.assertTrue(chat.antichannel_enabled)

        await antichannel_cmd(update, context)
        async with async_session() as session:
            chat = await session.get(Chat, chat_id)
            self.assertFalse(chat.antichannel_enabled)


class TestAntichannelListener(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_noop_when_disabled(self):
        chat_id = CHAT_ID - 2
        await _set_antichannel(chat_id, False)
        update, context = _make_channel_message_update(chat_id, sender_chat_id=999)
        await antichannel_listener(update, context)
        update.effective_message.delete.assert_not_awaited()
        context.bot.ban_chat_sender_chat.assert_not_awaited()

    async def test_noop_for_a_normal_user_message(self):
        chat_id = CHAT_ID - 3
        await _set_antichannel(chat_id, True)
        update, context = _make_user_message_update(chat_id)
        await antichannel_listener(update, context)
        update.effective_message.delete.assert_not_awaited()
        context.bot.ban_chat_sender_chat.assert_not_awaited()

    async def test_deletes_and_bans_a_spamming_channel_identity(self):
        chat_id = CHAT_ID - 4
        await _set_antichannel(chat_id, True)
        update, context = _make_channel_message_update(chat_id, sender_chat_id=777)
        await antichannel_listener(update, context)
        update.effective_message.delete.assert_awaited_once()
        context.bot.ban_chat_sender_chat.assert_awaited_once_with(chat_id=chat_id, sender_chat_id=777)

    async def test_leaves_the_groups_own_linked_channel_alone(self):
        chat_id = CHAT_ID - 5
        await _set_antichannel(chat_id, True)
        update, context = _make_channel_message_update(chat_id, sender_chat_id=888, linked_chat_id=888)
        await antichannel_listener(update, context)
        update.effective_message.delete.assert_not_awaited()
        context.bot.ban_chat_sender_chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
