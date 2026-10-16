"""Tests for the expanded personality pack (skins/presets library) and
bot/utils/ratelimit.py.

The skins/presets themselves ship as data, not code, so these tests
check the two things that actually matter: every shipped file is valid
and loads through the real loaders (SkinLoader / PresetLoader), and the
rate limiter's pass/block arithmetic is correct — rather than re-testing
the content of any single persona.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_personapack_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

from bot.personality.custom_instructions import instruction_processor  # noqa: E402
from bot.personality.personality import PersonalityLayer  # noqa: E402
from bot.utils import ratelimit  # noqa: E402
from bot.utils.cache import cache  # noqa: E402

SKINS_DIR = PROJECT_ROOT / "data" / "personality" / "skins"
PRESETS_DIR = PROJECT_ROOT / "data" / "personality" / "presets"

# Every skin must at least have these — SkinLoader and the response
# pipeline read them with .get(..., default) fallbacks, so a skin isn't
# REQUIRED to have every optional field, but a skin with none of its
# core identity fields is a malformed file, not a minimal one.
REQUIRED_SKIN_KEYS = {"name", "description"}
REQUIRED_PRESET_KEYS = {"persona_context"}


class TestSkinFiles(unittest.TestCase):
    def test_every_skin_file_is_valid_json_with_required_keys(self):
        files = list(SKINS_DIR.glob("*.json"))
        self.assertGreater(len(files), 20, "expected the expanded skin library, not just the original few")
        for path in files:
            with self.subTest(skin=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(REQUIRED_SKIN_KEYS.issubset(data.keys()), f"{path.name} missing required keys")

    def test_skin_loader_loads_every_shipped_skin(self):
        layer = PersonalityLayer()
        for name in layer.available_skins():
            with self.subTest(skin=name):
                data = layer._skin_loader.load(name)
                self.assertIsInstance(data, dict)

    def test_unknown_skin_falls_back_to_neko_rather_than_raising(self):
        layer = PersonalityLayer()
        data = layer._skin_loader.load("this_skin_does_not_exist")
        self.assertEqual(data.get("name"), "neko")

    def test_no_leftover_pre_rebrand_branding_in_any_skin(self):
        for path in SKINS_DIR.glob("*.json"):
            if path.stem == "nanora":
                continue  # explicitly kept as a labeled legacy/nostalgia skin
            text = path.read_text(encoding="utf-8")
            with self.subTest(skin=path.stem):
                self.assertNotIn("@nanorabot", text)


class TestPresetFiles(unittest.TestCase):
    def test_every_preset_file_is_valid_json(self):
        files = list(PRESETS_DIR.glob("*.json"))
        self.assertGreater(len(files), 20, "expected the expanded preset library")
        for path in files:
            with self.subTest(preset=path.stem):
                json.loads(path.read_text(encoding="utf-8"))  # just must parse

    def test_preset_loader_exposes_all_shipped_presets(self):
        names = set(instruction_processor.available_presets())
        on_disk = {p.stem for p in PRESETS_DIR.glob("*.json")}
        self.assertTrue(on_disk.issubset(names))


class TestRateLimit(unittest.IsolatedAsyncioTestCase):
    async def test_allows_up_to_the_limit_then_blocks(self):
        key = "test:allow_then_block"
        results = [await ratelimit.hit(key, limit=3, window_seconds=60) for _ in range(5)]
        allowed = [ok for ok, _ in results]
        self.assertEqual(allowed, [True, True, True, False, False])

    async def test_counts_are_monotonically_increasing(self):
        key = "test:monotonic_count"
        counts = [(await ratelimit.hit(key, limit=100, window_seconds=60))[1] for _ in range(4)]
        self.assertEqual(counts, [1, 2, 3, 4])

    async def test_zero_or_negative_limit_always_allows(self):
        ok, count = await ratelimit.hit("test:zero_limit", limit=0, window_seconds=60)
        self.assertTrue(ok)
        self.assertEqual(count, 0)

    async def test_remaining_reflects_hits_so_far(self):
        key = "test:remaining"
        await ratelimit.hit(key, limit=10, window_seconds=60)
        await ratelimit.hit(key, limit=10, window_seconds=60)
        self.assertEqual(await ratelimit.remaining(key, limit=10), 8)

    async def test_cache_failure_fails_open_rather_than_blocking_everyone(self):
        # A rate limiter that blocks all traffic when its backing store is
        # briefly unavailable is worse than no rate limiter at all.
        async def _boom(*a, **k):
            raise ConnectionError("cache unavailable")

        original = cache.incr
        cache.incr = _boom
        try:
            ok, count = await ratelimit.hit("test:cache_down", limit=1, window_seconds=60)
            self.assertTrue(ok)
        finally:
            cache.incr = original


if __name__ == "__main__":
    unittest.main()
