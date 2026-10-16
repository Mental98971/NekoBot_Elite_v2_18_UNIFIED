"""Tests for bot/plugins/toolbox.py (v2.16): sed, encryption, feed parsing/polling, notes, SSRF guard."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_toolbox_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP_DB.name}")

from sqlalchemy import select  # noqa: E402

from bot.core.database import RssFeed, User, async_session, init_db  # noqa: E402
import bot.plugins.toolbox as tb  # noqa: E402
from tests._fakes import FakeBot, admin, make_context, make_update  # noqa: E402

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>
<item><title>Three</title><link>http://e.com/3</link><guid>g3</guid></item>
<item><title>Two</title><link>http://e.com/2</link><guid>g2</guid></item>
<item><title>One</title><link>http://e.com/1</link><guid>g1</guid></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><id>a2</id><title>Two</title><link href="http://e.com/a2"/></entry>
<entry><id>a1</id><title>One</title><link rel="alternate" href="http://e.com/a1"/></entry></feed>"""


class TestSed(unittest.TestCase):
    def test_substitutions(self):
        self.assertEqual(tb.sed_substitute("s/cat/dog/", "a cat and a cat"), "a dog and a cat")
        self.assertEqual(tb.sed_substitute("s/cat/dog/g", "a cat and a cat"), "a dog and a dog")
        self.assertEqual(tb.sed_substitute("s/CAT/dog/gi", "Cat cAt"), "dog dog")
        self.assertEqual(tb.sed_substitute(r"s/a\/b/c/", "x a/b y"), "x c y")
        self.assertEqual(tb.sed_substitute("s/x//", "axb"), "ab")

    def test_non_matches_return_none(self):
        for cmd, text in (("s/zzz/y/", "abc"), ("hello", "abc"), ("s/a/b/", ""), ("s/a/a/", "a"), ("s//b/", "a"), ("s/" + "a" * 201 + "/b/", "a" * 201)):
            self.assertIsNone(tb.sed_substitute(cmd, text), cmd)

    def test_pattern_is_literal_so_redos_is_impossible(self):
        self.assertEqual(tb.sed_substitute("s/(a+)+$/X/", "aaaa(a+)+$"), "aaaaX")


class TestCrypto(unittest.TestCase):
    def test_roundtrip_and_failures(self):
        token = tb.encrypt_text("pw", "héllo wörld 🔐")
        self.assertEqual(tb.decrypt_text("pw", token), "héllo wörld 🔐")
        self.assertNotEqual(token, tb.encrypt_text("pw", "héllo wörld 🔐"))   # random salt
        for bad_pw, tok in (("nope", token), ("pw", token[:-4] + "AAAA"), ("pw", "not base64 !!"), ("pw", "")):
            with self.assertRaises(ValueError):
                tb.decrypt_text(bad_pw, tok)


class TestFeeds(unittest.TestCase):
    def test_rss_and_atom_are_parsed(self):
        title, rows = tb.parse_feed(RSS)
        self.assertEqual((title, [r[0] for r in rows]), ("News", ["g3", "g2", "g1"]))
        title, rows = tb.parse_feed(ATOM)
        self.assertEqual((title, rows[0]), ("A", ("a2", "Two", "http://e.com/a2")))
        self.assertEqual(rows[1][2], "http://e.com/a1")

    def test_hostile_or_broken_input_is_rejected(self):
        for raw in (b"<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>", b"<rss", b"<html/>", b"<rss version='2.0'/>", b""):
            with self.assertRaises(ValueError, msg=raw):
                tb.parse_feed(raw)

    def test_new_entries_logic(self):
        _, rows = tb.parse_feed(RSS)
        self.assertEqual(tb.new_entries(rows, None), [])                                   # first poll: baseline only
        self.assertEqual([r[0] for r in tb.new_entries(rows, "g3")], [])
        self.assertEqual([r[0] for r in tb.new_entries(rows, "g1")], ["g2", "g3"])          # oldest first
        self.assertEqual(len(tb.new_entries(rows, "gone")), 3)                              # last id fell off the feed
        many = [(f"e{i}", "t", "l") for i in range(10, 0, -1)]
        self.assertEqual(len(tb.new_entries(many, "gone")), tb.MAX_NEW_PER_POLL)


class TestSsrf(unittest.IsolatedAsyncioTestCase):
    async def test_private_addresses_are_refused(self):
        for u in ("http://localhost/x", "http://127.0.0.1/", "http://10.0.0.5/f", "http://169.254.169.254/latest", "http://[::1]/", "ftp://example.com/x", "not a url"):
            self.assertIsNone(await tb._public_url(u), u)

    async def test_host_resolving_to_private_ip_is_refused(self):
        fake = [(2, 1, 6, "", ("10.1.2.3", 0))]
        with mock.patch("asyncio.unix_events._UnixSelectorEventLoop.getaddrinfo", AsyncMock(return_value=fake), create=True):
            self.assertIsNone(await tb._public_url("http://rebind.example.com/feed"))
        good = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch("asyncio.unix_events._UnixSelectorEventLoop.getaddrinfo", AsyncMock(return_value=good), create=True):
            self.assertEqual(await tb._public_url("http://example.com/feed"), "http://example.com/feed")


class TestCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()

    async def test_setme_me_setbio_bio(self):
        bot = FakeBot()
        upd = make_update("/setme", user_id=910_001, chat_id=-910_000)
        await tb.setme_cmd(upd, make_context(bot, ["I", "like", "tea"]))
        upd = make_update("/me", user_id=910_001, chat_id=-910_000)
        await tb.me_cmd(upd, make_context(bot))
        self.assertIn("I like tea", upd.effective_message.reply_text.await_args.args[0])

        upd = make_update("/setme", user_id=910_001, chat_id=-910_000)
        await tb.setme_cmd(upd, make_context(bot, ["x" * 301]))
        self.assertIn("under", upd.effective_message.reply_text.await_args.args[0])

        target = SimpleNamespace(id=910_002, first_name="Bo", is_bot=False)
        upd = make_update("/setbio", user_id=910_001, chat_id=-910_000, reply=SimpleNamespace(from_user=target))
        upd.message.reply_to_message = upd.effective_message.reply_to_message
        await tb.setbio_cmd(upd, make_context(bot, ["kind", "soul"]))
        async with async_session() as s:
            self.assertEqual((await s.get(User, 910_002)).notes["bio"], "kind soul")
        selfie = SimpleNamespace(from_user=SimpleNamespace(id=910_001, first_name="A", is_bot=False))
        upd = make_update("/setbio", user_id=910_001, chat_id=-910_000, reply=selfie)
        upd.message.reply_to_message = selfie
        await tb.setbio_cmd(upd, make_context(bot, ["me"]))
        self.assertIn("/setme", upd.effective_message.reply_text.await_args.args[0])

    async def test_sed_listener_replies_with_correction(self):
        original = SimpleNamespace(text="I luv cats", caption=None, from_user=SimpleNamespace(first_name="Zed"), reply_text=AsyncMock())
        upd = make_update("s/luv/love/", user_id=1, chat_id=-910_000)
        upd.effective_message.reply_to_message = original
        await tb.sed_listener(upd, make_context(FakeBot()))
        self.assertIn("I love cats", original.reply_text.await_args.args[0])

    async def test_encrypt_then_decrypt_through_commands(self):
        bot = FakeBot()
        upd = make_update("/encrypt", user_id=1, chat_id=-910_000)
        await tb.encrypt_cmd(upd, make_context(bot, ["pw", "secret", "text"]))
        sent = upd.effective_message.reply_text.await_args.args[0]
        token = sent.split("<code>")[1].split("</code>")[0]
        upd.effective_message.delete.assert_awaited()          # the passphrase message is removed
        upd = make_update("/decrypt", user_id=1, chat_id=-910_000)
        await tb.decrypt_cmd(upd, make_context(bot, ["pw", token]))
        self.assertIn("secret text", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/decrypt", user_id=1, chat_id=-910_000)
        await tb.decrypt_cmd(upd, make_context(bot, ["wrong", token]))
        self.assertIn("Wrong passphrase", upd.effective_message.reply_text.await_args.args[0])


class TestRss(unittest.IsolatedAsyncioTestCase):
    CHAT = -920_000

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(RssFeed).where(RssFeed.chat_id == self.CHAT))
            await s.commit()

    def _bot(self):
        return FakeBot(members={(self.CHAT, 920_001): admin(920_001)})

    async def test_add_records_baseline_then_poll_delivers_only_new(self):
        state = {"rows": tb.parse_feed(RSS)[1]}

        async def fake_fetch(url):
            return "News", state["rows"]

        bot = self._bot()
        with mock.patch.object(tb, "_fetch_feed", fake_fetch):
            upd = make_update("/addrss", user_id=920_001, chat_id=self.CHAT)
            await tb.addrss_cmd(upd, make_context(bot, ["http://example.com/feed.xml"]))
            self.assertIn("Following", upd.effective_message.reply_text.await_args.args[0])
            dup = make_update("/addrss", user_id=920_001, chat_id=self.CHAT)
            await tb.addrss_cmd(dup, make_context(bot, ["http://example.com/feed.xml"]))
            self.assertIn("already", dup.effective_message.reply_text.await_args.args[0])

            await tb.rss_poll(make_context(bot))
            self.assertEqual(bot.called("send_message"), [])                       # nothing new yet
            state["rows"] = [("g4", "Four", "http://e.com/4")] + state["rows"]
            await tb.rss_poll(make_context(bot))
            sends = bot.called("send_message")
            self.assertEqual(len(sends), 1)
            self.assertIn("Four", sends[0][0][1])
            await tb.rss_poll(make_context(bot))
            self.assertEqual(len(bot.called("send_message")), 1)                   # not delivered twice

    async def test_failing_feed_is_dropped_after_repeated_failures(self):
        async def boom(url):
            raise ValueError("down")

        async with async_session() as s:
            s.add(RssFeed(chat_id=self.CHAT, url="http://dead.example.com/f", last_entry="x", failures=tb.MAX_FAILURES - 1))
            await s.commit()
        with mock.patch.object(tb, "_fetch_feed", boom):
            await tb.rss_poll(make_context(self._bot()))
        async with async_session() as s:
            self.assertEqual((await s.execute(select(RssFeed).where(RssFeed.chat_id == self.CHAT))).all(), [])

    async def test_private_feed_urls_are_refused_on_add(self):
        bot = self._bot()
        upd = make_update("/addrss", user_id=920_001, chat_id=self.CHAT)
        await tb.addrss_cmd(upd, make_context(bot, ["http://127.0.0.1/feed"]))
        self.assertRegex(upd.effective_message.reply_text.await_args.args[0], r"Couldn't read|valid web address")
        async with async_session() as s:
            self.assertEqual((await s.execute(select(RssFeed).where(RssFeed.chat_id == self.CHAT))).all(), [])

    async def test_non_admin_cannot_add(self):
        upd = make_update("/addrss", user_id=920_555, chat_id=self.CHAT)
        await tb.addrss_cmd(upd, make_context(self._bot(), ["http://example.com/f"]))
        async with async_session() as s:
            self.assertEqual((await s.execute(select(RssFeed).where(RssFeed.chat_id == self.CHAT))).all(), [])


class TestRegistration(unittest.TestCase):
    def test_commands_and_groups(self):
        added = []
        tb.register(SimpleNamespace(add_handler=lambda h, group=0: added.append((h, group)), job_queue=None))
        self.assertIn(16, [g for _, g in added])
        names = set()
        for h, _ in added:
            names |= set(getattr(h, "commands", ()) or ())
        for c in ("paste", "wiki", "github", "country", "covid", "setme", "me", "setbio", "bio", "encrypt", "decrypt", "addrss", "removerss", "rss"):
            self.assertIn(c, names)


if __name__ == "__main__":
    unittest.main()
