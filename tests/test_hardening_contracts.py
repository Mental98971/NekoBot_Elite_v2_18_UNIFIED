"""Regression contracts that do not require telegram runtime.

Static, source/data-level checks for invariants that have broken before
and are cheap to guard permanently: catalog size/shape, error-message
hygiene, menu button/panel consistency, superowner command wiring, and
the pytgcalls join-path regression.
"""
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestCatalogIntegrity(unittest.TestCase):
    def test_skins_and_presets_parse_and_align(self):
        skins_dir = PROJECT_ROOT / "data" / "personality" / "skins"
        presets_dir = PROJECT_ROOT / "data" / "personality" / "presets"
        skins = {p.stem for p in skins_dir.glob("*.json")}
        presets = {p.stem for p in presets_dir.glob("*.json")}
        self.assertGreaterEqual(len(skins), 100)
        self.assertGreaterEqual(len(presets), 100)
        self.assertTrue({"neko", "nanora"} <= skins)
        self.assertTrue({"neko", "nanora"} <= presets)
        for path in list(skins_dir.glob("*.json")) + list(presets_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)

    def test_no_stopword_only_preset_keywords(self):
        """Regression guard: a keyword list must not contain bare stopwords
        like 'and' — these previously caused unrelated presets to win
        keyword-matching ties against far more specific presets."""
        stopwords = {"and", "or", "the", "a", "an", "with", "for", "of", "to",
                     "in", "on", "at", "is", "be", "as", "but", "it", "by", "that"}
        presets_dir = PROJECT_ROOT / "data" / "personality" / "presets"
        offenders = []
        for path in presets_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for kw in data.get("keywords", []):
                if kw.strip().lower() in stopwords:
                    offenders.append((path.stem, kw))
        self.assertEqual(offenders, [], msg=f"stopword keywords found: {offenders}")

    def test_fewshot_bank_shape(self):
        path = PROJECT_ROOT / "data" / "personality" / "dataset" / "fewshot_bank.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("examples", data)
        self.assertGreaterEqual(len(data["examples"]), 300)


class TestErrorHygieneSource(unittest.TestCase):
    def test_errors_module_defines_leak_markers(self):
        src = (PROJECT_ROOT / "bot" / "core" / "errors.py").read_text(encoding="utf-8")
        for marker in ("gemini", "groq", "openrouter", "api key", "429"):
            self.assertIn(marker, src.lower())
        self.assertIn("def user_facing_error", src)
        self.assertIn("def _looks_like_internal_leak", src)

    def test_ai_plugin_uses_safe_error_helper_not_raw_fstrings(self):
        src = (PROJECT_ROOT / "bot" / "plugins" / "ai.py").read_text(encoding="utf-8")
        self.assertNotIn('f"❌ {e}"', src)
        self.assertNotIn('f"❌ AI Error: {e}"', src)
        self.assertIn("_user_facing_ai_error", src)


class TestMenuContractSource(unittest.TestCase):
    """Static contract for the /menu layout in bot/plugins/start.py
    (MAIN_MENU/PAGE_2/PAGE_3 buttons -> MENU_PANELS panels): every button
    resolves to a real panel, every panel is reachable from a button, no
    two buttons share a callback key, and real commands stay documented."""

    @staticmethod
    def _load():
        tree = ast.parse((PROJECT_ROOT / "bot" / "plugins" / "start.py").read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) in {"MENU_PANELS", "_KEY_OVERRIDES"}:
                found[node.target.id] = ast.literal_eval(node.value)
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in {"MAIN_MENU", "PAGE_2", "PAGE_3"}:
                        found[t.id] = ast.literal_eval(node.value)
        return found

    @staticmethod
    def _key(label, overrides):
        return overrides.get(label) or label.split()[-1].lower()

    def test_every_button_has_a_panel_and_keys_are_unique(self):
        m = self._load()
        panels, overrides = m["MENU_PANELS"], m.get("_KEY_OVERRIDES", {})
        keys = {}
        for page in ("MAIN_MENU", "PAGE_2", "PAGE_3"):
            for row in m[page]:
                for label in row:
                    k = self._key(label, overrides)
                    self.assertIn(k, panels, msg=f"button {label!r} -> no panel {k!r}")
                    self.assertNotIn(k, keys, msg=f"{label!r} collides with {keys.get(k)!r} on key {k!r}")
                    keys[k] = label
        unreachable = sorted(set(panels) - set(keys))
        self.assertEqual(unreachable, [], msg=f"panels with no button: {unreachable}")

    def test_panels_have_title_and_body(self):
        for key, (title, body) in self._load()["MENU_PANELS"].items():
            self.assertTrue(title.strip() and body.strip(), msg=key)

    def test_no_generic_feature_panel_stub(self):
        src = (PROJECT_ROOT / "bot" / "plugins" / "start.py").read_text(encoding="utf-8")
        self.assertNotIn("Feature panel for", src)

    def test_real_commands_are_documented_in_menu(self):
        m = self._load()
        text = "\n".join(f"{t} {b}" for t, b in m["MENU_PANELS"].values())
        for cmd in ("tts", "qr", "ud", "insta", "yt", "purge", "report", "leave",
                    "dice", "coinflip", "rps", "slots", "tictactoe", "trivia", "guess",
                    "remind", "reminders", "schedule", "announce", "poll", "cosplay", "chat",
                    "manhwa", "relations", "topanime", "wordseek", "wordgrid", "wordchain", "chess", "ludo", "uno",
                    "pokedex", "currency", "convert", "telegraph", "kang", "packinfo", "antichannel"):
            self.assertIn(f"/{cmd}", text, f"/{cmd} is a real command but is missing from /menu")

    def test_menu_does_not_advertise_nonexistent_commands(self):
        registered = set()
        for path in (PROJECT_ROOT / "bot" / "plugins").glob("*.py"):
            registered |= set(re.findall(r'CommandHandler\(\s*"([a-z0-9_]+)"', path.read_text(encoding="utf-8")))
            for group in re.findall(r'CommandHandler\(\s*\[([^\]]+)\]', path.read_text(encoding="utf-8")):
                registered |= set(re.findall(r'"([a-z0-9_]+)"', group))
        self.assertIn("play", registered)  # sanity: the scan actually found handlers
        text = "\n".join(b for _, b in self._load()["MENU_PANELS"].values())
        self.assertNotIn("/bet ", text + " ")


class TestHandlerGroupsAreUnique(unittest.TestCase):
    """PTB runs only the first matching handler per group, so two catch-all
    handlers sharing a group silently disable the later one."""

    def test_no_two_registrations_share_a_group(self):
        seen = {}
        files = list((PROJECT_ROOT / "bot" / "plugins").glob("*.py")) + [PROJECT_ROOT / "bot" / "core" / "bot.py"]
        for path in files:
            for m in re.finditer(r"add_handler\(MessageHandler\((.*?)\),\s*group=(-?\d+)\)", path.read_text(encoding="utf-8")):
                seen.setdefault(int(m.group(2)), []).append(f"{path.name}:{m.group(1)[:40]}")
        # collector's upload (PHOTO|VIDEO) and personality (TEXT) share 8 with disjoint filters.
        dup = {g: v for g, v in seen.items() if len(v) > 1 and g != 8}
        self.assertEqual(dup, {}, msg=f"handler groups shared: {dup}")


class TestPersonaAdminWired(unittest.TestCase):
    def test_superowner_commands_registered(self):
        src = (PROJECT_ROOT / "bot" / "plugins" / "personality.py").read_text(encoding="utf-8")
        for cmd in ("plist", "pshow", "padd", "pdel", "pmerge", "pset", "pdata", "pmeta", "pjson", "preload"):
            self.assertIn(f'"{cmd}"', src, msg=f"missing handler {cmd}")
        self.assertIn("owner_only", src)


class TestMusicJoinNotUsingStreamEnded(unittest.TestCase):
    def test_audio_engine_does_not_pass_stream_audio_ended(self):
        src = (PROJECT_ROOT / "bot" / "services" / "audio_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("stream_type=StreamAudioEnded()", src)
        self.assertIn("def _fresh_audio_url", src)


class TestPersonalityPathsAreCwdIndependent(unittest.TestCase):
    """Regression guard for the cwd-relative path bug: personality data
    loading must resolve against settings.data_dir, never a bare relative
    'data/...' literal, so the bot works regardless of process cwd."""

    def test_no_bare_relative_data_path_literals(self):
        for rel in ("bot/personality/personality.py", "bot/personality/examples.py",
                    "bot/plugins/health.py"):
            src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn('Path("data/personality', src, msg=f"cwd-relative path literal in {rel}")
            self.assertNotIn("Path('data/personality", src, msg=f"cwd-relative path literal in {rel}")


if __name__ == "__main__":
    unittest.main()
