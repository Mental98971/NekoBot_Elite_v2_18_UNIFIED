"""Tests for bot/plugins/myanimelist.py (v2.17): Jikan v4 lookups, formatting, caching, failure handling."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_mal_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP.name}")

import bot.plugins.myanimelist as mal  # noqa: E402
from tests._fakes import FakeBot, make_context, make_update  # noqa: E402

ANIME = {
    "mal_id": 1, "url": "https://myanimelist.net/anime/1/Cowboy_Bebop", "title": "Cowboy <Bebop>", "title_japanese": "カウボーイビバップ",
    "type": "TV", "source": "Original", "status": "Finished Airing", "episodes": 26, "duration": "24 min per ep", "score": 8.75,
    "rating": "R - 17+", "season": "spring", "year": 1998, "genres": [{"name": "Action"}, {"name": "Sci-Fi"}],
    "studios": [{"name": "Sunrise"}], "synopsis": "In 2071 <b>bounty</b> hunters...",
    "images": {"jpg": {"large_image_url": "https://cdn.myanimelist.net/images/anime/4/19644l.jpg"}},
    "trailer": {"url": "https://www.youtube.com/watch?v=qig4KOK2R2g"},
}
MANGA = {"title": "Berserk", "title_japanese": "ベルセルク", "type": "Manga", "status": "Publishing", "score": 9.47, "volumes": None,
         "chapters": None, "genres": [{"name": "Action"}], "authors": [{"name": "Miura, Kentarou"}], "synopsis": None,
         "url": "https://myanimelist.net/manga/2/Berserk", "images": {"jpg": {"image_url": "https://cdn.myanimelist.net/x.jpg"}}}
CHARACTER = {"mal_id": 7, "name": "Spike Spiegel", "name_kanji": "スパイク", "about": None, "url": "https://myanimelist.net/character/1",
             "images": {"jpg": {"image_url": "https://cdn.myanimelist.net/c.jpg"}}}


def _reply(update):
    return update.effective_message.reply_text.await_args


class TestFormatting(unittest.TestCase):
    def test_anime_card_escapes_api_text_and_handles_missing_fields(self):
        text = mal.format_anime(ANIME)
        self.assertIn("Cowboy &lt;Bebop&gt;", text)
        self.assertIn("&lt;b&gt;bounty&lt;/b&gt;", text)               # API markup is shown, never executed
        self.assertIn("Spring 1998", text)
        self.assertIn("Action, Sci-Fi", text)
        self.assertIn("cdn.myanimelist.net", text)
        sparse = mal.format_anime({"title": "X"})
        self.assertIn("<b>X</b>", sparse)
        self.assertIn("No synopsis available.", sparse)

    def test_manga_and_character_cards(self):
        self.assertIn("Miura, Kentarou", mal.format_manga(MANGA))
        self.assertIn("<code>-</code>", mal.format_manga(MANGA))        # unknown volumes/chapters
        self.assertIn("Spike Spiegel", mal.format_character(CHARACTER))
        self.assertIn("No synopsis available.", mal.format_character(CHARACTER))

    def test_upcoming_dedupes_caps_and_skips_untitled(self):
        items = [{"title": "A", "url": "https://x/a"}, {"title": "A", "url": "https://x/a2"}, {"url": "https://x/none"}] + \
                [{"title": f"T{i}", "url": f"https://x/{i}"} for i in range(40)]
        text = mal.format_upcoming(items)
        self.assertEqual(text.count("• "), mal.UPCOMING_LIMIT)
        self.assertEqual(text.count(">A<"), 1)

    def test_urls_with_ampersands_are_escaped_once_in_html_and_raw_in_buttons(self):
        entry = dict(ANIME, url="https://myanimelist.net/anime/1?a=1&b=2", trailer={"url": "https://youtu.be/x?t=1&u=2"},
                     images={"jpg": {"large_image_url": "https://cdn.myanimelist.net/i.jpg?x=1&y=2"}})
        self.assertIn("i.jpg?x=1&amp;y=2", mal.format_anime(entry))
        self.assertNotIn("&amp;amp;", mal.format_anime(entry))
        self.assertEqual(mal._buttons(entry, trailer=True).inline_keyboard[0][0].url, "https://myanimelist.net/anime/1?a=1&b=2")
        for bad in ("javascript:alert(1)", "https://x/a b", "https://x/'><b>", "ftp://x", ""):
            self.assertEqual(mal._safe_url(bad), "", bad)

    def test_buttons(self):
        kb = mal._buttons(ANIME, trailer=True)
        self.assertEqual([b.text for b in kb.inline_keyboard[0]], ["More Information", "Trailer"])
        self.assertEqual([b.text for b in mal._buttons(ANIME).inline_keyboard[0]], ["More Information"])
        self.assertIsNone(mal._buttons({"url": "javascript:alert(1)"}))


class TestLookups(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mal._cache.clear()

    async def _run(self, cmd, args, payloads):
        calls = []

        async def fake(path, params=None):
            calls.append((path, params))
            return payloads.get(path)

        upd = make_update("/x", user_id=1, chat_id=-1)
        with mock.patch.object(mal, "jikan_get", fake):
            await cmd(upd, make_context(FakeBot(), args))
        return upd, calls

    async def test_myanime_searches_sfw_and_replies_with_buttons(self):
        upd, calls = await self._run(mal.myanime_cmd, ["cowboy", "bebop"], {"/anime": [ANIME]})
        self.assertEqual(calls, [("/anime", {"q": "cowboy bebop", "limit": 1, "sfw": "true"})])
        args, kwargs = _reply(upd)
        self.assertIn("Cowboy &lt;Bebop&gt;", args[0])
        self.assertEqual(len(kwargs["reply_markup"].inline_keyboard[0]), 2)

    async def test_usage_and_not_found(self):
        for cmd, usage in ((mal.myanime_cmd, "/myanime"), (mal.mymanga_cmd, "/mymanga"), (mal.mycharacter_cmd, "/mycharacter")):
            upd, calls = await self._run(cmd, [], {})
            self.assertIn(usage, _reply(upd).args[0])
            self.assertEqual(calls, [])
        upd, _ = await self._run(mal.mymanga_cmd, ["zzzz"], {"/manga": []})
        self.assertEqual(_reply(upd).args[0], "No results found!")
        upd, _ = await self._run(mal.mymanga_cmd, ["zzzz"], {})
        self.assertEqual(_reply(upd).args[0], "No results found!")

    async def test_character_fetches_details_when_search_result_lacks_about(self):
        full = dict(CHARACTER, about="Bounty hunter.")
        upd, calls = await self._run(mal.mycharacter_cmd, ["spike"], {"/characters": [CHARACTER], "/characters/7": full})
        self.assertEqual([c[0] for c in calls], ["/characters", "/characters/7"])
        self.assertIn("Bounty hunter.", _reply(upd).args[0])

    async def test_upcoming(self):
        upd, calls = await self._run(mal.myupcoming_cmd, [], {"/seasons/upcoming": [{"title": "Soon", "url": "https://x/s"}]})
        self.assertEqual(calls[0][1]["sfw"], "true")
        self.assertIn("Soon", _reply(upd).args[0])
        upd, _ = await self._run(mal.myupcoming_cmd, [], {"/seasons/upcoming": []})
        self.assertIn("No upcoming", _reply(upd).args[0])

    async def test_query_is_length_limited(self):
        _, calls = await self._run(mal.myanime_cmd, ["x" * 500], {"/anime": [ANIME]})
        self.assertEqual(len(calls[0][1]["q"]), mal.MAX_QUERY)

    async def test_outage_gets_a_friendly_message(self):
        async def down(path, params=None):
            raise mal.JikanUnavailable("MyAnimeList is rate-limiting me - try again in a few seconds.")

        upd = make_update("/myanime", user_id=1, chat_id=-1)
        with mock.patch.object(mal, "jikan_get", down):
            await mal.myanime_cmd(upd, make_context(FakeBot(), ["x"]))
        self.assertIn("rate-limiting", _reply(upd).args[0])


class TestHttpLayer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mal._cache.clear()

    def _client(self, handler):
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient
        return mock.patch.object(mal.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))

    async def test_success_is_cached_and_params_are_sent(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": [ANIME]})

        with self._client(handler):
            self.assertEqual(await mal.jikan_get("/anime", {"q": "a b", "limit": 1}), [ANIME])
            self.assertEqual(await mal.jikan_get("/anime", {"q": "a b", "limit": 1}), [ANIME])
        self.assertEqual(len(seen), 1)
        self.assertIn("q=a+b", seen[0])

    async def test_status_codes(self):
        with self._client(lambda r: httpx.Response(404, json={})):
            self.assertIsNone(await mal.jikan_get("/characters/999999"))
        for status, needle in ((429, "rate-limiting"), (500, "Error connecting"), (503, "Error connecting")):
            mal._cache.clear()
            with self._client(lambda r, s=status: httpx.Response(s)), self.assertRaises(mal.JikanUnavailable) as ctx:
                await mal.jikan_get("/anime", {"q": str(status)})
            self.assertIn(needle, str(ctx.exception))

    async def test_network_failure_and_garbage_body(self):
        def boom(request):
            raise httpx.ConnectError("down")

        with self._client(boom), self.assertRaises(mal.JikanUnavailable):
            await mal.jikan_get("/anime", {"q": "x"})
        with self._client(lambda r: httpx.Response(200, content=b"<html>")), self.assertRaises(mal.JikanUnavailable):
            await mal.jikan_get("/anime", {"q": "y"})

    def test_registration(self):
        added = []
        mal.register(SimpleNamespace(add_handler=lambda h, group=0: added.append(h)))
        self.assertEqual({c for h in added for c in h.commands}, {"myanime", "mymanga", "mycharacter", "myupcoming"})


if __name__ == "__main__":
    unittest.main()
