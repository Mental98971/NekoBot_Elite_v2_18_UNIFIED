"""PersonaEngine.prepare()/finalize() and the overlay-compatible integration API (synthetic data only; standard library)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.persona_engine.engine import EngineConfig, EngineRequest  # noqa: E402
from tests.test_persona_engine import _Tmp, make_engine, persona_dir, scripted, synth_examples  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def req(text="kya kar rahi ho?", chat=7):
    return EngineRequest(chat_id=chat, text=text, user_key="u1")


class TestPrepareFinalize(unittest.TestCase):
    def test_prepare_builds_the_honest_prompt_without_calling_a_model(self):
        calls = []
        eng = make_engine(scripted(["x"], record=calls))
        prep = eng.prepare(req())
        self.assertIsNone(prep.direct_reply)
        self.assertEqual([m["role"] for m in prep.messages], ["system", "user"])
        self.assertIn("You are not Pat", prep.messages[0]["content"])
        self.assertNotIn("Do NOT claim to be an AI", prep.messages[0]["content"])
        self.assertIn("[CONVERSATION", prep.messages[1]["content"])
        self.assertEqual(calls, [])                                   # no model call happened
        self.assertTrue(prep.evidence and prep.trace["situation"])

    def test_identity_question_gets_the_fixed_disclosure_and_no_prompt(self):
        eng = make_engine(scripted(["x"]))
        prep = eng.prepare(req("are you really Pat?"))
        self.assertEqual(prep.messages, [])
        self.assertIn("I'm a bot", prep.direct_reply)
        self.assertIn("not Pat", prep.direct_reply)

    def test_finalize_accepts_a_clean_reply_and_records_it(self):
        eng = make_engine(scripted(["x"]))
        eng.observe_user(req())
        out = eng.finalize(7, "kuch nahi yaar")
        self.assertEqual(out, "kuch nahi yaar")
        self.assertEqual(eng.short.turns(7)[-1].who, "persona")
        self.assertIsNone(eng.finalize(7, "kuch nahi yaar"))          # an immediate repeat is refused by the repetition guard

    def test_finalize_rejects_every_unsafe_reply(self):
        eng = make_engine(scripted(["x"]))
        for bad in ("yes it's me, Pat here", "I am Pat", "call me on +91 98765 43210", "mail me a@b.com", "t.me/someone", "darnit",
                    "as an AI language model I cannot", "", "   "):
            with self.subTest(bad=bad):
                self.assertIsNone(eng.finalize(7, bad))

    def test_finalize_cleans_labels_and_quotes(self):
        eng = make_engine(scripted(["x"]))
        self.assertEqual(eng.finalize(8, '"Pat: bas padh rhi"'), "bas padh rhi")

    def test_respond_still_works_through_prepare(self):
        eng = make_engine(scripted(["kuch nahi yaar", "bas padh rhi", "ghar pe hu"]), cfg=EngineConfig(seed=5, candidates=3))
        res = run(eng.respond(req()))
        self.assertIsNotNone(res)
        self.assertEqual(res.source, "llm")
        self.assertIn(res.text, {"kuch nahi yaar", "bas padh rhi", "ghar pe hu"})


class TestLegacyIntegration(_Tmp):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_legacy_integration_under_test", ROOT / "bot/personality/persona_engine/integration.py")
        cls.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.mod
        spec.loader.exec_module(cls.mod)

    def setUp(self):
        super().setUp()
        data = persona_dir(self.tmp)                                  # <tmp>/persons/p/corpus.jsonl
        persons_dir = data.dir.parent
        (persons_dir / "registry.json").write_text(json.dumps({
            "schema": 1, "replay_filter": {"abusive_tokens": ["darnit"]},
            "persons": {"p": {"real_name": "Pat", "skin": "pat", "aliases": ["patty"],
                              "accounts": [{"id": 1001, "username": "pat_a"}, {"id": 1002, "username": "pat_b"}]}}}), encoding="utf-8")
        self.mod._reset_for_tests()
        self.mod._persons_module().reset_service(persons_dir)

    def tearDown(self):
        self.mod._reset_for_tests()
        super().tearDown()

    def test_lists_and_builds_engines_for_registered_persons(self):
        self.assertEqual(self.mod.list_available_persons(), ["p"])
        eng = self.mod.get_engine("p")
        self.assertIs(self.mod.get_engine("p"), eng)                  # cached
        self.assertEqual(eng.real_name, "Pat")
        with self.assertRaises(KeyError):
            self.mod.get_engine("nobody")

    def test_two_step_flow(self):
        eng = self.mod.get_engine("p")
        payload = self.mod.prepare_persona_reply(eng, "kya kar rahi ho?", chat_id="12345")
        self.assertEqual(payload["person_id"], "p")
        self.assertIsNone(payload["direct_reply"])
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertTrue(payload["evidence"] and set(payload["evidence"][0]) == {"id", "score"})     # metadata only, never dataset text
        self.assertEqual(eng.finalize("12345", "bas padh rhi"), "bas padh rhi")
        self.assertIsNone(eng.finalize("12345", "i'm Pat, really"))

    def test_identity_question_short_circuits(self):
        eng = self.mod.get_engine("p")
        payload = self.mod.prepare_persona_reply(eng, "are you a bot?", chat_id="9")
        self.assertEqual(payload["messages"], [])
        self.assertIn("I'm a bot", payload["direct_reply"])

    def test_non_numeric_chat_ids_are_stable(self):
        a, b = self.mod._chat_int("default"), self.mod._chat_int("default")
        self.assertEqual(a, b)
        self.assertGreater(a, 0)
        self.assertEqual(self.mod._chat_int("-100123"), -100123)

    def test_rebuild_profile_updates_the_engine(self):
        eng = self.mod.get_engine("p")
        before = eng.profile
        after = eng.rebuild_profile()
        self.assertIs(eng.core.profile, after)
        self.assertIsNot(before, after)
        self.assertEqual(after.n_messages, before.n_messages)

    def test_imitation_is_refused_when_consent_state_is_unknowable(self):
        async def _boom(_person_id):
            raise RuntimeError("consent store unreadable")

        self.assertFalse(self._call_gate(_boom))

    def test_imitation_is_allowed_when_consent_state_is_readable_and_not_opted_out(self):
        async def _not_opted_out(_person_id):
            return False

        self.assertTrue(self._call_gate(_not_opted_out))

    def test_imitation_is_refused_when_the_person_opted_out(self):
        async def _opted_out(_person_id):
            return True

        self.assertFalse(self._call_gate(_opted_out))

    def _call_gate(self, is_opted_out):
        import types
        from unittest import mock

        fake = types.ModuleType("bot.personality.person_consent")
        fake.is_opted_out = is_opted_out
        pkg = types.ModuleType("bot.personality")
        pkg.__path__ = []
        pkg.person_consent = fake
        with mock.patch.dict(sys.modules, {"bot.personality": pkg, "bot.personality.person_consent": fake}):
            return run(self.mod.is_imitation_allowed("p"))


if __name__ == "__main__":
    unittest.main()
