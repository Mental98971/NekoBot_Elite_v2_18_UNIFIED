"""Tests for bot/plugins/reactions.py."""
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

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_reactions_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.core.database import init_db  # noqa: E402
from bot.plugins.reactions import (  # noqa: E402
    ACTION_VERBS,
    ACTIONS,
    SOLO_ACTIONS,
    _build_caption,
    _reactions_enabled,
    reactions_toggle_cmd,
)


def _make_update(user_id: int, chat_id: int, args=None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = "Tester"
    update.effective_chat.id = chat_id
    update.effective_chat.title = "Test Chat"
    update.effective_chat.type = "supergroup"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    return update, context


class TestActionLists(unittest.TestCase):
    def test_no_overlap_between_verb_and_solo_actions(self):
        self.assertEqual(set(ACTION_VERBS) & SOLO_ACTIONS, set())

    def test_actions_is_the_union(self):
        self.assertEqual(set(ACTIONS), set(ACTION_VERBS) | SOLO_ACTIONS)

    def test_no_stray_unreachable_or_unintegrated_entries_carried_over(self):
        # Source had a mixed-case "hTojiy" (never matchable) and a
        # "/wallpaper" command tied to a different, unintegrated API —
        # neither should have made it into the ported action list.
        self.assertNotIn("hTojiy", ACTIONS)
        self.assertNotIn("wallpaper", ACTIONS)


class TestCaptionBuilding(unittest.TestCase):
    def test_targeted_interpersonal_action_builds_caption(self):
        caption = _build_caption("hug", "Alice", 1, "Bob", 2)
        self.assertIn("hugs", caption)
        self.assertIn("Alice", caption)
        self.assertIn("Bob", caption)

    def test_targeting_yourself_gets_the_self_caption(self):
        caption = _build_caption("pat", "Alice", 1, "Alice", 1)
        self.assertIn("themselves", caption)

    def test_solo_action_never_builds_a_caption_even_with_a_target(self):
        caption = _build_caption("cry", "Alice", 1, "Bob", 2)
        self.assertIsNone(caption)

    def test_no_target_means_no_caption(self):
        caption = _build_caption("hug", "Alice", 1, None, None)
        self.assertIsNone(caption)

    def test_every_verb_actions_kiss_pluralizes_correctly(self):
        # The one irregular case (kiss -> kisses, not "kisss") should be
        # spelled out explicitly rather than derived.
        self.assertEqual(ACTION_VERBS["kiss"], "kisses")


class TestReactionsToggle(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_toggle_on_then_off(self):
        chat_id = -100555
        update, context = _make_update(user_id=1, chat_id=chat_id, args=["on"])
        await reactions_toggle_cmd(update, context)
        self.assertTrue(await _reactions_enabled(chat_id))

        update, context = _make_update(user_id=1, chat_id=chat_id, args=["off"])
        await reactions_toggle_cmd(update, context)
        self.assertFalse(await _reactions_enabled(chat_id))

    async def test_defaults_to_disabled_for_a_chat_never_toggled(self):
        self.assertFalse(await _reactions_enabled(-100556))

    async def test_bad_argument_leaves_state_unchanged_and_explains_usage(self):
        chat_id = -100557
        update, context = _make_update(user_id=1, chat_id=chat_id, args=["maybe"])
        await reactions_toggle_cmd(update, context)
        update.message.reply_text.assert_awaited_once()
        self.assertIn("on | off", update.message.reply_text.call_args[0][0])
        self.assertFalse(await _reactions_enabled(chat_id))


if __name__ == "__main__":
    unittest.main()
