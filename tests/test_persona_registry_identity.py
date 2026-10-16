"""Identity resolution and engine smoke tests on the shipped persons data (ported from the PERSONA_ENGINE overlay's tests/persona).

Standard library only.  Numeric Telegram account ids are the only identity authority; display names are never ids.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONS = ROOT / "data" / "personality" / "persons"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def registry():
    return json.loads((PERSONS / "registry.json").read_text(encoding="utf-8"))


class TestRegistryIdentity(unittest.TestCase):
    def test_tanvi_accounts(self):
        tanvi = registry()["persons"]["tanvi"]
        self.assertEqual({a["id"] for a in tanvi["accounts"]}, {7883544685, 7187486402})
        self.assertEqual(tanvi["real_name"], "Tanvi")

    def test_yashmeet_account(self):
        yash = registry()["persons"]["yashmeet"]
        self.assertEqual({a["id"] for a in yash["accounts"]}, {5553376334})
        self.assertEqual(yash["real_name"], "Yashmeet")

    def test_sonal_account(self):
        sonal = registry()["persons"]["sonal"]
        self.assertEqual({a["id"] for a in sonal["accounts"]}, {5190868757})
        self.assertEqual(sonal["real_name"], "Sonal")
        self.assertEqual(sonal["accounts"][0]["username"], "FromPlanet_007897814")

    def test_every_registered_person_is_complete(self):
        """Registry entry -> corpus, skin, behaviour profile that matches the corpus, and a granted authorization record."""
        import hashlib
        auth = json.loads((ROOT / "training" / "authorizations.json").read_text(encoding="utf-8"))["persons"]
        for pid, person in registry()["persons"].items():
            with self.subTest(person=pid):
                corpus = PERSONS / pid / "corpus.jsonl"
                self.assertTrue(corpus.exists())
                self.assertTrue((ROOT / "data" / "personality" / "skins" / f"{person['skin']}.json").exists())
                profile = json.loads((PERSONS / pid / "behavior_profile.json").read_text(encoding="utf-8"))
                self.assertEqual(profile["source"]["corpus_sha256"], hashlib.sha256(corpus.read_bytes()).hexdigest(),
                                 "behaviour profile is stale: python scripts/build_behavior_profiles.py")
                self.assertEqual(auth[pid]["status"], "granted")
                self.assertIn("weights_training", auth[pid]["scope"])

    def test_corpora_never_shrink_below_what_was_merged(self):
        """The dataset updates were merges: nothing that was ever built may disappear (floors = sizes after the Oct 2026 merge)."""
        floors = {"tanvi": 11846, "yashmeet": 4826, "sonal": 3900}
        for pid, floor in floors.items():
            rows = sum(1 for line in (PERSONS / pid / "corpus.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
            self.assertGreaterEqual(rows, floor, pid)

    def test_every_account_is_identified_by_a_numeric_id_only(self):
        for pid, person in registry()["persons"].items():
            for account in person["accounts"]:
                self.assertIsInstance(account["id"], int, (pid, account))

    def test_an_account_belongs_to_one_person(self):
        seen = {}
        for pid, person in registry()["persons"].items():
            for account in person["accounts"]:
                self.assertNotIn(account["id"], seen, f"{account['id']} is listed for {seen.get(account['id'])} and {pid}")
                seen[account["id"]] = pid

    def test_corpus_is_present_and_derived_training_splits_are_not_shipped_in_data(self):
        for pid in ("tanvi", "yashmeet"):
            corpus = PERSONS / pid / "corpus.jsonl"
            self.assertTrue(corpus.exists() and corpus.stat().st_size > 1000, pid)
            for derived in ("train.jsonl", "val.jsonl", "test.jsonl"):
                self.assertFalse((PERSONS / pid / derived).exists(), f"{pid}/{derived}: splits are regenerated into training/_private/")


class TestEngineSmokeOnShippedData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_legacy_smoke", ROOT / "bot/personality/persona_engine/integration.py")
        cls.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.mod
        spec.loader.exec_module(cls.mod)
        cls.mod._persons_module().reset_service(PERSONS)

    @classmethod
    def tearDownClass(cls):
        cls.mod._reset_for_tests()

    def test_lists_all_persons(self):
        self.assertTrue({"tanvi", "yashmeet", "sonal"} <= set(self.mod.list_available_persons()))

    def test_prepare_for_each_person_builds_an_honest_two_message_prompt(self):
        for pid, name, text in (("tanvi", "Tanvi", "Hiii how r u"), ("yashmeet", "Yashmeet", "Kya haal"), ("sonal", "Sonal", "Hii kya kar rhi")):
            with self.subTest(person=pid):
                eng = self.mod.get_engine(pid, real_name=name)
                payload = self.mod.prepare_persona_reply(eng, text, chat_id=f"{pid}-smoke")
                self.assertEqual(payload["person_id"], pid)
                self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
                self.assertIn(name, payload["messages"][0]["content"])
                self.assertIn("bot", payload["messages"][0]["content"].lower())
                self.assertNotIn("Do NOT claim to be an AI", payload["messages"][0]["content"])
                self.assertGreater(payload["profile_summary"]["messages"], 200)

    def test_are_you_the_real_person_gets_the_disclosure(self):
        eng = self.mod.get_engine("tanvi")
        payload = self.mod.prepare_persona_reply(eng, "are you actually Tanvi?", chat_id="id-probe")
        self.assertEqual(payload["messages"], [])
        self.assertIn("bot", payload["direct_reply"].lower())


if __name__ == "__main__":
    unittest.main()
