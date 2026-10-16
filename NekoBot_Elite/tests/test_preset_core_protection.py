"""PresetLoader core-preset protection (restored from the first merged snapshot; its guard once checked *skin* names).

Regression for a real bug: PresetLoader.delete()'s "protect core presets" guard was copy-pasted from SkinLoader.delete() and
still compared against skin names ({"neko", "nanora"}) instead of preset names, so every built-in trait preset (cute, serious,
chaotic, ...) could be deleted with /pdel preset <name> and no override at all.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot.personality.custom_instructions import PresetLoader, _DEFAULT_PRESETS


class TestPresetLoaderCoreProtection(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.loader = PresetLoader(presets_dir=self.tmpdir.name)
        self.core_name = next(iter(_DEFAULT_PRESETS))
        # A built-in preset is not a file until saved once (the loader falls back to _DEFAULT_PRESETS in memory), so write it
        # out: a deployed bot has usually materialised it already through /padd or an older save.
        self.loader.save(self.core_name, _DEFAULT_PRESETS[self.core_name], overwrite=True)

    def test_deleting_a_core_preset_without_override_is_refused(self):
        with self.assertRaises(PermissionError):
            self.loader.delete(self.core_name)
        self.assertTrue((Path(self.tmpdir.name) / f"{self.core_name}.json").exists())

    def test_deleting_a_core_preset_with_override_succeeds(self):
        self.assertTrue(self.loader.delete(self.core_name, allow_core=True))
        self.assertFalse((Path(self.tmpdir.name) / f"{self.core_name}.json").exists())

    def test_deleting_a_non_core_preset_needs_no_override(self):
        self.loader.save("a_custom_persona", {"persona_context": "x", "keywords": ["zzzqqq"]})
        self.assertTrue(self.loader.delete("a_custom_persona"))

    def test_a_skin_name_is_judged_as_a_preset_not_silently_protected(self):
        # The original bug protected the strings "neko"/"nanora" (core *skin* names).  A preset that merely has such a name
        # is an ordinary preset and must be deletable.
        self.loader.save("neko", {"persona_context": "x", "keywords": ["zzzqqq2"]})
        self.assertTrue(self.loader.delete("neko"))

    def test_every_builtin_preset_is_protected(self):
        for name in _DEFAULT_PRESETS:
            with self.subTest(preset=name), self.assertRaises(PermissionError):
                self.loader.delete(name)


if __name__ == "__main__":
    unittest.main()
