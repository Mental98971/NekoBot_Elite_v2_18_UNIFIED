"""Unit tests for bot/utils/webtools.py and bot/core/menu.py (standard library only)."""
from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wt = _load("bot/utils/webtools.py", "_webtools_under_test")
menu = _load("bot/core/menu.py", "_menu_under_test")


class TestNormalizeUrl(unittest.TestCase):
    def test_accepts_public_addresses(self):
        for raw, want in [
            ("example.com", "https://example.com"),
            ("Example.com/a?b=1#c", "https://Example.com/a?b=1#c"),
            ("http://example.com/path", "http://example.com/path"),
            ("https://example.com:8443/x", "https://example.com:8443/x"),
            ("example.com:8080/x", "https://example.com:8080/x"),
            ("<https://example.org>", "https://example.org"),
            ("https://8.8.8.8/", "https://8.8.8.8/"),
            ("münchen.de", "https://münchen.de"),
            ("https://sub.domain.co.uk/a_b-c", "https://sub.domain.co.uk/a_b-c"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(wt.normalize_url(raw), want)

    def test_rejects_local_private_and_trick_addresses(self):
        for raw in [
            "localhost", "http://localhost:3000", "127.0.0.1", "http://127.0.0.1/admin", "http://[::1]/", "10.0.0.5",
            "192.168.1.1", "172.16.0.9", "169.254.169.254", "http://169.254.169.254/latest/meta-data/", "0.0.0.0",
            "http://[::ffff:127.0.0.1]/", "http://2130706433", "http://0x7f.0.0.1", "http://127.1", "http://017700000001",
            "intranet", "printer.local", "nas.lan", "metadata.google.internal", "foo.internal", "a.b.localhost",
        ]:
            with self.subTest(raw=raw):
                self.assertIsNone(wt.normalize_url(raw))

    def test_rejects_non_http_schemes_credentials_and_junk(self):
        for raw in [
            "javascript:alert(1)", "data:text/html;base64,AAAA", "file:///etc/passwd", "ftp://example.com", "mailto:a@b.com",
            "//example.com", "http://user:pw@example.com", "http://user@example.com", "https://exa mple.com", "https://example.com/a b",
            "", "   ", None, "http://", "https://.", "http://example.com:99999", "http://-bad-.com", "x" * 2100,
            "https://example.com\n.evil.com", "http://exa\x00mple.com",
        ]:
            with self.subTest(raw=raw):
                self.assertIsNone(wt.normalize_url(raw))

    def test_is_public_host(self):
        self.assertTrue(wt.is_public_host("example.com"))
        self.assertTrue(wt.is_public_host("1.1.1.1"))
        self.assertTrue(wt.is_public_host("2606:4700:4700::1111"))
        for h in (None, "", "localhost", "::1", "fe80::1", "fc00::1", "100.64.0.1", "224.0.0.1", "255.255.255.255", "::ffff:10.0.0.1"):
            self.assertFalse(wt.is_public_host(h), h)


class TestHrefAndText(unittest.TestCase):
    def test_safe_href_escapes_quotes_and_rejects_other_schemes(self):
        self.assertEqual(wt.safe_href('https://x.com/a?b="1"&c=2'), "https://x.com/a?b=&quot;1&quot;&amp;c=2")
        for bad in ("javascript:alert(1)", "tg://user?id=1", "", None, "ftp://x", "https://x y"):
            self.assertEqual(wt.safe_href(bad), "")

    def test_ddg_search_url_is_encoded(self):
        url = wt.ddg_search_url('a b&c="d"#e')
        self.assertTrue(url.startswith("https://duckduckgo.com/?q="))
        for ch in (' ', '"', "#", "&c="):
            self.assertNotIn(ch, url.split("?q=", 1)[1])

    def test_stable_seed_is_deterministic_and_bounded(self):
        self.assertEqual(wt.stable_seed("Anime"), zlib.crc32(b"anime") % 10000)
        self.assertEqual(wt.stable_seed("anime"), wt.stable_seed("ANIME"))
        for q in ("", "x", "日本語", "a" * 500):
            self.assertTrue(0 <= wt.stable_seed(q, 77) < 77)
        with self.assertRaises(ValueError):
            wt.stable_seed("x", 0)

    def test_parse_coordinates(self):
        self.assertEqual(wt.parse_coordinates("35.6586, 139.7454"), (35.6586, 139.7454))
        self.assertEqual(wt.parse_coordinates("-33.86 151.21"), (-33.86, 151.21))
        self.assertEqual(wt.parse_coordinates("90,180"), (90.0, 180.0))
        for bad in ("91, 0", "0, 181", "Tokyo Tower", "1,2,3", "", None, "12.5"):
            self.assertIsNone(wt.parse_coordinates(bad), bad)

    def test_clip(self):
        self.assertEqual(wt.clip("  hello \n world ", 50), "hello world")
        out = wt.clip("word " * 100, 30)
        self.assertLessEqual(len(out), 30)
        self.assertTrue(out.endswith("\u2026"))
        self.assertEqual(wt.clip("abc", 0), "")
        self.assertLessEqual(len(wt.clip("x" * 1000, 10)), 10)


class TestMenuSanitizer(unittest.TestCase):
    def test_keeps_valid_entries_in_order_and_drops_duplicates_and_invalid(self):
        kept, dropped = menu.sanitize_menu([("Start", "go"), ("/help", "h"), ("start", "dup"), ("bad name", "x"), ("ok_1", ""), ("", "x"), ("x" * 33, "long")])
        self.assertEqual(kept, [("start", "go"), ("help", "h")])
        self.assertEqual(len(dropped), 5)

    def test_cap_keeps_highest_priority_entries(self):
        cmds = [(f"c{i}", "d") for i in range(130)]
        kept, dropped = menu.sanitize_menu(cmds)
        self.assertEqual(len(kept), 100)
        self.assertEqual([k for k, _ in kept], [f"c{i}" for i in range(100)])
        self.assertEqual(dropped, [f"c{i}" for i in range(100, 130)])
        self.assertEqual(len(menu.sanitize_menu(cmds, limit=5)[0]), 5)

    def test_descriptions_are_trimmed_to_256(self):
        kept, _ = menu.sanitize_menu([("a", "x" * 400)])
        self.assertEqual(len(kept[0][1]), 256)

    def test_shipped_menu_needs_no_sanitising(self):
        tree = ast.parse((ROOT / "bot/core/bot.py").read_text(encoding="utf-8"))
        entries = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "commands"
                    and isinstance(node.value, ast.List)):
                entries = [(e.elts[0].value, e.elts[1].value) for e in node.value.elts if isinstance(e, ast.Tuple)]
        self.assertGreater(len(entries), 50)
        kept, dropped = menu.sanitize_menu(entries)
        self.assertEqual(dropped, [])
        self.assertEqual(len(kept), len(entries))


class TestExtrasPlugin(unittest.TestCase):
    """The plugin needs the Telegram stack to import, so it is inspected statically here."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "bot/plugins/extras.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def test_registers_every_documented_command(self):
        reg = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CommandHandler":
                arg = node.args[0]
                reg.update([arg.value] if isinstance(arg, ast.Constant) else [e.value for e in arg.elts])
        self.assertEqual(reg, {"truth", "dare", "carbon", "wall", "wallpaper", "webss", "ss", "screenshot", "gps", "gsearch", "google"})

    def test_no_nonexistent_telegram_names_and_no_unsafe_patterns(self):
        for bad in ("InputMediaGeo", "aiohttp.helpers", "hash(query)"):
            self.assertNotIn(bad, self.src)
        self.assertNotIn("hash(", self.src.replace("stable_seed(", ""))  # process-salted hash() must not seed anything

    def test_every_outside_call_has_a_timeout(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "AsyncClient":
                self.assertTrue(any(k.arg == "timeout" for k in node.keywords))
        self.assertNotIn("ClientSession(", self.src)


if __name__ == "__main__":
    unittest.main()
