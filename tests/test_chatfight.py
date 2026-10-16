"""ChatFight port: counting, flood guard, lazy day/week rollover, leaderboards with pending counts."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_chatfight_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP.name}"

from bot.core.database import init_db  # noqa: E402
from bot.plugins import chatfight as cf  # noqa: E402


def u(uid, name="N", username=None, bot=False):
    return NS(id=uid, first_name=name, username=username, is_bot=bot)


class TestPeriodKeys(unittest.TestCase):
    def test_iso_week_spans_new_year(self):
        # 2026-12-31 and 2027-01-01 are the same ISO week; ``%Y-%W`` would split them.
        a = cf.period_keys(datetime(2026, 12, 31, 12, tzinfo=timezone.utc), "UTC")
        b = cf.period_keys(datetime(2027, 1, 1, 12, tzinfo=timezone.utc), "UTC")
        self.assertEqual(a[1], b[1])
        self.assertNotEqual(a[0], b[0])

    def test_zone_decides_the_day(self):
        moment = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)  # already Oct 11 in IST
        self.assertEqual(cf.period_keys(moment, "Asia/Kolkata")[0], "2026-10-11")
        self.assertEqual(cf.period_keys(moment, "UTC")[0], "2026-10-10")

    def test_bad_zone_falls_back_to_utc(self):
        self.assertEqual(cf.period_keys(datetime(2026, 1, 1, tzinfo=timezone.utc), "Not/AZone")[0], "2026-01-01")


class TestGuard(unittest.TestCase):
    def setUp(self):
        cf.reset_state_for_tests()

    def test_flood_blocks_then_recovers(self):
        user = u(9)
        out = [cf.record_message(-5, user, "G", now=1 + i * 0.1) for i in range(12)]
        self.assertEqual(out[:9], [cf.COUNTED] * 9)
        self.assertEqual(out[9], cf.FLOODING)
        self.assertEqual(out[10:], [cf.SKIPPED, cf.SKIPPED])
        self.assertEqual(cf.pending_for_chat(-5)[9].count, 9)
        self.assertEqual(cf.record_message(-5, user, "G", now=5000), cf.COUNTED)

    def test_prune_bounds_memory(self):
        cf.record_message(-5, u(1), "G", now=1)
        cf.prune_guard_state(now=10_000)
        self.assertEqual(len(cf._recent), 0)


class TestPersistence(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        cf.reset_state_for_tests()
        self.chat = -1000 - id(self) % 100000

    async def test_flush_then_read_merges_pending(self):
        for i in range(3):
            cf.record_message(self.chat, u(1, "A", "alice"), "G", now=100 + i * 100)
        cf.record_message(self.chat, u(2, "B"), "G", now=1000)
        self.assertEqual(await cf.flush_counts(), 2)
        for i in range(5):  # B overtakes A while the new counts are still in memory
            cf.record_message(self.chat, u(2, "B"), "G", now=2000 + i * 100)
        rows, total = await cf.leaderboard(self.chat, "overall")
        self.assertEqual([(r.user_id, r.count) for r in rows], [(2, 6), (1, 3)])
        self.assertEqual(total, 9)
        today, _ = await cf.leaderboard(self.chat, "today")
        self.assertEqual([r.count for r in today], [6, 3])

    async def test_pending_only_member_is_ranked(self):
        cf.record_message(self.chat, u(7, "P"), "G", now=1)
        rows, total = await cf.leaderboard(self.chat, "week")
        self.assertEqual([(r.user_id, r.count) for r in rows], [(7, 1)])
        self.assertEqual(total, 1)

    async def test_stale_period_counts_as_zero_and_resets_on_write(self):
        from sqlalchemy import update
        from bot.core.database import ActivityStat, async_session

        cf.record_message(self.chat, u(1, "A"), "G", now=1)
        await cf.flush_counts()
        async with async_session() as s:  # pretend the stored counters belong to an earlier day/week
            await s.execute(update(ActivityStat).where(ActivityStat.chat_id == self.chat).values(day_key="2000-01-01", week_key="2000-W01"))
            await s.commit()
        self.assertEqual((await cf.leaderboard(self.chat, "today"))[0], [])
        self.assertEqual((await cf.leaderboard(self.chat, "overall"))[0][0].count, 1)
        cf.record_message(self.chat, u(1, "A"), "G", now=2)
        await cf.flush_counts()
        today = (await cf.leaderboard(self.chat, "today"))[0]
        self.assertEqual(today[0].count, 1)  # reset, not 2
        self.assertEqual((await cf.leaderboard(self.chat, "overall"))[0][0].count, 2)

    async def test_failed_flush_keeps_the_counts(self):
        cf.record_message(self.chat, u(1, "A"), "G", now=1)
        original = cf.async_session

        def boom():
            raise RuntimeError("db down")

        cf.async_session = boom
        try:
            self.assertEqual(await cf.flush_counts(), 0)
        finally:
            cf.async_session = original
        cf.record_message(self.chat, u(1, "A"), "G", now=2)
        self.assertEqual(cf.pending_for_chat(self.chat)[1].count, 2)
        self.assertEqual(await cf.flush_counts(), 1)
        self.assertEqual((await cf.member_total(self.chat, 1)).count, 2)

    async def test_concurrent_flushes_do_not_lose_or_double_count(self):
        for i in range(10):
            cf.record_message(self.chat, u(3, "C"), "G", now=i * 100)
        await asyncio.gather(cf.flush_counts(), cf.flush_counts())
        self.assertEqual((await cf.member_total(self.chat, 3)).count, 10)

    async def test_lookup_by_username_and_mytop(self):
        cf.record_message(self.chat, u(4, "D", "Dee"), "Group D", now=1)
        await cf.flush_counts()
        self.assertEqual((await cf.find_member_by_username(self.chat, "@dEE")).user_id, 4)
        self.assertIsNone(await cf.find_member_by_username(self.chat, "@nobody"))
        ranked = await cf.my_top(4, "overall")
        self.assertIn((self.chat, "Group D", 1), ranked)


class TestPresentation(unittest.TestCase):
    def test_names_are_escaped(self):
        text = cf.format_leaderboard("overall", [cf.Row(1, "<b>x</b>", None, 3)], 3)
        self.assertNotIn("<b>x</b>", text.replace("<b>1.", ""))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)

    def test_images_render(self):
        png = cf.render_leaderboard_png([cf.Row(1, "Alice", None, 9), cf.Row(2, "Bob", None, 4)])
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertTrue(cf.render_stats_png(["a", "b"]).startswith(b"\x89PNG"))
        self.assertTrue(cf.render_leaderboard_png([]).startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
