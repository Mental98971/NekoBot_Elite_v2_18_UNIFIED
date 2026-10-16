"""
Tests for bot/plugins/utilities.py.

_safe_calc is the highest-risk piece here: it evaluates arbitrary
user-supplied arithmetic text. It must accept ordinary arithmetic and
reject anything that isn't — no eval() of arbitrary Python, no name
lookups, no function/attribute access, no unbounded exponents.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="test_utilities_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.name}"

import bot.plugins.utilities as utilities_plugin  # noqa: E402
from bot.plugins.utilities import _safe_calc, register  # noqa: E402


class TestSafeCalc(unittest.TestCase):
    def test_basic_arithmetic(self):
        self.assertEqual(_safe_calc("2+2"), 4)
        self.assertEqual(_safe_calc("(3+5)*2"), 16)
        self.assertEqual(_safe_calc("10 / 4"), 2.5)
        self.assertEqual(_safe_calc("10 // 4"), 2)
        self.assertEqual(_safe_calc("10 % 3"), 1)

    def test_exponentiation_both_notations(self):
        self.assertEqual(_safe_calc("2**10"), 1024)
        self.assertEqual(_safe_calc("2^10"), 1024)

    def test_negative_numbers(self):
        self.assertEqual(_safe_calc("-5+3"), -2)
        self.assertEqual(_safe_calc("-(2+3)"), -5)

    def test_division_by_zero_raises(self):
        with self.assertRaises(ZeroDivisionError):
            _safe_calc("1/0")

    def test_oversized_exponent_rejected(self):
        with self.assertRaises(ValueError):
            _safe_calc("9999999**9999999")

    def test_function_call_rejected(self):
        with self.assertRaises(Exception):
            _safe_calc("__import__('os').system('echo pwned')")

    def test_name_lookup_rejected(self):
        with self.assertRaises(Exception):
            _safe_calc("os.system('echo pwned')")

    def test_attribute_access_rejected(self):
        with self.assertRaises(Exception):
            _safe_calc("().__class__")

    def test_string_literal_rejected(self):
        with self.assertRaises(Exception):
            _safe_calc("'a' * 1000000")


class _FakeResponse:
    def __init__(self, json_data=None):
        self._json = json_data or {}

    async def json(self, **_kwargs):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses):
        self._responses = responses  # list of (url_substring, _FakeResponse)

    def get(self, url, **kwargs):
        for substr, resp in self._responses:
            if substr in url:
                return resp
        raise AssertionError(f"no fake response configured for: {url}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestImdbTmdbFlow(unittest.IsolatedAsyncioTestCase):
    """Recovered from an earlier snapshot of this suite (they were dropped in a later merge)."""

    def _make_update(self, args=None):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = args or []
        return update, context

    async def test_no_query_shows_usage(self):
        update, context = self._make_update([])
        await utilities_plugin.imdb_cmd(update, context)
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    async def test_tmdb_search_shows_tappable_results_when_key_configured(self):
        update, context = self._make_update(["all of us are dead"])
        search_json = {"results": [
            {"media_type": "tv", "id": 99966, "name": "All of Us Are Dead", "first_air_date": "2022-01-28"},
        ]}
        fake_session = _FakeSession([("search/multi", _FakeResponse(search_json))])
        with patch.object(utilities_plugin.settings, "tmdb_api_key", "fake-tmdb-key"), \
             patch.object(utilities_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await utilities_plugin.imdb_cmd(update, context)
        update.message.reply_text.assert_awaited_once()
        _, kwargs = update.message.reply_text.call_args
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertIn("All of Us Are Dead", button.text)
        self.assertEqual(button.callback_data, "imdbsel:tv:99966")

    async def test_falls_back_to_wikipedia_when_no_keys_configured(self):
        update, context = self._make_update(["some obscure title"])
        wiki_json = ["some obscure title", ["Some Obscure Title"], ["A description."], ["https://en.wikipedia.org/x"]]
        fake_session = _FakeSession([("wikipedia.org", _FakeResponse(wiki_json))])
        with patch.object(utilities_plugin.settings, "tmdb_api_key", None), \
             patch.object(utilities_plugin.settings, "omdb_api_key", None), \
             patch.object(utilities_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await utilities_plugin.imdb_cmd(update, context)
        self.assertIn("Some Obscure Title", update.message.reply_text.call_args[0][0])

    async def test_callback_shows_poster_and_details(self):
        update = MagicMock()
        update.callback_query.data = "imdbsel:tv:99966"
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.reply_photo = AsyncMock()
        update.callback_query.delete_message = AsyncMock()
        context = MagicMock()
        details_json = {
            "id": 99966, "name": "All of Us Are Dead", "first_air_date": "2022-01-28",
            "episode_run_time": [65], "vote_average": 8.284,
            "genres": [{"name": "Drama"}, {"name": "Horror"}],
            "overview": "A high school becomes ground zero for a zombie outbreak.",
            "poster_path": "/abc123.jpg",
        }
        fake_session = _FakeSession([("themoviedb.org", _FakeResponse(details_json))])
        with patch.object(utilities_plugin.settings, "tmdb_api_key", "fake-tmdb-key"), \
             patch.object(utilities_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await utilities_plugin.imdb_callback(update, context)
        update.callback_query.message.reply_photo.assert_awaited_once()
        args, kwargs = update.callback_query.message.reply_photo.call_args
        self.assertIn("image.tmdb.org", args[0])
        self.assertIn("All of Us Are Dead", kwargs["caption"])
        update.callback_query.delete_message.assert_awaited_once()

    async def test_callback_rejects_malformed_selection(self):
        update = MagicMock()
        update.callback_query.data = "imdbsel:tv:not-a-number"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        await utilities_plugin.imdb_callback(update, MagicMock())
        self.assertIn("Invalid", update.callback_query.edit_message_text.call_args[0][0])


class TestUtilitiesRegistration(unittest.TestCase):
    def test_register_adds_handlers_without_error(self):
        added = []

        class FakeApp:
            def add_handler(self, handler, group=0):
                added.append(handler)

        register(FakeApp())
        self.assertGreater(len(added), 0)

    def test_no_duplicate_command_strings_within_this_plugin(self):
        from telegram.ext import CommandHandler

        added = []

        class FakeApp:
            def add_handler(self, handler, group=0):
                added.append(handler)

        register(FakeApp())
        seen = set()
        for h in added:
            if isinstance(h, CommandHandler):
                for c in h.commands:
                    self.assertNotIn(c, seen, f"/{c} registered twice within utilities.py")
                    seen.add(c)


if __name__ == "__main__":
    unittest.main()
