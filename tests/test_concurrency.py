"""Tests for bot/core/concurrency.py — per-chat ordering with cross-chat parallelism.

``KeyedSerializer`` and ``update_serial_key`` are dependency-free, so these run without
python-telegram-bot installed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.core.concurrency import KeyedSerializer, build_update_processor, update_serial_key  # noqa: E402


class TestKeyedSerializer(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_runs_in_arrival_order_one_at_a_time(self):
        s = KeyedSerializer()
        log, running, peak = [], 0, 0

        async def job(n):
            nonlocal running, peak
            async with s.hold("chat"):
                running += 1
                peak = max(peak, running)
                log.append(("start", n))
                await asyncio.sleep(0.01)
                log.append(("end", n))
                running -= 1

        await asyncio.gather(*(job(n) for n in range(5)))
        self.assertEqual(peak, 1)
        self.assertEqual([n for kind, n in log if kind == "start"], [0, 1, 2, 3, 4])
        self.assertEqual(log, [(k, n) for n in range(5) for k in ("start", "end")])

    async def test_different_keys_overlap(self):
        s = KeyedSerializer()
        started = asyncio.Event()
        slow_release = asyncio.Event()

        async def slow():
            async with s.hold("A"):
                started.set()
                await slow_release.wait()

        async def other():
            await started.wait()
            async with s.hold("B"):
                return "ran while A was busy"

        task = asyncio.create_task(slow())
        self.assertEqual(await asyncio.wait_for(other(), timeout=1), "ran while A was busy")
        slow_release.set()
        await task

    async def test_no_state_is_left_behind(self):
        s = KeyedSerializer()

        async def job(key):
            async with s.hold(key):
                await asyncio.sleep(0)

        await asyncio.gather(*(job(k % 7) for k in range(50)))
        self.assertEqual(s.active_keys(), 0)

    async def test_failure_releases_the_turn(self):
        s = KeyedSerializer()
        with self.assertRaises(RuntimeError):
            async with s.hold("k"):
                raise RuntimeError("boom")
        async with s.hold("k"):
            pass
        self.assertEqual(s.active_keys(), 0)

    async def test_cancelled_waiter_does_not_wedge_the_key(self):
        s = KeyedSerializer()
        gate = asyncio.Event()

        async def holder():
            async with s.hold("k"):
                await gate.wait()

        async def waiter():
            async with s.hold("k"):
                return "never"

        h = asyncio.create_task(holder())
        await asyncio.sleep(0)
        w = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        w.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await w
        gate.set()
        await h
        async with s.hold("k"):
            pass
        self.assertEqual(s.active_keys(), 0)


# The processor wrapper subclasses python-telegram-bot's BaseUpdateProcessor; the
# serializer tested above has no such dependency.
HAVE_PTB = importlib.util.find_spec("telegram") is not None


def _update(chat_id):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), effective_user=SimpleNamespace(id=1))


@unittest.skipUnless(HAVE_PTB, "python-telegram-bot not installed")
class TestPerChatUpdateProcessor(unittest.IsolatedAsyncioTestCase):
    async def test_same_chat_ordered_other_chats_parallel(self):
        proc = build_update_processor(8)
        log, running = [], {"A": 0, "peak_A": 0}

        def handler(chat, n, delay):
            async def run():
                if chat == "A":
                    running["A"] += 1
                    running["peak_A"] = max(running["peak_A"], running["A"])
                log.append(("start", chat, n))
                await asyncio.sleep(delay)
                log.append(("end", chat, n))
                if chat == "A":
                    running["A"] -= 1
            return run()

        jobs = [proc.process_update(_update("A"), handler("A", n, 0.02)) for n in range(3)]
        jobs.append(proc.process_update(_update("B"), handler("B", 0, 0.005)))
        await asyncio.gather(*jobs)
        self.assertEqual(running["peak_A"], 1)                                    # one at a time per chat
        a_starts = [n for kind, c, n in log if kind == "start" and c == "A"]
        self.assertEqual(a_starts, [0, 1, 2])                                     # arrival order kept
        self.assertLess(log.index(("end", "B", 0)), log.index(("end", "A", 0)))   # B was not stuck behind A

    async def test_a_busy_chat_cannot_starve_the_global_slots(self):
        """Queued updates of one chat wait for their chat turn *before* taking a slot."""
        proc = build_update_processor(2)
        release_a, b_done = asyncio.Event(), asyncio.Event()

        async def slow_a():
            await release_a.wait()

        async def quick_b():
            b_done.set()

        tasks = [asyncio.create_task(proc.process_update(_update("A"), slow_a())) for _ in range(5)]
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(proc.process_update(_update("B"), quick_b())))
        await asyncio.wait_for(b_done.wait(), timeout=1)   # would hang if A's backlog hogged both slots
        release_a.set()
        await asyncio.gather(*tasks)

    async def test_updates_without_chat_or_user_still_run(self):
        proc = build_update_processor(2)
        ran = []

        async def job():
            ran.append(1)

        await proc.process_update(object(), job())
        self.assertEqual(ran, [1])

    async def test_global_limit_is_respected(self):
        proc = build_update_processor(2)
        running = peak = 0

        async def job():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

        await asyncio.gather(*(proc.process_update(_update(c), job()) for c in range(6)))
        self.assertEqual(peak, 2)

    async def test_lifecycle_hooks_are_noops(self):
        proc = build_update_processor(1)
        await proc.initialize()
        await proc.shutdown()


class TestSerialKey(unittest.TestCase):
    def test_chat_updates_share_the_chat_id(self):
        u = SimpleNamespace(effective_chat=SimpleNamespace(id=-100), effective_user=SimpleNamespace(id=5))
        self.assertEqual(update_serial_key(u), ("chat", -100))

    def test_chatless_updates_fall_back_to_the_user(self):
        u = SimpleNamespace(effective_chat=None, effective_user=SimpleNamespace(id=5))
        self.assertEqual(update_serial_key(u), ("user", 5))

    def test_anonymous_updates_are_not_serialised(self):
        self.assertIsNone(update_serial_key(SimpleNamespace(effective_chat=None, effective_user=None)))
        self.assertIsNone(update_serial_key(object()))


if __name__ == "__main__":
    unittest.main()
