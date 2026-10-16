"""Tests for bot/plugins/warnfilters.py (v2.16): /addwarn warn filters, /strongwarn, sticker-pack blacklist."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_warnfilters_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP_DB.name}")

from sqlalchemy import select  # noqa: E402

from bot.core.database import Chat, StickerBlacklist, Warn, WarnFilter, async_session, init_db  # noqa: E402
import bot.plugins.warnfilters as wf  # noqa: E402
from tests._fakes import FakeBot, admin, make_context, make_update  # noqa: E402

CHAT = -990_001
ADMIN_ID = 990_010
MEMBER_ID = 990_020


def _bot():
    return FakeBot(members={(CHAT, ADMIN_ID): admin(ADMIN_ID)})


class TestPureHelpers(unittest.TestCase):
    def test_keyword_and_reason_parsing(self):
        self.assertEqual(wf.split_keyword_and_reason("spam no ads"), ("spam", "no ads"))
        self.assertEqual(wf.split_keyword_and_reason('"free money" scam'), ("free money", "scam"))
        self.assertEqual(wf.split_keyword_and_reason("'Hello World'"), ("hello world", ""))
        self.assertEqual(wf.split_keyword_and_reason("onlykeyword"), ("onlykeyword", ""))
        self.assertEqual(wf.split_keyword_and_reason("   ")[0], None)
        self.assertIsNone(wf.split_keyword_and_reason("*** x")[0])                 # wildcard-only is refused
        self.assertEqual(len(wf.split_keyword_and_reason("k " + "r" * 999)[1]), wf.MAX_REASON_LEN)

    def test_trigger_cleaning(self):
        self.assertEqual(wf.clean_trigger("Bad_Pack"), "bad_pack")
        self.assertEqual(wf.clean_trigger("bad_*"), "bad_*")
        for bad in ("", "  ", "has space", "a-b", "*", "***", "a*b*c*d", "x" * 65):
            self.assertIsNone(wf.clean_trigger(bad), bad)

    def test_sticker_match(self):
        self.assertEqual(wf.sticker_match("Bad_Pack", ["bad_pack"]), "bad_pack")
        self.assertEqual(wf.sticker_match("anime_by_bot", ["*_by_bot"]), "*_by_bot")
        self.assertIsNone(wf.sticker_match("good", ["bad_*"]))
        self.assertIsNone(wf.sticker_match(None, ["bad_*"]))


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            if await s.get(Chat, CHAT) is None:
                s.add(Chat(id=CHAT, type="supergroup", title="t"))
                await s.commit()
        for kind in ("warn", "sticker"):
            wf._cache.pop((kind, CHAT), None)

    async def _reset(self):
        async with async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(WarnFilter).where(WarnFilter.chat_id == CHAT))
            await s.execute(delete(StickerBlacklist).where(StickerBlacklist.chat_id == CHAT))
            await s.execute(delete(Warn).where(Warn.chat_id == CHAT))
            chat = await s.get(Chat, CHAT)
            chat.blsticker_action = None
            chat.blsticker_duration = None
            await s.commit()
        wf._invalidate(CHAT)


class TestWarnFilters(_Base):
    async def test_add_list_trigger_and_remove(self):
        await self._reset()
        bot = _bot()
        upd = make_update("/addwarn spam no ads here", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.addwarn_cmd(upd, make_context(bot))
        self.assertIn("Warn filter set", upd.effective_message.reply_text.await_args.args[0])

        upd = make_update("/warnlist", user_id=MEMBER_ID, chat_id=CHAT)
        await wf.warnlist_cmd(upd, make_context(bot))
        self.assertIn("no ads here", upd.effective_message.reply_text.await_args.args[0])

        hit = make_update("this is SPAM, folks", user_id=MEMBER_ID, chat_id=CHAT)
        await wf.policy_listener(hit, make_context(bot))
        text = hit.effective_message.reply_text.await_args.args[0]
        self.assertIn("Warned", text)
        self.assertIn("no ads here", text)
        async with async_session() as s:
            warns = (await s.execute(select(Warn).where(Warn.chat_id == CHAT, Warn.user_id == MEMBER_ID))).scalars().all()
        self.assertEqual(len(warns), 1)

        miss = make_update("spammer is a different word", user_id=MEMBER_ID, chat_id=CHAT)      # whole words only
        await wf.policy_listener(miss, make_context(bot))
        miss.effective_message.reply_text.assert_not_awaited()

        cmd = make_update("/spam", user_id=MEMBER_ID, chat_id=CHAT)                                # commands are ignored
        await wf.policy_listener(cmd, make_context(bot))
        cmd.effective_message.reply_text.assert_not_awaited()

        upd = make_update("/nowarn spam", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.nowarn_cmd(upd, make_context(bot))
        self.assertIn("Removed", upd.effective_message.reply_text.await_args.args[0])
        again = make_update("spam", user_id=MEMBER_ID, chat_id=CHAT)
        await wf.policy_listener(again, make_context(bot))
        again.effective_message.reply_text.assert_not_awaited()

    async def test_admins_are_exempt_and_members_cannot_add_filters(self):
        await self._reset()
        bot = _bot()
        await wf.addwarn_cmd(make_update("/addwarn spam", user_id=ADMIN_ID, chat_id=CHAT), make_context(bot))
        adm = make_update("spam", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.policy_listener(adm, make_context(bot))
        adm.effective_message.reply_text.assert_not_awaited()

        upd = make_update("/addwarn evil", user_id=MEMBER_ID, chat_id=CHAT)
        await wf.addwarn_cmd(upd, make_context(bot))
        async with async_session() as s:
            kws = (await s.execute(select(WarnFilter.keyword).where(WarnFilter.chat_id == CHAT))).scalars().all()
        self.assertEqual(kws, ["spam"])

    async def test_redefining_a_filter_replaces_its_reason_and_the_limit_is_enforced(self):
        await self._reset()
        bot = _bot()
        await wf.addwarn_cmd(make_update("/addwarn spam one", user_id=ADMIN_ID, chat_id=CHAT), make_context(bot))
        await wf.addwarn_cmd(make_update("/addwarn spam two", user_id=ADMIN_ID, chat_id=CHAT), make_context(bot))
        self.assertEqual(await wf._warn_rules(CHAT), {"spam": "two"})
        async with async_session() as s:
            for i in range(wf.MAX_WARN_FILTERS - 1):
                s.add(WarnFilter(chat_id=CHAT, keyword=f"k{i}", reason="r"))
            await s.commit()
        wf._invalidate(CHAT)
        upd = make_update("/addwarn onetoomany", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.addwarn_cmd(upd, make_context(bot))
        self.assertIn("at most", upd.effective_message.reply_text.await_args.args[0])

    async def test_strongwarn_toggles_the_warn_action(self):
        await self._reset()
        bot = _bot()
        upd = make_update("/strongwarn on", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.strongwarn_cmd(upd, make_context(bot, ["on"]))
        self.assertIn("banned", upd.effective_message.reply_text.await_args.args[0])
        async with async_session() as s:
            self.assertEqual((await s.get(Chat, CHAT)).warn_action, "ban")
        upd = make_update("/strongwarn off", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.strongwarn_cmd(upd, make_context(bot, ["off"]))
        async with async_session() as s:
            self.assertEqual((await s.get(Chat, CHAT)).warn_action, "kick")
        upd = make_update("/strongwarn maybe", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.strongwarn_cmd(upd, make_context(bot, ["maybe"]))
        self.assertIn("Usage", upd.effective_message.reply_text.await_args.args[0])


class TestStickerBlacklist(_Base):
    def _sticker_update(self, user_id, set_name):
        upd = make_update("", user_id=user_id, chat_id=CHAT)
        upd.effective_message.sticker = SimpleNamespace(set_name=set_name)
        return upd

    async def test_add_by_reply_by_name_list_and_delete_on_match(self):
        await self._reset()
        bot = _bot()
        reply = SimpleNamespace(sticker=SimpleNamespace(set_name="Bad_Pack"), from_user=SimpleNamespace(id=1, first_name="x", is_bot=False),
                                forum_topic_created=None, forum_topic_edited=None, forum_topic_closed=None, forum_topic_reopened=None,
                                general_forum_topic_hidden=None, general_forum_topic_unhidden=None)
        upd = make_update("/addblsticker", user_id=ADMIN_ID, chat_id=CHAT, reply=reply)
        await wf.addblsticker_cmd(upd, make_context(bot))
        self.assertIn("blacklisted", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/addblsticker", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.addblsticker_cmd(upd, make_context(bot, ["spam_*"]))
        self.assertEqual(sorted((await wf._sticker_rules(CHAT))[0]), ["bad_pack", "spam_*"])

        hit = self._sticker_update(MEMBER_ID, "BAD_PACK")
        await wf.policy_listener(hit, make_context(bot))
        hit.effective_message.delete.assert_awaited()
        wild = self._sticker_update(MEMBER_ID, "spam_things")
        await wf.policy_listener(wild, make_context(bot))
        wild.effective_message.delete.assert_awaited()
        ok = self._sticker_update(MEMBER_ID, "nice_pack")
        await wf.policy_listener(ok, make_context(bot))
        ok.effective_message.delete.assert_not_awaited()
        adm = self._sticker_update(ADMIN_ID, "bad_pack")
        await wf.policy_listener(adm, make_context(bot))
        adm.effective_message.delete.assert_not_awaited()

        upd = make_update("/blsticker", user_id=MEMBER_ID, chat_id=CHAT)
        await wf.blsticker_cmd(upd, make_context(bot))
        self.assertIn("bad_pack", upd.effective_message.reply_text.await_args.args[0])

        upd = make_update("/unblsticker", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.unblsticker_cmd(upd, make_context(bot, ["bad_pack"]))
        self.assertIn("Removed", upd.effective_message.reply_text.await_args.args[0])
        self.assertEqual(await wf._sticker_rules(CHAT), (["spam_*"], None, None))

    async def test_mode_ban_is_applied_to_the_sender(self):
        await self._reset()
        bot = _bot()
        await wf.addblsticker_cmd(make_update("/addblsticker", user_id=ADMIN_ID, chat_id=CHAT), make_context(bot, ["evil"]))
        upd = make_update("/blstickermode ban", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.blstickermode_cmd(upd, make_context(bot, ["ban"]))
        self.assertIn("ban", upd.effective_message.reply_text.await_args.args[0])
        hit = self._sticker_update(MEMBER_ID, "evil")
        await wf.policy_listener(hit, make_context(bot))
        self.assertEqual(bot.called("ban_chat_member")[0][0][:2], (CHAT, MEMBER_ID))

    async def test_mode_validation(self):
        await self._reset()
        bot = _bot()
        upd = make_update("/blstickermode explode", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.blstickermode_cmd(upd, make_context(bot, ["explode"]))
        self.assertIn("Usage", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/blstickermode tban 1s", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.blstickermode_cmd(upd, make_context(bot, ["tban", "1s"]))
        self.assertIn("❌", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/blstickermode tban 2h", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.blstickermode_cmd(upd, make_context(bot, ["tban", "2h"]))
        self.assertEqual((await wf._sticker_rules(CHAT))[1:], ("tban", 7200))

    async def test_stickers_without_a_pack_and_junk_names_are_rejected(self):
        await self._reset()
        bot = _bot()
        reply = SimpleNamespace(sticker=SimpleNamespace(set_name=None), from_user=SimpleNamespace(id=1, first_name="x", is_bot=False),
                                forum_topic_created=None, forum_topic_edited=None, forum_topic_closed=None, forum_topic_reopened=None,
                                general_forum_topic_hidden=None, general_forum_topic_unhidden=None)
        upd = make_update("/addblsticker", user_id=ADMIN_ID, chat_id=CHAT, reply=reply)
        await wf.addblsticker_cmd(upd, make_context(bot))
        self.assertIn("doesn't belong to a pack", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/addblsticker", user_id=ADMIN_ID, chat_id=CHAT)
        await wf.addblsticker_cmd(upd, make_context(bot, ["bad name!"]))
        self.assertIn("Usage", upd.effective_message.reply_text.await_args.args[0])


class TestRegistration(unittest.TestCase):
    def test_all_commands_and_one_listener_group(self):
        added = []
        wf.register(SimpleNamespace(add_handler=lambda h, group=0: added.append((h, group))))
        self.assertIn(15, [g for _, g in added])
        names = set()
        for h, _ in added:
            names |= set(getattr(h, "commands", ()) or ())
        for cmd in ("addwarn", "nowarn", "stopwarn", "warnlist", "warnfilters", "strongwarn", "addblsticker", "unblsticker",
                    "rmblsticker", "blsticker", "blstickers", "blstickermode"):
            self.assertIn(cmd, names)


if __name__ == "__main__":
    unittest.main()
