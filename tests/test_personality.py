"""Tests for the Neko personality layer v2.3.

Covers:
  - Custom instruction parsing (presets, numeric overrides, word-boundary matching)
  - Metaphor regex colon matching (v2.3 fix)
  - Short message trigger excludes known keywords (v2.3 fix)
  - Deadpan-safe preset flag (v2.3)
  - Anti-repetition tracking (v2.3)
  - PersonalityConfig defaults and overrides
  - Trigger engine basic patterns
  - Joke database basic operations
  - Memory manager data structures

Note: These are unit tests for logic that doesn't require a database.
Database-dependent tests are marked as skipped.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add the project root to sys.path so imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestCustomInstructions(unittest.TestCase):
    """Test custom instruction parsing (v2.3 fixes)."""

    def setUp(self):
        # Mock the logger and settings to avoid import errors
        if not os.path.exists("data/personality/skins"):
            os.makedirs("data/personality/skins", exist_ok=True)
        if not os.path.exists("data/personality/skins/nanora.json"):
            with open("data/personality/skins/nanora.json", "w") as f:
                json.dump({}, f)

    @patch("bot.utils.logger.get_logger")
    def test_cute_preset_detection(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("be cute and soft")
        self.assertEqual(traits.tone, "cute")
        self.assertAlmostEqual(traits.sarcasm_level, 0.05)
        self.assertFalse(traits.deadpan)
        self.assertFalse(traits.deadpan_safe)

    @patch("bot.utils.logger.get_logger")
    def test_chaotic_preset_detection(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("be chaotic and unhinged")
        self.assertEqual(traits.tone, "chaotic")
        self.assertAlmostEqual(traits.sarcasm_level, 1.0)

    @patch("bot.utils.logger.get_logger")
    def test_medieval_preset_with_themed_metaphors(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("speak like a medieval knight")
        self.assertEqual(traits.tone, "medieval")
        self.assertIn("programming", traits.themed_metaphors)
        self.assertGreater(len(traits.themed_metaphors["programming"]), 0)

    @patch("bot.utils.logger.get_logger")
    def test_explicit_sarcasm_override(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("sarcasm: 0.7")
        self.assertAlmostEqual(traits.sarcasm_level, 0.7)

    @patch("bot.utils.logger.get_logger")
    def test_metaphor_regex_colon_matching(self, mock_logger):
        """v2.3 fix: metaphor regex should handle colon separator."""
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        # This should now work with the fixed regex
        traits = proc.parse("metaphors: 0.5")
        self.assertAlmostEqual(traits.metaphor_frequency, 0.5)

    @patch("bot.utils.logger.get_logger")
    def test_metaphor_regex_with_frequency_keyword(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("metaphor frequency 0.3")
        self.assertAlmostEqual(traits.metaphor_frequency, 0.3)

    @patch("bot.utils.logger.get_logger")
    def test_word_boundary_keyword_matching(self, mock_logger):
        """v2.3 fix: 'kind' should not match 'unkind', 'mean' should not match 'meaning'."""
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        # "meaning" should NOT trigger the "mean" preset
        traits = proc.parse("the meaning of life")
        self.assertNotEqual(traits.tone, "mean")
        # "unkind" should NOT trigger "kind" in cute preset
        traits2 = proc.parse("you are unkind to me")
        # "unkind" shouldn't match "kind" with word boundaries
        # Note: "unkind" contains "kind" but word boundary prevents match
        self.assertNotEqual(traits2.tone, "cute")

    @patch("bot.utils.logger.get_logger")
    def test_deadpan_safe_flag(self, mock_logger):
        """v2.3: Each preset has a deadpan_safe flag."""
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        # Robot preset is deadpan_safe=True
        traits_robot = proc.parse("be a robot, mechanical and computing")
        self.assertTrue(traits_robot.deadpan_safe)
        # Cute preset is deadpan_safe=False
        traits_cute = proc.parse("be cute and wholesome")
        self.assertFalse(traits_cute.deadpan_safe)

    @patch("bot.utils.logger.get_logger")
    def test_free_text_falls_back_to_defaults(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("be really sarcastic but also nice sometimes")
        # Should have persona_context set
        self.assertTrue(traits.persona_context)

    @patch("bot.utils.logger.get_logger")
    def test_empty_instructions(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("")
        self.assertEqual(traits.tone, "")

    @patch("bot.utils.logger.get_logger")
    def test_available_presets(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        presets = proc.available_presets()
        self.assertIn("cute", presets)
        self.assertIn("chaotic", presets)
        self.assertIn("medieval", presets)
        self.assertGreaterEqual(len(presets), 10)


class TestTriggers(unittest.TestCase):
    """Test trigger engine patterns (v2.3 fixes)."""

    @patch("bot.utils.logger.get_logger")
    def test_short_message_excludes_known_words(self, mock_logger):
        """v2.3 fix: short_message should not match 'hi', 'lol', 'gg', etc."""
        from bot.personality.triggers import TriggerEngine
        engine = TriggerEngine()

        # "hi" should match greeting, NOT short_message
        matches = engine.analyze("hi")
        intents = [m.intent for m in matches]
        self.assertIn("greeting", intents)
        self.assertNotIn("short_message", intents)

        # "lol" should match meme, NOT short_message
        matches = engine.analyze("lol")
        intents = [m.intent for m in matches]
        self.assertNotIn("short_message", intents)

        # "str" (an unknown short word) SHOULD match short_message
        matches = engine.analyze("str")
        intents = [m.intent for m in matches]
        self.assertIn("short_message", intents)

    @patch("bot.utils.logger.get_logger")
    def test_math_trigger(self, mock_logger):
        from bot.personality.triggers import TriggerEngine
        engine = TriggerEngine()
        matches = engine.analyze("what is 2+2")
        intents = [m.intent for m in matches]
        self.assertIn("math", intents)

    @patch("bot.utils.logger.get_logger")
    def test_japanese_romaji_trigger(self, mock_logger):
        from bot.personality.triggers import TriggerEngine
        engine = TriggerEngine()
        matches = engine.analyze("gomenasai")
        intents = [m.intent for m in matches]
        self.assertIn("anime_greeting", intents)


class TestPersonalityConfig(unittest.TestCase):
    """Test PersonalityConfig defaults."""

    @patch("bot.utils.logger.get_logger")
    def test_default_config(self, mock_logger):
        from bot.personality.personality import PersonalityConfig
        cfg = PersonalityConfig()
        self.assertAlmostEqual(cfg.sarcasm_level, 0.8)
        self.assertTrue(cfg.deadpan)
        self.assertEqual(cfg.max_response_length, 400)

    @patch("bot.utils.logger.get_logger")
    def test_config_override(self, mock_logger):
        from bot.personality.personality import PersonalityConfig
        cfg = PersonalityConfig(sarcasm_level=0.1, deadpan=False)
        self.assertAlmostEqual(cfg.sarcasm_level, 0.1)
        self.assertFalse(cfg.deadpan)


class TestAntiRepetition(unittest.TestCase):
    """Test v2.3 anti-repetition tracking."""

    @patch("bot.utils.logger.get_logger")
    @patch("bot.config.settings")
    @patch("bot.personality.personality.SkinLoader")
    def test_repeated_response_is_modified(self, mock_skin_loader, mock_settings, mock_logger):
        from bot.personality.personality import PersonalityLayer
        mock_settings.enable_personality = True

        layer = PersonalityLayer()
        chat_id = 12345

        # Record a response
        result1 = layer._check_and_record_response(chat_id, "Hello world")
        self.assertEqual(result1, "Hello world")

        # Same response again should be modified
        result2 = layer._check_and_record_response(chat_id, "Hello world")
        # It should be different from the original
        self.assertNotEqual(result2, "Hello world")

    @patch("bot.utils.logger.get_logger")
    @patch("bot.config.settings")
    @patch("bot.personality.personality.SkinLoader")
    def test_unique_responses_pass_through(self, mock_skin_loader, mock_settings, mock_logger):
        from bot.personality.personality import PersonalityLayer
        mock_settings.enable_personality = True

        layer = PersonalityLayer()
        chat_id = 99999

        result1 = layer._check_and_record_response(chat_id, "First response")
        result2 = layer._check_and_record_response(chat_id, "Second response")
        result3 = layer._check_and_record_response(chat_id, "Third response")

        self.assertEqual(result1, "First response")
        self.assertEqual(result2, "Second response")
        self.assertEqual(result3, "Third response")


class TestJokeDatabase(unittest.TestCase):
    """Test joke database basic operations.

    Uses an isolated temp directory + an explicit `path=` argument so this
    test can never touch the real data/jokes.json. The original version
    wrote straight to that relative path unconditionally, which — if the
    suite is ever run from the project root, as `PROJECT_ROOT` here
    implies it will be — silently overwrote and truncated the real
    95-entry joke bank down to 2 test entries.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.jokes_path = Path(self._tmpdir.name) / "jokes.json"
        test_jokes = [
            {"id": "test_1", "category": "programming", "text": "Why do programmers prefer dark mode? Because light attracts bugs."},
            {"id": "test_2", "category": "general", "text": "I told my computer I needed a break, and it said 'No problem, I'll go to sleep.'"},
        ]
        self.jokes_path.write_text(json.dumps(test_jokes), encoding="utf-8")

    def tearDown(self):
        self._tmpdir.cleanup()

    @patch("bot.utils.logger.get_logger")
    def test_get_random_joke(self, mock_logger):
        from bot.personality.jokes import JokeDatabase
        db = JokeDatabase(path=self.jokes_path)
        joke = db.get_random("programming")
        self.assertIsNotNone(joke)
        self.assertEqual(joke.category, "programming")

    @patch("bot.utils.logger.get_logger")
    def test_exclude_ids(self, mock_logger):
        from bot.personality.jokes import JokeDatabase
        db = JokeDatabase(path=self.jokes_path)
        joke = db.get_random("programming", exclude_ids=["test_1"])
        self.assertIsNone(joke)

    @patch("bot.utils.logger.get_logger")
    def test_count(self, mock_logger):
        from bot.personality.jokes import JokeDatabase
        db = JokeDatabase(path=self.jokes_path)
        self.assertEqual(db.count(), 2)


class TestMemoryDataStructures(unittest.TestCase):
    """Test memory manager data structures (no DB required)."""

    def test_chat_personality_settings_defaults(self):
        from bot.personality.memory import ChatPersonalitySettings
        s = ChatPersonalitySettings()
        self.assertIsNone(s.custom_instructions)
        self.assertIsNone(s.user_limit)
        self.assertIsNone(s.response_delay)
        self.assertIsNone(s.skin)

    def test_message_context(self):
        from bot.personality.memory import MessageContext
        ctx = MessageContext(role="user", content="hello", timestamp="2024-01-01T00:00:00")
        self.assertEqual(ctx.role, "user")
        self.assertEqual(ctx.content, "hello")


class TestWordBoundaryMatching(unittest.TestCase):
    """Test the shared contains_word() helper subplugins use for keyword
    detection. Plain substring `kw in text` checks used to false-positive
    on "gg" inside "struggling", "play" inside "display", and "lag"
    inside "flag" — this was true since the original bot and survived
    unchanged through every reviewed revision."""

    def test_short_keyword_does_not_match_inside_longer_word(self):
        from bot.personality.subplugins.base import contains_word
        self.assertFalse(contains_word("i am struggling with this", ["gg"]))
        self.assertFalse(contains_word("check the css display property", ["play"]))
        self.assertFalse(contains_word("raise the flag", ["lag"]))

    def test_short_keyword_matches_as_a_whole_word(self):
        from bot.personality.subplugins.base import contains_word
        self.assertTrue(contains_word("gg well played", ["gg"]))
        self.assertTrue(contains_word("let's play a game", ["play"]))
        self.assertTrue(contains_word("the lag is unbearable", ["lag"]))

    def test_multi_word_phrase_matching(self):
        from bot.personality.subplugins.base import contains_word
        self.assertTrue(contains_word("that's a skill issue honestly", ["skill issue"]))
        self.assertFalse(contains_word("no skills issued here", ["skill issue"]))


class TestLLMPersonaRewriteGating(unittest.TestCase):
    """v2.3 added settings.personality_llm_enabled (default False) but
    _try_llm_rewrite never checked it, so the "opt-in" LLM persona
    rewrite would fire for any chat with custom instructions set the
    moment any AI provider key was configured for something unrelated
    like /ai. These confirm the gate is actually enforced."""

    @patch("bot.utils.logger.get_logger")
    @patch("bot.personality.personality.settings")
    @patch("bot.personality.personality.SkinLoader")
    def test_disabled_by_default_returns_none_without_calling_out(
        self, mock_skin_loader, mock_settings, mock_logger,
    ):
        import asyncio
        from bot.personality.personality import PersonalityLayer
        from bot.personality.custom_instructions import InstructionTraits

        mock_settings.enable_personality = True
        mock_settings.personality_llm_enabled = False

        layer = PersonalityLayer()
        traits = InstructionTraits(tone="pirate", persona_context="Talk like a pirate.")

        result = asyncio.run(layer._try_llm_rewrite(traits, "base response", "ahoy"))
        self.assertIsNone(result)

    @patch("bot.utils.logger.get_logger")
    @patch("bot.personality.personality.settings")
    @patch("bot.personality.personality.SkinLoader")
    def test_enabled_but_no_persona_context_returns_none(
        self, mock_skin_loader, mock_settings, mock_logger,
    ):
        import asyncio
        from bot.personality.personality import PersonalityLayer
        from bot.personality.custom_instructions import InstructionTraits

        mock_settings.enable_personality = True
        mock_settings.personality_llm_enabled = True

        layer = PersonalityLayer()
        traits = InstructionTraits(tone="", persona_context="")

        result = asyncio.run(layer._try_llm_rewrite(traits, "base response", "hello"))
        self.assertIsNone(result)


class TestPresetExternalization(unittest.TestCase):
    """All 10 presets should load from JSON now, not just the 3
    (cute, chaotic, medieval) that originally shipped with data files —
    the other 7 silently fell back to Python-only defaults, which meant
    admins couldn't actually edit them without a code change despite
    that being the whole point of externalizing presets to JSON."""

    @patch("bot.utils.logger.get_logger")
    def test_all_ten_presets_available(self, mock_logger):
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        presets = set(proc.available_presets())
        original_ten = {
            "cute", "serious", "chaotic", "medieval", "pirate",
            "robot", "supportive", "mean", "shy", "energetic",
        }
        # The original ten must still all be present — the personality
        # pack only adds new named presets, never removes or renames
        # these. Beyond that, more presets is a feature: don't assert
        # an exact count here, or this test breaks every time a new
        # preset ships.
        self.assertTrue(original_ten.issubset(presets))
        self.assertGreaterEqual(len(presets), len(original_ten))

    @patch("bot.utils.logger.get_logger")
    def test_preset_match_preserves_user_wording(self, mock_logger):
        """A matched preset shouldn't silently discard the user's own
        phrasing beyond the matched keyword — persona_context should
        carry both the preset's voice and the user's literal request."""
        from bot.personality.custom_instructions import CustomInstructionProcessor
        proc = CustomInstructionProcessor()
        traits = proc.parse("be chaotic and obsessed with pineapples")
        self.assertEqual(traits.tone, "chaotic")
        self.assertIn("pineapples", traits.persona_context)


if __name__ == "__main__":
    unittest.main()


class TestExampleBank(unittest.TestCase):
    """Tests for the curated few-shot bank derived from the conversation dataset."""

    @patch("bot.utils.logger.get_logger")
    def test_bank_loads(self, mock_logger):
        from bot.personality.examples import ExampleBank
        bank = ExampleBank("data/personality/dataset/fewshot_bank.json")
        bank.load()
        self.assertGreater(bank.count, 50)
        self.assertIn("Identity / Personality", bank.categories())

    @patch("bot.utils.logger.get_logger")
    def test_relevant_retrieval(self, mock_logger):
        from bot.personality.examples import ExampleBank
        bank = ExampleBank("data/personality/dataset/fewshot_bank.json")
        bank.load()
        hits = bank.get_relevant("who are you and what is your personality", limit=3)
        self.assertTrue(len(hits) >= 1)
        for h in hits:
            self.assertIn("question", h)
            self.assertIn("response", h)
            # No old branding
            self.assertNotIn("Nanora", h.get("response", ""))
            self.assertNotIn("Sari", h.get("response", ""))

    @patch("bot.utils.logger.get_logger")
    def test_format_for_prompt(self, mock_logger):
        from bot.personality.examples import ExampleBank
        bank = ExampleBank("data/personality/dataset/fewshot_bank.json")
        bank.load()
        # Specifically neko-tagged examples, not a cross-skin sample() —
        # format_for_prompt() now labels each line by its own example's
        # skin, so a mixed sample legitimately contains other labels
        # ("Tsundere:", "Cute:", ...) alongside or instead of "Neko:".
        sample = bank.get_by_personality("neko", 2)
        block = bank.format_for_prompt(sample, max_chars=800)
        self.assertIn("Neko:", block)
        self.assertNotIn("Nanora:", block)

    @patch("bot.utils.logger.get_logger")
    def test_full_dataset_cleaned(self, mock_logger):
        """Sanity: full cleaned jsonl exists and contains no Nanora/Sari."""
        path = Path("data/personality/dataset/full_cleaned.jsonl")
        if not path.exists():
            self.skipTest("full dataset not present in test env")
        bad = 0
        total = 0
        with path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                total += 1
                if "Nanora" in line or "Sari" in line or "@nanorabot" in line:
                    bad += 1
        self.assertGreater(total, 500)
        self.assertEqual(bad, 0, f"Found {bad} lines still containing old branding")


class TestIdentityHelpers(unittest.TestCase):
    @patch("bot.config.settings")
    def test_identity_defaults(self, mock_settings):
        mock_settings.bot_display_name = "Neko"
        mock_settings.bot_display_username = "Nekooooooobot"
        mock_settings.bot_owner_name = "Star"
        from bot.identity import bot_name, bot_username, owner_name
        self.assertEqual(bot_name(), "Neko")
        self.assertEqual(bot_username(), "Nekooooooobot")
        self.assertEqual(owner_name(), "Star")


class TestCacheLayer(unittest.TestCase):
    def test_memory_cache_roundtrip(self):
        import asyncio
        from bot.utils.cache import Cache
        c = Cache()
        # force memory backend
        c._backend = None
        c._init_attempted = False
        async def run():
            await c.set("k", {"a": 1}, ttl=10)
            v = await c.get("k")
            self.assertEqual(v, {"a": 1})
            n = await c.incr("counter", ttl=10)
            self.assertEqual(n, 1)
            n2 = await c.incr("counter", ttl=10)
            self.assertEqual(n2, 2)
        asyncio.run(run())


class TestValidation(unittest.TestCase):
    def test_require_positive_int(self):
        from bot.utils.validation import require_positive_int
        from bot.core.errors import ValidationError
        self.assertEqual(require_positive_int("42"), 42)
        with self.assertRaises(ValidationError):
            require_positive_int("-1")
        with self.assertRaises(ValidationError):
            require_positive_int("abc")

    def test_sanitize_display_name(self):
        from bot.utils.validation import sanitize_display_name
        self.assertEqual(sanitize_display_name("Alice"), "Alice")
        self.assertEqual(sanitize_display_name(""), "User")


class TestErrors(unittest.TestCase):
    def test_neko_error_message(self):
        from bot.core.errors import NekoError, ValidationError
        e = ValidationError(user_message="bad input")
        self.assertEqual(e.user_message, "bad input")
        e2 = NekoError("internal")
        self.assertTrue(len(e2.user_message) > 0)
