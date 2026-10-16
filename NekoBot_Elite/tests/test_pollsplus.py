"""Tests for bot/plugins/pollsplus.py (v2.17): Masha-style polls, /stoppoll and /forgotpollid."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_polls_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP.name}")

from sqlalchemy import delete, select  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from bot.core.database import PollRecord, async_session, init_db  # noqa: E402
import bot.plugins.pollsplus as pp  # noqa: E402
from tests._fakes import FakeBot, admin, make_context, make_update  # noqa: E402

CHAT = -950_001
ADMIN, OTHER_ADMIN, MEMBER = 950_010, 950_011, 950_020


class PollBot(FakeBot):
    def __init__(self, **kw):
        super().__init__(members={(CHAT, ADMIN): admin(ADMIN), (CHAT, OTHER_ADMIN): admin(OTHER_ADMIN)}, **kw)
        self.stop_result = SimpleNamespace(
            question="Pizza?", total_voter_count=4, correct_option_id=None,
            options=[SimpleNamespace(text="yes", voter_count=3), SimpleNamespace(text="no", voter_count=1)])

    async def send_poll(self, *args, **kwargs):
        self.calls.append(("send_poll", args, kwargs))
        if "send_poll" in self.fail:
            raise self.fail["send_poll"]
        return SimpleNamespace(message_id=777)

    async def stop_poll(self, *args, **kwargs):
        self.calls.append(("stop_poll", args, kwargs))
        if "stop_poll" in self.fail:
            raise self.fail["stop_poll"]
        return self.stop_result


class TestParsing(unittest.TestCase):
    def test_basic_and_quiz_forms(self):
        spec = pp.parse_apoll("12345 | am i cool? | False False False yes no")
        self.assertEqual((spec["code"], spec["question"], spec["options"], spec["quiz_answer"], spec["public"], spec["multiple"]),
                         ("12345", "am i cool?", ["yes", "no"], None, False, False))
        quiz = pp.parse_apoll("12345 | 2+2? | True@2 True False 3 4 5")
        self.assertEqual((quiz["quiz_answer"], quiz["public"], quiz["options"]), (2, True, ["3", "4", "5"]))

    def test_quoted_options_and_case_insensitive_flags(self):
        spec = pp.parse_apoll('54321 | Where? | false TRUE true "the park" beach "at home"')
        self.assertEqual(spec["options"], ["the park", "beach", "at home"])
        self.assertEqual((spec["public"], spec["multiple"]), (True, True))

    def test_errors_have_clear_messages(self):
        bad = {
            "12345 | q": "Usage",
            " | q | False False False a b": "5 digits",
            "1234 | q | False False False a b": "5 digits",
            "12345 |  | False False False a b": "question",
            "12345 | q | False False False a": "two options",
            "12345 | q | maybe False False a b": "False or True@",
            "12345 | q | True False False a b": "right answer",
            "12345 | q | True@9 False False a b": "between 1 and 2",
            "12345 | q | True@1 False True a b": "multiple answers",
            "12345 | q | False yes? False a b": "public votes",
            "12345 | q | False False perhaps a b": "multiple choice",
            "12345 | q | False False False a a": "different",
            "12345 | q | False False False " + " ".join(f"o{i}" for i in range(11)): "at most 10",
            "12345 | " + "q" * 301 + " | False False False a b": "300",
            "12345 | q | False False False " + "x" * 101 + " b": "100",
            "12345 | q | False": "After the question",
        }
        for raw, needle in bad.items():
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(pp.PollSyntaxError) as ctx:
                    pp.parse_apoll(raw)
                self.assertIn(needle, str(ctx.exception))

    def test_poll_routing_detection(self):
        self.assertTrue(pp.looks_like_apoll("12345 | q | False False False a b"))
        self.assertFalse(pp.looks_like_apoll("Pizza? | yes | no"))
        self.assertFalse(pp.looks_like_apoll("1234 | q | a"))

    def test_results_format(self):
        text = pp.format_results(PollBot().stop_result)
        self.assertIn("Pizza?", text)
        self.assertIn("yes: 3 (75%)", text)
        self.assertIn("4 voters", text)


class TestCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            await s.execute(delete(PollRecord).where(PollRecord.chat_id == CHAT))
            await s.commit()

    async def _create(self, bot, text="/apoll 12345 | q? | False False False yes no", user=ADMIN):
        upd = make_update(text, user_id=user, chat_id=CHAT)
        await pp.apoll_cmd(upd, make_context(bot))
        return upd

    async def test_create_sends_poll_deletes_command_and_records_it(self):
        bot = PollBot()
        upd = await self._create(bot, "/apoll 12345 | q? | True@2 True False a b c")
        (args, kwargs), = bot.called("send_poll")
        self.assertEqual(args[:3], (CHAT, "q?", ["a", "b", "c"]))
        self.assertEqual((kwargs["type"], kwargs["correct_option_id"], kwargs["is_anonymous"]), ("quiz", 1, False))
        upd.effective_message.delete.assert_awaited()
        async with async_session() as s:
            rec = (await s.execute(select(PollRecord).where(PollRecord.chat_id == CHAT))).scalars().one()
        self.assertEqual((rec.poll_code, rec.creator_id, rec.message_id), ("12345", ADMIN, 777))

    async def test_members_cannot_create_and_bad_syntax_is_reported(self):
        bot = PollBot()
        upd = await self._create(bot, user=MEMBER)
        self.assertIn("Only admins", upd.effective_message.reply_text.await_args.args[0])
        self.assertEqual(bot.called("send_poll"), [])
        upd = await self._create(bot, "/apoll 12 | q | False False False a b")
        self.assertIn("5 digits", upd.effective_message.reply_text.await_args.args[0])

    async def test_one_active_poll_per_creator_and_unique_ids(self):
        bot = PollBot()
        await self._create(bot)
        upd = await self._create(bot, "/apoll 22222 | another | False False False a b")
        self.assertIn("stop your previous poll", upd.effective_message.reply_text.await_args.args[0])
        upd = await self._create(bot, "/apoll 12345 | clash | False False False a b", user=OTHER_ADMIN)
        self.assertIn("already used", upd.effective_message.reply_text.await_args.args[0])
        self.assertEqual(len(bot.called("send_poll")), 1)

    async def test_telegram_refusal_does_not_leave_a_record(self):
        bot = PollBot(fail={"send_poll": BadRequest("Poll must have at least 2 options")})
        upd = await self._create(bot)
        self.assertIn("refused", upd.effective_message.reply_text.await_args.args[0])
        async with async_session() as s:
            self.assertEqual((await s.execute(select(PollRecord).where(PollRecord.chat_id == CHAT))).all(), [])

    async def test_stoppoll_by_creator_by_admin_and_not_by_members(self):
        bot = PollBot()
        await self._create(bot)
        upd = make_update("/stoppoll", user_id=MEMBER, chat_id=CHAT)
        await pp.stoppoll_cmd(upd, make_context(bot, ["12345"]))
        self.assertIn("creator or an admin", upd.effective_message.reply_text.await_args.args[0])
        self.assertEqual(bot.called("stop_poll"), [])

        upd = make_update("/stoppoll", user_id=OTHER_ADMIN, chat_id=CHAT)
        await pp.stoppoll_cmd(upd, make_context(bot, ["12345"]))
        self.assertEqual(bot.called("stop_poll")[0][0], (CHAT, 777))
        self.assertIn("yes: 3", upd.effective_message.reply_text.await_args.args[0])
        async with async_session() as s:
            self.assertEqual((await s.execute(select(PollRecord).where(PollRecord.chat_id == CHAT))).all(), [])
        await self._create(bot, "/apoll 33333 | again | False False False a b")           # the id was released

    async def test_stoppoll_validation_and_mismatched_reply(self):
        bot = PollBot()
        await self._create(bot)
        for args, needle in (([], "Where is the poll id"), (["abc"], "5 digits"), (["99999"], "don't have")):
            upd = make_update("/stoppoll", user_id=ADMIN, chat_id=CHAT)
            await pp.stoppoll_cmd(upd, make_context(bot, args))
            self.assertIn(needle, upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/stoppoll", user_id=ADMIN, chat_id=CHAT, reply=SimpleNamespace(message_id=1, from_user=SimpleNamespace(id=1, first_name="x", is_bot=False)))
        upd.effective_message.reply_to_message = SimpleNamespace(message_id=1)
        await pp.stoppoll_cmd(upd, make_context(bot, ["12345"]))
        self.assertIn("different poll", upd.effective_message.reply_text.await_args.args[0])

    async def test_stoppoll_of_a_deleted_poll_frees_the_id(self):
        bot = PollBot(fail={"stop_poll": BadRequest("Poll has already been closed")})
        await self._create(bot)
        upd = make_update("/stoppoll", user_id=ADMIN, chat_id=CHAT)
        await pp.stoppoll_cmd(upd, make_context(bot, ["12345"]))
        self.assertIn("id is free", upd.effective_message.reply_text.await_args.args[0])
        async with async_session() as s:
            self.assertEqual((await s.execute(select(PollRecord).where(PollRecord.chat_id == CHAT))).all(), [])

    async def test_forgotpollid(self):
        bot = PollBot()
        upd = make_update("/forgotpollid", user_id=ADMIN, chat_id=CHAT)
        await pp.forgotpollid_cmd(upd, make_context(bot))
        self.assertIn("haven't created", upd.effective_message.reply_text.await_args.args[0])
        await self._create(bot)
        upd = make_update("/forgotpollid", user_id=ADMIN, chat_id=CHAT)
        await pp.forgotpollid_cmd(upd, make_context(bot))
        self.assertIn("create a new poll", upd.effective_message.reply_text.await_args.args[0])

    async def test_plain_poll_command_routes_masha_syntax_here_and_keeps_the_simple_form(self):
        from bot.plugins import scheduling
        bot = PollBot()
        upd = make_update("/poll 12345 | q? | False False False yes no", user_id=ADMIN, chat_id=CHAT)
        await scheduling.poll_cmd(upd, make_context(bot))
        self.assertEqual(len(bot.called("send_poll")), 1)
        async with async_session() as s:
            self.assertEqual(len((await s.execute(select(PollRecord).where(PollRecord.chat_id == CHAT))).all()), 1)
        bot2 = PollBot()
        upd = make_update("/poll Pizza? | yes | no", user_id=MEMBER, chat_id=CHAT)
        await scheduling.poll_cmd(upd, make_context(bot2))
        (args, kwargs), = bot2.called("send_poll")
        self.assertEqual(kwargs["chat_id"], CHAT) if "chat_id" in kwargs else self.assertEqual(args[0], CHAT)

    def test_registration(self):
        added = []
        pp.register(SimpleNamespace(add_handler=lambda h, group=0: added.append(h)))
        self.assertEqual({c for h in added for c in h.commands}, {"apoll", "stoppoll", "forgotpollid"})


if __name__ == "__main__":
    unittest.main()
