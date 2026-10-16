"""Real-person personas (Tanvi, Yashmeet): registry, identity resolution, data integrity, privacy.

Stdlib-only: loads bot/personality/persons.py without importing the bot package's heavy
dependencies, so it runs anywhere (CI included).
"""
from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONS_DIR = ROOT / "data" / "personality" / "persons"
SKINS_DIR = ROOT / "data" / "personality" / "skins"


def _load(name: str, rel: str):
    for pkg, path in (("bot", ROOT / "bot"), ("bot.personality", ROOT / "bot" / "personality")):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [str(path)]
            sys.modules[pkg] = m
    full = f"bot.personality.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def _lines(pid):
    with (PERSONS_DIR / pid / "corpus.jsonl").open(encoding="utf-8") as fh:
        return fh.read().splitlines()


pt = _load("persona_text", "bot/personality/persona_text.py")
persons = _load("persons", "bot/personality/persons.py")


class TestIdentityResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = persons.PersonRegistry.load(PERSONS_DIR / "registry.json")

    def test_tanvi_accounts_resolve_to_one_person(self):
        a = self.reg.resolve(user_id=7883544685)
        b = self.reg.resolve(user_id=7187486402)
        self.assertEqual((a.person.id, b.person.id), ("tanvi", "tanvi"))
        self.assertEqual(self.reg.resolve(username="You_are_mine_143").person.id, "tanvi")
        self.assertEqual(self.reg.resolve(username="@ICHIIREN").account.id, 7187486402)

    def test_yashmeet_resolves(self):
        self.assertEqual(self.reg.resolve(user_id=5553376334).person.id, "yashmeet")
        self.assertEqual(self.reg.resolve(username="concyclicity").person.real_name, "Yashmeet")

    def test_id_beats_username(self):
        # a foreign id carrying someone's username must not be hijacked by the username path... but an
        # unknown id with a registered username falls back to the username deterministically
        self.assertEqual(self.reg.resolve(user_id=5553376334, username="ichiiren").person.id, "yashmeet")
        self.assertEqual(self.reg.resolve(user_id=1, username="concyclicity").via, "username")

    def test_display_names_never_identify(self):
        self.assertIsNone(self.reg.resolve(username="Iziiee"))
        self.assertIsNone(self.reg.resolve(username="GALAXY ANHILATOR"))
        self.assertIsNone(self.reg.resolve(user_id=42))
        self.assertIsNone(self.reg.resolve())

    def test_aliases_map_to_skins(self):
        self.assertEqual(self.reg.canonical_skin("GALAXY_ANHILATOR"), "yashmeet")
        self.assertEqual(self.reg.canonical_skin("Iziiee"), "tanvi")
        self.assertEqual(self.reg.canonical_skin("neko"), "neko")

    def test_conflicting_registry_is_rejected(self):
        a = persons.Account(1, "x")
        with self.assertRaises(ValueError):
            persons.PersonRegistry([persons.Person("p", "P", "p", (), (a,)), persons.Person("q", "Q", "q", (), (a,))])
        with self.assertRaises(ValueError):
            persons.PersonRegistry([persons.Person("p", "P", "p", (), (persons.Account(1, "x"),)),
                                    persons.Person("q", "Q", "q", (), (persons.Account(2, "X"),))])

    def test_missing_or_broken_registry_disables_feature(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(persons.PersonRegistry.load(Path(d) / "nope.json"))
            bad = Path(d) / "r.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertFalse(persons.PersonRegistry.load(bad))
            bad.write_text(json.dumps({"persons": {"x": {"accounts": [{"id": "abc"}]}}}), encoding="utf-8")
            self.assertFalse(persons.PersonRegistry.load(bad))


class TestSkinsAndData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = persons.PersonService(PERSONS_DIR)

    def test_every_registry_skin_has_a_valid_skin_file(self):
        for name in self.svc.registry.skin_names():
            skin = json.loads((SKINS_DIR / f"{name}.json").read_text(encoding="utf-8"))
            self.assertEqual(skin["persona_kind"], "person")
            owner, acct = self.svc.registry.skin_owner(name)
            self.assertEqual(skin["person_id"], owner.id)
            self.assertEqual(skin.get("variant_account"), str(acct.id) if acct else None)
            self.assertTrue(skin["disclosure"])
            self.assertTrue(skin["direct_responses"]["general"])

    def test_variant_skins_use_only_their_own_account(self):
        d = self.svc.data("tanvi").load()
        for sk, aid in (("tanvi_iziiee", 7883544685), ("tanvi_ichiiren", 7187486402)):
            got = self.svc.data_for_skin(json.loads((SKINS_DIR / f"{sk}.json").read_text(encoding="utf-8")))
            self.assertEqual(got[1], aid)
            self.assertTrue(all(e.acct == aid for _s, e in d.retrieve("what are you doing today", 20, aid)))

    def test_profiles_are_computed_not_invented(self):
        for pid, accounts in (("tanvi", {"7883544685", "7187486402"}), ("yashmeet", {"5553376334"})):
            prof = self.svc.data(pid).load().profile
            self.assertEqual(set(prof["records"]["by_account"]), accounts)
            self.assertEqual(prof["records"]["total"], sum(prof["records"]["by_account"].values()))
            self.assertGreater(prof["style"]["n"], 1000)

    def test_no_cross_person_contamination(self):
        t = {json.loads(l)["id"] for l in _lines("tanvi")}
        y = {json.loads(l)["id"] for l in _lines("yashmeet")}
        self.assertFalse(t & y)
        for pid, ok_accts in (("tanvi", {"7883544685", "7187486402"}), ("yashmeet", {"5553376334"})):
            for l in _lines(pid):
                self.assertIn(json.loads(l)["acct"], ok_accts)

    def test_corpus_is_scrubbed(self):
        bad = re.compile(r"https?:|t\.me/|tg://|@(?!user\b)\w{3,}|\d{6,}|[\w.]+@[\w]+\.\w")
        for pid in ("tanvi", "yashmeet"):
            for l in _lines(pid):
                r = json.loads(l)
                for f in ("text", "ctx"):
                    self.assertIsNone(bad.search(r.get(f) or ""), (pid, f, r.get(f)))

    def test_replay_rows_have_no_abusive_or_name_tokens(self):
        abusive = self.svc.registry.abusive_tokens
        self.assertIn("bc", abusive)
        for pid in ("tanvi", "yashmeet"):
            for l in _lines(pid):
                r = json.loads(l)
                if r.get("rp"):
                    self.assertFalse(set(pt.tokens(r["text"])) & abusive, r["text"])
            prof = self.svc.data(pid).load().profile
            for tbl in (prof["short_replies"], prof["reply_by_context"]):
                for items in tbl.values():
                    for text, _c in items:
                        self.assertFalse(set(pt.tokens(text)) & abusive, text)

    def test_provenance_and_dedup(self):
        for pid in ("tanvi", "yashmeet"):
            ids = [json.loads(l)["id"] for l in _lines(pid)]
            self.assertEqual(len(ids), len(set(ids)))
        row = json.loads(_lines("tanvi")[0])
        self.assertTrue(row["src"])


class TestReplies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = persons.PersonService(PERSONS_DIR)

    def test_reply_is_short_deterministic_with_seed_and_in_pool(self):
        for pid in ("tanvi", "yashmeet"):
            d = self.svc.data(pid)
            r1 = d.reply("hello", None, random.Random(1))
            r2 = d.reply("hello", None, random.Random(1))
            self.assertEqual(r1, r2)
            self.assertLessEqual(len(r1), d.max_chars())
            self.assertFalse(set(pt.tokens(r1)) & self.svc.registry.abusive_tokens)

    def test_always_answers_even_for_unseen_input(self):
        d = self.svc.data("yashmeet")
        for msg in ("", "   ", "explain quantum chromodynamics in detail", "😭" * 50, "x" * 5000):
            out = d.reply(msg, None, random.Random(0))
            self.assertTrue(out and len(out) < 500)

    def test_reply_for_skin_ignores_non_person_skin(self):
        self.assertIsNone(self.svc.reply_for_skin({"name": "neko"}, "hi"))

    def test_conform(self):
        self.assertEqual(persons.conform('**Tanvi:** "lol okay"\n\nmore', "Tanvi", 60), "lol okay")
        self.assertLessEqual(len(persons.conform("word " * 100, "Tanvi", 50)), 50)
        self.assertEqual(persons.conform("", "Tanvi", 50), "")

    def test_prompt_discloses_and_uses_only_measured_facts(self):
        d = self.svc.data("tanvi")
        speaker = self.svc.registry.resolve(user_id=5553376334)
        p = persons.build_system_prompt(d, "hi there", speaker=speaker)
        self.assertIn("You are not Tanvi", p)
        self.assertIn("bot imitating their style", p)
        self.assertIn("The user you are talking to is Yashmeet", p)
        self.assertIn("Measured from their real messages", p)
        self.assertLess(len(p), 4000)
        self.assertNotIn("7883544685", p)

    def test_text_classifiers(self):
        self.assertEqual(pt.classify_utterance("Hieee"), "greeting")
        self.assertEqual(pt.classify_utterance("kya kar rahi ho"), "question")
        self.assertEqual(pt.classify_utterance("lmaooo"), "laughter")
        self.assertEqual(pt.message_kind("[Media/Non-text]"), "media")
        self.assertEqual(pt.message_kind("/start@bot"), "command")
        self.assertEqual(pt.message_kind("😭😭"), "emoji_only")
        self.assertNotIn("@", pt.scrub("hey @someone mail me a@b.com +91 98765 43210 https://x.y", 200).replace("@user", ""))


class TestIntegrationContracts(unittest.TestCase):
    """Static checks that the persona layer is wired into the existing pipeline (no bypass)."""

    def test_pipeline_wiring(self):
        pl = (ROOT / "bot/personality/personality.py").read_text(encoding="utf-8")
        self.assertIn("_resolve_active_skin", pl)
        self.assertGreaterEqual(pl.count('skin.get("persona_kind") == "person"'), 2)  # rewrite + generate_direct
        pp = (ROOT / "bot/plugins/personality.py").read_text(encoding="utf-8")
        for needle in ("_identity_key", "person_consent.is_opted_out", "cmd_pconsent", "cmd_ppersons", "cmd_pwho",
                       'skin_data.get("persona_kind") == "person"'):
            self.assertIn(needle, pp)
        self.assertIn("generate_persona_reply", (ROOT / "bot/plugins/ai.py").read_text(encoding="utf-8"))

    def test_settings_documented(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for k in ("PERSON_PERSONAS_ENABLED", "PERSON_PERSONA_DISCLOSURE"):
            self.assertIn(k, env)
            self.assertIn(k, (ROOT / "bot/config.py").read_text(encoding="utf-8"))

    def test_optin_is_limited_to_the_person(self):
        pp = (ROOT / "bot/plugins/personality.py").read_text(encoding="utf-8")
        self.assertIn("Only {target.real_name} can switch imitation back on", pp)

    def test_builder_is_deterministic_and_stdlib_only(self):
        src = (ROOT / "scripts/build_person_personas.py").read_text(encoding="utf-8")
        for mod in ("requests", "numpy", "pandas", "openai"):
            self.assertNotIn(f"import {mod}", src)


if __name__ == "__main__":
    unittest.main()
