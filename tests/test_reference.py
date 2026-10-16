"""
Tests for bot/plugins/reference.py (Pokedex, Currency, Telegraph).

This sandbox's network egress is restricted to package-registry domains
only, so pokeapi.co / api.frankfurter.app / api.telegra.ph can't be hit
live from here — that's a property of this dev environment, not of the
deployed bot. What's tested instead: usage/validation messages (no
network needed), and the response-parsing/error-handling logic against
mocked aiohttp responses shaped exactly like each API's real, documented
JSON contract, so the parsing itself is verified even though the live
call isn't.
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

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}")

import bot.plugins.reference as reference_plugin  # noqa: E402


def _make_update(args=None, reply_text=None):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    if reply_text is not None:
        update.message.reply_to_message.text = reply_text
    else:
        update.message.reply_to_message = None
    context = MagicMock()
    context.args = args or []
    return update, context


class _FakeResponse:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json = json_data or {}

    async def json(self):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Maps a URL (by substring) to a canned _FakeResponse for get/post."""

    def __init__(self, responses):
        self._responses = responses  # list of (substring, _FakeResponse)

    def _match(self, url):
        for substr, resp in self._responses:
            if substr in url:
                return resp
        raise AssertionError(f"no fake response configured for URL: {url}")

    def get(self, url, **kwargs):
        return self._match(url)

    def post(self, url, **kwargs):
        return self._match(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestPokedexUsage(unittest.IsolatedAsyncioTestCase):
    async def test_no_args_shows_usage(self):
        update, context = _make_update([])
        await reference_plugin.pokedex_cmd(update, context)
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    async def test_not_found_reports_cleanly(self):
        update, context = _make_update(["not-a-real-pokemon-xyz"])
        fake_session = _FakeSession([("pokemon/", _FakeResponse(status=404))])
        with patch.object(reference_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await reference_plugin.pokedex_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("No Pokemon found", sent)

    async def test_successful_lookup_sends_a_photo_with_real_data(self):
        update, context = _make_update(["pikachu"])
        pokemon_json = {
            "name": "pikachu", "id": 25,
            "types": [{"type": {"name": "electric"}}],
            "height": 4, "weight": 60,
            "stats": [{"base_stat": 35, "stat": {"name": "hp"}}],
            "species": {"url": "https://pokeapi.co/api/v2/pokemon-species/25/"},
            "sprites": {"front_default": "https://example.com/pikachu.png"},
        }
        species_json = {"flavor_text_entries": [{"language": {"name": "en"}, "flavor_text": "A mouse Pokemon."}]}
        fake_session = _FakeSession([
            ("pokemon-species", _FakeResponse(status=200, json_data=species_json)),
            ("pokemon/", _FakeResponse(status=200, json_data=pokemon_json)),
        ])
        with patch.object(reference_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await reference_plugin.pokedex_cmd(update, context)
        update.message.reply_photo.assert_awaited_once()
        caption = update.message.reply_photo.call_args.kwargs.get("caption", "")
        self.assertIn("Pikachu", caption)
        self.assertIn("Electric", caption)


class TestCurrencyValidation(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_arg_count_shows_usage(self):
        update, context = _make_update(["100", "USD"])
        await reference_plugin.currency_cmd(update, context)
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    async def test_non_numeric_amount_rejected(self):
        update, context = _make_update(["abc", "USD", "EUR"])
        await reference_plugin.currency_cmd(update, context)
        self.assertIn("number", update.message.reply_text.call_args[0][0].lower())

    async def test_successful_conversion(self):
        update, context = _make_update(["100", "usd", "eur"])
        fake_session = _FakeSession([
            ("frankfurter.app", _FakeResponse(status=200, json_data={"rates": {"EUR": 92.5}, "date": "2026-09-27"})),
        ])
        with patch.object(reference_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await reference_plugin.currency_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("92.5", sent)
        self.assertIn("EUR", sent)


class _NewsResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload

    async def json(self, **_kwargs):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _NewsSession:
    """Records every request so the tests can inspect URL, params and headers."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestNews(unittest.IsolatedAsyncioTestCase):
    async def test_without_a_key_it_explains_how_to_enable(self):
        update, context = _make_update([])
        with patch.object(reference_plugin.settings, "news_api_key", None):
            await reference_plugin.news_cmd(update, context)
        self.assertIn("NEWS_API_KEY", update.message.reply_text.call_args[0][0])

    async def test_headlines_are_escaped_linked_and_key_stays_out_of_the_url(self):
        update, context = _make_update([])
        payload = {"status": "ok", "articles": [
            {"title": "Cats <b>rule</b> & dogs", "url": "https://example.com/a?x=1&y=2", "source": {"name": "Daily <Paw>"}},
            {"title": "No link", "url": "javascript:alert(1)", "source": {}},
            {"title": "", "url": "https://example.com/empty", "source": {}},
        ]}
        session = _NewsSession(_NewsResponse(200, payload))
        with patch.object(reference_plugin.settings, "news_api_key", "SECRET-KEY"), \
             patch.object(reference_plugin.aiohttp, "ClientSession", return_value=session):
            await reference_plugin.news_cmd(update, context)
        url, kwargs = session.calls[0]
        self.assertIn("top-headlines", url)
        self.assertNotIn("SECRET-KEY", url)
        self.assertNotIn("SECRET-KEY", str(kwargs.get("params")))
        self.assertEqual(kwargs["headers"], {"X-Api-Key": "SECRET-KEY"})
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Cats &lt;b&gt;rule&lt;/b&gt; &amp; dogs", sent)
        self.assertIn("Daily &lt;Paw&gt;", sent)
        self.assertIn("&amp;y=2", sent)            # href is attribute-escaped too
        self.assertNotIn("javascript:", sent)      # non-http(s) links are dropped
        self.assertNotIn("No link", sent)
        self.assertEqual(update.message.reply_text.call_args[1]["parse_mode"], "HTML")

    async def test_topic_search_uses_everything_endpoint(self):
        update, context = _make_update(["space", "x"])
        session = _NewsSession(_NewsResponse(200, {"status": "ok", "articles": []}))
        with patch.object(reference_plugin.settings, "news_api_key", "k"), \
             patch.object(reference_plugin.aiohttp, "ClientSession", return_value=session):
            await reference_plugin.news_cmd(update, context)
        url, kwargs = session.calls[0]
        self.assertIn("/everything", url)
        self.assertEqual(kwargs["params"]["q"], "space x")
        self.assertIn("No stories", update.message.reply_text.call_args[0][0])

    async def test_api_error_is_reported_cleanly(self):
        update, context = _make_update([])
        session = _NewsSession(_NewsResponse(429, {"status": "error", "code": "rateLimited"}))
        with patch.object(reference_plugin.settings, "news_api_key", "k"), \
             patch.object(reference_plugin.aiohttp, "ClientSession", return_value=session):
            await reference_plugin.news_cmd(update, context)
        self.assertIn("didn't return results", update.message.reply_text.call_args[0][0])


class TestTelegraphUsage(unittest.IsolatedAsyncioTestCase):
    async def test_no_args_and_no_reply_shows_usage(self):
        update, context = _make_update([])
        await reference_plugin.telegraph_cmd(update, context)
        self.assertIn("Usage", update.message.reply_text.call_args[0][0])

    async def test_publish_via_pipe_syntax(self):
        reference_plugin._telegraph_token = "cached-token-for-test"
        update, context = _make_update(["My", "Title", "|", "Hello", "world"])
        fake_session = _FakeSession([
            ("createPage", _FakeResponse(status=200, json_data={"ok": True, "result": {"url": "https://telegra.ph/My-Title-01-01"}})),
        ])
        with patch.object(reference_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await reference_plugin.telegraph_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("https://telegra.ph/", sent)

    async def test_publish_via_reply(self):
        reference_plugin._telegraph_token = "cached-token-for-test"
        update, context = _make_update(["My Title"], reply_text="This is the body of the replied message.")
        fake_session = _FakeSession([
            ("createPage", _FakeResponse(status=200, json_data={"ok": True, "result": {"url": "https://telegra.ph/My-Title-01-02"}})),
        ])
        with patch.object(reference_plugin.aiohttp, "ClientSession", return_value=fake_session):
            await reference_plugin.telegraph_cmd(update, context)
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("https://telegra.ph/", sent)


if __name__ == "__main__":
    unittest.main()
