"""Per-chat moderation settings, reports, log channel, /connect and the admin-utility commands (v2.12)."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_modsettings_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from sqlalchemy import select  # noqa: E402
from telegram.error import BadRequest, Forbidden  # noqa: E402

from _fakes import FakeBot, admin, make_context, make_update, member, owner  # noqa: E402
import bot.plugins.adminutils as au  # noqa: E402
import bot.plugins.connection as conn  # noqa: E402
import bot.plugins.modsettings as ms  # noqa: E402
from bot.core.database import Report, async_session, init_db  # noqa: E402
from bot.middleware.antiflood import flood_config  # noqa: E402
from bot.utils import chatcache, modtools as mt  # noqa: E402

ME, MOD = 999, 5
_next = [-1006000000000]


def fresh_chat() -> int:
    _next[0] -= 1
    return _next[0]


def replies(update) -> str:
    text = "\n".join(str(c.args[0]) for c in update.effective_message.reply_text.call_args_list)
    assert "went sideways" not in text, text
    return text


def reply_to(uid, first="Pal", message_id=50):
    r = SimpleNamespace(from_user=SimpleNamespace(id=uid, first_name=first, username=None), message_id=message_id,
                        forum_topic_created=None, text="rude words <b>", caption=None)
    r.reply_text = AsyncMock()
    return r


class _Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    def setUp(self):
        mt._admin_cache.clear()
        chatcache.clear()
        ms._report_cooldown.clear()

    def world(self, chat, *, mod=None, extra=None, **kw):
        members = {(chat, MOD): mod or admin(MOD), (chat, ME): admin(ME)}
        members.update(extra or {})
        return FakeBot(bot_id=ME, members=members, **kw)

    async def run_cmd(self, handler, chat, text, *, bot=None, mod=None, user=MOD, **kw):
        bot = bot or self.world(chat, mod=mod)
        update = make_update(text, chat_id=chat, user_id=user, **kw)
        await handler(update, make_context(bot, text.split()[1:]))
        return update, bot


class TestFloodSettings(_Base):
    async def test_setflood_on_off_and_validation(self):
        chat = fresh_chat()
        update, _b = await self.run_cmd(ms.setflood_cmd, chat, "/setflood 6")
        self.assertIn("6", replies(update))
        snap = await chatcache.get_chat_settings(chat)
        self.assertEqual((flood_config(snap)[0], flood_config(snap)[1]), (True, 6))
        await self.run_cmd(ms.setflood_cmd, chat, "/setflood off")
        self.assertFalse(flood_config(await chatcache.get_chat_settings(chat))[0])
        for bad in ("/setflood 2", "/setflood 1000", "/setflood abc", "/setflood"):
            update, _b = await self.run_cmd(ms.setflood_cmd, chat, bad)
            self.assertIn("Usage", replies(update), bad)

    async def test_setflood_warns_when_the_bot_cannot_enforce(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, ME): admin(ME, can_delete_messages=False)})
        update, _b = await self.run_cmd(ms.setflood_cmd, chat, "/setflood 6", bot=bot)
        self.assertIn("Delete messages", replies(update))

    async def test_setfloodmode(self):
        chat = fresh_chat()
        update, _b = await self.run_cmd(ms.setfloodmode_cmd, chat, "/setfloodmode tmute 2h")
        self.assertIn("2h", replies(update))
        enabled, limit, action, seconds = flood_config(await chatcache.get_chat_settings(chat))
        self.assertEqual((action, seconds), ("tmute", 7200))
        short, _b = await self.run_cmd(ms.setfloodmode_cmd, chat, "/setfloodmode tban 10s")
        self.assertIn("30s", replies(short))
        bad, _b = await self.run_cmd(ms.setfloodmode_cmd, chat, "/setfloodmode explode")
        self.assertIn("Usage", replies(bad))
        await self.run_cmd(ms.setfloodmode_cmd, chat, "/setfloodmode kick")
        self.assertEqual(flood_config(await chatcache.get_chat_settings(chat))[2], "kick")

    async def test_settings_commands_need_the_change_info_right(self):
        chat = fresh_chat()
        update, _b = await self.run_cmd(ms.setflood_cmd, chat, "/setflood 6", mod=admin(MOD, can_change_info=False))
        self.assertIn("Change group info", replies(update))
        self.assertIsNone(await chatcache.get_chat_settings(chat))

    async def test_slowmode(self):
        chat = fresh_chat()
        no_delete = self.world(chat, extra={(chat, ME): admin(ME, can_delete_messages=False)})
        refused, _b = await self.run_cmd(ms.slowmode_cmd, chat, "/slowmode 30", bot=no_delete)
        self.assertIn("deleting", replies(refused))
        self.assertIsNone(await chatcache.get_chat_settings(chat))
        ok, _b = await self.run_cmd(ms.slowmode_cmd, chat, "/slowmode 30")
        self.assertIn("30s", replies(ok))
        self.assertEqual((await chatcache.get_chat_settings(chat)).slowmode_seconds, 30)
        shown, _b = await self.run_cmd(ms.slowmode_cmd, chat, "/slowmode")
        self.assertIn("30s", replies(shown))
        bad, _b = await self.run_cmd(ms.slowmode_cmd, chat, "/slowmode 99999")
        self.assertIn("Usage", replies(bad))
        await self.run_cmd(ms.slowmode_cmd, chat, "/slowmode off")
        self.assertIsNone((await chatcache.get_chat_settings(chat)).slowmode_seconds)

    async def test_v210_slowmode_called_a_bot_api_method_that_does_not_exist(self):
        from telegram import Bot
        self.assertFalse(hasattr(Bot, "set_chat_slow_mode_delay"), "premise: Telegram's Bot API has no slow-mode method")

    async def test_antispam_switch_and_warn_policy(self):
        chat = fresh_chat()
        await self.run_cmd(ms.antispam_cmd, chat, "/antispam off")
        self.assertIs((await chatcache.get_chat_settings(chat)).antispam_enabled, False)
        await self.run_cmd(ms.antispam_cmd, chat, "/antispam on")
        self.assertIs((await chatcache.get_chat_settings(chat)).antispam_enabled, True)
        for bad in ("/setwarnlimit 0", "/setwarnlimit 101", "/setwarnlimit x"):
            update, _b = await self.run_cmd(ms.setwarnlimit_cmd, chat, bad)
            self.assertIn("Usage", replies(update), bad)
        await self.run_cmd(ms.setwarnlimit_cmd, chat, "/setwarnlimit 5")
        await self.run_cmd(ms.setwarnaction_cmd, chat, "/setwarnaction kick")
        self.assertEqual(await mt.warn_settings(chat), (5, "kick"))
        bad, _b = await self.run_cmd(ms.setwarnaction_cmd, chat, "/setwarnaction explode")
        self.assertIn("Usage", replies(bad))

    async def test_existing_warn_settings_stay_available_to_any_admin(self):
        chat = fresh_chat()
        await self.run_cmd(ms.setwarnlimit_cmd, chat, "/setwarnlimit 4", mod=admin(MOD, can_change_info=False))
        self.assertEqual((await mt.warn_settings(chat))[0], 4)

    async def test_summary_lists_everything(self):
        chat = fresh_chat()
        await self.run_cmd(ms.setflood_cmd, chat, "/setflood 8")
        update, _b = await self.run_cmd(ms.modsettings_cmd, chat, "/modsettings")
        text = replies(update)
        for needle in ("Anti-flood", "Slow mode", "Anti-spam", "Warns", "Word blacklist", "Approved users", "Reports", "Log channel"):
            self.assertIn(needle, text)


class TestLogChannelAndReports(_Base):
    async def test_setlog_verifies_it_can_post_and_stores_the_channel(self):
        chat = fresh_chat()
        update, bot = await self.run_cmd(ms.setlog_cmd, chat, "/setlog -100777")
        self.assertIn("-100777", replies(update))
        self.assertEqual((await chatcache.get_chat_settings(chat)).log_channel, -100777)
        self.assertEqual(bot.called("send_message")[0][0][0], -100777)

    async def test_setlog_rejects_the_same_chat_and_an_unpostable_channel(self):
        chat = fresh_chat()
        same, _b = await self.run_cmd(ms.setlog_cmd, chat, f"/setlog {chat}")
        self.assertIn("different chat", replies(same))
        bot = self.world(chat, fail={"send_message": Forbidden("bot was kicked from the channel")})
        denied, _b = await self.run_cmd(ms.setlog_cmd, chat, "/setlog -100778", bot=bot)
        self.assertIn("can't post", replies(denied))
        self.assertIsNone(await chatcache.get_chat_settings(chat))

    async def test_unsetlog_and_logchannel(self):
        chat = fresh_chat()
        await self.run_cmd(ms.setlog_cmd, chat, "/setlog -100779")
        shown, _b = await self.run_cmd(ms.logchannel_cmd, chat, "/logchannel")
        self.assertIn("-100779", replies(shown))
        await self.run_cmd(ms.unsetlog_cmd, chat, "/unsetlog")
        self.assertIsNone((await chatcache.get_chat_settings(chat)).log_channel)

    async def test_log_event_posts_escaped_and_never_raises(self):
        chat = fresh_chat()
        await mt.update_chat(chat, log_channel=-100780, title="T<i>")
        bot = FakeBot(bot_id=ME)
        await mt.log_event(make_context(bot), chat, "Banned", actor=(1, "Ad<min>"), target=(2, "Bob"), reason="<b>x</b>")
        (args, kwargs), = bot.called("send_message")
        self.assertEqual(args[0], -100780)
        self.assertNotIn("<min>", args[1])
        self.assertNotIn("<b>x</b>", args[1])
        broken = FakeBot(bot_id=ME, fail={"send_message": BadRequest("chat not found")})
        await mt.log_event(make_context(broken), chat, "Banned")      # swallowed

    async def test_report_notifies_admins_stores_it_and_logs(self):
        chat = fresh_chat()
        await mt.update_chat(chat, log_channel=-100781)
        bot = self.world(chat, extra={(chat, 42): member(uid=42), (chat, 88): admin(88)})
        reply = reply_to(42)
        update = make_update("/report spam <b>", chat_id=chat, user_id=7, reply=reply)
        await ms.report_cmd(update, make_context(bot, ["spam", "<b>"]))
        text = reply.reply_text.call_args.args[0]
        self.assertIn('tg://user?id=88', text)
        self.assertIn("&lt;b&gt;", text)
        self.assertNotIn("reason: <b>", text.lower())
        async with async_session() as s:
            row = (await s.execute(select(Report).where(Report.chat_id == chat))).scalar_one()
        self.assertEqual((row.reporter_id, row.reported_user_id, row.reason), (7, 42, "spam <b>"))
        logged = [a for a, _k in bot.called("send_message") if a[0] == -100781]
        self.assertTrue(logged and "#REPORT" in logged[0][1])

    async def test_report_edge_cases(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 42): member(uid=42), (chat, 88): admin(88)})

        async def report(reporter, target, **kw):
            update = make_update("/report", chat_id=chat, user_id=reporter, reply=reply_to(target) if target else None)
            await ms.report_cmd(update, make_context(bot, []))
            return replies(update)

        self.assertIn("Reply to", await report(7, None))
        self.assertIn("yourself", await report(42, 42))
        self.assertIn("admin", await report(7, 88))
        self.assertIn("Nice try", await report(7, ME))
        await mt.update_chat(chat, reports_enabled=False)
        self.assertIn("turned off", await report(8, 42))
        await mt.update_chat(chat, reports_enabled=True)
        await report(9, 42)
        self.assertIn("Please wait", await report(9, 42))

    async def test_reports_toggle(self):
        chat = fresh_chat()
        await self.run_cmd(ms.reports_cmd, chat, "/reports off")
        self.assertIs((await chatcache.get_chat_settings(chat)).reports_enabled, False)
        shown, _b = await self.run_cmd(ms.reports_cmd, chat, "/reports")
        self.assertIn("off", replies(shown))


class TestConnection(_Base):
    async def test_connect_inside_a_group(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 42): member(uid=42)})
        update = make_update("/connect", chat_id=chat, user_id=42)
        await conn.connect_cmd(update, make_context(bot, []))
        self.assertEqual(await mt.get_connection(42), chat)
        self.assertEqual(bot.called("send_message")[0][0][0], 42)
        self.assertIn("Connected", replies(update))

    async def test_connect_when_the_user_never_started_the_bot(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 43): member(uid=43)}, fail={"send_message": Forbidden("bot can't initiate conversation")})
        update = make_update("/connect", chat_id=chat, user_id=43)
        await conn.connect_cmd(update, make_context(bot, []))
        self.assertEqual(await mt.get_connection(43), chat)
        self.assertIn("press Start", replies(update))

    async def test_strangers_and_anonymous_admins_cannot_connect(self):
        chat = fresh_chat()
        bot = self.world(chat)
        stranger = make_update("/connect", chat_id=chat, user_id=77)
        await conn.connect_cmd(stranger, make_context(bot, []))
        self.assertIsNone(await mt.get_connection(77))
        self.assertIn("member", replies(stranger))
        anon = make_update("/connect", chat_id=chat, user_id=mt.ANON_ADMIN_BOT_ID, sender_chat=SimpleNamespace(id=chat))
        await conn.connect_cmd(anon, make_context(bot, []))
        self.assertIn("Anonymous", replies(anon))

    async def test_allowconnect_off_keeps_members_out_but_not_admins(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 42): member(uid=42)})
        await self.run_cmd(conn.allowconnect_cmd, chat, "/allowconnect off", bot=bot)
        member_try = make_update("/connect", chat_id=chat, user_id=42)
        await conn.connect_cmd(member_try, make_context(bot, []))
        self.assertIn("admins only", replies(member_try).lower())
        self.assertIsNone(await mt.get_connection(42))
        admin_try = make_update("/connect", chat_id=chat, user_id=MOD)
        await conn.connect_cmd(admin_try, make_context(bot, []))
        self.assertEqual(await mt.get_connection(MOD), chat)

    async def test_connect_from_dm_by_id_and_failure_modes(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 42): member(uid=42)}, chat_info={"title": "Club"})
        ok = make_update(f"/connect {chat}", chat_id=42, chat_type="private", user_id=42)
        await conn.connect_cmd(ok, make_context(bot, [str(chat)]))
        self.assertIn("Club", replies(ok))
        self.assertEqual(await mt.get_connection(42), chat)

        channel = FakeBot(bot_id=ME, chat_info={"type": "channel"})
        not_group = make_update("/connect -1009", chat_id=44, chat_type="private", user_id=44)
        await conn.connect_cmd(not_group, make_context(channel, ["-1009"]))
        self.assertIn("isn't a group", replies(not_group))

        bot_absent = FakeBot(bot_id=ME, members={(chat, 45): member(uid=45)})      # the bot is not a member
        absent = make_update(f"/connect {chat}", chat_id=45, chat_type="private", user_id=45)
        await conn.connect_cmd(absent, make_context(bot_absent, [str(chat)]))
        self.assertIn("not a member", replies(absent))
        self.assertIsNone(await mt.get_connection(45))

        unknown = FakeBot(bot_id=ME, fail={"get_chat": BadRequest("Chat not found")})
        missing = make_update("/connect nowhere", chat_id=46, chat_type="private", user_id=46)
        await conn.connect_cmd(missing, make_context(unknown, ["nowhere"]))
        self.assertIn("can't find", replies(missing))

    async def test_disconnect_and_connection(self):
        chat = fresh_chat()
        await mt.set_connection(60, chat)
        bot = self.world(chat, extra={(chat, 60): member(uid=60)})
        shown = make_update("/connection", chat_id=60, chat_type="private", user_id=60)
        await conn.connection_cmd(shown, make_context(bot, []))
        self.assertIn(str(chat), replies(shown))
        gone = make_update("/disconnect", chat_id=60, chat_type="private", user_id=60)
        await conn.disconnect_cmd(gone, make_context(bot, []))
        self.assertIn("Disconnected", replies(gone))
        self.assertIsNone(await mt.get_connection(60))
        again = make_update("/disconnect", chat_id=60, chat_type="private", user_id=60)
        await conn.disconnect_cmd(again, make_context(bot, []))
        self.assertIn("weren't connected", replies(again))


class TestAdminUtils(_Base):
    async def test_promote_only_grants_rights_both_the_caller_and_the_bot_hold(self):
        chat = fresh_chat()
        bot = self.world(chat, mod=admin(MOD, can_restrict_members=False), extra={(chat, 42): member(uid=42)})
        update = make_update("/promote Helper", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await au.promote_cmd(update, make_context(bot, ["Helper"]))
        (args, kwargs), = bot.called("promote_chat_member")
        self.assertNotIn("can_restrict_members", kwargs, "an admin cannot hand out a right they do not have")
        self.assertTrue(kwargs["can_delete_messages"] and kwargs["can_pin_messages"])
        self.assertNotIn("can_promote_members", kwargs)
        self.assertEqual(bot.called("set_chat_administrator_custom_title")[0][0][2], "Helper")

    async def test_fullpromote_and_owner_target(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 42): member(uid=42), (chat, 77): owner(77)})
        update = make_update("/fullpromote", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await au.fullpromote_cmd(update, make_context(bot, []))
        kwargs = bot.called("promote_chat_member")[0][1]
        self.assertTrue(kwargs["can_promote_members"] and kwargs["can_change_info"])
        owner_try = make_update("/promote", chat_id=chat, user_id=MOD, reply=reply_to(77))
        await au.promote_cmd(owner_try, make_context(bot, []))
        self.assertIn("owner", replies(owner_try))
        self.assertEqual(len(bot.called("promote_chat_member")), 1)

    async def test_promote_needs_the_promote_right_and_nothing_to_grant_is_explained(self):
        chat = fresh_chat()
        bot = self.world(chat, mod=admin(MOD, can_promote_members=False), extra={(chat, 42): member(uid=42)})
        update = make_update("/promote", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await au.promote_cmd(update, make_context(bot, []))
        self.assertIn("Add new admins", replies(update))
        self.assertEqual(bot.called("promote_chat_member"), [])
        bare = admin(MOD, can_delete_messages=False, can_restrict_members=False, can_pin_messages=False,
                     can_invite_users=False, can_change_info=False)
        bare.can_manage_video_chats = False
        bot2 = self.world(chat, mod=bare, extra={(chat, 42): member(uid=42)})
        none_update = make_update("/promote", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await au.promote_cmd(none_update, make_context(bot2, []))
        self.assertIn("nothing I can grant", replies(none_update))

    async def test_demote_and_title(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 42): admin(42), (chat, 43): member(uid=43)})
        demote = make_update("/demote", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await au.demote_cmd(demote, make_context(bot, []))
        kwargs = bot.called("promote_chat_member")[0][1]
        self.assertTrue(all(v is False for v in kwargs.values()))
        title = make_update("/title Boss", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await au.title_cmd(title, make_context(bot, ["Boss"]))
        self.assertIn("Boss", replies(title))
        not_admin = make_update("/title Boss", chat_id=chat, user_id=MOD, reply=reply_to(43))
        await au.title_cmd(not_admin, make_context(bot, ["Boss"]))
        self.assertIn("isn't an admin", replies(not_admin))
        too_long = make_update("/title x", chat_id=chat, user_id=MOD, reply=reply_to(42))
        await au.title_cmd(too_long, make_context(bot, ["x" * 20]))
        self.assertIn("16", replies(too_long))

    async def test_admins_list_is_escaped_and_hides_bots_and_anonymous(self):
        chat = fresh_chat()
        anon = admin(50, first="Hidden")
        anon.is_anonymous = True
        boss = owner(51, first="Boss<b>")
        boss.custom_title = "Founder"
        bot = self.world(chat, extra={(chat, 50): anon, (chat, 51): boss, (chat, 52): admin(52, first="Zed")})
        update = make_update("/admins", chat_id=chat, user_id=7)
        await au.admins_cmd(update, make_context(bot, []))
        text = replies(update)
        self.assertIn("Boss&lt;b&gt;", text)
        self.assertIn("Founder", text)
        self.assertNotIn("Hidden", text)
        self.assertIn("anonymous", text)
        self.assertLess(text.index("👑"), text.index("👮"))

    async def test_unpin_is_targeted_and_unpinall_asks_first(self):
        chat = fresh_chat()
        bot = self.world(chat)
        targeted = make_update("/unpin", chat_id=chat, user_id=MOD, reply=reply_to(42, message_id=33))
        await au.unpin_cmd(targeted, make_context(bot, []))
        (args, kwargs), = bot.called("unpin_chat_message")
        self.assertEqual((args, kwargs), ((chat,), {"message_id": 33}))
        latest = make_update("/unpin", chat_id=chat, user_id=MOD)
        await au.unpin_cmd(latest, make_context(bot, []))
        self.assertEqual(bot.called("unpin_chat_message")[1], ((chat,), {}))
        self.assertEqual(bot.called("unpin_all_chat_messages"), [], "v2.10's /unpin silently removed every pin")
        asked = make_update("/unpinall", chat_id=chat, user_id=MOD)
        await au.unpinall_cmd(asked, make_context(bot, []))
        self.assertIn("reply_markup", asked.effective_message.reply_text.call_args.kwargs)
        self.assertEqual(bot.called("unpin_all_chat_messages"), [])
        query = SimpleNamespace(data=f"unpinall:y:{chat}", from_user=SimpleNamespace(id=MOD, first_name="M"),
                                answer=AsyncMock(), edit_message_text=AsyncMock())
        fake = SimpleNamespace(callback_query=query, effective_user=query.from_user, effective_chat=None, effective_message=None)
        await au.unpinall_callback(fake, make_context(bot, []))
        self.assertEqual(len(bot.called("unpin_all_chat_messages")), 1)

    async def test_unpinall_button_checks_the_presser(self):
        chat = fresh_chat()
        bot = self.world(chat, extra={(chat, 42): member(uid=42)})
        query = SimpleNamespace(data=f"unpinall:y:{chat}", from_user=SimpleNamespace(id=42, first_name="M"),
                                answer=AsyncMock(), edit_message_text=AsyncMock())
        fake = SimpleNamespace(callback_query=query, effective_user=query.from_user, effective_chat=None, effective_message=None)
        await au.unpinall_callback(fake, make_context(bot, []))
        self.assertEqual(bot.called("unpin_all_chat_messages"), [])
        query.answer.assert_awaited()

    async def test_pin_modes(self):
        chat = fresh_chat()
        bot = self.world(chat)
        loud = make_update("/pin", chat_id=chat, user_id=MOD, reply=reply_to(42, message_id=9))
        await au.pin_cmd(loud, make_context(bot, []))
        quiet = make_update("/pin silent", chat_id=chat, user_id=MOD, reply=reply_to(42, message_id=10))
        await au.pin_cmd(quiet, make_context(bot, ["silent"]))
        first, second = bot.called("pin_chat_message")
        self.assertFalse(first[1]["disable_notification"])
        self.assertTrue(second[1]["disable_notification"])
        needs_reply = make_update("/pin", chat_id=chat, user_id=MOD)
        await au.pin_cmd(needs_reply, make_context(bot, []))
        self.assertIn("Reply to", replies(needs_reply))

    async def test_invitelink_variants(self):
        chat = fresh_chat()
        public = self.world(chat, chat_info={"username": "club"})
        update, _b = await self.run_cmd(au.invitelink_cmd, chat, "/invitelink", bot=public)
        self.assertIn("https://t.me/club", replies(update))
        existing = self.world(chat, chat_info={"invite_link": "https://t.me/+abc"})
        update, bot = await self.run_cmd(au.invitelink_cmd, chat, "/invitelink", bot=existing)
        self.assertIn("+abc", replies(update))
        self.assertEqual(bot.called("export_chat_invite_link"), [], "an existing link must not be rotated")
        fresh = self.world(chat)
        _u, bot = await self.run_cmd(au.invitelink_cmd, chat, "/invitelink", bot=fresh)
        self.assertEqual(len(bot.called("export_chat_invite_link")), 1)

    async def test_group_info_validation(self):
        chat = fresh_chat()
        ok, bot = await self.run_cmd(au.setgtitle_cmd, chat, "/setgtitle New Name")
        self.assertEqual(bot.called("set_chat_title")[0][0], (chat, "New Name"))
        bad, _b = await self.run_cmd(au.setgtitle_cmd, chat, "/setgtitle " + "x" * 200)
        self.assertIn("Usage", replies(bad))
        desc, bot = await self.run_cmd(au.setdesc_cmd, chat, "/setdesc Be kind")
        self.assertEqual(bot.called("set_chat_description")[0][0], (chat, "Be kind"))
        too_long, _b = await self.run_cmd(au.setdesc_cmd, chat, "/setdesc " + "y" * 300)
        self.assertIn("Usage", replies(too_long))

    async def test_pinned_link(self):
        chat = -1006100000001
        bot = self.world(chat, chat_info={"pinned_message": SimpleNamespace(message_id=77)})
        update, _b = await self.run_cmd(au.pinned_cmd, chat, "/pinned", bot=bot)
        self.assertIn("https://t.me/c/6100000001/77", replies(update))
        nothing, _b = await self.run_cmd(au.pinned_cmd, fresh_chat(), "/pinned", bot=FakeBot(bot_id=ME))
        self.assertIn("Nothing is pinned", replies(nothing))


if __name__ == "__main__":
    unittest.main()
