"""
End-to-end tests for the personality auto-reply path: an ambient (non-
command) group message arriving, running through the same middleware
chain a real Update would, and reaching bot/plugins/personality.py's
handle_message().

This file exists because every other personality test exercises pieces
in isolation (PersonalityLayer.rewrite(), the example bank, trigger
matching, ...) — none of them call handle_message() itself, which is
the actual entry point a real Telegram message hits. That gap is how a
bot with 76+ passing tests could still never send an ambient reply in
practice: force_sub_middleware defaulted to *enabled*, pointed at a
channel the operator doesn't own, and raised ApplicationHandlerStop for
every non-admin user in every chat, in a handler group that runs before
personality.py ever sees the update — and nothing exercised that chain
end to end to catch it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_e2e_reply_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "999999999")  # the "admin" in these tests
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.config import settings  # noqa: E402
from bot.core.database import Chat, async_session, init_db  # noqa: E402
from bot.middleware.access_control import access_control_middleware  # noqa: E402
from bot.middleware.force_sub import force_sub_middleware  # noqa: E402
from bot.plugins import personality as personality_plugin  # noqa: E402
from telegram import MessageEntity  # noqa: E402

GROUP_CHAT_ID = -100123456
REGULAR_USER_ID = 111111  # a normal, non-admin user — the realistic case
BOT_ID = 987654321
BOT_USERNAME = "Nekooooooobot"


def _make_update(*, user_id: int, chat_id: int, text: str, chat_type: str = "supergroup"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = "Regular"
    update.effective_user.username = "regularuser"
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_chat.title = "Test Group"
    update.effective_message = update.message = MagicMock()
    update.message.text = text
    update.message.entities = []
    update.message.message_id = 4242
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    return update


def _make_context():
    context = MagicMock()
    context.args = []
    context.job_queue = None  # forces the immediate-send branch — simplest to assert on
    context.bot = MagicMock()
    context.bot.id = BOT_ID
    context.bot.username = BOT_USERNAME
    context.bot.send_message = AsyncMock()
    context.bot.get_chat_member = AsyncMock()
    return context


async def _enable_personality(chat_id: int) -> None:
    async with async_session() as session:
        row = await session.get(Chat, chat_id)
        if row is None:
            row = Chat(id=chat_id, type="supergroup")
            session.add(row)
        row.personality_enabled = True
        await session.commit()
    personality_plugin._enabled_cache.pop((chat_id, "supergroup"), None)


class E2EReplyTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    def setUp(self):
        # Cooldown/enabled state is process-global — start each test clean.
        personality_plugin._cooldowns.clear()
        personality_plugin._enabled_cache.clear()


class TestMiddlewareChainDoesNotBlockANormalUser(E2EReplyTestCase):
    """Regression coverage for the actual bug: with default settings, a
    normal non-admin user's ordinary message must reach personality.py,
    not get silently stopped by middleware in front of it."""

    async def test_force_sub_does_not_block_with_default_settings(self):
        self.assertFalse(settings.force_sub_enabled, "force_sub must default off")
        self.assertFalse(settings.force_sub_channel, "force_sub_channel must default blank")
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text="hey neko")
        context = _make_context()
        try:
            await force_sub_middleware(update, context)
        except Exception as e:
            self.fail(f"force_sub_middleware raised with default settings: {e!r}")
        update.message.reply_text.assert_not_awaited()  # no "join our channel" prompt either

    async def test_force_sub_still_works_correctly_when_operator_enables_it(self):
        # Not a regression test for the default — a correctness check that
        # the fix didn't break the feature for someone who *does* want it.
        with unittest.mock.patch.object(settings, "force_sub_enabled", True), \
             unittest.mock.patch.object(settings, "force_sub_channel", "SomeRealChannel"):
            update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text="hey neko")
            context = _make_context()
            context.bot.get_chat_member.return_value = MagicMock(status="left")
            with self.assertRaises(Exception):  # ApplicationHandlerStop
                await force_sub_middleware(update, context)

    async def test_access_control_does_not_block_with_default_settings(self):
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text="hey neko")
        context = _make_context()
        try:
            await access_control_middleware(update, context)
        except Exception as e:
            self.fail(f"access_control_middleware raised for an unrestricted user/chat: {e!r}")


class TestHandleMessageEndToEnd(E2EReplyTestCase):
    """Drives bot/plugins/personality.py's actual entry point, not the
    pieces underneath it."""

    async def test_enabled_chat_actually_sends_a_reply(self):
        await _enable_personality(GROUP_CHAT_ID)
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text="tell me a joke")
        context = _make_context()

        await personality_plugin.handle_message(update, context)

        update.message.reply_text.assert_awaited_once()
        (sent_text,), _ = update.message.reply_text.call_args
        self.assertIsInstance(sent_text, str)
        self.assertGreater(len(sent_text), 0)

    async def test_disabled_chat_sends_nothing(self):
        # Fresh chat_id, never toggled — must hit the documented
        # group-default (off) and stay silent, not error.
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID + 1, text="hello")
        context = _make_context()

        await personality_plugin.handle_message(update, context)

        update.message.reply_text.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()

    async def test_full_chain_middleware_then_handler_replies_for_a_normal_user(self):
        """The actual end-to-end path: force_sub -> access_control ->
        handle_message, exactly as PTB's handler groups would run them
        for a real incoming Update, for a plain non-admin user."""
        chat_id = GROUP_CHAT_ID + 2
        await _enable_personality(chat_id)
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=chat_id, text="good morning")
        context = _make_context()

        await force_sub_middleware(update, context)
        await access_control_middleware(update, context)
        await personality_plugin.handle_message(update, context)

        update.message.reply_text.assert_awaited_once()


class TestDirectAddressDetection(unittest.TestCase):
    """_is_direct_address is a plain sync helper — no DB/event loop needed."""

    def test_plain_message_is_not_a_direct_address(self):
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text="just chatting")
        context = _make_context()
        self.assertFalse(personality_plugin._is_direct_address(update, context))

    def test_reply_to_the_bots_message_is_a_direct_address(self):
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text="what do you mean?")
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.from_user.id = BOT_ID
        context = _make_context()
        self.assertTrue(personality_plugin._is_direct_address(update, context))

    def test_reply_to_a_different_user_is_not_a_direct_address(self):
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text="agreed")
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.from_user.id = REGULAR_USER_ID + 1  # someone else
        context = _make_context()
        self.assertFalse(personality_plugin._is_direct_address(update, context))

    def test_mentioning_the_bot_by_username_is_a_direct_address(self):
        text = f"hey @{BOT_USERNAME} what's up"
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text=text)
        entity = MagicMock()
        entity.type = MessageEntity.MENTION
        entity.offset = text.index("@")
        entity.length = len(BOT_USERNAME) + 1
        update.message.entities = [entity]
        context = _make_context()
        self.assertTrue(personality_plugin._is_direct_address(update, context))

    def test_mentioning_someone_else_is_not_a_direct_address(self):
        text = "hey @someone_else what's up"
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text=text)
        entity = MagicMock()
        entity.type = MessageEntity.MENTION
        entity.offset = text.index("@")
        entity.length = len("@someone_else")
        update.message.entities = [entity]
        context = _make_context()
        self.assertFalse(personality_plugin._is_direct_address(update, context))


class TestStripBotMention(unittest.TestCase):
    """_strip_bot_mention removes the bot's own @mention before the text
    goes into the generation pipeline, so a reply isn't generated from
    (and doesn't echo back) the mention as if it were part of what the
    user actually said."""

    def test_leading_mention_is_removed(self):
        text = f"@{BOT_USERNAME} how are you today"
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text=text)
        entity = MagicMock()
        entity.type = MessageEntity.MENTION
        entity.offset = text.index("@")
        entity.length = len(BOT_USERNAME) + 1
        update.message.entities = [entity]
        context = _make_context()
        cleaned = personality_plugin._strip_bot_mention(update, context, text)
        self.assertNotIn(BOT_USERNAME, cleaned)
        self.assertIn("how are you today", cleaned)

    def test_mid_message_mention_is_removed(self):
        text = f"hey @{BOT_USERNAME} what's up"
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text=text)
        entity = MagicMock()
        entity.type = MessageEntity.MENTION
        entity.offset = text.index("@")
        entity.length = len(BOT_USERNAME) + 1
        update.message.entities = [entity]
        context = _make_context()
        cleaned = personality_plugin._strip_bot_mention(update, context, text)
        self.assertNotIn(f"@{BOT_USERNAME}", cleaned)
        self.assertIn("hey", cleaned)
        self.assertIn("what's up", cleaned)

    def test_no_mention_present_returns_text_unchanged(self):
        text = "just chatting, no mention here"
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text=text)
        update.message.entities = []
        context = _make_context()
        cleaned = personality_plugin._strip_bot_mention(update, context, text)
        self.assertEqual(cleaned, text)

    def test_mentioning_someone_else_is_left_untouched(self):
        text = "hey @someone_else what's up"
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID, text=text)
        entity = MagicMock()
        entity.type = MessageEntity.MENTION
        entity.offset = text.index("@")
        entity.length = len("@someone_else")
        update.message.entities = [entity]
        context = _make_context()
        cleaned = personality_plugin._strip_bot_mention(update, context, text)
        self.assertEqual(cleaned, text)


class TestMentionAndReplyBypassTheChatToggle(E2EReplyTestCase):
    """The actual feature request: a direct @mention or reply should get
    a personality reply even in a chat that hasn't opted into ambient
    banter — only genuinely ambient (untargeted) messages respect that
    toggle."""

    async def test_mention_gets_a_reply_even_when_disabled_for_the_chat(self):
        chat_id = GROUP_CHAT_ID + 10  # never toggled — disabled by the group default
        text = f"@{BOT_USERNAME} tell me a joke"
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=chat_id, text=text)
        entity = MagicMock()
        entity.type = MessageEntity.MENTION
        entity.offset = text.index("@")
        entity.length = len(BOT_USERNAME) + 1
        update.message.entities = [entity]
        context = _make_context()

        await personality_plugin.handle_message(update, context)

        update.message.reply_text.assert_awaited_once()

    async def test_reply_to_bot_gets_a_reply_even_when_disabled_for_the_chat(self):
        chat_id = GROUP_CHAT_ID + 11
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=chat_id, text="what did you say?")
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.from_user.id = BOT_ID
        context = _make_context()

        await personality_plugin.handle_message(update, context)

        update.message.reply_text.assert_awaited_once()

    async def test_plain_ambient_message_still_respects_the_disabled_default(self):
        chat_id = GROUP_CHAT_ID + 12
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=chat_id, text="no mention or reply here")
        context = _make_context()

        await personality_plugin.handle_message(update, context)

        update.message.reply_text.assert_not_awaited()


class TestChatCommand(E2EReplyTestCase):
    """/chat — explicit personality invocation, independent of /ai."""

    async def test_chat_command_replies_even_when_disabled_for_the_chat(self):
        chat_id = GROUP_CHAT_ID + 20  # never toggled on
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=chat_id, text="/chat")
        context = _make_context()
        context.args = ["how", "are", "you"]

        await personality_plugin.cmd_chat(update, context)

        update.message.reply_text.assert_awaited_once()

    async def test_chat_command_with_no_args_and_no_reply_shows_usage(self):
        update = _make_update(user_id=REGULAR_USER_ID, chat_id=GROUP_CHAT_ID + 21, text="/chat")
        context = _make_context()
        context.args = []

        await personality_plugin.cmd_chat(update, context)

        update.message.reply_text.assert_awaited_once()
        (sent_text,), _ = update.message.reply_text.call_args
        self.assertIn("Usage", sent_text)

    async def test_ai_and_chat_are_no_longer_the_same_handler(self):
        from bot.plugins import ai as ai_plugin
        self.assertIsNot(ai_plugin.ai_chat, personality_plugin.cmd_chat)


if __name__ == "__main__":
    unittest.main()
