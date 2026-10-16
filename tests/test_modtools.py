"""Tests for bot/utils/modtools.py and bot/utils/chatcache.py (v2.12 moderation core).

The first group reproduces defects of v2.10 *as regression tests*: the time-zone bug is shown with
the old formula so the test fails if anyone reintroduces it. DB-backed tests use a real,
disposable SQLite file (same approach as test_economy.py) and unique chat/user ids because all test
modules in one pytest process share the first engine that gets created.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_modtools_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from sqlalchemy import select  # noqa: E402
from telegram import ChatPermissions  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from _fakes import FakeBot, admin, make_context, make_update, member, owner  # noqa: E402
from bot.config import settings  # noqa: E402
from bot.core.database import Approval, Ban, Chat, TempAction, User, Warn, async_session, init_db  # noqa: E402
from bot.utils import chatcache, modtools as mt  # noqa: E402

CHAT = -1002100000001
ME = 999


class TestDurations(unittest.TestCase):
    def test_epoch_after_ignores_the_host_time_zone(self):
        """v2.10 built until_date from a naive UTC datetime: .timestamp() treats it as *local* time."""
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Asia/Kolkata"   # UTC+5:30
            time.tzset()
            old_formula = int((datetime.utcnow() + timedelta(seconds=1800)).timestamp())
            self.assertLess(old_formula - time.time(), 1800 - 3600, "premise: the old formula is hours off in IST")
            self.assertLess(old_formula, time.time(), "premise: a 30 min mute landed in the past (= permanent)")
            self.assertAlmostEqual(mt.epoch_after(1800), time.time() + 1800, delta=2)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_epoch_after_takes_an_explicit_clock(self):
        self.assertEqual(mt.epoch_after(60, now=1000.0), 1060)

    def test_parse_duration(self):
        self.assertEqual(mt.parse_duration("30m"), 1800)
        self.assertEqual(mt.parse_duration("2h"), 7200)
        self.assertEqual(mt.parse_duration("1w"), 604800)
        self.assertIsNone(mt.parse_duration("spam"))
        self.assertIsNone(mt.parse_duration(None))
        self.assertIsNone(mt.parse_duration("-5m"))

    def test_telegram_treats_short_and_huge_restrictions_as_permanent_so_they_are_rejected(self):
        with self.assertRaises(mt.DurationError):
            mt.validate_duration(10)
        with self.assertRaises(mt.DurationError):
            mt.validate_duration(367 * 86400)
        self.assertEqual(mt.validate_duration(30), 30)
        self.assertEqual(mt.validate_duration(366 * 86400), 366 * 86400)

    def test_format_span_keeps_two_units_instead_of_truncating(self):
        self.assertEqual(mt.format_span(5400), "1h 30m")
        self.assertEqual(mt.format_span(45), "45s")
        self.assertEqual(mt.format_span(93784), "1d 2h")
        self.assertEqual(mt.format_span(0), "0s")

    def test_clean_reason_is_bounded_and_has_a_default(self):
        self.assertEqual(mt.clean_reason([]), "No reason given")
        self.assertEqual(len(mt.clean_reason(["x" * 900])), mt.REASON_MAX)


class TestFriendlyErrors(unittest.TestCase):
    def test_known_errors_become_actionable_and_unknown_ones_do_not_leak(self):
        self.assertIn("admin", mt.friendly_telegram_error(BadRequest("Not enough rights to restrict/unrestrict chat member")))
        self.assertIn("owner", mt.friendly_telegram_error(BadRequest("Can't remove chat owner")))
        self.assertIn("admin", mt.friendly_telegram_error(BadRequest("User is an administrator of the chat")))
        generic = mt.friendly_telegram_error(BadRequest("some brand new internal failure xyz123"))
        self.assertNotIn("xyz123", generic)


class TestRights(unittest.IsolatedAsyncioTestCase):
    def test_member_has_right(self):
        self.assertTrue(mt.member_has_right(owner(), "promote"))
        self.assertTrue(mt.member_has_right(admin(), "restrict"))
        self.assertFalse(mt.member_has_right(admin(can_restrict_members=False), "restrict"))
        self.assertTrue(mt.member_has_right(admin(can_restrict_members=False), None), "None = any admin")
        self.assertFalse(mt.member_has_right(member(), None))
        self.assertFalse(mt.member_has_right(None, "restrict"))

    async def test_check_caller(self):
        bot = FakeBot(members={(CHAT, 1): owner(1), (CHAT, 2): admin(2), (CHAT, 3): admin(3, can_restrict_members=False),
                               (CHAT, 4): member(uid=4)})
        for uid, right, expect in [(1, "restrict", None), (2, "restrict", None), (3, "restrict", "Ban users"),
                                   (4, "restrict", "Admins only"), (77, "restrict", "verify")]:
            update = make_update(user_id=uid, chat_id=CHAT)
            problem = await mt.check_caller(update, make_context(bot), CHAT, right)
            if expect is None:
                self.assertIsNone(problem, (uid, right))
            else:
                self.assertIn(expect, problem or "", (uid, right))

    async def test_anonymous_admin_is_accepted_except_for_promote(self):
        update = make_update(user_id=mt.ANON_ADMIN_BOT_ID, chat_id=CHAT, sender_chat=SimpleNamespace(id=CHAT))
        ctx = make_context(FakeBot())
        self.assertIsNone(await mt.check_caller(update, ctx, CHAT, "restrict"))
        self.assertIn("Anonymous", await mt.check_caller(update, ctx, CHAT, "promote") or "")

    async def test_a_linked_channel_identity_is_not_an_anonymous_admin(self):
        update = make_update(user_id=mt.CHANNEL_BOT_ID, chat_id=CHAT, sender_chat=SimpleNamespace(id=-100999))
        self.assertFalse(mt.is_anonymous_admin(update))

    async def test_check_bot(self):
        ctx = make_context(FakeBot(members={(CHAT, ME): admin(ME, can_restrict_members=False)}))
        self.assertIn("Ban users", await mt.check_bot(ctx, CHAT, "restrict") or "")
        self.assertIsNone(await mt.check_bot(ctx, CHAT, "delete"))
        self.assertIn("admin", await mt.check_bot(make_context(FakeBot(members={(CHAT, ME): member(uid=ME)})), CHAT, None) or "")

    async def test_admin_status_is_cached_and_can_be_invalidated(self):
        bot = FakeBot(members={(CHAT, 5): admin(5)})
        mt._admin_cache.clear()
        self.assertTrue(await mt.is_chat_admin(bot, CHAT, 5))
        self.assertTrue(await mt.is_chat_admin(bot, CHAT, 5))
        self.assertEqual(len(bot.called("get_chat_member")), 1)
        mt.invalidate_admin(CHAT, 5)
        bot.members[(CHAT, 5)] = member(uid=5)
        self.assertFalse(await mt.is_chat_admin(bot, CHAT, 5))

    async def test_lookup_failure_is_not_trusted_and_only_briefly_cached(self):
        bot = FakeBot()
        mt._admin_cache.clear()
        self.assertFalse(await mt.is_chat_admin(bot, CHAT, 6))
        self.assertLess(mt._admin_cache[(CHAT, 6)][1] - time.monotonic(), 6)


class TestTargets(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())
        async def seed():
            async with async_session() as s:
                s.add(User(id=777001, username="SeenPerson", first_name="Seen"))
                await s.commit()
        asyncio.run(seed())

    async def test_reply_wins_and_all_args_are_the_reason(self):
        reply = SimpleNamespace(from_user=SimpleNamespace(id=42, first_name="Bob", username=None), forum_topic_created=None)
        t = await mt.resolve_target(make_update(reply=reply), make_context(FakeBot(), ["too", "loud"]))
        self.assertEqual((t.user_id, t.name, t.rest, t.from_reply), (42, "Bob", ["too", "loud"], True))

    async def test_reply_to_a_forum_topic_header_is_not_a_reply_to_a_person(self):
        header = SimpleNamespace(from_user=SimpleNamespace(id=1, first_name="TopicCreator", username=None), forum_topic_created=object())
        t = await mt.resolve_target(make_update(reply=header), make_context(FakeBot(), ["555", "spam"]))
        self.assertEqual((t.user_id, t.rest, t.from_reply), (555, ["spam"], False))
        self.assertIsNone(await mt.resolve_target(make_update(reply=header), make_context(FakeBot(), [])))

    async def test_numeric_id(self):
        t = await mt.resolve_target(make_update(), make_context(FakeBot(), ["12345", "bad", "bot"]))
        self.assertEqual((t.user_id, t.rest), (12345, ["bad", "bot"]))

    async def test_bad_ids_are_not_targets(self):
        for arg in ("-100123", "0", "12a", "²³", "1" * 20, "name"):
            self.assertIsNone(await mt.resolve_target(make_update(), make_context(FakeBot(), [arg])), arg)

    async def test_text_mention_with_a_multi_word_name(self):
        ent = SimpleNamespace(type="text_mention", user=SimpleNamespace(id=808, first_name="John", username=None), _text="John Doe")
        update = make_update(entities=[ent])
        t = await mt.resolve_target(update, make_context(FakeBot(), ["John", "Doe", "spamming", "links"]))
        self.assertEqual((t.user_id, t.name, t.rest), (808, "John", ["spamming", "links"]))

    async def test_username_seen_before_resolves_case_insensitively(self):
        t = await mt.resolve_target(make_update(), make_context(FakeBot(), ["@seenperson", "reason"]))
        self.assertEqual((t.user_id, t.name, t.rest), (777001, "Seen", ["reason"]))

    async def test_unknown_username_is_not_guessed(self):
        bot = MagicMock()
        async def boom(_):
            raise BadRequest("Chat not found")
        bot.get_chat = boom
        self.assertIsNone(await mt.resolve_target(make_update(), make_context(bot, ["@never_seen_zzz"])))

    async def test_block_reasons(self):
        bot = FakeBot(members={(CHAT, 10): owner(10), (CHAT, 11): admin(11), (CHAT, 12): member(uid=12)})
        ctx = make_context(bot)

        async def reason(uid, actor=5, **kw):
            return await mt.target_block_reason(ctx, CHAT, mt.Target(uid, str(uid)), actor, **kw)

        self.assertIn("myself", await reason(ME))
        self.assertIn("yourself", await reason(5, actor=5))
        self.assertIsNone(await reason(5, actor=5, allow_self=True) if False else None)
        self.assertIn("owners", await reason(settings.owner_id))
        self.assertIn("anonymous", await reason(mt.ANON_ADMIN_BOT_ID))
        self.assertIn("chat owner", await reason(10))
        self.assertIn("admin", await reason(11))
        self.assertIsNone(await reason(11, allow_admin=True))
        self.assertIsNone(await reason(12))
        self.assertIsNone(await reason(31337), "a non-member can still be pre-banned")


class TestActions(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_ban_is_permanent_and_recorded(self):
        chat, uid = CHAT - 10, 5001
        bot = FakeBot()
        err = await mt.punish(make_context(bot), chat, uid, "ban", reason="spam", by=1)
        self.assertIsNone(err)
        (args, kwargs), = bot.called("ban_chat_member")
        self.assertEqual((args, kwargs.get("until_date")), ((chat, uid), None))
        async with async_session() as s:
            row = (await s.execute(select(Ban).where(Ban.chat_id == chat, Ban.user_id == uid))).scalar_one()
        self.assertEqual((row.reason, row.banned_by, row.until), ("spam", 1, None))

    async def test_tban_uses_a_real_future_epoch_and_schedules_the_expiry_notice(self):
        chat, uid = CHAT - 11, 5002
        queue = MagicMock()
        bot = FakeBot()
        err = await mt.punish(make_context(bot, job_queue=queue), chat, uid, "tban", seconds=7200, reason="x", by=1)
        self.assertIsNone(err)
        (args, kwargs), = bot.called("ban_chat_member")
        self.assertAlmostEqual(kwargs["until_date"], time.time() + 7200, delta=5)
        queue.run_once.assert_called_once()
        self.assertAlmostEqual(queue.run_once.call_args.kwargs["when"], 7200, delta=1)
        async with async_session() as s:
            ta = (await s.execute(select(TempAction).where(TempAction.chat_id == chat, TempAction.user_id == uid))).scalar_one()
        self.assertEqual((ta.action, ta.reversed), ("ban", False))
        self.assertAlmostEqual((ta.expires_at - mt.utcnow()).total_seconds(), 7200, delta=5)

    async def test_a_ten_second_tmute_is_refused_instead_of_becoming_permanent(self):
        bot = FakeBot()
        err = await mt.punish(make_context(bot), CHAT - 12, 5003, "tmute", seconds=10)
        self.assertIn("30s", err)
        self.assertEqual(bot.calls, [])

    async def test_tmute_and_mute(self):
        chat, uid = CHAT - 13, 5004
        bot = FakeBot()
        self.assertIsNone(await mt.punish(make_context(bot), chat, uid, "tmute", seconds=1800))
        self.assertIsNone(await mt.punish(make_context(bot), chat, uid + 1, "mute"))
        first, second = bot.called("restrict_chat_member")
        self.assertFalse(first[0][2].can_send_messages)
        self.assertAlmostEqual(first[1]["until_date"], time.time() + 1800, delta=5)
        self.assertIsNone(second[1]["until_date"])

    async def test_kick_bans_then_unbans_only_if_banned(self):
        bot = FakeBot()
        self.assertIsNone(await mt.punish(make_context(bot), CHAT - 14, 5005, "kick"))
        self.assertEqual([n for n, _a, _k in bot.calls], ["ban_chat_member", "unban_chat_member"])
        self.assertTrue(bot.called("unban_chat_member")[0][1]["only_if_banned"])

    async def test_telegram_errors_come_back_as_friendly_text(self):
        bot = FakeBot(fail={"ban_chat_member": BadRequest("Not enough rights to restrict/unrestrict chat member")})
        err = await mt.punish(make_context(bot), CHAT - 15, 5006, "ban")
        self.assertIn("admin right", err)
        self.assertNotIn("restrict/unrestrict", err)
        async with async_session() as s:
            rows = (await s.execute(select(Ban).where(Ban.chat_id == CHAT - 15))).scalars().all()
        self.assertEqual(rows, [], "a failed ban must not be recorded")

    async def test_unban_clears_the_record_and_reverses_the_temp_action(self):
        chat, uid = CHAT - 16, 5007
        await mt.punish(make_context(FakeBot(), job_queue=MagicMock()), chat, uid, "tban", seconds=3600, by=1)
        bot = FakeBot()
        self.assertIsNone(await mt.lift(make_context(bot), chat, uid, "ban"))
        self.assertTrue(bot.called("unban_chat_member")[0][1]["only_if_banned"])
        async with async_session() as s:
            self.assertEqual((await s.execute(select(Ban).where(Ban.chat_id == chat))).scalars().all(), [])
            ta = (await s.execute(select(TempAction).where(TempAction.chat_id == chat))).scalar_one()
        self.assertTrue(ta.reversed, "otherwise the bot would later announce an expiry of a ban that was lifted early")

    async def test_unmute_restores_the_chats_default_permissions_not_admin_powers(self):
        defaults = ChatPermissions(can_send_messages=True, can_send_polls=False, can_change_info=False, can_pin_messages=False)
        bot = FakeBot(chat_permissions=defaults)
        self.assertIsNone(await mt.lift(make_context(bot), CHAT - 17, 5008, "mute"))
        (args, _kw), = bot.called("restrict_chat_member")
        self.assertIs(args[2], defaults)

    async def test_unmute_fallback_never_grants_change_info_or_pin(self):
        """v2.10's /unmute handed every unmuted member can_change_info + can_pin_messages."""
        bot = FakeBot(fail={"get_chat": BadRequest("chat not found")})
        await mt.lift(make_context(bot), CHAT - 18, 5009, "mute")
        perms = bot.called("restrict_chat_member")[0][0][2]
        self.assertTrue(perms.can_send_messages)
        for risky in ("can_change_info", "can_pin_messages", "can_invite_users", "can_manage_topics"):
            self.assertFalse(getattr(perms, risky), risky)

    async def test_bulk_delete_batches_by_100_and_falls_back_per_message(self):
        bot = FakeBot()
        self.assertEqual(await mt.delete_messages(bot, CHAT, list(range(1, 251))), 250)
        self.assertEqual([len(a[1]) for a, _k in bot.called("delete_messages")], [100, 100, 50])
        bad = FakeBot(fail={"delete_messages": BadRequest("Message can't be deleted")})
        self.assertEqual(await mt.delete_messages(bad, CHAT, [5, 6, 7]), 3)
        self.assertEqual(len(bad.called("delete_message")), 3)


class TestWarns(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_limit_reached_applies_the_chat_action_and_clears_warns(self):
        chat, uid = CHAT - 20, 6001
        async with async_session() as s:
            s.add(Chat(id=chat, title="Warn chat", type="supergroup", warn_limit=2, warn_action="kick"))
            await s.commit()
        bot = FakeBot()
        ctx = make_context(bot)
        first = await mt.add_warn(ctx, chat, uid, by=1, reason="one")
        self.assertEqual((first.count, first.limit, first.punished), (1, 2, False))
        second = await mt.add_warn(ctx, chat, uid, by=1, reason="two")
        self.assertEqual((second.count, second.punished, second.action), (2, True, "kick"))
        self.assertEqual(await mt.count_warns(chat, uid), 0)
        self.assertEqual(len(bot.called("ban_chat_member")), 1)

    async def test_failed_action_keeps_the_warns_so_it_can_be_retried(self):
        chat, uid = CHAT - 21, 6002
        async with async_session() as s:
            s.add(Chat(id=chat, title="W", type="supergroup", warn_limit=1, warn_action="ban"))
            await s.commit()
        bot = FakeBot(fail={"ban_chat_member": BadRequest("Not enough rights")})
        out = await mt.add_warn(make_context(bot), chat, uid, by=1, reason="x")
        self.assertFalse(out.punished)
        self.assertIsNotNone(out.error)
        self.assertEqual(await mt.count_warns(chat, uid), 1)
        self.assertIn("couldn't act", mt.describe_warn_outcome("U", out, "x"))

    async def test_defaults_come_from_settings(self):
        limit, action = await mt.warn_settings(CHAT - 22)
        self.assertEqual((limit, action), (settings.default_warn_limit, settings.default_warn_action))

    async def test_warning_a_user_with_no_rows_creates_the_rows_foreign_keys_need(self):
        chat, uid = CHAT - 23, 6003
        await mt.add_warn(make_context(FakeBot()), chat, uid, by=1, reason="x", user_name="Newcomer")
        async with async_session() as s:
            self.assertIsNotNone(await s.get(Chat, chat))
            self.assertEqual((await s.get(User, uid)).first_name, "Newcomer")
            self.assertEqual(len((await s.execute(select(Warn).where(Warn.chat_id == chat))).scalars().all()), 1)

    async def test_reason_is_html_escaped_in_the_notice(self):
        out = mt.WarnOutcome(count=1, limit=3, action="mute")
        self.assertIn("&lt;b&gt;", mt.describe_warn_outcome("U", out, "<b>"))


class TestEnforceAction(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_nothing_and_delete_say_nothing(self):
        bot = FakeBot()
        for action in (None, "nothing", "delete"):
            self.assertIsNone(await mt.enforce_action(make_context(bot), CHAT - 30, 7001, "N", action, reason="r"))
        self.assertEqual(bot.calls, [])

    async def test_ban_and_tmute_notices(self):
        ctx = make_context(FakeBot(), job_queue=MagicMock())
        banned = await mt.enforce_action(ctx, CHAT - 31, 7002, "N<", "ban", reason="bad <word>")
        self.assertIn("banned", banned)
        self.assertIn("&lt;word&gt;", banned)
        muted = await mt.enforce_action(ctx, CHAT - 31, 7003, "N", "tmute", seconds=1800, reason="r")
        self.assertIn("muted for 30m", muted)

    async def test_failure_is_swallowed(self):
        bot = FakeBot(fail={"ban_chat_member": BadRequest("Not enough rights")})
        self.assertIsNone(await mt.enforce_action(make_context(bot), CHAT - 32, 7004, "N", "ban", reason="r"))

    async def test_warn_action_goes_through_the_warn_system(self):
        out = await mt.enforce_action(make_context(FakeBot()), CHAT - 33, 7005, "N", "warn", reason="r")
        self.assertIn("1/", out)


class TestChatCache(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    def setUp(self):
        chatcache.clear()

    async def test_missing_chat_is_none_and_commit_makes_it_visible_immediately(self):
        chat = CHAT - 40
        self.assertIsNone(await chatcache.get_chat_settings(chat))
        async with async_session() as s:
            s.add(Chat(id=chat, title="Cached", type="supergroup", lock_url=True))
            await s.commit()
        snap = await chatcache.get_chat_settings(chat)   # would still be None for CHAT_TTL without the commit hook
        self.assertEqual((snap.title, snap.lock_url), ("Cached", True))

    async def test_update_by_any_writer_is_seen_on_the_next_read(self):
        chat = CHAT - 41
        async with async_session() as s:
            s.add(Chat(id=chat, title="A", type="supergroup", lock_url=False))
            await s.commit()
        self.assertFalse((await chatcache.get_chat_settings(chat)).lock_url)
        async with async_session() as s:
            (await s.get(Chat, chat)).lock_url = True
            await s.commit()
        self.assertTrue((await chatcache.get_chat_settings(chat)).lock_url)

    async def test_repeated_reads_hit_the_database_once(self):
        from sqlalchemy import event
        from bot.core.database import engine

        chat = CHAT - 42
        async with async_session() as s:
            s.add(Chat(id=chat, title="Q", type="supergroup"))
            await s.commit()
        chatcache.clear()
        statements = []
        listener = lambda conn, cur, stmt, params, ctx, many: statements.append(stmt)  # noqa: E731
        event.listen(engine.sync_engine, "before_cursor_execute", listener)
        try:
            for _ in range(5):
                await chatcache.get_chat_settings(chat)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", listener)
        self.assertEqual(len([s for s in statements if "FROM chats" in s]), 1)

    async def test_rolled_back_changes_do_not_invalidate_and_do_not_leak(self):
        chat = CHAT - 43
        async with async_session() as s:
            s.add(Chat(id=chat, title="Keep", type="supergroup"))
            await s.commit()
        await chatcache.get_chat_settings(chat)
        async with async_session() as s:
            (await s.get(Chat, chat)).title = "Discarded"
            await s.flush()
            await s.rollback()
        self.assertEqual((await chatcache.get_chat_settings(chat)).title, "Keep")

    async def test_a_read_that_overlaps_an_invalidation_is_not_stored(self):
        chat = CHAT - 44
        real = chatcache.async_session

        class Racy:
            def __call__(self):
                chatcache.invalidate_chat(chat)   # a commit lands while the reader is mid-query
                return real()

        chatcache.async_session = Racy()
        try:
            await chatcache.get_chat_settings(chat)
        finally:
            chatcache.async_session = real
        self.assertNotIn(chat, chatcache._chat)

    async def test_approvals_are_cached_and_invalidated_by_commit(self):
        chat = CHAT - 45
        self.assertFalse(await chatcache.is_approved(chat, 8001))
        async with async_session() as s:
            s.add(Approval(chat_id=chat, user_id=8001, first_name="A", approved_by=1))
            await s.commit()
        self.assertTrue(await chatcache.is_approved(chat, 8001))
        async with async_session() as s:
            await s.delete((await s.execute(select(Approval).where(Approval.chat_id == chat))).scalar_one())
            await s.commit()
        self.assertFalse(await chatcache.is_approved(chat, 8001))

    async def test_exemption_covers_admins_approved_users_and_non_users(self):
        chat = CHAT - 46
        bot = FakeBot(members={(chat, 1): admin(1), (chat, 2): member(uid=2)})
        mt._admin_cache.clear()
        async with async_session() as s:
            s.add(Approval(chat_id=chat, user_id=3, first_name="Trusted", approved_by=1))
            await s.commit()
        self.assertTrue(await mt.is_exempt(bot, chat, 1))
        self.assertTrue(await mt.is_exempt(bot, chat, 3))
        self.assertTrue(await mt.is_exempt(bot, chat, settings.owner_id))
        self.assertTrue(await mt.is_exempt(bot, chat, mt.ANON_ADMIN_BOT_ID))
        self.assertFalse(await mt.is_exempt(bot, chat, 2))


if __name__ == "__main__":
    unittest.main()
