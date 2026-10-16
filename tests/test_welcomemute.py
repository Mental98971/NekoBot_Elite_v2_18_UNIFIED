"""Tests for bot/plugins/welcomemute.py (v2.17): soft/strong welcome mute, verification button, sweeper, /captcha link."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_welcomemute_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP.name}")

from sqlalchemy import delete, select  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from bot.core.database import Chat, GlobalBan, PendingVerification, VerifiedMember, async_session, init_db  # noqa: E402
import bot.plugins.fedtools as fedtools  # noqa: E402
import bot.plugins.welcomemute as wm  # noqa: E402
from bot.utils import modtools as mt  # noqa: E402
from tests._fakes import FakeBot, admin, make_context, make_update  # noqa: E402

CHAT = -970_001
ADMIN, JOINER, OTHER = 970_010, 970_020, 970_030


def _user(uid, bot=False):
    return SimpleNamespace(id=uid, first_name=f"U{uid}", last_name=None, username=None, is_bot=bot)


class TestPure(unittest.TestCase):
    def test_effective_mode(self):
        for stored, captcha, expected in (("soft", True, "soft"), ("off", True, "off"), (None, True, "strong"), (None, False, "off"),
                                          (None, None, "off"), ("strong", False, "strong"), ("junk", True, "strong")):
            self.assertEqual(wm.effective_mode(stored, captcha), expected, (stored, captcha))


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        fedtools.invalidate_enforcement_cache()
        async with async_session() as s:
            for model in (PendingVerification, VerifiedMember):
                await s.execute(delete(model).where(model.chat_id == CHAT))
            await s.execute(delete(GlobalBan).where(GlobalBan.user_id == JOINER))
            chat = await s.get(Chat, CHAT)
            if chat is None:
                s.add(Chat(id=CHAT, type="supergroup", title="t", welcome_mute=None, captcha_enabled=False, welcome_enabled=True,
                           welcome_text="Hi {first}!"))
            else:
                chat.welcome_mute, chat.captcha_enabled = None, False
            await s.commit()

    async def _set(self, **cols):
        async with async_session() as s:
            chat = await s.get(Chat, CHAT)
            for k, v in cols.items():
                setattr(chat, k, v)
            await s.commit()

    def _bot(self, **kw):
        return FakeBot(members={(CHAT, ADMIN): admin(ADMIN), (CHAT, 999): admin(999)}, **kw)

    def _join(self, joiner=None, added_by=None):
        joiner = joiner or _user(JOINER)
        upd = make_update("", user_id=(added_by or joiner.id), chat_id=CHAT)
        upd.effective_message.new_chat_members = [joiner]
        upd.effective_message.from_user = _user(added_by) if added_by else joiner
        return upd

    async def _pending(self):
        async with async_session() as s:
            return (await s.execute(select(PendingVerification).where(PendingVerification.chat_id == CHAT))).scalars().all()


class TestJoinGuard(Base):
    async def test_off_does_nothing(self):
        bot = self._bot()
        await wm.join_guard(self._join(), make_context(bot))
        self.assertEqual(bot.called("restrict_chat_member"), [])

    async def test_soft_restricts_media_for_24_hours(self):
        await self._set(welcome_mute="soft")
        bot = self._bot()
        await wm.join_guard(self._join(), make_context(bot))
        (args, kwargs), = bot.called("restrict_chat_member")
        self.assertEqual(args[:2], (CHAT, JOINER))
        perms = args[2]
        self.assertTrue(perms.can_send_messages)
        self.assertFalse(perms.can_send_photos or perms.can_send_other_messages or perms.can_send_polls)
        self.assertIsNotNone(kwargs["until_date"])
        self.assertEqual(await self._pending(), [])

    async def test_strong_mutes_prompts_and_records_a_deadline(self):
        await self._set(welcome_mute="strong")
        bot = self._bot()
        await wm.join_guard(self._join(), make_context(bot))
        (args, _), = bot.called("restrict_chat_member")
        self.assertFalse(args[2].can_send_messages)
        (sargs, skw), = bot.called("send_message")
        self.assertIn("not a bot", sargs[1])
        self.assertEqual(skw["reply_markup"].inline_keyboard[0][0].callback_data, f"wmv:{JOINER}")
        (rec,) = await self._pending()
        self.assertEqual((rec.user_id, rec.message_id > 0), (JOINER, True))
        self.assertTrue(timedelta(seconds=100) < rec.deadline - mt.utcnow() <= timedelta(seconds=wm.VERIFY_SECONDS))

    async def test_captcha_flag_now_means_strong_unless_welcomemute_chose_off(self):
        await self._set(captcha_enabled=True)
        bot = self._bot()
        await wm.join_guard(self._join(), make_context(bot))
        self.assertEqual(len(bot.called("restrict_chat_member")), 1)
        await self._set(welcome_mute="off")
        bot2 = self._bot()
        await wm.join_guard(self._join(), make_context(bot2))
        self.assertEqual(bot2.called("restrict_chat_member"), [])

    async def test_exemptions(self):
        await self._set(welcome_mute="strong")
        bot = self._bot()
        await wm.join_guard(self._join(joiner=_user(ADMIN)), make_context(bot))                       # admin
        await wm.join_guard(self._join(joiner=_user(555, bot=True)), make_context(bot))                # bot
        await wm.join_guard(self._join(joiner=_user(JOINER), added_by=OTHER), make_context(bot))       # added by someone else
        async with async_session() as s:
            s.add(VerifiedMember(chat_id=CHAT, user_id=JOINER))
            await s.commit()
        await wm.join_guard(self._join(), make_context(bot))                                           # already verified
        self.assertEqual(bot.called("restrict_chat_member"), [])

    async def test_banned_users_are_left_to_the_enforcer(self):
        await self._set(welcome_mute="strong")
        async with async_session() as s:
            s.add(GlobalBan(user_id=JOINER, reason="x", banned_by=1))
            await s.commit()
        fedtools.invalidate_enforcement_cache()
        bot = self._bot()
        await wm.join_guard(self._join(), make_context(bot))
        self.assertEqual(bot.called("restrict_chat_member"), [])

    async def test_no_prompt_when_the_bot_cannot_restrict(self):
        await self._set(welcome_mute="strong")
        bot = self._bot(fail={"restrict_chat_member": BadRequest("Not enough rights")})
        await wm.join_guard(self._join(), make_context(bot))
        self.assertEqual(bot.called("send_message"), [])
        self.assertEqual(await self._pending(), [])

    async def test_rejoin_replaces_the_pending_row(self):
        await self._set(welcome_mute="strong")
        for _ in range(2):
            await wm.join_guard(self._join(), make_context(self._bot()))
        self.assertEqual(len(await self._pending()), 1)


class TestVerification(Base):
    def _press(self, presser, target=JOINER):
        q = SimpleNamespace(data=f"wmv:{target}", from_user=_user(presser), answer=AsyncMock(),
                            message=SimpleNamespace(delete=AsyncMock(), chat=SimpleNamespace(id=CHAT)))
        chat = SimpleNamespace(id=CHAT, type="supergroup", title="t")
        return SimpleNamespace(callback_query=q, effective_chat=chat, effective_user=q.from_user, effective_message=None), q

    async def test_only_the_newcomer_can_press_and_it_unmutes_greets_and_remembers(self):
        await self._set(welcome_mute="strong")
        bot = self._bot()
        await wm.join_guard(self._join(), make_context(bot))
        bot.calls.clear()

        upd, q = self._press(OTHER)
        await wm.verify_callback(upd, make_context(bot))
        q.answer.assert_awaited_with("This button isn't for you.", show_alert=True)
        self.assertEqual(len(await self._pending()), 1)

        upd, q = self._press(JOINER)
        await wm.verify_callback(upd, make_context(bot))
        self.assertEqual(await self._pending(), [])
        self.assertTrue(bot.called("restrict_chat_member"))                       # unmuted (default permissions restored)
        q.message.delete.assert_awaited()
        greeting = [a for a, _ in bot.called("send_message") if "Hi " in a[1]]
        self.assertTrue(greeting, "the held-back welcome is sent after verification")
        self.assertTrue(await wm._is_verified(CHAT, JOINER))

        upd, q = self._press(JOINER)                                               # pressing again is harmless
        await wm.verify_callback(upd, make_context(bot))
        self.assertIn("already finished", q.answer.await_args.args[0])

    async def test_welcome_is_deferred_only_for_pending_members(self):
        await self._set(welcome_mute="strong")
        await wm.join_guard(self._join(), make_context(self._bot()))
        kept = await wm.filter_deferred(CHAT, [_user(JOINER), _user(OTHER)])
        self.assertEqual([u.id for u in kept], [OTHER])

        from bot.plugins import group_mgmt
        bot = self._bot()
        await group_mgmt.welcome_listener(self._join(), make_context(bot))          # JOINER is pending -> no greeting
        self.assertEqual([a for a, _ in bot.called("send_message") if "Hi " in a[1]], [])
        await group_mgmt.welcome_listener(self._join(joiner=_user(OTHER)), make_context(bot))
        self.assertTrue([a for a, _ in bot.called("send_message") if "Hi " in a[1]])


class TestSweep(Base):
    async def test_expired_members_are_kicked_and_unexpired_are_left(self):
        async with async_session() as s:
            s.add(PendingVerification(chat_id=CHAT, user_id=JOINER, message_id=11, deadline=mt.utcnow() - timedelta(seconds=1)))
            s.add(PendingVerification(chat_id=CHAT, user_id=OTHER, message_id=12, deadline=mt.utcnow() + timedelta(seconds=60)))
            await s.commit()
        bot = self._bot()
        await wm.sweep(make_context(bot))
        self.assertEqual(bot.called("ban_chat_member")[0][0][:2], (CHAT, JOINER))
        self.assertEqual(bot.called("unban_chat_member")[0][0][:2], (CHAT, JOINER))     # kick, not ban
        self.assertEqual(bot.called("delete_message")[0][0], (CHAT, 11))
        self.assertEqual([p.user_id for p in await self._pending()], [OTHER])

    async def test_sweep_survives_telegram_errors_and_still_clears_rows(self):
        async with async_session() as s:
            s.add(PendingVerification(chat_id=CHAT, user_id=JOINER, message_id=11, deadline=mt.utcnow() - timedelta(seconds=1)))
            await s.commit()
        bot = self._bot(fail={"ban_chat_member": BadRequest("User not found"), "delete_message": BadRequest("gone")})
        await wm.sweep(make_context(bot))
        self.assertEqual(await self._pending(), [])


class TestCommands(Base):
    async def test_set_show_validate_and_permission(self):
        bot = self._bot()
        upd = make_update("/welcomemute", user_id=ADMIN, chat_id=CHAT)
        await wm.welcomemute_cmd(upd, make_context(bot, ["strong"]))
        self.assertIn("prove they're human", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/welcomemute", user_id=ADMIN, chat_id=CHAT)
        await wm.welcomemute_cmd(upd, make_context(bot, []))
        self.assertIn("<b>strong</b>", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/welcomemute", user_id=ADMIN, chat_id=CHAT)
        await wm.welcomemute_cmd(upd, make_context(bot, ["no"]))                  # Saitama's alias for off
        async with async_session() as s:
            self.assertEqual((await s.get(Chat, CHAT)).welcome_mute, "off")
        upd = make_update("/welcomemute", user_id=ADMIN, chat_id=CHAT)
        await wm.welcomemute_cmd(upd, make_context(bot, ["loud"]))
        self.assertIn("Please enter", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/welcomemute", user_id=OTHER, chat_id=CHAT)
        await wm.welcomemute_cmd(upd, make_context(bot, ["strong"]))
        async with async_session() as s:
            self.assertEqual((await s.get(Chat, CHAT)).welcome_mute, "off")

    async def test_help(self):
        upd = make_update("/welcomemutehelp", user_id=OTHER, chat_id=CHAT)
        await wm.welcomemutehelp_cmd(upd, make_context(self._bot()))
        self.assertIn("soft", upd.effective_message.reply_text.await_args.args[0])

    def test_registration(self):
        added = []
        wm.register(SimpleNamespace(add_handler=lambda h, group=0: added.append((h, group)), job_queue=None))
        self.assertIn(-7, [g for _, g in added])
        names = {c for h, _ in added for c in (getattr(h, "commands", ()) or ())}
        self.assertEqual(names, {"welcomemute", "welcomemutehelp"})


if __name__ == "__main__":
    unittest.main()
