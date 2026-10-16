"""Whisper port: parsing, atomic single-read claims, storage, inline results, callback and PM flows."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_whisper_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP.name}"

from _fakes import FakeBot, make_context  # noqa: E402

from bot.core import inline as router  # noqa: E402
from bot.core.database import init_db  # noqa: E402
from bot.plugins import whisper as wp  # noqa: E402
from bot.utils import whisper_store as store  # noqa: E402
from bot.utils import whisper_text as wt  # noqa: E402
from bot.utils.whisper_store import MediaItem, WhisperData  # noqa: E402


class TestParsing(unittest.TestCase):
    def test_recipient_formats(self):
        r = wt.extract_recipient("@alice hello there")
        self.assertEqual((r.username, r.text), ("@alice", "hello there"))
        r = wt.extract_recipient("hello there @alice")
        self.assertEqual((r.username, r.text), ("@alice", "hello there"))
        r = wt.extract_recipient("123456789 psst")
        self.assertEqual((r.user_id, r.text), (123456789, "psst"))
        r = wt.extract_recipient("psst 123456789")
        self.assertEqual((r.user_id, r.text), (123456789, "psst"))

    def test_everyone_and_small_numbers_are_text(self):
        for text in ("see you at 5", "7 is lucky", "just text", "@ab short handle"):
            r = wt.extract_recipient(text)
            self.assertTrue(r.is_everyone, text)
            self.assertEqual(r.text, text)
        self.assertIsNone(wt.extract_recipient("   "))

    def test_username_validation(self):
        self.assertTrue(wt.is_valid_username("@abcd"))
        self.assertFalse(wt.is_valid_username("@abc"))
        self.assertFalse(wt.is_valid_username("@1abcd"))
        self.assertFalse(wt.is_valid_username("abcd"))

    def test_advanced_tokens_and_start_payloads(self):
        uid = wt.new_unique_id()
        self.assertTrue(wt.is_advanced_inline(wt.advanced_query(uid)))
        self.assertEqual(wt.advanced_unique_id(wt.advanced_query(uid)), uid)
        self.assertEqual(wt.advanced_unique_id("-wh::bad id::"), "")
        self.assertEqual(wt.parse_start_payload(wt.start_payload(uid)), uid)
        self.assertEqual(wt.parse_start_payload("create"), "")
        self.assertRegex(wt.start_payload(uid), r"^[A-Za-z0-9_-]{1,64}$")  # legal Telegram deep-link payload
        self.assertEqual(len({wt.new_unique_id() for _ in range(500)}), 500)

    def test_toggle(self):
        self.assertIs(wt.parse_toggle("ON"), True)
        self.assertIs(wt.parse_toggle("disable"), False)
        self.assertIsNone(wt.parse_toggle("maybe"))


class TestSemantics(unittest.TestCase):
    def test_can_read(self):
        w = WhisperData("a", sender=1, recipient=2)
        self.assertTrue(w.can_read(1, None) and w.can_read(2, None))
        self.assertFalse(w.can_read(3, "x"))
        w = WhisperData("a", sender=1, recipient_username="@Alice")
        self.assertTrue(w.can_read(5, "alice"))
        self.assertFalse(w.can_read(5, None))
        self.assertFalse(w.can_read(5, "bob"))
        self.assertTrue(WhisperData("a", sender=1).can_read(9, None))

    def test_redirect_rules(self):
        self.assertFalse(WhisperData("a", 1, text="x" * 199).should_redirect)
        self.assertTrue(WhisperData("a", 1, text="x" * 200).should_redirect)
        self.assertTrue(WhisperData("a", 1, type=int(wt.WhisperType.PHOTO)).should_redirect)


class TestStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()

    async def test_roundtrip_album_and_single_read_claim(self):
        w = WhisperData(wt.new_unique_id(), 1, text="cap", recipient=2, type=int(wt.WhisperType.PHOTO),
                        album=[MediaItem(2, "f1", "a"), MediaItem(3, "f2", "b")])
        await store.add_whisper(w)
        got = await store.get_whisper(w.unique_id)
        self.assertEqual([(i.type, i.file_id, i.caption) for i in got.album], [(2, "f1", "a"), (3, "f2", "b")])
        import asyncio
        wins = await asyncio.gather(*[store.claim_whisper(w.unique_id) for _ in range(8)])
        self.assertEqual(sum(wins), 1)
        self.assertIsNone(await store.get_whisper(w.unique_id))
        await store.restore_whisper(got)
        await store.restore_whisper(got)  # duplicate restore is harmless
        self.assertIsNotNone(await store.get_whisper(w.unique_id))

    async def test_expiry_and_sender_delete(self):
        old = WhisperData(wt.new_unique_id(), 10, created_ts=int(time.time()) - 20 * 86400)
        fresh = WhisperData(wt.new_unique_id(), 10)
        await store.add_whisper(old)
        await store.add_whisper(fresh)
        self.assertGreaterEqual(await store.purge_expired(14 * 86400), 1)
        self.assertIsNone(await store.get_whisper(old.unique_id))
        self.assertFalse(await store.delete_whisper_of_sender(fresh.unique_id, 999))
        self.assertTrue(await store.delete_whisper_of_sender(fresh.unique_id, 10))

    async def test_history_keeps_most_recent_distinct(self):
        owner = 5000 + os.getpid() % 1000
        for target in (11, 12, 13, 14, 12):
            await store.save_history(owner, target, f"n{target}", keep=3)
            time.sleep(1.01)
        ids = [h.target_id for h in await store.get_history(owner, 3)]
        self.assertEqual(ids, [12, 14, 13])
        await store.save_history(owner, owner, "me", keep=3)  # never yourself
        self.assertNotIn(owner, [h.target_id for h in await store.get_history(owner, 3)])
        await store.set_pref(owner, "history_disabled", True)
        self.assertEqual(await store.get_history(owner, 3), [])
        self.assertTrue(await store.clear_history(owner))

    async def test_prefs(self):
        await store.set_pref(777, "privacy_mode", True)
        await store.set_pref(777, "notify_on_read", True)
        p = await store.get_prefs(777)
        self.assertTrue(p.privacy_mode and p.notify_on_read and not p.banned)
        with self.assertRaises(ValueError):
            await store.set_pref(777, "nonsense", True)


class TestInlineRouter(unittest.TestCase):
    C = router.InlineContribution

    def test_exclusive_wins(self):
        results, button, personal, cache = router.merge([self.C(results=["char"]), self.C(results=["w"], exclusive=True, personal=True, cache_time=0)])
        self.assertEqual((results, personal, cache), (["w"], True, 0))

    def test_ordering_and_defaults(self):
        results, _b, personal, cache = router.merge([
            self.C(results=["after"], placement=router.AFTER, personal=True, cache_time=0),
            self.C(results=["normal"]), self.C(results=["before"], placement=router.BEFORE)])
        self.assertEqual(results, ["before", "normal", "after"])
        self.assertTrue(personal)
        self.assertEqual(router.merge([self.C(results=["x"])])[3], router.DEFAULT_CACHE_SECONDS)

    def test_cap_and_button(self):
        results, button, *_ = router.merge([self.C(results=list(range(80)), button="b")])
        self.assertEqual((len(results), button), (50, "b"))


class TestInlineDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_failing_provider_is_isolated(self):
        saved = dict(router._providers)
        router._providers.clear()
        try:
            async def bad(*a):
                raise RuntimeError("x")

            async def good(*a):
                return router.InlineContribution(results=["ok"])

            router.register_provider("bad", bad, 1)
            router.register_provider("good", good, 2)
            answer = AsyncMock()
            await router.dispatch(NS(inline_query=NS(query=" hi ", answer=answer)), None)
            self.assertEqual(answer.call_args[0][0], ["ok"])
        finally:
            router._providers.clear()
            router._providers.update(saved)


def _inline_update(query, uid=1, qid="q1"):
    return NS(inline_query=NS(query=query, id=qid, from_user=NS(id=uid, first_name="S", username="sender")))


class TestWhisperInline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        wp.reset_state_for_tests()
        self.ctx = make_context(FakeBot())

    async def test_recipient_query_is_exclusive_placeholder(self):
        c = await wp.whisper_inline_provider(_inline_update("@alice hello"), self.ctx, "@alice hello")
        self.assertTrue(c.exclusive)
        self.assertEqual(c.results[0].id, "wsn:q1")

    async def test_plain_text_is_offered_after_other_results(self):
        c = await wp.whisper_inline_provider(_inline_update("pikachu"), self.ctx, "pikachu")
        self.assertFalse(c.exclusive)
        self.assertEqual(c.placement, router.AFTER)

    async def test_empty_and_too_long(self):
        c = await wp.whisper_inline_provider(_inline_update(""), self.ctx, "")
        self.assertEqual((c.results, c.button.start_parameter), ([], wt.START_HELP_INLINE))
        c = await wp.whisper_inline_provider(_inline_update("x" * 300), self.ctx, "x" * 300)
        self.assertEqual(c.button.start_parameter, wt.START_CREATE)

    async def test_advanced_only_for_sender_and_once(self):
        w = WhisperData(wt.new_unique_id(), 1, text="long", recipient_username="@alice")
        await store.add_whisper(w)
        q = wt.advanced_query(w.unique_id)
        ok = await wp.whisper_inline_provider(_inline_update(q, uid=1), self.ctx, q)
        self.assertEqual(ok.results[0].id, wt.ADVANCED_RESULT_PREFIX + w.unique_id)
        other = await wp.whisper_inline_provider(_inline_update(q, uid=2), self.ctx, q)
        self.assertEqual(other.results, [])
        await store.set_inline_message_id(w.unique_id, "IM")
        again = await wp.whisper_inline_provider(_inline_update(q, uid=1), self.ctx, q)
        self.assertEqual(again.results, [])

    async def test_chosen_creates_whisper_and_edits_placeholder(self):
        bot = FakeBot()
        res = NS(result_id="wsn:q1", from_user=NS(id=41), query="@bobby the secret", inline_message_id="IMID")
        await wp.chosen_inline_result(NS(chosen_inline_result=res), make_context(bot))
        edits = bot.called("edit_message_text")
        self.assertEqual(len(edits), 1)
        markup = edits[0][1]["reply_markup"]
        uid = markup.inline_keyboard[0][0].callback_data[len(wp.SHOW_PREFIX):]
        w = await store.get_whisper(uid)
        self.assertEqual((w.sender, w.text, w.recipient_username, w.inline_message_id), (41, "the secret", "@bobby", "IMID"))
        foreign = NS(result_id="123", from_user=NS(id=41), query="x", inline_message_id="I2")
        await wp.chosen_inline_result(NS(chosen_inline_result=foreign), make_context(bot))  # collector card: ignored
        self.assertEqual(len(bot.called("edit_message_text")), 1)

    async def test_history_result_id_is_validated(self):
        self.assertEqual(wp.parse_history_result_id("wsh::5::9::abc"), (5, 9))
        self.assertEqual(wp.parse_history_result_id("wsh::x::9::abc"), (0, 0))
        bot = FakeBot()
        spoof = NS(result_id="wsh::999::9::abc", from_user=NS(id=41), query="hi", inline_message_id="I")
        await wp.chosen_inline_result(NS(chosen_inline_result=spoof), make_context(bot))
        self.assertEqual(bot.called("edit_message_text"), [])


def _cb_update(data, uid, username=None, inline_id="INL"):
    q = NS(data=data, from_user=NS(id=uid, first_name=f"U{uid}", username=username), inline_message_id=inline_id,
           message=None, answer=AsyncMock(), edit_message_text=AsyncMock())
    return NS(callback_query=q, effective_user=q.from_user), q


class TestShowCallback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        wp.reset_state_for_tests()
        self.bot = FakeBot()
        self.bot.username = "nekobot"
        self.ctx = make_context(self.bot)

    async def _make(self, **kw):
        w = WhisperData(wt.new_unique_id(), sender=1, text="the secret", inline_message_id="INL", **kw)
        await store.add_whisper(w)
        return w

    async def test_recipient_reads_once_and_chat_message_is_edited(self):
        w = await self._make(recipient=2)
        update, q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 2)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertEqual(q.answer.call_args[0][0], "the secret")
        self.assertTrue(q.answer.call_args[1]["show_alert"])
        self.assertIsNone(await store.get_whisper(w.unique_id))
        self.assertIn("read the whisper", self.bot.called("edit_message_text")[0][1]["text"])
        update2, q2 = _cb_update(wp.SHOW_PREFIX + w.unique_id, 2)
        await wp.show_whisper_callback(update2, self.ctx)
        self.assertIn("too old", q2.answer.call_args[0][0])

    async def test_stranger_is_refused_and_whisper_survives(self):
        w = await self._make(recipient=2)
        update, q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 3)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertIn("not for you", q.answer.call_args[0][0])
        self.assertIsNotNone(await store.get_whisper(w.unique_id))

    async def test_sender_rereads_without_consuming(self):
        w = await self._make(recipient=2)
        update, q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 1)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertEqual(q.answer.call_args[0][0], "the secret")
        self.assertIsNotNone(await store.get_whisper(w.unique_id))

    async def test_privacy_mode_reads_silently_but_still_consumes(self):
        w = await self._make(recipient=22)
        await store.set_pref(22, "privacy_mode", True)
        update, q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 22)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertEqual(self.bot.called("edit_message_text"), [])
        self.assertIsNone(await store.get_whisper(w.unique_id))

    async def test_username_recipient_and_everyone_first_reader_wins(self):
        w = await self._make(recipient_username="@Carol")
        bad, qb = _cb_update(wp.SHOW_PREFIX + w.unique_id, 5, "dave")
        await wp.show_whisper_callback(bad, self.ctx)
        self.assertIn("not for you", qb.answer.call_args[0][0])
        good, qg = _cb_update(wp.SHOW_PREFIX + w.unique_id, 6, "carol")
        await wp.show_whisper_callback(good, self.ctx)
        self.assertEqual(qg.answer.call_args[0][0], "the secret")
        e = await self._make()
        a, qa = _cb_update(wp.SHOW_PREFIX + e.unique_id, 7)
        b, qb2 = _cb_update(wp.SHOW_PREFIX + e.unique_id, 8)
        await wp.show_whisper_callback(a, self.ctx)
        await wp.show_whisper_callback(b, self.ctx)
        self.assertEqual(qa.answer.call_args[0][0], "the secret")
        self.assertIn("already been read", qb2.answer.call_args[0][0])

    async def test_media_redirects_to_pm_without_consuming(self):
        w = await self._make(recipient=2, type=int(wt.WhisperType.PHOTO), file_id="F")
        update, q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 2)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertEqual(q.answer.call_args[1]["url"], f"https://t.me/nekobot?start=w_{w.unique_id}")
        self.assertIsNotNone(await store.get_whisper(w.unique_id))

    async def test_unborn_and_banned(self):
        update, q = _cb_update(wp.SHOW_PREFIX, 2)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertIn("not generated", q.answer.call_args[0][0])
        w = await self._make(recipient=33)
        await store.set_pref(33, "banned", True)
        update, q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 33)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertIn("can't open", q.answer.call_args[0][0])

    async def test_failed_answer_restores_the_whisper(self):
        from telegram.error import NetworkError
        w = await self._make(recipient=44)
        update, q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 44)
        q.answer = AsyncMock(side_effect=[NetworkError("boom"), None])
        await wp.show_whisper_callback(update, self.ctx)
        self.assertIsNotNone(await store.get_whisper(w.unique_id))

    async def test_notify_sender_on_read(self):
        w = await self._make(recipient=55)
        await store.set_pref(1, "notify_on_read", True)
        update, _q = _cb_update(wp.SHOW_PREFIX + w.unique_id, 55)
        await wp.show_whisper_callback(update, self.ctx)
        self.assertEqual(self.bot.called("send_message")[0][0][0], 1)


class TestPmFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        wp.reset_state_for_tests()
        self.bot = FakeBot()
        self.bot.username = "nekobot"

    def _msg(self, text, uid=9):
        reply = AsyncMock()
        msg = NS(text=text, caption=None, photo=None, entities=[], media_group_id=None, chat_id=uid, message_id=1,
                 reply_text=reply, get_bot=lambda: self.bot, video=None, audio=None, voice=None, sticker=None,
                 document=None, video_note=None, animation=None, dice=None)
        upd = NS(effective_user=NS(id=uid, first_name="Me", username="me"), effective_message=msg,
                 effective_chat=NS(id=uid, type="private"), message=msg)
        return upd, reply

    async def test_compose_text_whisper_end_to_end(self):
        ctx = make_context(self.bot)
        upd, reply = self._msg("/whisper")
        await wp.create_cmd(upd, ctx)
        self.assertEqual(wp._compose[9].stage, "target")
        for text in ("not a target", "@alice_w"):
            upd, reply = self._msg(text)
            with self.assertRaises(wp.ApplicationHandlerStop):
                await wp.compose_listener(upd, ctx)
        self.assertEqual(wp._compose[9].target_username, "@alice_w")
        upd, reply = self._msg("a very long secret " * 30)
        with self.assertRaises(wp.ApplicationHandlerStop):
            await wp.compose_listener(upd, ctx)
        self.assertNotIn(9, wp._compose)
        listed = await store.list_whispers(9)
        self.assertEqual((listed[0].recipient_username, listed[0].text.startswith("a very long")), ("@alice_w", True))
        sent = self.bot.called("send_message")[-1][1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(sent.switch_inline_query, wt.advanced_query(listed[0].unique_id))

    async def test_listener_ignores_users_who_are_not_composing(self):
        upd, _ = self._msg("hello", uid=12345)
        await wp.compose_listener(upd, make_context(self.bot))  # no stop raised

    async def test_cancel(self):
        ctx = make_context(self.bot)
        upd, reply = self._msg("/whisper", uid=8)
        await wp.create_cmd(upd, ctx)
        upd, reply = self._msg("/cancel", uid=8)
        await wp.cancel_cmd(upd, ctx)
        self.assertNotIn(8, wp._compose)
        self.assertIn("cancelled", reply.call_args[0][0])

    async def test_pm_delivery_consumes_and_is_html_safe(self):
        w = WhisperData(wt.new_unique_id(), sender=1, text="<b>hi</b> & bye", recipient=9, inline_message_id="INL")
        await store.add_whisper(w)
        upd, reply = self._msg("/start", uid=9)
        ctx = make_context(self.bot, args=[wt.start_payload(w.unique_id)])
        self.assertTrue(await wp.handle_start_payload(upd, ctx))
        body = reply.call_args[0][0]
        self.assertIn("&lt;b&gt;hi&lt;/b&gt; &amp; bye", body)
        self.assertIsNone(await store.get_whisper(w.unique_id))
        upd2, reply2 = self._msg("/start", uid=9)
        self.assertTrue(await wp.handle_start_payload(upd2, ctx))
        self.assertIn("too old", reply2.call_args[0][0])

    async def test_media_album_is_sent_with_per_item_types(self):
        w = WhisperData(wt.new_unique_id(), sender=1, recipient=9, type=2,
                        album=[MediaItem(2, "p", "c1"), MediaItem(3, "v", "")])
        await store.add_whisper(w)
        upd, _ = self._msg("/start", uid=9)
        await wp.handle_start_payload(upd, make_context(self.bot, args=[wt.start_payload(w.unique_id)]))
        group = self.bot.called("send_media_group")[0][0][1]
        self.assertEqual([type(m).__name__ for m in group], ["InputMediaPhoto", "InputMediaVideo"])

    async def test_non_whisper_payload_falls_through(self):
        upd, _ = self._msg("/start", uid=9)
        self.assertFalse(await wp.handle_start_payload(upd, make_context(self.bot, args=["ref123"])))

    async def test_privacy_command_and_list_delete(self):
        ctx = make_context(self.bot, args=["on"])
        upd, reply = self._msg("/privacy on", uid=66)
        await wp.privacy_cmd(upd, ctx)
        self.assertTrue((await store.get_prefs(66)).privacy_mode)
        w = WhisperData(wt.new_unique_id(), sender=66, text="x")
        await store.add_whisper(w)
        upd, reply = self._msg("/list", uid=66)
        await wp.list_cmd(upd, make_context(self.bot))
        self.assertIn("Your pending whispers", reply.call_args[0][0])
        upd_cb, q = _cb_update(f"ws:del:{w.unique_id}", 66)
        await wp.ws_callback(upd_cb, make_context(self.bot))
        self.assertIsNone(await store.get_whisper(w.unique_id))


if __name__ == "__main__":
    unittest.main()
