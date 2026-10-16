"""Tests for bot/plugins/cleanblue.py (v2.17): the blue-text cleaner and its ignore lists."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_cleanblue_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP.name}")

from sqlalchemy import delete  # noqa: E402
from telegram.ext import CommandHandler  # noqa: E402

from bot.core.database import BlueIgnore, Chat, async_session, init_db  # noqa: E402
import bot.plugins.cleanblue as cb  # noqa: E402
from tests._fakes import FakeBot, admin, make_context, make_update  # noqa: E402

CHAT = -960_001
ADMIN, MEMBER = 960_010, 960_020


async def _noop(update, context):  # a registered command
    return None


class TestPure(unittest.TestCase):
    def test_normalize_command(self):
        self.assertEqual(cb.normalize_command("/Play@SomeBot"), "play")
        self.assertEqual(cb.normalize_command("play"), "play")
        for bad in ("", "/", "has space", "a-b", "x" * 33, "naïve"):
            self.assertIsNone(cb.normalize_command(bad), bad)

    def test_parse_message_command(self):
        f = cb.parse_message_command
        self.assertEqual(f("/Hello world", "mybot"), "hello")
        self.assertEqual(f("/hello@MyBot x", "mybot"), "hello")
        self.assertEqual(f("/hello@MyBot", "@mybot"), "hello")
        self.assertIsNone(f("/play@OtherBot song", "mybot"))          # someone else's command
        self.assertIsNone(f("/play@OtherBot", None))                   # own name unknown: don't guess
        for text in ("hello", "/", "", "  ", "text /cmd", "//x"[:1]):
            self.assertIsNone(f(text, "mybot"), text)

    def test_known_commands_are_collected_and_refreshed(self):
        app = SimpleNamespace(handlers={0: [CommandHandler(["Start", "go"], _noop)], 1: [object()]})
        self.assertEqual(cb.known_commands(app), frozenset({"start", "go"}))
        app.handlers[0].append(CommandHandler("later", _noop))
        self.assertIn("later", cb.known_commands(app))


class TestListener(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            await s.execute(delete(BlueIgnore).where(BlueIgnore.chat_id.in_([CHAT, cb.GLOBAL])))
            chat = await s.get(Chat, CHAT)
            if chat is None:
                s.add(Chat(id=CHAT, type="supergroup", title="t", clean_blue=True))
            else:
                chat.clean_blue = True
            await s.commit()
        cb.invalidate()

    def _ctx(self, bot=None):
        bot = bot or FakeBot(members={(CHAT, ADMIN): admin(ADMIN)})
        bot.username = "MyBot"
        ctx = make_context(bot)
        ctx.application = SimpleNamespace(handlers={0: [CommandHandler(["start", "ban"], _noop)]})
        return ctx

    async def _send(self, text, user=MEMBER, ctx=None):
        upd = make_update(text, user_id=user, chat_id=CHAT)
        await cb.blue_listener(upd, ctx or self._ctx())
        return upd

    async def test_unknown_command_is_deleted_known_and_foreign_are_not(self):
        self.assertTrue((await self._send("/madeup hello")).effective_message.delete.await_count)
        self.assertTrue((await self._send("/madeup@MyBot")).effective_message.delete.await_count)
        for keep in ("/start", "/BAN @x", "/ban@MyBot", "/play@OtherBot song", "no command here", "/"):
            self.assertEqual((await self._send(keep)).effective_message.delete.await_count, 0, keep)

    async def test_disabled_chats_and_private_chats_are_untouched(self):
        async with async_session() as s:
            (await s.get(Chat, CHAT)).clean_blue = False
            await s.commit()
        cb.invalidate()
        self.assertEqual((await self._send("/madeup")).effective_message.delete.await_count, 0)
        upd = make_update("/madeup", user_id=MEMBER, chat_id=MEMBER, chat_type="private")
        await cb.blue_listener(upd, self._ctx())
        self.assertEqual(upd.effective_message.delete.await_count, 0)

    async def test_local_and_global_ignore_lists(self):
        ctx = self._ctx()
        upd = make_update("/ignoreblue /Play", user_id=ADMIN, chat_id=CHAT)
        await cb.ignoreblue_cmd(upd, make_context(ctx.bot, ["/Play"]))
        self.assertIn("added", upd.effective_message.reply_text.await_args.args[0])
        self.assertEqual((await self._send("/play now", ctx=ctx)).effective_message.delete.await_count, 0)

        from bot.config import settings
        upd = make_update("/gignoreblue", user_id=settings.owner_id, chat_id=CHAT)
        await cb.gignoreblue_cmd(upd, make_context(ctx.bot, ["skip"]))
        self.assertEqual((await self._send("/skip", ctx=ctx)).effective_message.delete.await_count, 0)

        upd = make_update("/listblue", user_id=ADMIN, chat_id=CHAT)
        await cb.listblue_cmd(upd, make_context(ctx.bot))
        text = upd.effective_message.reply_text.await_args.args[0]
        self.assertIn("globally", text)
        self.assertIn("skip", text)
        self.assertIn("play", text)

        upd = make_update("/unignoreblue", user_id=ADMIN, chat_id=CHAT)
        await cb.unignoreblue_cmd(upd, make_context(ctx.bot, ["play"]))
        self.assertTrue((await self._send("/play", ctx=ctx)).effective_message.delete.await_count)
        upd = make_update("/unignoreblue", user_id=ADMIN, chat_id=CHAT)
        await cb.unignoreblue_cmd(upd, make_context(ctx.bot, ["play"]))
        self.assertIn("isn't ignored", upd.effective_message.reply_text.await_args.args[0])

    async def test_global_list_is_sudo_only(self):
        ctx = self._ctx()
        upd = make_update("/gignoreblue", user_id=ADMIN, chat_id=CHAT)            # a chat admin is not a sudoer
        await cb.gignoreblue_cmd(upd, make_context(ctx.bot, ["evil"]))
        self.assertIn("Sudo only", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/ungignoreblue", user_id=ADMIN, chat_id=CHAT)
        await cb.ungignoreblue_cmd(upd, make_context(ctx.bot, ["evil"]))
        self.assertIn("Sudo only", upd.effective_message.reply_text.await_args.args[0])
        self.assertTrue((await self._send("/evil", ctx=ctx)).effective_message.delete.await_count)

    async def test_ignore_validation_and_duplicates(self):
        bot = FakeBot(members={(CHAT, ADMIN): admin(ADMIN)})
        for args, needle in (([], "No command"), (["bad name!"], "valid command"), (["ok"], "added"), (["ok"], "already")):
            upd = make_update("/ignoreblue", user_id=ADMIN, chat_id=CHAT)
            await cb.ignoreblue_cmd(upd, make_context(bot, args))
            self.assertIn(needle, upd.effective_message.reply_text.await_args.args[0])

    async def test_cleanblue_toggle_status_and_permission(self):
        bot = FakeBot(members={(CHAT, ADMIN): admin(ADMIN)})
        upd = make_update("/cleanblue", user_id=ADMIN, chat_id=CHAT)
        await cb.cleanblue_cmd(upd, make_context(bot, ["off"]))
        self.assertIn("Disabled", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/cleanblue", user_id=ADMIN, chat_id=CHAT)
        await cb.cleanblue_cmd(upd, make_context(bot))
        self.assertIn("Disabled", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/cleanblue", user_id=ADMIN, chat_id=CHAT)
        await cb.cleanblue_cmd(upd, make_context(bot, ["maybe"]))
        self.assertIn("Invalid", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/cleanblue", user_id=ADMIN, chat_id=CHAT)
        await cb.cleanblue_cmd(upd, make_context(bot, ["on"]))
        self.assertIn("Enabled", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/cleanblue", user_id=MEMBER, chat_id=CHAT)             # members cannot toggle
        await cb.cleanblue_cmd(upd, make_context(bot, ["off"]))
        async with async_session() as s:
            self.assertTrue((await s.get(Chat, CHAT)).clean_blue)

    def test_registration(self):
        added = []
        cb.register(SimpleNamespace(add_handler=lambda h, group=0: added.append((h, group))))
        self.assertIn(17, [g for _, g in added])
        names = {c for h, _ in added for c in (getattr(h, "commands", ()) or ())}
        self.assertEqual(names, {"cleanblue", "ignoreblue", "unignoreblue", "gignoreblue", "ungignoreblue", "listblue"})


if __name__ == "__main__":
    unittest.main()
