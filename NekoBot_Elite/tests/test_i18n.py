"""Multi-language support: catalogs, resolution, detection, and that every key used in the code exists (standard library only)."""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "data" / "locales"


def _load():
    spec = importlib.util.spec_from_file_location("_i18n_under_test", ROOT / "bot/utils/i18n.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


i18n = _load()
CATALOGS = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(LOCALES.glob("*.json"))}


class TestCatalogs(unittest.TestCase):
    def test_expected_languages_ship(self):
        self.assertEqual(set(CATALOGS), {"en", "hi", "hl", "es", "ru", "id", "ar", "zh", "fr", "pt"})

    def test_every_catalog_has_exactly_the_english_keys(self):
        en = set(CATALOGS["en"])
        for code, cat in CATALOGS.items():
            with self.subTest(language=code):
                self.assertEqual(set(cat) - en, set(), "keys that English does not have")
                self.assertEqual(en - set(cat), set(), "keys missing from this language")

    def test_placeholders_agree_with_english(self):
        for code, cat in CATALOGS.items():
            for key, text in cat.items():
                with self.subTest(language=code, key=key):
                    self.assertEqual(i18n.placeholders(text), i18n.placeholders(CATALOGS["en"][key]))

    def test_no_empty_strings_and_html_is_balanced(self):
        for code, cat in CATALOGS.items():
            for key, text in cat.items():
                with self.subTest(language=code, key=key):
                    self.assertTrue(text.strip())
                    for tag in ("b", "i", "code"):
                        self.assertEqual(text.count(f"<{tag}>"), text.count(f"</{tag}>"))

    def test_translations_are_actually_translated(self):
        for code in CATALOGS:
            if code in ("en", "hl"):  # Hinglish keeps many English words by nature
                continue
            same = [k for k, v in CATALOGS[code].items() if v == CATALOGS["en"][k] and not k.startswith(("help.cat.", "menu.btn.")) and k != "menu.back"]  # labels may legitimately stay (AI, AFK, IMDb, "Menu"...)
            with self.subTest(language=code):
                self.assertEqual(same, [])

    def test_command_names_survive_translation(self):
        for code, cat in CATALOGS.items():
            for key in ("lang.usage", "start.tap", "help.footer", "help.language", "start.language"):
                with self.subTest(language=code, key=key):
                    self.assertTrue(any(c in cat[key] for c in ("/lang", "/help", "/menu")), cat[key])


class TestTranslate(unittest.TestCase):
    def test_translates_formats_and_falls_back(self):
        self.assertEqual(i18n.t("common.admin_only", "en"), "🚫 Admin only.")
        self.assertIn("एडमिन", i18n.t("common.admin_only", "hi"))
        self.assertEqual(i18n.t("common.admin_only", "pt-BR"), CATALOGS["pt"]["common.admin_only"])   # region codes map to the base
        self.assertEqual(i18n.t("common.admin_only", "xx"), "🚫 Admin only.")                          # unknown language -> English
        self.assertEqual(i18n.t("no.such.key", "hi"), "no.such.key")                                   # unknown key -> the key
        self.assertIn("Ravi", i18n.t("start.welcome", "es", name="Ravi"))

    def test_malformed_values_never_raise(self):
        self.assertEqual(i18n.t("start.welcome", "en"), CATALOGS["en"]["start.welcome"])             # missing placeholder stays visible
        self.assertIn("{bot}", i18n.t("start.tagline", "en", wrong="x"))

    def test_normalize_language(self):
        for raw, want in (("pt-BR", "pt"), ("ZH_cn", "zh"), ("Hinglish", "hl"), ("hi-Latn", "hl"), ("iw", "he"), ("EN", "en"), ("zh-Hant", "zh")):
            self.assertEqual(i18n.normalize_language(raw), want, raw)
        for bad in (None, "", "x", "123", "a b", "<script>"):
            self.assertIsNone(i18n.normalize_language(bad), bad)

    def test_resolution_order(self):
        r = i18n.resolve_language
        self.assertEqual(r("hi", "es"), "hi")                    # explicit chat setting wins
        self.assertEqual(r(None, "es"), "es")                    # then the Telegram client language
        self.assertEqual(r(None, "pt-BR"), "pt")
        self.assertEqual(r(None, "ja"), "en")                    # no catalog -> default
        self.assertEqual(r("ja", "fr"), "fr")                    # an unusable setting falls through to the next source
        self.assertEqual(r(None, "es", allowed=["en", "hi"]), "en")      # operator limit
        self.assertEqual(r("hi", None, default="fr"), "hi")
        self.assertEqual(r(None, None, default="fr"), "fr")
        self.assertEqual(r(None, None, default="xx"), "en")

    def test_available_languages_lists_english_first(self):
        langs = i18n.available_languages()
        self.assertEqual(langs[0], "en")
        self.assertEqual(sorted(langs[1:]), langs[1:])
        self.assertEqual(set(langs), set(CATALOGS))

    def test_script_detection(self):
        d = i18n.detect_script_language
        self.assertEqual(d("नमस्ते कैसे हो आप"), "hi")
        self.assertEqual(d("مرحبا كيف حالك"), "ar")
        self.assertEqual(d("你好，今天怎么样"), "zh")
        self.assertEqual(d("Привет, как дела?"), "ru")
        for none in ("hello how are you", "kya haal hai bhai", "hola qué tal", "", None, "こんにちは、元気ですか", "안녕하세요", "ok 好"):
            self.assertIsNone(d(none), none)

    def test_language_directive(self):
        self.assertEqual(i18n.language_directive("en"), "")
        self.assertEqual(i18n.language_directive(None), "")
        self.assertEqual(i18n.language_directive("zz"), "")
        self.assertIn("Spanish", i18n.language_directive("es-MX"))
        self.assertIn("Latin alphabet", i18n.language_directive("hl"))
        self.assertIn("match theirs", i18n.language_directive("hi"))

    def test_rtl(self):
        self.assertTrue(i18n.is_rtl("ar"))
        self.assertFalse(i18n.is_rtl("hi"))


class TestKeysUsedInCode(unittest.TestCase):
    """Every literal key passed to t()/tr()/L()/_say() exists in English; every key in English is used somewhere."""

    @classmethod
    def setUpClass(cls):
        cls.used = set()
        for top in ("bot",):
            for path in (ROOT / top).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                        if name in ("t", "tr", "L", "_say") and node.args:
                            for arg in node.args[:2]:
                                for sub in ast.walk(arg):  # also keys chosen by a conditional: t("a" if x else "b", lang)
                                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "." in sub.value and " " not in sub.value:
                                        cls.used.add(sub.value)
                    # f-string keys like L('help.cat.moderation') inside f-strings are ordinary Call nodes: covered above

    def test_every_used_key_exists(self):
        missing = sorted(k for k in self.used if k not in CATALOGS["en"] and k.split(".")[0] in {"lang", "common", "start", "help", "menu", "prefs"})
        self.assertEqual(missing, [])

    def test_every_catalog_key_is_used(self):
        # menu.btn.<panel key> entries are looked up dynamically; TestMenuTranslations checks them against the real menu instead
        unused = sorted(k for k in CATALOGS["en"] if k not in self.used and not k.startswith("menu.btn."))
        self.assertEqual(unused, [], "a catalog entry nothing refers to (remove it, or use it)")


class TestMenuTranslations(unittest.TestCase):
    """The /menu keeps its English MAIN_MENU/MENU_PANELS literals (callback data and contract tests depend on them); labels and titles
    are translated at render time from menu.btn.<key>.  These tests tie the catalogs to the real menu."""

    @classmethod
    def setUpClass(cls):
        src = (ROOT / "bot/plugins/start.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        cls.pages, cls.overrides, cls.panels = {}, {}, {}
        keep = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ("MAIN_MENU", "PAGE_2", "PAGE_3"):
                cls.pages[node.targets[0].id] = ast.literal_eval(node.value)
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_KEY_OVERRIDES":
                cls.overrides = ast.literal_eval(node.value)
                keep.append(node)
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MENU_PANELS":
                cls.panels = ast.literal_eval(node.value)
            if isinstance(node, ast.FunctionDef) and node.name in ("_callback_key", "_localized_label"):
                keep.append(node)
        cls.ns = {"i18n": i18n}
        exec(compile(ast.Module(body=sorted(keep, key=lambda n: not isinstance(n, ast.AnnAssign)), type_ignores=[]), "start_helpers", "exec"), cls.ns)
        cls.labels = [label for page in cls.pages.values() for row in page for label in row]

    def test_catalog_has_exactly_one_entry_per_menu_button(self):
        keys = {f"menu.btn.{self.ns['_callback_key'](lab)}" for lab in self.labels}
        for code, cat in CATALOGS.items():
            with self.subTest(language=code):
                self.assertEqual({k for k in cat if k.startswith("menu.btn.")}, keys)

    def test_english_catalog_text_is_the_english_label(self):
        for lab in self.labels:
            self.assertEqual(lab.partition(" ")[2], CATALOGS["en"][f"menu.btn.{self.ns['_callback_key'](lab)}"], lab)

    def test_every_panel_can_be_titled_in_every_language(self):
        for key in self.panels:
            for code in CATALOGS:
                with self.subTest(panel=key, language=code):
                    self.assertIn(f"menu.btn.{key}", CATALOGS[code])

    def test_translation_changes_the_label_but_never_the_callback(self):
        f, ck = self.ns["_localized_label"], self.ns["_callback_key"]
        self.assertEqual(f("🛡️ Admin", "en"), "🛡️ Admin")
        self.assertEqual(f("🛡️ Admin", "hi"), "🛡️ " + CATALOGS["hi"]["menu.btn.admin"])
        self.assertEqual(f("🛡️ Admin", "xx"), "🛡️ Admin")                                  # unknown language -> English label
        self.assertEqual(f("🎧 Live Music", "es"), "🎧 Música en vivo")
        self.assertEqual(f("🛡️ Admin & moderation", "es", "admin"), "🛡️ Admin")            # titles use the panel key, not their last word
        self.assertEqual(f("🛡️ Admin & moderation", "ru", "admin"), "🛡️ Админ")
        for lab in self.labels:                                                              # callbacks always derive from the English label
            self.assertEqual(ck(lab), ck(lab))
            self.assertNotIn(ck(lab), ("",))
        self.assertEqual(ck("🎧 Live Music"), "live")
        self.assertEqual(ck("🧰 Admin Tools"), "admintools")

    def test_every_translated_label_is_non_empty_and_has_no_markup(self):
        for code, cat in CATALOGS.items():
            for k, v in cat.items():
                if k.startswith("menu.btn."):
                    with self.subTest(language=code, key=k):
                        self.assertTrue(v.strip())
                        self.assertNotIn("<", v)
                        self.assertLessEqual(len(v), 24, "keeps the button readable on a phone")


class TestEnglishIsUnchanged(unittest.TestCase):
    """Translating must not alter what English users see.  The fixture holds the pre-i18n English text; only the lines that were added on
    purpose (the /lang hints) may differ."""

    @classmethod
    def setUpClass(cls):
        cls.fx = json.loads((ROOT / "tests" / "english_ui_fixture.json").read_text(encoding="utf-8"))
        tree = ast.parse((ROOT / "bot/plugins/start.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "help_command")
        cls.help_list = next(n for n in fn.body if isinstance(n, ast.Assign) and n.targets[0].id == "text").value.args[0]

    @staticmethod
    def en(key, **values):
        return i18n.t(key, "en", **values)

    def test_start_text(self):
        text = "\n".join([self.en("start.welcome", name="USER"), "", self.en("start.tagline", bot="NEKO"), self.en("start.commands"),
                          self.en("start.tap"), self.en("start.language")])
        self.assertEqual(text, self.fx["start"] + "\n🌐 Language: /lang")

    def test_help_text(self):
        expr = ast.fix_missing_locations(ast.Expression(body=ast.Call(
            func=ast.Attribute(value=ast.Constant("\n"), attr="join", ctx=ast.Load()), args=[self.help_list], keywords=[])))
        text = eval(compile(expr, "help", "eval"), {"i18n": i18n, "lang": "en", "bot_name": lambda: "NEKO", "L": lambda k: i18n.t(k, "en")})
        self.assertEqual(text, self.fx["help"] + "\n<i>🌐 Change the language: /lang</i>")

    def test_settings_text(self):
        text = "\n".join([self.en("prefs.title", bot="NEKO"), "",
                          self.en("prefs.personality", state=self.en("prefs.state.on_dm")),
                          self.en("prefs.live", state=self.en("prefs.live.missing")), self.en("prefs.ai"), self.en("prefs.more")])
        self.assertEqual(text, self.fx["settings"])
        self.assertEqual([self.en(f"prefs.state.{k}") for k in ("default", "on", "off", "on_dm", "off_dm")],
                         ["default", "on", "off", "on (DM default)", "off (DM default)"])
        self.assertEqual(self.en("prefs.live.configured"), "configured")

    def test_menu_header_and_navigation(self):
        self.assertEqual(self.en("menu.header", bot="NEKO", page=2, total=3), self.fx["menu_header"])
        self.assertEqual(self.en("menu.back"), self.fx["menu_back"])
        self.assertEqual(self.en("menu.close"), self.fx["menu_close"])


class TestCommandTokensSurvive(unittest.TestCase):
    """Every /command, ENV_VAR and option word that the English text shows must still be there after translation."""

    @staticmethod
    def tokens(text):
        plain = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">")
        return set(re.findall(r"(?<![\w<])/[a-z_]{2,}", plain)) | set(re.findall(r"\b[A-Z][A-Z0-9]+_[A-Z0-9_/]+\b", plain))

    def test_tokens_are_preserved_in_every_language(self):
        for key, en in CATALOGS["en"].items():
            need = self.tokens(en)
            for code, cat in CATALOGS.items():
                with self.subTest(key=key, language=code):
                    self.assertLessEqual(need, self.tokens(cat[key]), f"{key} lost {need - self.tokens(cat[key])}")


class TestScriptSanity(unittest.TestCase):
    """A coarse guard against an entry that was left in English (or pasted in the wrong language): in the non-Latin catalogs every sentence
    must contain at least one character of that language's script.  Brand-like labels (AI, AFK, IMDb, QR...) are exempt."""

    SCRIPT = {"hi": re.compile(r"[\u0900-\u097F]"), "ru": re.compile(r"[\u0400-\u04FF]"), "ar": re.compile(r"[\u0600-\u06FF]"),
              "zh": re.compile(r"[\u4E00-\u9FFF]")}

    def test_non_latin_catalogs_use_their_script(self):
        for code, rx in self.SCRIPT.items():
            for key, text in CATALOGS[code].items():
                if key.startswith(("menu.btn.", "help.cat.")):
                    continue
                plain = re.sub(r"<[^>]+>|\{[^}]+\}|&[a-z]+;", "", text)
                with self.subTest(language=code, key=key):
                    self.assertTrue(rx.search(plain), f"no {code} text in {text!r}")

    def test_script_labels_that_are_not_brands_use_their_script(self):
        brands = {"AI", "AFK", "IMDb", "QR", "TTS", "Urban", "Stickers", "Admin", "Info", "Karma"}
        for code, rx in self.SCRIPT.items():
            for key, text in CATALOGS[code].items():
                if key.startswith(("menu.btn.", "help.cat.")) and text not in brands:
                    with self.subTest(language=code, key=key):
                        self.assertTrue(rx.search(text) or text in brands or re.fullmatch(r"[A-Za-z0-9 ()/@-]+", text) is None,
                                        f"{key}: {text!r}")


class TestCatalogsAreShipped(unittest.TestCase):
    """data/* is git-ignored except named paths: a catalog directory that is not on that list silently disappears from a git deploy
    and /start would print raw keys.  Docker (COPY . .) is checked against .dockerignore the same way."""

    def test_gitignore_does_not_hide_the_catalogs(self):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            shutil.copy(ROOT / ".gitignore", tmp)
            (Path(tmp) / "data" / "locales").mkdir(parents=True)
            (Path(tmp) / "data" / "locales" / "en.json").write_text("{}", encoding="utf-8")
            result = subprocess.run(["git", "-C", tmp, "check-ignore", "-q", "data/locales/en.json"])
            self.assertEqual(result.returncode, 1, "data/locales/ is git-ignored: add `!data/locales/` to .gitignore")

    def test_dockerignore_does_not_exclude_the_catalogs(self):
        import fnmatch
        patterns = [ln.strip().rstrip("/") for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.startswith("#") and not ln.startswith("!")]
        for pattern in patterns:
            self.assertFalse(fnmatch.fnmatch("data/locales/en.json", pattern) or fnmatch.fnmatch("data/locales", pattern)
                             or fnmatch.fnmatch("data", pattern), f".dockerignore pattern {pattern!r} would drop the catalogs")


class TestWiring(unittest.TestCase):
    def test_lang_plugin_registers_its_commands(self):
        tree = ast.parse((ROOT / "bot/plugins/language.py").read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "CommandHandler":
                names += [e.value for e in node.args[0].elts]
        self.assertEqual(sorted(names), ["lang", "language", "languages", "setlang"])

    def test_chat_model_has_a_language_column(self):
        self.assertIn("language = Column(String(8), nullable=True)", (ROOT / "bot/core/database.py").read_text(encoding="utf-8"))

    def test_default_supported_languages_match_the_catalogs(self):
        src = (ROOT / "bot/config.py").read_text(encoding="utf-8")
        for code in CATALOGS:
            self.assertIn(f'"{code}"', src.split("supported_languages")[1].split("alias=")[0], code)

    def test_ai_paths_only_get_a_directive_for_non_english(self):
        src = (ROOT / "bot/plugins/ai.py").read_text(encoding="utf-8")
        self.assertIn("i18n.language_directive(language)", src)
        self.assertIn('return {"language": lang} if lang else {}', src)          # English path keeps the original call signature


if __name__ == "__main__":
    unittest.main()
