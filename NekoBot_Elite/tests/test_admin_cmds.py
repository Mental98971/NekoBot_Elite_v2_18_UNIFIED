"""Behavioural tests for the v2.12 moderation commands (bot/plugins/admin.py) and the admin_only fix.

Telegram is replaced by tests/_fakes.py; the database is a real disposable SQLite file. Each test
uses its own chat id because every test module in one pytest process shares the first engine.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_admin_cmds_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from sqlalchemy import select  # noqa: E402
from telegram import ChatPermissions  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from _fakes import FakeBot, admin, make_context, make_update, member, owner  # noqa: E402
import bot.plugins.admin as adm  # noqa: E402
from bot.core.database import Ban, Chat, async_session, init_db  # noqa: E402
from bot.core.decorators import admin_only  # noqa: E402
from bot.utils import chatcache, modtools as mt  # noqa: E402

ME, MOD = 999, 5          # the bot, the moderator who types the commands
_next_chat = [-1003000000000]


def fresh_chat() -> int:
    _next_chat[0] -= 1
    return _next_chat[0]


def reply_to(uid, first="Victim", message_id=50):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid, first_name=first, username=None), message_id=message_id,
                           forum_topic_created=None, text="spam spam", caption=None)


def replies(update) -> str:
    text = "\n".join(str(c.args[0]) for c in update.effective_message.reply_text.call_args_list)
    assert "went sideways" not in text, f"handler crashed: {text}"
    return text


def world(chat, *, mod=None, extra=None, **bot_kw):
    members = {(chat, MOD): mod or admin(MOD), (chat, ME): admin(ME)}
    members.update(extra or {})
    return FakeBot(bot_id=ME, members=members, **bot_kw)


class _DB(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    def setUp(self):
        mt._admin_cache.clear()
        chatcache.clear()


class TestBanFamily(_DB):
    async def test_ban_by_reply_is_permanent_and_recorded(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        update = make_update("/ban spamming links", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.ban_cmd(update, make_context(bot, ["spamming", "links"]))
        (args, kwargs), = bot.called("ban_chat_member")
        self.assertEqual((args, kwargs.get("until_date")), ((chat, 42), None))
        text = replies(update)
        self.assertIn("Banned", text)
        self.assertIn("permanently", text)
        self.assertIn("spamming links", text)
        async with async_session() as s:
            row = (await s.execute(select(Ban).where(Ban.chat_id == chat))).scalar_one()
        self.assertEqual((row.user_id, row.reason, row.banned_by), (42, "spamming links", MOD))

    async def test_ban_by_id_with_time_and_reason(self):
        chat = fresh_chat()
        bot = world(chat)
        update = make_update("/ban 12345 30m flooding", chat_id=chat, user_id=MOD)
        await adm.ban_cmd(update, make_context(bot, ["12345", "30m", "flooding"], job_queue=MagicMock()))
        (args, kwargs), = bot.called("ban_chat_member")
        self.assertEqual(args, (chat, 12345))
        self.assertAlmostEqual(kwargs["until_date"], time.time() + 1800, delta=5)
        self.assertIn("for 30m", replies(update))
        self.assertIn("flooding", replies(update))

    async def test_tban_without_a_time_does_not_silently_become_permanent(self):
        chat = fresh_chat()
        bot = world(chat)
        update = make_update("/tban 12345 spam", chat_id=chat, user_id=MOD)
        await adm.tban_cmd(update, make_context(bot, ["12345", "spam"]))
        self.assertEqual(bot.called("ban_chat_member"), [])
        self.assertIn("<time>", replies(update))

    async def test_a_ten_second_mute_is_refused(self):
        chat = fresh_chat()
        bot = world(chat)
        update = make_update("/mute 12345 10s", chat_id=chat, user_id=MOD)
        await adm.mute_cmd(update, make_context(bot, ["12345", "10s"]))
        self.assertEqual(bot.called("restrict_chat_member"), [])
        self.assertIn("30s", replies(update))

    async def test_admins_cannot_be_banned_and_nothing_is_called(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): admin(42)})
        update = make_update("/ban", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.ban_cmd(update, make_context(bot))
        self.assertEqual(bot.called("ban_chat_member"), [])
        self.assertIn("admin", replies(update))

    async def test_the_owner_and_the_bot_and_yourself_are_protected(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 77): owner(77)})
        for uid, needle in ((77, "owner"), (ME, "myself"), (MOD, "yourself")):
            update = make_update("/ban", chat_id=chat, user_id=MOD, reply=reply_to(uid))
            await adm.ban_cmd(update, make_context(bot))
            self.assertIn(needle, replies(update), uid)
        self.assertEqual(bot.called("ban_chat_member"), [])

    async def test_an_admin_without_the_ban_right_is_refused(self):
        """v2.10 only asked 'is the caller an admin?' - a pin-only admin could ban anyone."""
        chat = fresh_chat()
        bot = world(chat, mod=admin(MOD, can_restrict_members=False), extra={(chat, 42): member(uid=42)})
        update = make_update("/ban", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.ban_cmd(update, make_context(bot))
        self.assertEqual(bot.called("ban_chat_member"), [])
        self.assertIn("Ban users", replies(update))

    async def test_a_plain_member_is_refused(self):
        chat = fresh_chat()
        bot = world(chat, mod=member(uid=MOD), extra={(chat, 42): member(uid=42)})
        update = make_update("/ban", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.ban_cmd(update, make_context(bot))
        self.assertEqual(bot.called("ban_chat_member"), [])
        self.assertIn("Admins only", replies(update))

    async def test_the_bot_without_the_right_says_so_instead_of_echoing_telegram(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42), (chat, ME): admin(ME, can_restrict_members=False)})
        update = make_update("/ban", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.ban_cmd(update, make_context(bot))
        self.assertIn("I need the", replies(update))
        self.assertEqual(bot.called("ban_chat_member"), [])

    async def test_telegram_failure_is_reported_in_plain_words(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42)}, fail={"ban_chat_member": BadRequest("Not enough rights to restrict/unrestrict chat member")})
        update = make_update("/ban", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.ban_cmd(update, make_context(bot))
        text = replies(update)
        self.assertIn("❌", text)
        self.assertNotIn("restrict/unrestrict", text)

    async def test_anonymous_admin_can_ban(self):
        """v2.10's admin_only asked Telegram about @GroupAnonymousBot, which is not a member."""
        chat = fresh_chat()
        bot = FakeBot(bot_id=ME, members={(chat, ME): admin(ME), (chat, 42): member(uid=42)})
        update = make_update("/ban", chat_id=chat, user_id=mt.ANON_ADMIN_BOT_ID, sender_chat=SimpleNamespace(id=chat), reply=reply_to(42))
        await adm.ban_cmd(update, make_context(bot))
        self.assertEqual(len(bot.called("ban_chat_member")), 1)

    async def test_silent_ban_removes_the_command_and_says_nothing(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        update = make_update("/sban", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.sban_cmd(update, make_context(bot))
        self.assertEqual(len(bot.called("ban_chat_member")), 1)
        update.effective_message.delete.assert_awaited_once()
        update.effective_message.reply_text.assert_not_awaited()

    async def test_dban_also_deletes_the_replied_message(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        update = make_update("/dban", chat_id=chat, user_id=MOD, reply=reply_to(42, message_id=77))
        await adm.dban_cmd(update, make_context(bot))
        self.assertEqual(len(bot.called("ban_chat_member")), 1)
        self.assertEqual(bot.called("delete_message")[0][0], (chat, 77))

    async def test_dban_without_delete_rights_refuses_before_banning(self):
        chat = fresh_chat()
        bot = world(chat, mod=admin(MOD, can_delete_messages=False), extra={(chat, 42): member(uid=42)})
        update = make_update("/dban", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.dban_cmd(update, make_context(bot))
        self.assertEqual(bot.called("ban_chat_member"), [])
        self.assertIn("Delete messages", replies(update))

    async def test_reason_html_is_escaped(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        update = make_update("/kick", chat_id=chat, user_id=MOD, reply=reply_to(42, first="<b>Evil</b>"))
        await adm.kick_cmd(update, make_context(bot, ["<script>x</script>"]))
        text = replies(update)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<b>Evil</b>", text)

    async def test_kick_removes_and_lets_them_return(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        update = make_update("/kick", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.kick_cmd(update, make_context(bot))
        self.assertEqual([n for n, _a, _k in bot.calls if n in ("ban_chat_member", "unban_chat_member")], ["ban_chat_member", "unban_chat_member"])


class TestUnbanUnmute(_DB):
    async def test_unban_by_id_works_and_clears_the_record(self):
        """v2.10: `/unban <id>` crashed (it awaited an attribute of an un-awaited coroutine)."""
        chat = fresh_chat()
        bot = world(chat)
        await adm.ban_cmd(make_update("/ban 555", chat_id=chat, user_id=MOD), make_context(bot, ["555"]))
        update = make_update("/unban 555", chat_id=chat, user_id=MOD)
        await adm.unban_cmd(update, make_context(bot, ["555"]))
        self.assertIn("unbanned", replies(update))
        self.assertTrue(bot.called("unban_chat_member")[-1][1]["only_if_banned"])
        async with async_session() as s:
            self.assertEqual((await s.execute(select(Ban).where(Ban.chat_id == chat))).scalars().all(), [])

    async def test_unmute_restores_default_permissions_only(self):
        chat = fresh_chat()
        defaults = ChatPermissions(can_send_messages=True, can_change_info=False, can_pin_messages=False)
        bot = world(chat, extra={(chat, 42): member(uid=42)}, chat_permissions=defaults)
        update = make_update("/unmute", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.unmute_cmd(update, make_context(bot))
        self.assertIs(bot.called("restrict_chat_member")[0][0][2], defaults)
        self.assertIn("can speak", replies(update))

    async def test_unban_needs_a_target(self):
        chat = fresh_chat()
        update = make_update("/unban", chat_id=chat, user_id=MOD)
        await adm.unban_cmd(update, make_context(world(chat)))
        self.assertIn("Usage", replies(update))


class TestWarns(_DB):
    async def _chat_with(self, **cols) -> int:
        chat = fresh_chat()
        async with async_session() as s:
            s.add(Chat(id=chat, title="W", type="supergroup", **cols))
            await s.commit()
        return chat

    async def test_warn_counts_up_and_applies_the_action_at_the_limit(self):
        chat = await self._chat_with(warn_limit=2, warn_action="mute")
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        first = make_update("/warn", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.warn_cmd(first, make_context(bot, ["rude"]))
        self.assertIn("1/2", replies(first))
        second = make_update("/warn", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.warn_cmd(second, make_context(bot, ["rude", "again"]))
        self.assertIn("Limit reached", replies(second))
        self.assertEqual(len(bot.called("restrict_chat_member")), 1)
        self.assertEqual(await mt.count_warns(chat, 42), 0, "warns are cleared once the action was applied")

    async def test_warning_an_admin_is_refused(self):
        chat = await self._chat_with()
        bot = world(chat, extra={(chat, 42): admin(42)})
        update = make_update("/warn", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.warn_cmd(update, make_context(bot))
        self.assertIn("admin", replies(update))
        self.assertEqual(await mt.count_warns(chat, 42), 0)

    async def test_rmwarn_resetwarns_and_warns(self):
        chat = await self._chat_with(warn_limit=5)
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        for reason in ("one", "two", "three"):
            await adm.warn_cmd(make_update("/warn", chat_id=chat, user_id=MOD, reply=reply_to(42)), make_context(bot, [reason]))
        shown = make_update("/warns", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.warns_cmd(shown, make_context(bot))
        text = replies(shown)
        self.assertIn("3/5", text)
        self.assertLess(text.index("three"), text.index("one"), "newest first")
        removed = make_update("/rmwarn", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.rmwarn_cmd(removed, make_context(bot))
        self.assertIn("2/5", replies(removed))
        reset = make_update("/resetwarns", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.resetwarns_cmd(reset, make_context(bot))
        self.assertIn("Cleared 2", replies(reset))
        nothing = make_update("/rmwarn", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await adm.rmwarn_cmd(nothing, make_context(bot))
        self.assertIn("no warnings", replies(nothing))

    async def test_members_can_check_their_own_warns(self):
        chat = await self._chat_with(warn_limit=3)
        await mt.add_warn(make_context(FakeBot()), chat, 42, by=MOD, reason="x")
        bot = world(chat, extra={(chat, 42): member(uid=42)})
        update = make_update("/warns", chat_id=chat, user_id=42)
        await adm.warns_cmd(update, make_context(bot))
        self.assertIn("1/3", replies(update))


class TestCleanup(_DB):
    async def test_purge_by_count_is_bulk_capped_and_does_not_block_the_chat(self):
        chat = fresh_chat()
        bot = world(chat)
        queue = MagicMock()
        update = make_update("/purge 5000", chat_id=chat, user_id=MOD, message_id=3000)
        started = time.monotonic()
        await adm.purge_cmd(update, make_context(bot, ["5000"], job_queue=queue))
        self.assertLess(time.monotonic() - started, 2, "v2.10 slept 5 s inside the handler, freezing the chat's update queue")
        batches = [len(a[1]) for a, _k in bot.called("delete_messages")]
        cap = adm.settings.purge_max_messages
        self.assertEqual(sum(batches), cap + 1, "capped at PURGE_MAX_MESSAGES (+ the command itself)")
        self.assertTrue(all(b <= 100 for b in batches))
        queue.run_once.assert_called_once()            # the confirmation is removed later, by a job
        self.assertIn("limited", bot.called("send_message")[0][0][1])

    async def test_purge_by_reply_covers_reply_to_command(self):
        chat = fresh_chat()
        bot = world(chat)
        update = make_update("/purge", chat_id=chat, user_id=MOD, message_id=120, reply=reply_to(42, message_id=100))
        await adm.purge_cmd(update, make_context(bot, [], job_queue=MagicMock()))
        ids = sorted(i for a, _k in bot.called("delete_messages") for i in a[1])
        self.assertEqual((ids[0], ids[-1], len(ids)), (100, 120, 21))

    async def test_purge_needs_the_delete_right_and_a_target(self):
        chat = fresh_chat()
        bot = world(chat, mod=admin(MOD, can_delete_messages=False))
        update = make_update("/purge 5", chat_id=chat, user_id=MOD, message_id=300)
        await adm.purge_cmd(update, make_context(bot, ["5"]))
        self.assertEqual(bot.called("delete_messages"), [])
        usage = make_update("/purge", chat_id=chat, user_id=MOD, message_id=300)
        await adm.purge_cmd(usage, make_context(world(chat), []))
        self.assertIn("Reply to the oldest", replies(usage))

    async def test_del_removes_reply_and_command(self):
        chat = fresh_chat()
        bot = world(chat)
        update = make_update("/del", chat_id=chat, user_id=MOD, reply=reply_to(42, message_id=88))
        await adm.del_cmd(update, make_context(bot))
        self.assertEqual(bot.called("delete_message")[0][0], (chat, 88))
        update.effective_message.delete.assert_awaited_once()

    async def test_kickme(self):
        chat = fresh_chat()
        bot = world(chat, extra={(chat, 42): member(uid=42), (chat, 43): admin(43)})
        ok = make_update("/kickme", chat_id=chat, user_id=42)
        await adm.kickme_cmd(ok, make_context(bot))
        self.assertEqual(len(bot.called("ban_chat_member")), 1)
        refused = make_update("/kickme", chat_id=chat, user_id=43)
        await adm.kickme_cmd(refused, make_context(bot))
        self.assertIn("admin", replies(refused))
        self.assertEqual(len(bot.called("ban_chat_member")), 1)


class TestConnectedDm(_DB):
    async def test_ban_from_a_private_chat_acts_on_the_connected_group(self):
        chat = fresh_chat()
        await mt.set_connection(MOD, chat)
        bot = world(chat)
        update = make_update("/ban 12345 raid", chat_id=MOD, chat_type="private", user_id=MOD)
        await adm.ban_cmd(update, make_context(bot, ["12345", "raid"]))
        (args, _kw), = bot.called("ban_chat_member")
        self.assertEqual(args, (chat, 12345))
        self.assertIn("Banned", replies(update))

    async def test_remote_commands_still_check_the_senders_right_in_that_chat(self):
        chat = fresh_chat()
        await mt.set_connection(MOD, chat)
        bot = world(chat, mod=member(uid=MOD))      # connected, but just a member there
        update = make_update("/ban 12345", chat_id=MOD, chat_type="private", user_id=MOD)
        await adm.ban_cmd(update, make_context(bot, ["12345"]))
        self.assertEqual(bot.called("ban_chat_member"), [])
        self.assertIn("Admins only", replies(update))

    async def test_leaving_the_chat_ends_the_connection(self):
        chat = fresh_chat()
        await mt.set_connection(MOD, chat)
        bot = FakeBot(bot_id=ME, members={(chat, ME): admin(ME)})      # the sender is no longer a member
        update = make_update("/ban 12345", chat_id=MOD, chat_type="private", user_id=MOD)
        await adm.ban_cmd(update, make_context(bot, ["12345"]))
        self.assertEqual(bot.called("ban_chat_member"), [])
        self.assertIsNone(await mt.get_connection(MOD))

    async def test_unconnected_dm_gets_guidance(self):
        update = make_update("/ban 1", chat_id=61, chat_type="private", user_id=61)
        await adm.ban_cmd(update, make_context(FakeBot(), ["1"]))
        self.assertIn("/connect", replies(update))


class TestAdminOnlyDecorator(unittest.IsolatedAsyncioTestCase):
    async def test_anonymous_admins_pass_and_lookup_failures_are_handled(self):
        calls = []

        @admin_only
        async def handler(update, context):
            calls.append(1)

        anon = make_update(user_id=mt.ANON_ADMIN_BOT_ID, chat_id=-1004, sender_chat=SimpleNamespace(id=-1004))
        anon.message = anon.effective_message
        await handler(anon, make_context(FakeBot()))
        self.assertEqual(calls, [1], "an anonymous admin must reach the handler")

        lookup_fails = make_update(user_id=5, chat_id=-1004)
        lookup_fails.message = lookup_fails.effective_message
        await handler(lookup_fails, make_context(FakeBot()))      # FakeBot raises BadRequest for unknown members
        self.assertEqual(calls, [1])
        self.assertIn("couldn't verify", lookup_fails.message.reply_text.call_args.args[0])

        plain = make_update(user_id=6, chat_id=-1004)
        plain.message = plain.effective_message
        await handler(plain, make_context(FakeBot(members={(-1004, 6): member(uid=6)})))
        self.assertIn("Admin only", plain.message.reply_text.call_args.args[0])
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
