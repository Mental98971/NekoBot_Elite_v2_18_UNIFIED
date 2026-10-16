"""
Tests for bot/config.py.

data_dir / sessions_dir / downloads_dir absolute-path regression test:
this project has already fixed several instances of the same bug class
(SkinLoader, examples.py's search_full_corpus) where code built a path
relative to the process's working directory instead of the actual
project/data location — broken under Docker/systemd, where the cwd at
startup isn't guaranteed to be the project root. Those fixes route
through settings.data_dir, but the field validator that builds it did
not call .resolve() — so with no DATA_DIR override configured (the
common case), settings.data_dir silently stayed relative too, and the
exact same bug class was still reachable through the "fixed" path.
This locks in that the validator always returns an absolute path,
whether or not an override is configured.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}")

from bot.config import Settings  # noqa: E402


class TestDataDirsAreAlwaysAbsolute(unittest.TestCase):
    def test_default_data_dir_is_absolute(self):
        s = Settings(bot_token="123:test", owner_id=123456)
        self.assertTrue(s.data_dir.is_absolute(), f"data_dir was relative: {s.data_dir}")

    def test_default_sessions_and_downloads_dirs_are_absolute(self):
        s = Settings(bot_token="123:test", owner_id=123456)
        self.assertTrue(s.sessions_dir.is_absolute(), f"sessions_dir was relative: {s.sessions_dir}")
        self.assertTrue(s.downloads_dir.is_absolute(), f"downloads_dir was relative: {s.downloads_dir}")

    def test_explicit_relative_override_is_still_resolved_absolute(self):
        # The regression this guards: a relative override used to be
        # returned as-is instead of being resolved against the cwd.
        s = Settings(bot_token="123:test", owner_id=123456, data_dir="./some_relative_data_dir")
        self.assertTrue(s.data_dir.is_absolute(), f"explicit relative override stayed relative: {s.data_dir}")

    def test_explicit_absolute_override_is_unchanged(self):
        abs_path = tempfile.mkdtemp()
        s = Settings(bot_token="123:test", owner_id=123456, data_dir=abs_path)
        self.assertEqual(str(s.data_dir), abs_path)


if __name__ == "__main__":
    unittest.main()
