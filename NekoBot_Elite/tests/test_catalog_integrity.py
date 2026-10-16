"""Regression guards recovered from older archives during the v2.10 audit.

Each class below existed in an older archive (named in its docstring) but had been dropped from
v2.9's tests even though the feature it protects is still shipped.  All of these run with the
standard library only; the one that needs the bot's dependency stack skips itself when that stack
is not installed (it runs in CI / the Docker image).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = PROJECT_ROOT / "data" / "personality" / "presets"
MANIFEST_PATH = PROJECT_ROOT / "data" / "personality" / "catalog_manifest.json"


def _load_by_path(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestImportedPersonaCatalog(unittest.TestCase):
    """From neko-bot-merged: guards the *shipped data* from scripts/import_personas.py."""

    ORDINARY_TONE_WORDS = {
        "calm", "quiet", "energy", "without", "time", "blunt", "fight", "light", "king", "doctor",
        "dark", "cold", "serious", "sassy", "funny", "brief", "direct", "polite", "rude", "friendly",
        "warm", "soft", "kind", "gentle", "cute", "chaotic", "wild",
    }
    # Content this project decided never to ship (see catalog_manifest.json "excluded").
    MUST_NEVER_BE_SHIPPED = {
        "adolf_hitler", "bill_gates", "messi", "neymar", "ronaldo", "michael_jordan", "jesus_christ",
        "muhammad", "gautama_buddha", "cult_leader", "cultist", "serial_killer", "hisoka",
        "shinobu_monogatari", "shinobu_oshino", "sss_suicide_gongja", "sss_suicide_hunter",
        "child", "toddler", "preteen", "teenager",
    }

    @classmethod
    def setUpClass(cls):
        cls.presets = {}
        for path in PRESETS_DIR.glob("*.json"):
            try:
                cls.presets[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise AssertionError(f"invalid preset file {path.name}: {exc}") from exc

    def _imported_ids(self):
        return set(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("imported", {}))

    def test_no_ordinary_tone_word_is_a_trigger_keyword_for_an_imported_character(self):
        offenders = {}
        for name in self._imported_ids() & set(self.presets):
            hit = self.ORDINARY_TONE_WORDS & {k.lower() for k in self.presets[name].get("keywords", [])}
            if hit:
                offenders[name] = hit
        self.assertFalse(offenders, f"ordinary tone words used as character triggers: {offenders}")

    def test_no_two_presets_share_an_identical_keyword_set(self):
        by_sig = {}
        for name, data in self.presets.items():
            kws = frozenset(k.lower() for k in data.get("keywords", []) if k)
            if kws:
                by_sig.setdefault(kws, []).append(name)
        dupes = {sig: names for sig, names in by_sig.items() if len(names) > 1}
        self.assertFalse(dupes, f"presets with identical trigger keywords (a permanent tie): {dupes}")

    def test_every_preset_has_at_least_one_keyword(self):
        empty = [n for n, d in self.presets.items() if not d.get("keywords")]
        self.assertFalse(empty, f"presets with no trigger keyword (unreachable): {empty}")

    def test_excluded_personas_are_not_on_disk(self):
        present = self.MUST_NEVER_BE_SHIPPED & set(self.presets)
        self.assertFalse(present, f"excluded content is present in the shipped catalog: {present}")

    def test_manifest_is_consistent_with_the_shipped_files(self):
        missing = self._imported_ids() - set(self.presets)
        self.assertFalse(missing, f"manifest claims these were imported but no preset file exists: {missing}")


class TestPaginateJoined(unittest.TestCase):
    """From neko-bot-merged: /plist output must be split below Telegram's message limit."""

    @classmethod
    def setUpClass(cls):
        cls.paginate_joined = staticmethod(_load_by_path("textutil_under_test", "bot/utils/textutil.py").paginate_joined)

    def test_short_list_fits_on_one_page(self):
        self.assertEqual(self.paginate_joined(["a", "b", "c"], max_len=100), ["a, b, c"])

    def test_long_list_splits_without_dropping_or_truncating_any_item(self):
        items = [f"persona_{i:04d}" for i in range(1500)]
        pages = self.paginate_joined(items, max_len=200)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(p) <= 200 for p in pages))
        self.assertEqual(", ".join(pages).split(", "), items)

    def test_empty_list_yields_no_pages(self):
        self.assertEqual(self.paginate_joined([], max_len=100), [])

    def test_current_preset_catalog_needs_pagination(self):
        names = sorted(p.stem for p in PRESETS_DIR.glob("*.json"))
        self.assertGreater(len(", ".join(names)), 4000)
        self.assertGreater(len(self.paginate_joined(names, max_len=3800)), 1)


class TestPresetLoaderCoreProtection(unittest.TestCase):
    """From neko-bot-merged: built-in trait presets must not be deletable without an override.

    Needs the bot's dependency stack (pydantic/structlog); skipped when it is not installed.
    """

    def setUp(self):
        try:
            from bot.personality.custom_instructions import PresetLoader, _DEFAULT_PRESETS
        except ImportError as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"bot dependency stack not installed: {exc}")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loader = PresetLoader(presets_dir=self.tmp.name)
        self.core = next(iter(_DEFAULT_PRESETS))
        self.loader.save(self.core, _DEFAULT_PRESETS[self.core], overwrite=True)

    def test_delete_core_without_override_is_refused(self):
        with self.assertRaises(PermissionError):
            self.loader.delete(self.core)
        self.assertTrue((Path(self.tmp.name) / f"{self.core}.json").exists())

    def test_delete_core_with_override_succeeds(self):
        self.assertTrue(self.loader.delete(self.core, allow_core=True))

    def test_delete_non_core_needs_no_override(self):
        self.loader.save("a_custom_persona", {"persona_context": "x", "keywords": ["zzzqqq"]})
        self.assertTrue(self.loader.delete("a_custom_persona"))

    def test_skin_name_collision_is_not_treated_as_core(self):
        self.loader.save("neko", {"persona_context": "x", "keywords": ["zzzqqq2"]})
        self.assertTrue(self.loader.delete("neko"))


class TestMenuPanels(unittest.TestCase):
    """Successor of Production's test_menu_labels_have_panel_keys, using the real key function.

    Every menu button must open a real panel (no dead buttons, no 'Feature panel for …' stubs), and
    every panel must be reachable from a button (no orphaned panels).
    """

    @classmethod
    def setUpClass(cls):
        src = (PROJECT_ROOT / "bot" / "plugins" / "start.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        cls.vals, fn_src = {}, None
        for n in tree.body:
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in {"MAIN_MENU", "PAGE_2", "PAGE_3"}:
                cls.vals[n.targets[0].id] = ast.literal_eval(n.value)
            elif isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") in {"MENU_PANELS", "_KEY_OVERRIDES"}:
                cls.vals[n.target.id] = ast.literal_eval(n.value)
            elif isinstance(n, ast.FunctionDef) and n.name == "_callback_key":
                fn_src = ast.get_source_segment(src, n)
        assert fn_src, "_callback_key not found in start.py"
        ns = {"_KEY_OVERRIDES": cls.vals["_KEY_OVERRIDES"], "re": re}
        exec(fn_src, ns)  # the function under test is pure; executing it in isolation avoids importing telegram
        cls.key = staticmethod(ns["_callback_key"])
        cls.src = src

    def _labels(self):
        return [l for page in ("MAIN_MENU", "PAGE_2", "PAGE_3") for row in self.vals[page] for l in row]

    def test_every_button_has_a_panel(self):
        missing = [(l, self.key(l)) for l in self._labels() if self.key(l) not in self.vals["MENU_PANELS"]]
        self.assertEqual(missing, [])

    def test_every_panel_is_reachable_and_button_keys_are_unique(self):
        keys = [self.key(l) for l in self._labels()]
        self.assertEqual(len(keys), len(set(keys)), "two buttons resolve to the same panel key")
        self.assertEqual(set(self.vals["MENU_PANELS"]) - set(keys), set(), "panels with no button")

    def test_panels_have_title_and_body(self):
        for key, value in self.vals["MENU_PANELS"].items():
            self.assertEqual(len(value), 2, key)
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in value), key)

    def test_no_generic_feature_panel_stub(self):
        self.assertNotIn("Feature panel for", self.src)


class TestCatchAllListenerGroupsDontCollide(unittest.TestCase):
    """From neko-bot-merged: PTB runs only the first matching handler per group, so two
    ``MessageHandler(filters.ALL)`` listeners in one group silently shadow each other."""

    def test_every_filters_all_messagehandler_has_a_unique_group(self):
        seen = {}
        for path in sorted((PROJECT_ROOT / "bot" / "plugins").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_handler"):
                    continue
                if not node.args or not isinstance(node.args[0], ast.Call):
                    continue
                call_src = ast.unparse(node.args[0])
                if "MessageHandler" not in call_src or "filters.ALL" not in call_src:
                    continue
                group_kw = next((kw for kw in node.keywords if kw.arg == "group"), None)
                group = ast.literal_eval(group_kw.value) if group_kw else 0
                self.assertNotIn(group, seen, f"group={group} used by both {seen.get(group)} and {path.name}")
                seen[group] = path.name
        self.assertGreater(len(seen), 0, "expected at least one filters.ALL listener")


if __name__ == "__main__":
    unittest.main()
