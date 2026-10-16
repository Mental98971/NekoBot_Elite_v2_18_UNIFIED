"""Anti-flood, slow mode, anti-spam, word blacklist and approvals (v2.12)."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_automod_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from sqlalchemy import select  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402

from _fakes import FakeBot, admin, make_context, make_update, member, owner  # noqa: E402
import bot.middleware.antiflood as flood  # noqa: E402
import bot.middleware.antispam as spam  # noqa: E402
import bot.plugins.approvals as appr  # noqa: E402
import bot.plugins.blacklist as blp  # noqa: E402
from bot.config import settings  # noqa: E402
from bot.core.database import Approval, Chat, async_session, init_db  # noqa: E402
from bot.utils import blacklist as bl, chatcache, modtools as mt  # noqa: E402

ME, MOD = 999, 5
_next = [-1005000000000]


def fresh_chat() -> int:
    _next[0] -= 1
    return _next[0]


async def make_chat(**cols) -> int:
    chat = fresh_chat()
    async with async_session() as s:
        s.add(Chat(id=chat, title="Auto", type="supergroup", **cols))
        await s.commit()
    return chat


def replies(update) -> str:
    text = "\n".join(str(c.args[0]) for c in update.effective_message.reply_text.call_args_list)
    assert "went sideways" not in text, text
    return text


class _Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    def setUp(self):
        mt._admin_cache.clear()
        chatcache.clear()
        flood._burst.clear()
        flood._last_post.clear()
        spam._last_notice.clear()


# ── anti-flood ───────────────────────────────────────────────────────────────
class TestAntiFlood(_Base):
    async def _spam(self, chat, user, ids, bot, **kw):
        last = None
        for mid in ids:
            update = make_update("hi", chat_id=chat, user_id=user, message_id=mid, **kw)
            try:
                await flood.antiflood_middleware(update, make_context(bot))
            except ApplicationHandlerStop:
                return update, True
            last = update
        return last, False

    async def test_per_chat_limit_warns_then_acts_and_deletes_only_the_flooders_messages(self):
        chat = await make_chat(antiflood_enabled=True, antiflood_limit=3)
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        ids = [101, 103, 105, 107, 109, 111]            # other members posted 102, 104, ... in between
        update, stopped = await self._spam(chat, 7, ids, bot)
        self.assertTrue(stopped)
        deleted = sorted(i for a, _k in bot.called("delete_messages") for i in a[1])
        self.assertEqual(deleted, ids, "v2.10 deleted the whole id range, taking other members' messages with it")
        (args, kwargs), = bot.called("ban_chat_member")
        self.assertEqual(args, (chat, 7))
        self.assertAlmostEqual(kwargs["until_date"], time.time() + settings.antiflood_ban_duration, delta=5)
        self.assertIn("Anti-Flood", bot.called("send_message")[0][0][1])

    async def test_the_third_message_gets_a_warning_with_limit_three(self):
        chat = await make_chat(antiflood_enabled=True, antiflood_limit=3)
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        update, stopped = await self._spam(chat, 7, [1, 2, 3], bot)
        self.assertFalse(stopped)
        self.assertIn("Slow down", update.effective_message.reply_text.call_args.args[0])

    async def test_the_configured_action_and_duration_are_used(self):
        chat = await make_chat(antiflood_enabled=True, antiflood_limit=3, antiflood_action="tmute", antiflood_action_seconds=900)
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        await self._spam(chat, 7, range(1, 8), bot)
        (args, kwargs), = bot.called("restrict_chat_member")
        self.assertAlmostEqual(kwargs["until_date"], time.time() + 900, delta=5)
        self.assertEqual(bot.called("ban_chat_member"), [])

    async def test_switched_off_per_chat(self):
        chat = await make_chat(antiflood_enabled=False)
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        _u, stopped = await self._spam(chat, 7, range(1, 30), bot)
        self.assertFalse(stopped)
        self.assertEqual(bot.calls, [])

    async def test_admins_approved_users_and_owners_are_exempt(self):
        chat = await make_chat(antiflood_enabled=True, antiflood_limit=3)
        async with async_session() as s:
            s.add(Approval(chat_id=chat, user_id=8, first_name="Trusted", approved_by=1))
            await s.commit()
        bot = FakeBot(bot_id=ME, members={(chat, 7): admin(7), (chat, 8): member(uid=8)})
        for uid in (7, 8, settings.owner_id):
            _u, stopped = await self._spam(chat, uid, range(1, 20), bot)
            self.assertFalse(stopped, uid)
        self.assertEqual(bot.called("ban_chat_member"), [])

    async def test_edited_messages_do_not_count(self):
        chat = await make_chat(antiflood_enabled=True, antiflood_limit=3)
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        _u, stopped = await self._spam(chat, 7, range(1, 20), bot, edited=True)
        self.assertFalse(stopped)

    async def test_anonymous_admin_and_channel_posts_are_ignored(self):
        chat = await make_chat(antiflood_enabled=True, antiflood_limit=3)
        bot = FakeBot(bot_id=ME)
        _u, stopped = await self._spam(chat, mt.ANON_ADMIN_BOT_ID, range(1, 20), bot, sender_chat=SimpleNamespace(id=chat))
        self.assertFalse(stopped)

    async def test_slow_mode_deletes_early_messages_and_exempts_admins(self):
        chat = await make_chat(antiflood_enabled=False, slowmode_seconds=30)
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7), (chat, 8): admin(8)})
        first = make_update("a", chat_id=chat, user_id=7, message_id=1)
        await flood.antiflood_middleware(first, make_context(bot))
        first.effective_message.delete.assert_not_awaited()
        second = make_update("b", chat_id=chat, user_id=7, message_id=2)
        with self.assertRaises(ApplicationHandlerStop):
            await flood.antiflood_middleware(second, make_context(bot))
        second.effective_message.delete.assert_awaited_once()
        for mid in (3, 4):
            a = make_update("x", chat_id=chat, user_id=8, message_id=mid)
            await flood.antiflood_middleware(a, make_context(bot))
            a.effective_message.delete.assert_not_awaited()
        flood._last_post[(chat, 7)] -= 31      # the gap has passed
        third = make_update("c", chat_id=chat, user_id=7, message_id=5)
        await flood.antiflood_middleware(third, make_context(bot))
        third.effective_message.delete.assert_not_awaited()

    async def test_tracker_memory_is_bounded(self):
        now = time.monotonic()
        flood._burst[(1, 1)] = flood.deque([(now - 100, 1)])
        flood._burst[(1, 2)] = flood.deque([(now, 2)])
        flood._burst[(1, 3)] = flood.deque()
        flood._last_post[(1, 1)] = now - 7200
        flood._prune(now, 10)
        self.assertEqual(list(flood._burst), [(1, 2)])
        self.assertEqual(flood._last_post, {})


# ── anti-spam + blacklist (same middleware) ──────────────────────────────────
# links (+0.30) + emoji flood (+0.20) + forwarded (+0.15) + spam keyword (+0.15) = 0.80 >= SPAM_ML_THRESHOLD (0.75)
SPAM = "free crypto winner click here https://a.example https://b.example https://c.example 🔥🔥🔥🔥🔥🔥🔥"


def spam_update(chat, user, message_id=1):
    update = make_update(SPAM, chat_id=chat, user_id=user, message_id=message_id)
    update.effective_message.forward_origin = object()
    return update


class TestAntiSpam(_Base):
    async def _run(self, update, bot):
        try:
            await spam.antispam_middleware(update, make_context(bot))
        except ApplicationHandlerStop:
            return True
        return False

    async def test_spam_is_deleted_once_noticed_once_and_stops_the_chain(self):
        chat = await make_chat()
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        stops = 0
        for mid in (1, 2, 3):
            update = spam_update(chat, 7, mid)
            stops += await self._run(update, bot)
            update.effective_message.delete.assert_awaited_once()
        self.assertEqual(stops, 3)
        self.assertEqual(len(bot.called("send_message")), 1, "a spammer used to get one public notice per message")

    async def test_switched_off_per_chat(self):
        off = await make_chat(antispam_enabled=False)
        update = spam_update(off, 7)
        self.assertFalse(await self._run(update, FakeBot(bot_id=ME, members={(off, 7): member(uid=7)})))
        update.effective_message.delete.assert_not_awaited()

    async def test_approved_users_and_admins_are_exempt_but_others_in_the_same_chat_are_not(self):
        chat = await make_chat()
        async with async_session() as s:
            s.add(Approval(chat_id=chat, user_id=9, first_name="T", approved_by=1))
            await s.commit()
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7), (chat, 8): admin(8)})
        for uid in (9, 8):
            update = spam_update(chat, uid)
            self.assertFalse(await self._run(update, bot), uid)
            update.effective_message.delete.assert_not_awaited()
        control = spam_update(chat, 7)
        self.assertTrue(await self._run(control, bot), "control: the same text from a plain member is spam")

    async def test_telegrams_own_account_is_never_scored(self):
        chat = await make_chat()
        update = spam_update(chat, mt.TELEGRAM_SERVICE_ID)
        self.assertFalse(await self._run(update, FakeBot(bot_id=ME)))
        update.effective_message.delete.assert_not_awaited()
        control = spam_update(chat, 7)
        self.assertTrue(await self._run(control, FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})))

    async def test_a_message_that_cannot_be_deleted_is_not_announced(self):
        chat = await make_chat()
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        update = spam_update(chat, 7)
        update.effective_message.delete.side_effect = Exception("no rights")
        self.assertFalse(await self._run(update, bot))
        update.effective_message.delete.assert_awaited_once()
        self.assertEqual(bot.called("send_message"), [])


class TestBlacklistMatching(unittest.TestCase):
    def test_whole_words_only(self):
        self.assertIsNone(bl.find_match("a first-class idea", ["ass"]))
        self.assertEqual(bl.find_match("what an ass", ["ass"]), "ass")
        self.assertIsNone(bl.find_match("badwords", ["badword"]))

    def test_case_unicode_and_zero_width_evasion(self):
        self.assertEqual(bl.find_match("BaDwOrD!", ["badword"]), "badword")
        self.assertEqual(bl.find_match("ｂａｄｗｏｒｄ", ["badword"]), "badword")
        self.assertEqual(bl.find_match("bad\u200bword", ["badword"]), "badword")

    def test_phrases_and_wildcards(self):
        self.assertEqual(bl.find_match("this is  a bad\nword here", ["bad word"]), "bad word")
        self.assertEqual(bl.find_match("spammers!", ["spam*"]), "spam*")
        self.assertIsNone(bl.find_match("sp am", ["spam*"]))

    def test_entries_are_literal_so_regex_syntax_cannot_break_or_hang(self):
        self.assertIsNone(bl.find_match("anything", ["(a+)+$", "[", "\\"]))
        self.assertEqual(bl.find_match("price is $$$ now", ["$$$"]), "$$$")
        started = time.monotonic()
        bl.find_match("a" * 5000 + "!", ["(a+)+$", "a*a*"])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_entry_cleaning(self):
        self.assertEqual(bl.clean_entry("  Bad   WORD "), "bad word")
        self.assertIsNone(bl.clean_entry(""))
        self.assertIsNone(bl.clean_entry("*"))
        self.assertIsNone(bl.clean_entry("a*b*c*d"))
        self.assertIsNone(bl.clean_entry("x" * 65))
        self.assertEqual(bl.split_entries("one\nTwo words\n\none\n"), ["one", "two words"])


class TestBlacklistEnforcement(_Base):
    async def _post(self, chat, text, *, user=7, bot=None, **kw):
        bot = bot or FakeBot(bot_id=ME, members={(chat, user): member(uid=user)})
        update = make_update(text, chat_id=chat, user_id=user, **kw)
        stopped = False
        try:
            await spam.antispam_middleware(update, make_context(bot, job_queue=MagicMock()))
        except ApplicationHandlerStop:
            stopped = True
        return update, bot, stopped

    async def test_default_action_deletes_and_warns(self):
        chat = await make_chat(blacklist_words=["badword"])
        update, bot, stopped = await self._post(chat, "this has BADWORD in it")
        self.assertTrue(stopped)
        update.effective_message.delete.assert_awaited_once()
        self.assertIn("Blacklisted word: badword", bot.called("send_message")[0][0][1])
        self.assertEqual(await mt.count_warns(chat, 7), 1)

    async def test_clean_and_near_miss_messages_pass(self):
        chat = await make_chat(blacklist_words=["badword"])
        for text in ("perfectly fine", "badwords are not the word", "bad word"):
            update, bot, stopped = await self._post(chat, text)
            self.assertFalse(stopped, text)
            update.effective_message.delete.assert_not_awaited()

    async def test_captions_and_edits_are_checked(self):
        chat = await make_chat(blacklist_words=["badword"], blacklist_action="delete")
        update, _bot, stopped = await self._post(chat, None, caption="look: badword")
        self.assertTrue(stopped)
        update, _bot, stopped = await self._post(chat, "badword", edited=True)
        self.assertTrue(stopped)
        update.effective_message.delete.assert_awaited_once()

    async def test_delete_action_is_silent(self):
        chat = await make_chat(blacklist_words=["badword"], blacklist_action="delete")
        _u, bot, stopped = await self._post(chat, "badword")
        self.assertTrue(stopped)
        self.assertEqual(bot.called("send_message"), [])
        self.assertEqual(await mt.count_warns(chat, 7), 0)

    async def test_timed_action_uses_the_configured_duration(self):
        chat = await make_chat(blacklist_words=["badword"], blacklist_action="tmute", blacklist_duration=1200)
        _u, bot, _s = await self._post(chat, "badword")
        (args, kwargs), = bot.called("restrict_chat_member")
        self.assertAlmostEqual(kwargs["until_date"], time.time() + 1200, delta=5)

    async def test_admins_and_approved_users_are_exempt_and_cost_no_api_call_for_clean_text(self):
        chat = await make_chat(blacklist_words=["badword"])
        async with async_session() as s:
            s.add(Approval(chat_id=chat, user_id=8, first_name="T", approved_by=1))
            await s.commit()
        for uid, bot in ((7, FakeBot(bot_id=ME, members={(chat, 7): admin(7)})), (8, FakeBot(bot_id=ME))):
            update, _b, stopped = await self._post(chat, "badword", user=uid, bot=bot)
            self.assertFalse(stopped, uid)
            update.effective_message.delete.assert_not_awaited()
        _u, quiet_bot, _s = await self._post(chat, "hello there", bot=FakeBot(bot_id=ME))
        self.assertEqual(quiet_bot.calls, [], "the admin lookup only happens after a blacklist hit")

    async def test_not_stopping_when_nothing_could_be_done(self):
        chat = await make_chat(blacklist_words=["badword"], blacklist_action="delete")
        bot = FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})
        update = make_update("badword", chat_id=chat, user_id=7)
        update.effective_message.delete.side_effect = Exception("cannot delete")
        await spam.antispam_middleware(update, make_context(bot))          # does not raise


class TestBlacklistCommands(_Base):
    def _ctx(self, chat, text, args=None, mod=None):
        bot = FakeBot(bot_id=ME, members={(chat, MOD): mod or admin(MOD), (chat, ME): admin(ME)})
        return make_update(text, chat_id=chat, user_id=MOD), make_context(bot, args or text.split()[1:]), bot

    async def test_add_list_remove_roundtrip_with_several_lines(self):
        chat = fresh_chat()
        update, ctx, _b = self._ctx(chat, "/addblacklist Foo\nbar baz\nfoo")
        await blp.addblacklist_cmd(update, ctx)
        self.assertIn("foo", replies(update))
        snap = await chatcache.get_chat_settings(chat)
        self.assertEqual(snap.blacklist_words, ["foo", "bar baz"])
        again, ctx, _b = self._ctx(chat, "/addblacklist foo")
        await blp.addblacklist_cmd(again, ctx)
        self.assertIn("already", replies(again))
        listed, ctx, _b = self._ctx(chat, "/blacklist")
        await blp.blacklist_cmd(listed, ctx)
        self.assertIn("bar baz", replies(listed))
        removed, ctx, _b = self._ctx(chat, "/unblacklist foo\nnope")
        await blp.unblacklist_cmd(removed, ctx)
        self.assertIn("Removed", replies(removed))
        self.assertIn("weren't", replies(removed))
        self.assertEqual((await chatcache.get_chat_settings(chat)).blacklist_words, ["bar baz"])

    async def test_list_is_capped_and_overflow_is_reported(self):
        chat = fresh_chat()
        words = "\n".join(f"word{i}" for i in range(bl.MAX_ENTRIES + 5))
        update, ctx, _b = self._ctx(chat, "/addblacklist " + words)
        await blp.addblacklist_cmd(update, ctx)
        self.assertEqual(len((await chatcache.get_chat_settings(chat)).blacklist_words), bl.MAX_ENTRIES)
        self.assertIn("full", replies(update))

    async def test_usage_and_invalid_entries(self):
        chat = fresh_chat()
        update, ctx, _b = self._ctx(chat, "/addblacklist")
        await blp.addblacklist_cmd(update, ctx)
        self.assertIn("Usage", replies(update))
        long, ctx, _b = self._ctx(chat, "/addblacklist " + "x" * 80)
        await blp.addblacklist_cmd(long, ctx)
        self.assertIn("Usage", replies(long))

    async def test_a_warning_when_the_bot_cannot_delete(self):
        chat = fresh_chat()
        bot = FakeBot(bot_id=ME, members={(chat, MOD): admin(MOD), (chat, ME): admin(ME, can_delete_messages=False)})
        update = make_update("/addblacklist foo", chat_id=chat, user_id=MOD)
        await blp.addblacklist_cmd(update, make_context(bot, ["foo"]))
        self.assertIn("does nothing", replies(update))

    async def test_mode_validation_and_persistence(self):
        chat = fresh_chat()
        bad, ctx, _b = self._ctx(chat, "/blacklistmode explode")
        await blp.blacklistmode_cmd(bad, ctx)
        self.assertIn("Usage", replies(bad))
        short, ctx, _b = self._ctx(chat, "/blacklistmode tmute 5s")
        await blp.blacklistmode_cmd(short, ctx)
        self.assertIn("30s", replies(short))
        ok, ctx, _b = self._ctx(chat, "/blacklistmode tban 2h")
        await blp.blacklistmode_cmd(ok, ctx)
        snap = await chatcache.get_chat_settings(chat)
        self.assertEqual((snap.blacklist_action, snap.blacklist_duration), ("tban", 7200))
        alias, ctx, _b = self._ctx(chat, "/blacklistmode nothing")
        await blp.blacklistmode_cmd(alias, ctx)
        self.assertEqual((await chatcache.get_chat_settings(chat)).blacklist_action, "delete")

    async def test_editing_needs_the_change_info_right(self):
        chat = fresh_chat()
        update, ctx, bot = self._ctx(chat, "/addblacklist foo", mod=admin(MOD, can_change_info=False))
        await blp.addblacklist_cmd(update, ctx)
        self.assertIn("Change group info", replies(update))
        self.assertIsNone(await chatcache.get_chat_settings(chat))


# ── approvals ────────────────────────────────────────────────────────────────
def reply_to(uid, first="Pal"):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid, first_name=first, username=None), message_id=50,
                           forum_topic_created=None, text="x", caption=None)


class TestApprovals(_Base):
    async def test_approve_unapprove_and_idempotence(self):
        chat = fresh_chat()
        bot = FakeBot(bot_id=ME, members={(chat, MOD): admin(MOD), (chat, 42): member(uid=42)})
        first = make_update("/approve", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await appr.approve_cmd(first, make_context(bot))
        self.assertIn("is approved", replies(first))
        self.assertTrue(await chatcache.is_approved(chat, 42))
        second = make_update("/approve", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await appr.approve_cmd(second, make_context(bot))
        self.assertIn("already approved", replies(second))
        gone = make_update("/unapprove", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await appr.unapprove_cmd(gone, make_context(bot))
        self.assertIn("no longer approved", replies(gone))
        self.assertFalse(await chatcache.is_approved(chat, 42), "the cache must see the change immediately")
        again = make_update("/unapprove", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await appr.unapprove_cmd(again, make_context(bot))
        self.assertIn("wasn't approved", replies(again))

    async def test_admins_bots_and_strangers_cannot_be_approved(self):
        chat = fresh_chat()
        bot = FakeBot(bot_id=ME, members={(chat, MOD): admin(MOD), (chat, 43): admin(43), (chat, 44): member("left", uid=44)})
        for uid, needle in ((43, "always exempt"), (44, "isn't in this chat"), (ME, "myself"), (31337, "isn't in this chat")):
            update = make_update("/approve", chat_id=chat, user_id=MOD, reply=reply_to(uid))
            await appr.approve_cmd(update, make_context(bot))
            self.assertIn(needle, replies(update), uid)
        async with async_session() as s:
            self.assertEqual((await s.execute(select(Approval).where(Approval.chat_id == chat))).scalars().all(), [])

    async def test_approving_requires_the_ban_right(self):
        chat = fresh_chat()
        bot = FakeBot(bot_id=ME, members={(chat, MOD): admin(MOD, can_restrict_members=False), (chat, 42): member(uid=42)})
        update = make_update("/approve", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await appr.approve_cmd(update, make_context(bot))
        self.assertFalse(await chatcache.is_approved(chat, 42))

    async def test_list_and_self_check(self):
        chat = fresh_chat()
        bot = FakeBot(bot_id=ME, members={(chat, MOD): admin(MOD), (chat, 42): member(uid=42, first="Pat<b>")})
        await appr.approve_cmd(make_update("/approve", chat_id=chat, user_id=MOD, reply=reply_to(42, "Pat<b>")), make_context(bot))
        listed = make_update("/approved", chat_id=chat, user_id=MOD)
        await appr.approved_cmd(listed, make_context(bot))
        text = replies(listed)
        self.assertIn("Pat&lt;b&gt;", text)
        self.assertNotIn("Pat<b>", text)
        mine = make_update("/approval", chat_id=chat, user_id=42)
        await appr.approval_cmd(mine, make_context(bot))
        self.assertIn("approved", replies(mine))
        empty = make_update("/approved", chat_id=fresh_chat(), user_id=MOD)
        chat2 = empty.effective_chat.id
        await appr.approved_cmd(empty, make_context(FakeBot(bot_id=ME, members={(chat2, MOD): admin(MOD)})))
        self.assertIn("Nobody", replies(empty))

    async def test_unapproveall_is_owner_only_and_confirmed(self):
        chat = fresh_chat()
        async with async_session() as s:
            s.add_all([Approval(chat_id=chat, user_id=u, first_name="x", approved_by=1) for u in (1, 2, 3)])
            await s.commit()
        bot = FakeBot(bot_id=ME, members={(chat, MOD): admin(MOD), (chat, 6): owner(6)})
        denied = make_update("/unapproveall", chat_id=chat, user_id=MOD)
        await appr.unapproveall_cmd(denied, make_context(bot))
        self.assertIn("owner", replies(denied))
        asked = make_update("/unapproveall", chat_id=chat, user_id=6)
        await appr.unapproveall_cmd(asked, make_context(bot))
        self.assertIn("reply_markup", asked.effective_message.reply_text.call_args.kwargs)
        query = SimpleNamespace(data=f"unapproveall:y:{chat}", from_user=SimpleNamespace(id=MOD, first_name="M"),
                                answer=AsyncMock(), edit_message_text=AsyncMock())
        await appr.unapproveall_callback(SimpleNamespace(callback_query=query, effective_user=query.from_user, effective_chat=None, effective_message=None), make_context(bot))
        self.assertEqual(len(await chatcache.get_approved_ids(chat)), 3, "a non-owner pressing the button changes nothing")
        query.from_user = SimpleNamespace(id=6, first_name="O")
        await appr.unapproveall_callback(SimpleNamespace(callback_query=query, effective_user=query.from_user, effective_chat=None, effective_message=None), make_context(bot))
        self.assertEqual(len(await chatcache.get_approved_ids(chat)), 0)


if __name__ == "__main__":
    unittest.main()
