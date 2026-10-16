"""Static contracts for the v2.6 unified build (no network / PTB install required)."""
import re
import unittest
from collections import defaultdict
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[1] / "bot" / "plugins"


def _commands() -> dict:
    reg = defaultdict(list)
    for path in PLUGINS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"CommandHandler\(\s*(\[[^\]]*\]|\"[^\"]+\"|'[^']+')", text):
            for cmd in re.findall(r"[\"']([a-z0-9_]+)[\"']", m.group(1)):
                reg[cmd].append(path.name)
    return reg


class SynthesisContracts(unittest.TestCase):
    def test_no_command_registered_twice(self):
        dupes = {c: f for c, f in _commands().items() if len(f) > 1}
        self.assertEqual(dupes, {}, msg=f"duplicate command registrations: {dupes}")

    def test_reverse_is_owned_by_anime_and_delegates_to_fonts(self):
        self.assertEqual(_commands()["reverse"], ["anime.py"])
        anime = (PLUGINS / "anime.py").read_text(encoding="utf-8")
        self.assertIn("from bot.plugins.fonts import reverse_cmd", anime)

    def test_imdb_callback_registered(self):
        text = (PLUGINS / "utilities.py").read_text(encoding="utf-8")
        self.assertIn('CallbackQueryHandler(imdb_callback, pattern=r"^imdbsel:")', text)

    def test_tmdb_key_not_embedded_in_urls(self):
        text = (PLUGINS / "utilities.py").read_text(encoding="utf-8")
        self.assertNotIn("api_key={tmdb_key}", text)


if __name__ == "__main__":
    unittest.main()
