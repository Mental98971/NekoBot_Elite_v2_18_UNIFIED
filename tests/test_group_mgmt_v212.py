"""Notes, filters, locks, rules, night mode and clean-service/welcome behaviour after the v2.12 rework.

(The pre-existing tests/test_group_mgmt.py keeps covering welcome/goodbye/rules rendering and lock
rules with fakes; this file covers what v2.12 changed or added, against a real SQLite database.)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_gm212_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from telegram import ChatPermissions  # noqa: E402
from telegram.error import BadRequest, Forbidden  # noqa: E402

from _fakes import FakeBot, admin, make_context, make_update, member, owner  # noqa: E402
import bot.plugins.group_mgmt as gm  # noqa: E402
from bot.core.database import Chat, Note, async_session, init_db  # noqa: E402
from bot.utils import chatcache, modtools as mt  # noqa: E402

ME, MOD = 999, 5
_next = [-1007000000000]


def fresh_chat() -> int:
    _next[0] -= 1
    return _next[0]


def replies(update) -> str:
    text = "\n".join(str(c.args[0]) for c in update.effective_message.reply_text.call_args_list)
    assert "went sideways" not in text, text
    return text


def person(uid, first="Joiner"):
    return SimpleNamespace(id=uid, first_name=first, last_name=None, username=None, is_bot=False)


class _Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    def setUp(self):
        mt._admin_cache.clear()
        chatcache.clear()
        gm._lock_notice.clear()
        gm._night_failures.clear()

    def world(self, chat, *, mod=None, extra=None, **kw):
        members = {(chat, MOD): mod or admin(MOD), (chat, ME): admin(ME)}
        members.update(extra or {})
        return FakeBot(bot_id=ME, members=members, **kw)

    async def cmd(self, handler, chat, text, *, bot=None, user=MOD, mod=None, **kw):
        bot = bot or self.world(chat, mod=mod)
        update = make_update(text, chat_id=chat, user_id=user, **kw)
        await handler(update, make_context(bot, text.split()[1:]))
        return update, bot

    async def make_chat(self, **cols) -> int:
        chat = fresh_chat()
        async with async_session() as s:
            s.add(Chat(id=chat, title="GM", type="supergroup", **cols))
            await s.commit()
        return chat


class TestNotes(_Base):
    async def test_saving_an_existing_name_updates_it_instead_of_breaking_get(self):
        """v2.10 inserted a second row; /get then crashed with MultipleResultsFound."""
        chat = fresh_chat()
        first, _b = await self.cmd(gm.save_cmd, chat, "/save rules be nice")
        self.assertIn("saved", replies(first))
        second, _b = await self.cmd(gm.save_cmd, chat, "/save RULES be kind")
        self.assertIn("updated", replies(second))
        async with async_session() as s:
            rows = (await s.execute(select(Note).where(Note.chat_id == chat))).scalars().all()
        self.assertEqual([(r.name, r.content) for r in rows], [("rules", "be kind")])
        got, _b = await self.cmd(gm.get_cmd, chat, "/get rules", user=7)
        self.assertEqual(got.effective_message.reply_text.call_args.args[0], "be kind")

    async def test_the_database_itself_refuses_duplicate_names(self):
        chat = fresh_chat()
        async with async_session() as s:
            s.add(Note(chat_id=chat, name="dup", content="a"))
            await s.commit()
        with self.assertRaises(IntegrityError):
            async with async_session() as s:
                s.add(Note(chat_id=chat, name="dup", content="b"))
                await s.commit()

    async def test_media_notes_keep_their_real_type(self):
        photo = SimpleNamespace(text=None, caption="cap", photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")],
                                video=None, animation=None, document=None, audio=None, voice=None, sticker=None)
        self.assertEqual(gm._note_from_message(photo, ""), ("cap", "big", "photo"))
        gif = SimpleNamespace(text=None, caption=None, photo=None, video=None, animation=SimpleNamespace(file_id="gif1"),
                              document=SimpleNamespace(file_id="gif1"), audio=None, voice=None, sticker=None)
        self.assertEqual(gm._note_from_message(gif, "")[2], "animation", "Telegram sets both fields on a GIF; saving it as a document stops it animating")
        self.assertEqual(gm._note_from_message(None, "plain"), ("plain", None, "text"))
        self.assertEqual(gm._note_from_message(None, ""), (None, None, "text"))

    async def test_save_validation(self):
        chat = fresh_chat()
        none, _b = await self.cmd(gm.save_cmd, chat, "/save")
        self.assertIn("Usage", replies(none))
        long, _b = await self.cmd(gm.save_cmd, chat, "/save " + "n" * 70 + " text")
        self.assertIn("1-64", replies(long))
        empty, _b = await self.cmd(gm.save_cmd, chat, "/save lonely")
        self.assertIn("Reply to a message", replies(empty))

    async def test_hash_shortcut_clear_and_list(self):
        chat = fresh_chat()
        await self.cmd(gm.save_cmd, chat, "/save hello Hi there")
        update = make_update("#Hello please", chat_id=chat, user_id=7)
        await gm.filter_listener(update, make_context(self.world(chat)))
        self.assertEqual(update.effective_message.reply_text.call_args.args[0], "Hi there")
        listed, _b = await self.cmd(gm.notes_cmd, chat, "/notes", user=7)
        self.assertIn("hello", replies(listed))
        cleared, _b = await self.cmd(gm.clear_cmd, chat, "/clear hello")
        self.assertIn("removed", replies(cleared))
        missing, _b = await self.cmd(gm.clear_cmd, chat, "/clear hello")
        self.assertIn("No note named", replies(missing))
        empty, _b = await self.cmd(gm.notes_cmd, chat, "/notes", user=7)
        self.assertIn("No notes", replies(empty))

    async def test_clearall_is_owner_only_and_needs_confirmation(self):
        chat = fresh_chat()
        await self.cmd(gm.save_cmd, chat, "/save a one")
        await self.cmd(gm.save_cmd, chat, "/save b two")
        bot = self.world(chat, extra={(chat, 6): owner(6)})
        denied, _b = await self.cmd(gm.clearall_cmd, chat, "/clearall", bot=bot)
        self.assertIn("owner", replies(denied))
        asked, _b = await self.cmd(gm.clearall_cmd, chat, "/clearall", bot=bot, user=6)
        self.assertIn("reply_markup", asked.effective_message.reply_text.call_args.kwargs)
        query = SimpleNamespace(data=f"clearnotes:y:{chat}", from_user=SimpleNamespace(id=MOD, first_name="M"),
                                answer=AsyncMock(), edit_message_text=AsyncMock())
        fake = SimpleNamespace(callback_query=query, effective_user=query.from_user, effective_chat=None, effective_message=None)
        await gm.clearall_callback(fake, make_context(bot))
        async with async_session() as s:
            self.assertEqual(len((await s.execute(select(Note).where(Note.chat_id == chat))).scalars().all()), 2)
        query.from_user = SimpleNamespace(id=6, first_name="O")
        await gm.clearall_callback(fake, make_context(bot))
        async with async_session() as s:
            self.assertEqual((await s.execute(select(Note).where(Note.chat_id == chat))).scalars().all(), [])

    async def test_a_connected_admin_can_save_a_note_from_a_private_chat(self):
        chat = fresh_chat()
        await mt.set_connection(MOD, chat)
        bot = self.world(chat)
        update = make_update("/save remote works", chat_id=MOD, chat_type="private", user_id=MOD)
        await gm.save_cmd(update, make_context(bot, ["remote", "works"]))
        self.assertIn("saved", replies(update))
        async with async_session() as s:
            self.assertEqual((await s.execute(select(Note.name).where(Note.chat_id == chat))).scalars().all(), ["remote"])


class TestFilters(_Base):
    def test_whole_word_matching_longest_trigger_first(self):
        """v2.10 used `trigger in text`: 'hi' fired on 'this' and 'pi' on 'spider'."""
        self.assertIsNone(gm.match_filter("this is spider season", ["hi", "pi"]))
        self.assertEqual(gm.match_filter("oh hi there", ["hi", "pi"]), "hi")
        self.assertEqual(gm.match_filter("good morning everyone", ["good", "good morning"]), "good morning")
        self.assertEqual(gm.match_filter("HELLO!", ["hello"]), "hello")
        self.assertIsNone(gm.match_filter("anything", ["(a+)+$"]))
        self.assertEqual(gm.match_filter("wait (a+)+$ ok", ["(a+)+$"]), "(a+)+$")

    async def test_add_list_trigger_and_remove(self):
        chat = fresh_chat()
        added, _b = await self.cmd(gm.addfilter_cmd, chat, "/filter hi Hello to you!")
        self.assertIn("added", replies(added))
        listed, _b = await self.cmd(gm.listfilters_cmd, chat, "/filters")
        self.assertIn("hi", replies(listed))
        hit = make_update("oh hi", chat_id=chat, user_id=7)
        await gm.filter_listener(hit, make_context(self.world(chat)))
        self.assertEqual(hit.effective_message.reply_text.call_args.args[0], "Hello to you!")
        miss = make_update("this spider", chat_id=chat, user_id=7)
        await gm.filter_listener(miss, make_context(self.world(chat)))
        miss.effective_message.reply_text.assert_not_awaited()
        removed, _b = await self.cmd(gm.stopfilter_cmd, chat, "/stopfilter hi")
        self.assertIn("Removed", replies(removed))
        again, _b = await self.cmd(gm.stopfilter_cmd, chat, "/stopfilter hi")
        self.assertIn("No filter", replies(again))

    async def test_limits(self):
        chat = fresh_chat()
        long, _b = await self.cmd(gm.addfilter_cmd, chat, "/filter " + "k" * 70 + " reply")
        self.assertIn("limited", replies(long))
        for i in range(gm.MAX_FILTERS):
            await gm._save_filter(chat, "T", "supergroup", f"k{i}", {"type": "text", "text": "x"})
        self.assertFalse(await gm._save_filter(chat, "T", "supergroup", "one-too-many", {"type": "text", "text": "x"}))
        self.assertTrue(await gm._save_filter(chat, "T", "supergroup", "k0", {"type": "text", "text": "replaced"}), "replacing an existing trigger is not growth")

    async def test_stopallfilters(self):
        chat = fresh_chat()
        await self.cmd(gm.addfilter_cmd, chat, "/filter a one")
        await self.cmd(gm.addfilter_cmd, chat, "/filter b two")
        await self.cmd(gm.stopallfilters_cmd, chat, "/stopallfilters")
        self.assertFalse((await chatcache.get_chat_settings(chat)).filters)


class TestLocksAndService(_Base):
    async def test_locking_needs_a_bot_that_can_delete(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, ME): admin(ME, can_delete_messages=False)})
        refused, _b = await self.cmd(gm.lock_cmd, chat, "/lock url", bot=bot)
        self.assertIn("Delete messages", replies(refused))
        self.assertIsNone(await chatcache.get_chat_settings(chat))
        ok, _b = await self.cmd(gm.lock_cmd, chat, "/lock url")
        self.assertTrue((await chatcache.get_chat_settings(chat)).lock_url)
        await self.cmd(gm.unlock_cmd, chat, "/unlock url")
        self.assertFalse((await chatcache.get_chat_settings(chat)).lock_url)

    async def test_anonymous_admin_posts_are_not_deleted_by_locks(self):
        chat = await self.make_chat(lock_photo=True)
        update = make_update("", chat_id=chat, user_id=mt.ANON_ADMIN_BOT_ID, sender_chat=SimpleNamespace(id=chat))
        update.effective_message.photo = [object()]
        await gm.lock_listener(update, make_context(FakeBot(bot_id=ME)))
        update.effective_message.delete.assert_not_awaited()
        control = make_update("", chat_id=chat, user_id=7)
        control.effective_message.photo = [object()]
        await gm.lock_listener(control, make_context(FakeBot(bot_id=ME, members={(chat, 7): member(uid=7)})))
        control.effective_message.delete.assert_awaited_once()

    async def test_clean_service_removes_join_and_pin_notices_but_not_chat(self):
        chat = await self.make_chat(clean_service=True)
        join = make_update("", chat_id=chat, user_id=7)
        join.effective_message.new_chat_members = [person(7)]
        await gm.lock_listener(join, make_context(FakeBot(bot_id=ME)))
        join.effective_message.delete.assert_awaited_once()
        pin = make_update("", chat_id=chat, user_id=7)
        pin.effective_message.pinned_message = object()
        await gm.lock_listener(pin, make_context(FakeBot(bot_id=ME)))
        pin.effective_message.delete.assert_awaited_once()
        chatter = make_update("hello", chat_id=chat, user_id=7)
        await gm.lock_listener(chatter, make_context(FakeBot(bot_id=ME)))
        chatter.effective_message.delete.assert_not_awaited()

    async def test_toggles_follow_the_chat_default_and_check_the_bot_right(self):
        chat = fresh_chat()
        shown, _b = await self.cmd(gm.cleanservice_cmd, chat, "/cleanservice")
        self.assertIn("off", replies(shown))
        no_delete = self.world(chat, extra={(chat, ME): admin(ME, can_delete_messages=False)})
        refused, _b = await self.cmd(gm.cleanservice_cmd, chat, "/cleanservice on", bot=no_delete)
        self.assertIn("Delete messages", replies(refused))
        await self.cmd(gm.cleanservice_cmd, chat, "/cleanservice on")
        self.assertTrue((await chatcache.get_chat_settings(chat)).clean_service)
        await self.cmd(gm.privaterules_cmd, chat, "/privaterules on")
        self.assertTrue((await chatcache.get_chat_settings(chat)).private_rules)


class TestWelcomeAndRules(_Base):
    async def test_clean_welcome_deletes_the_previous_welcome(self):
        chat = await self.make_chat(welcome_text="Hi {first}", clean_welcome=True)
        bot = FakeBot(bot_id=ME)
        for uid in (11, 12):
            update = make_update("", chat_id=chat, user_id=uid)
            update.effective_message.new_chat_members = [person(uid, f"U{uid}")]
            await gm.welcome_listener(update, make_context(bot))
        sent_ids = [9000 + i for i, (n, _a, _k) in enumerate(bot.calls, start=1) if n == "send_message"]
        deletes = bot.called("delete_message")
        self.assertEqual(len(deletes), 1, "only the first welcome is removed, by the second")
        self.assertEqual(deletes[0][0], (chat, sent_ids[0]))
        self.assertIsNotNone((await chatcache.get_chat_settings(chat)).last_welcome_id)

    async def test_welcome_is_left_alone_without_the_setting(self):
        chat = await self.make_chat(welcome_text="Hi {first}")
        bot = FakeBot(bot_id=ME)
        for uid in (21, 22):
            update = make_update("", chat_id=chat, user_id=uid)
            update.effective_message.new_chat_members = [person(uid)]
            await gm.welcome_listener(update, make_context(bot))
        self.assertEqual(bot.called("delete_message"), [])

    async def test_private_rules_go_to_dm_with_a_fallback_for_users_who_never_started_the_bot(self):
        chat = await self.make_chat(rules="1. Be kind", private_rules=True)
        bot = FakeBot(bot_id=ME)
        update, _b = await self.cmd(gm.rules_cmd, chat, "/rules", bot=bot, user=7)
        self.assertEqual(bot.called("send_message")[0][0][0], 7)
        self.assertIn("private message", replies(update))
        blocked = FakeBot(bot_id=ME, fail={"send_message": Forbidden("bot can't initiate conversation with a user")})
        update2, _b = await self.cmd(gm.rules_cmd, chat, "/rules", bot=blocked, user=8)
        self.assertIn("start me", replies(update2).lower())

    async def test_rules_roundtrip_and_limit(self):
        chat = fresh_chat()
        ok, _b = await self.cmd(gm.setrules_cmd, chat, "/setrules Be nice")
        self.assertIn("updated", replies(ok))
        shown, _b = await self.cmd(gm.rules_cmd, chat, "/rules", user=7)
        self.assertIn("Be nice", shown.effective_message.reply_text.call_args.args[0])
        big, _b = await self.cmd(gm.setrules_cmd, chat, "/setrules " + "x" * (gm.MAX_RULES + 1))
        self.assertIn("limited", replies(big))


class TestNightMode(_Base):
    def setUp(self):
        super().setUp()

        async def reset():       # the job scans every chat row; leftovers from other tests must not take part
            from sqlalchemy import update as sa_update
            async with async_session() as s:
                await s.execute(sa_update(Chat).values(nightmode_enabled=False, nightmode_active=False))
                await s.commit()
        asyncio.run(reset())

    @staticmethod
    def perm_calls(bot, chat):
        return [c for c in bot.called("set_chat_permissions") if c[0][0] == chat]

    @staticmethod
    def window(offset_start_min, offset_end_min):
        now = datetime.now(timezone.utc)
        fmt = lambda d: d.strftime("%H:%M")  # noqa: E731
        return fmt(now + timedelta(minutes=offset_start_min)), fmt(now + timedelta(minutes=offset_end_min))

    def test_window_logic_including_wrap_past_midnight(self):
        from datetime import time as t
        self.assertTrue(gm.in_night_window(t(23, 30), "23:00", "06:00"))
        self.assertTrue(gm.in_night_window(t(2, 0), "23:00", "06:00"))
        self.assertFalse(gm.in_night_window(t(12, 0), "23:00", "06:00"))
        self.assertTrue(gm.in_night_window(t(10, 0), "09:00", "17:00"))
        self.assertFalse(gm.in_night_window(t(17, 0), "09:00", "17:00"), "the end is exclusive")
        self.assertFalse(gm.in_night_window(t(5, 0), "05:00", "05:00"), "an empty window never closes the chat")

    async def test_begin_once_then_restore_the_original_permissions_at_dawn(self):
        defaults = ChatPermissions(can_send_messages=True, can_send_polls=False, can_change_info=False,
                                   can_pin_messages=False, can_invite_users=False)
        start, end = self.window(-60, 60)
        chat = await self.make_chat(nightmode_enabled=True, nightmode_start=start, nightmode_end=end)
        bot = FakeBot(bot_id=ME, chat_permissions=defaults)
        await gm.nightmode_job(make_context(bot))
        await gm.nightmode_job(make_context(bot))
        (args, _kw), = self.perm_calls(bot, chat)            # applied once, not every minute
        self.assertEqual((args[0], args[1].can_send_messages), (chat, False))
        async with async_session() as s:
            row = await s.get(Chat, chat)
            self.assertTrue(row.nightmode_active)
            self.assertFalse(row.nightmode_prev_permissions["can_change_info"])
        async with async_session() as s:        # dawn: the window is over
            row = await s.get(Chat, chat)
            row.nightmode_start, row.nightmode_end = self.window(60, 120)
            await s.commit()
        await gm.nightmode_job(make_context(bot))
        restore = self.perm_calls(bot, chat)[-1][0][1]
        self.assertEqual(len(self.perm_calls(bot, chat)), 2)
        self.assertEqual(restore.to_dict(), defaults.to_dict())
        self.assertFalse(restore.can_change_info or restore.can_pin_messages or restore.can_invite_users,
                         "v2.10 handed every member can_change_info / can_pin_messages / can_invite_users each morning")
        async with async_session() as s:
            row = await s.get(Chat, chat)
            self.assertEqual((row.nightmode_active, row.nightmode_prev_permissions), (False, None))

    async def test_turning_night_mode_off_during_the_night_reopens_the_chat(self):
        """v2.10 left such a chat muted forever."""
        defaults = ChatPermissions(can_send_messages=True, can_send_polls=True)
        start, end = self.window(-60, 60)
        chat = await self.make_chat(nightmode_enabled=True, nightmode_start=start, nightmode_end=end)
        bot = FakeBot(bot_id=ME, chat_permissions=defaults, members={(chat, MOD): admin(MOD), (chat, ME): admin(ME)})
        await gm.nightmode_job(make_context(bot))
        update = make_update("/nightmode off", chat_id=chat, user_id=MOD)
        await gm.nightmode_cmd(update, make_context(bot, ["off"]))
        self.assertEqual(self.perm_calls(bot, chat)[-1][0][1].to_dict(), defaults.to_dict())
        await gm.nightmode_job(make_context(bot))
        self.assertEqual(len(self.perm_calls(bot, chat)), 2, "nothing more to do once it is off and restored")

    async def test_failures_are_retried_with_backoff_and_do_not_corrupt_state(self):
        start, end = self.window(-60, 60)
        chat = await self.make_chat(nightmode_enabled=True, nightmode_start=start, nightmode_end=end)
        bot = FakeBot(bot_id=ME, fail={"set_chat_permissions": BadRequest("Not enough rights")})
        for _ in range(8):
            await gm.nightmode_job(make_context(bot))
        self.assertEqual(len(self.perm_calls(bot, chat)), 5, "after 5 failures the chat is retried only every ~30 minutes")
        async with async_session() as s:
            self.assertFalse((await s.get(Chat, chat)).nightmode_active)

    async def test_command_validation_and_bot_right(self):
        chat = fresh_chat()
        bad, _b = await self.cmd(gm.nightmode_cmd, chat, "/nightmode 25:00 06:00")
        self.assertIn("Usage", replies(bad))
        same, _b = await self.cmd(gm.nightmode_cmd, chat, "/nightmode 05:00 05:00")
        self.assertIn("Usage", replies(same))
        no_right = self.world(chat, extra={(chat, ME): admin(ME, can_restrict_members=False)})
        refused, _b = await self.cmd(gm.nightmode_cmd, chat, "/nightmode on", bot=no_right)
        self.assertIn("Ban users", replies(refused))
        ok, _b = await self.cmd(gm.nightmode_cmd, chat, "/nightmode 22:30 07:00")
        self.assertIn("22:30", replies(ok))
        on, _b = await self.cmd(gm.nightmode_cmd, chat, "/nightmode on")
        self.assertIn("enabled", replies(on))


class TestDisableCommands(_Base):
    async def test_disable_enable_and_protected_commands(self):
        chat = fresh_chat()
        ok, _b = await self.cmd(gm.disable_cmd, chat, "/disable gm")
        self.assertIn("disabled", replies(ok))
        self.assertEqual((await chatcache.get_chat_settings(chat)).disabled_commands, ["gm"])
        for protected in ("enable", "start", "connect"):
            refused, _b = await self.cmd(gm.disable_cmd, chat, f"/disable {protected}")
            self.assertIn("can't be disabled", replies(refused), protected)
        listed, _b = await self.cmd(gm.disabled_cmd, chat, "/disabled", user=7)
        self.assertIn("/gm", replies(listed))
        await self.cmd(gm.enable_cmd, chat, "/enable gm")
        self.assertEqual((await chatcache.get_chat_settings(chat)).disabled_commands, [])


if __name__ == "__main__":
    unittest.main()


class TestDonorGapCommands(_Base):
    async def test_goodbye_toggle_reset_commands_and_locktypes(self):
        chat = fresh_chat()
        shown, _b = await self.cmd(gm.goodbye_cmd, chat, "/goodbye")
        self.assertIn("off", replies(shown))
        await self.cmd(gm.goodbye_cmd, chat, "/goodbye on")
        self.assertTrue((await chatcache.get_chat_settings(chat)).goodbye_enabled)
        await mt.update_chat(chat, welcome_text="hi", goodbye_text="bye", rules="be nice")
        await self.cmd(gm.resetwelcome_cmd, chat, "/resetwelcome")
        await self.cmd(gm.resetgoodbye_cmd, chat, "/resetgoodbye")
        await self.cmd(gm.clearrules_cmd, chat, "/clearrules")
        snap = await chatcache.get_chat_settings(chat)
        self.assertEqual((snap.welcome_text, snap.goodbye_text, snap.rules), (None, None, None))
        types, _b = await self.cmd(gm.locktypes_cmd, chat, "/locktypes", user=7)
        self.assertIn("sticker", replies(types))


class TestDtbanDtmute(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    async def test_timed_and_deleting(self):
        import time
        import bot.plugins.admin as adm
        from types import SimpleNamespace as NS
        chat = fresh_chat()
        bot = FakeBot(bot_id=ME, members={(chat, MOD): admin(MOD), (chat, ME): admin(ME), (chat, 42): member(uid=42)})
        reply = NS(from_user=NS(id=42, first_name="V", username=None), message_id=61, forum_topic_created=None, text="x", caption=None)
        update = make_update("/dtmute 30m", chat_id=chat, user_id=MOD, reply=reply)
        await adm.dtmute_cmd(update, make_context(bot, ["30m"], job_queue=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()))
        (args, kwargs), = bot.called("restrict_chat_member")
        self.assertAlmostEqual(kwargs["until_date"], time.time() + 1800, delta=5)
        self.assertEqual(bot.called("delete_message")[0][0], (chat, 61))
        no_time = make_update("/dtban", chat_id=chat, user_id=MOD, reply=reply)
        await adm.dtban_cmd(no_time, make_context(bot, []))
        self.assertIn("<time>", no_time.effective_message.reply_text.call_args.args[0])
        self.assertEqual(bot.called("ban_chat_member"), [])
