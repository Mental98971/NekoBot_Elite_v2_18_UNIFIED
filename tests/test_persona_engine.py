"""Persona engine (bot/persona_engine) - standard library only, synthetic data (no real chat text is read).

The scripted "models" below are test doubles that exercise the pipeline; they say nothing about how well a real
model imitates anyone - that needs the evaluation in docs/EVALUATION_REPORT.md run with a real provider.
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.persona_engine import antirepeat, core, identity, memory, prompt, runtime, safety, scoring  # noqa: E402
from bot.persona_engine import store as st  # noqa: E402
from bot.persona_engine.engine import EngineConfig, EngineRequest, PersonaEngine  # noqa: E402
from bot.persona_engine.profile import BehaviorProfile, context_dependence_test  # noqa: E402
from bot.persona_engine.providers import CallableProvider, OpenAICompatProvider, choose_provider  # noqa: E402
from bot.persona_engine.resilience import CircuitBreaker  # noqa: E402


# ── synthetic world ──────────────────────────────────────────────────────────
TRIGGERS = {
    "question_wh": ["kya kar rahi ho?", "kaise ho aaj?", "kal kahan ja rahe ho?", "what are you doing today?"],
    "greeting": ["hi", "hello", "good morning", "hey there"],
    "statement": ["i got the tickets finally", "exam went really badly today", "new phone arrived yesterday"],
    "laughter": ["haha that was so funny", "lol no way"],
}
REPLIES = {
    "question_wh": ["kuch nahi yaar", "bas padh rhi", "ghar pe hu", "nothing much"],
    "greeting": ["hii", "heyy 😂", "hello"],
    "statement": ["wow nice", "ohh", "kya baat hai 😂", "oh no"],
    "laughter": ["haha", "lmao", "😂"],
}


def synth_examples(n: int = 600, seed: int = 1, chats=("c1", "c2", "c3"), emoji_by_chat=None, person="p") -> list:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        act = rng.choice(list(TRIGGERS))
        chat = chats[i % len(chats)]
        reply = rng.choice(REPLIES[act])
        if emoji_by_chat is not None and rng.random() < emoji_by_chat[chat] and "😂" not in reply:
            reply += " 😂"
        e = st.Example(
            id=f"e{seed}_{i}", person=person, acct="a1" if i % 5 else "a2", chat=chat, conv=f"{chat}-{i // 8}",
            ts=f"2025-{1 + i % 12:02d}-{1 + i % 27:02d}T10:00:00Z", reply=reply, kind="text",
            trigger=rng.choice(TRIGGERS[act]), lat=float(rng.choice([3, 8, 20, 60, 200])),
        ).finalize()
        out.append(e)
    return out


def make_profile(examples=None, **kw):
    ex = examples or synth_examples()
    return BehaviorProfile.build(ex, person="p", background=[e.trigger for e in ex], n_perm=kw.pop("n_perm", 60), **kw)


def make_engine(provider, *, cfg=None, examples=None, **kw):
    ex = examples or synth_examples()
    prof = make_profile(ex)
    own = [e.reply for e in ex]
    bg = [e.trigger for e in ex]
    scorer = scoring.CandidateScorer(
        prof, scoring.PersonaLM(own, bg), frozenset(w for t in own for w in core.words(t)), frozenset(w for t in bg for w in core.words(t)),
        blocked=["darnit"], names=["Pat", "Patty"], max_chars=80)
    return PersonaEngine(person_id="p", real_name="Pat", names=["Pat", "Patty"], store=st.ExampleStore(ex), profile=prof, scorer=scorer,
                         provider=provider, cfg=cfg or EngineConfig(seed=5), blocked=["darnit"], max_chars=80, **kw)


def scripted(*batches, record=None):
    """Provider returning the given batches in turn (the last one repeats)."""
    state = {"i": 0}

    async def fn(messages, n, temperature, max_tokens):
        if record is not None:
            record.append({"messages": messages, "n": n, "temperature": temperature})
        b = batches[min(state["i"], len(batches) - 1)]
        state["i"] += 1
        if isinstance(b, BaseException):
            raise b
        return list(b)
    return CallableProvider(fn, "scripted")


def run(coro):
    return asyncio.run(coro)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="persona_engine_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ── situation analysis ───────────────────────────────────────────────────────
class TestSituation(unittest.TestCase):
    def test_dialogue_acts(self):
        cases = {
            "hi!": "greeting", "good night": "goodbye", "thanks a lot": "thanks", "haha lol": "laughter", "😂😂": "emoji_only",
            "kya kar rahi ho?": "question_wh", "are you coming tomorrow": "question_yn", "is it done?": "question_yn",
            "send me the link pls": "request", "sorry yaar": "apology", "nahi yaar": "disagree", "haan sahi hai": "agree",
            "i bought a new phone": "statement", "do it now": "statement", "": "empty", "[media/non-text]": "media",
            "/start": "structured",
        }
        wrong = {t: (core.analyze(t).act, want) for t, want in cases.items() if core.analyze(t).act != want}
        self.assertEqual(wrong, {})

    def test_cues_and_language(self):
        s = core.analyze("NOOOO that is soooo funny lol!!")
        self.assertTrue({"caps", "elongation", "laugh", "exclaim"} <= set(s.cues))
        self.assertGreater(core.analyze("kya kar rahi ho tum").hinglish, 0.4)
        self.assertEqual(core.script_of("नमस्ते दोस्त"), "devanagari")
        self.assertEqual(core.script_of("hello नमस्ते"), "mixed")

    def test_similarity_orders_situations_sensibly(self):
        q = core.analyze("kya kar rahi ho?", has_reply_target=True)
        same = core.analyze("kaise ho aaj?", has_reply_target=True)
        other = core.analyze("good morning", has_reply_target=True)
        self.assertGreater(core.sit_similarity(q, same), core.sit_similarity(q, other))
        self.assertAlmostEqual(core.sit_similarity(q, q), 1.0, places=6)

    def test_char_vectors_tolerate_spelling_variation(self):
        a, b, c = core.char_vec("haan yaar"), core.char_vec("haan yar"), core.char_vec("completely different")
        self.assertGreater(core.cosine(a, b), 0.6)
        self.assertLess(core.cosine(a, c), 0.2)

    def test_length_buckets_cover_every_length(self):
        seen = {core.length_bucket(n) for n in range(0, 200)}
        self.assertEqual(seen, set(range(core.N_LEN)))

    def test_smoothing_is_a_distribution(self):
        p = core.smooth([5, 0, 1], [0.2, 0.5, 0.3], 4.0)
        self.assertAlmostEqual(sum(p), 1.0, places=9)
        self.assertTrue(all(x > 0 for x in p))


# ── identity honesty ─────────────────────────────────────────────────────────
class TestIdentity(unittest.TestCase):
    N = ["Tanvi", "Iziiee"]

    def test_probes_and_false_positives(self):
        probes = ["are you real?", "r u a bot", "who are you", "tum kaun ho", "kya tum real ho", "is this you", "bot hai kya", "AI?",
                  "are you tanvi", "prove you are real", "Tanvi ho kya", "is this Tanvi", "kya tum Iziiee"]
        normal = ["kya haal hai", "where are you from", "I am a real fan of this", "are you coming tomorrow?", "who is this song by",
                  "real talk bro", "what are you doing", "kaun jeeta", "i got a bot for discord", "tanvi kahan hai"]
        self.assertEqual([t for t in probes if not identity.is_identity_probe(t, self.N)], [])
        self.assertEqual([t for t in normal if identity.is_identity_probe(t, self.N)], [])

    def test_claims_of_being_the_real_person(self):
        yes = ["I am Tanvi", "haan main Tanvi hu", "real tanvi here", "it's me", "haan main hi hu", "im iziiee", "Tanvi here"]
        no = ["tanvi ko bol do", "this is Tanvi's bag", "I'm tired", "main tanvi se milunga", "kya tanvi aayegi"]
        self.assertEqual([t for t in yes if not identity.claims_to_be(t, self.N)], [])
        self.assertEqual([t for t in no if identity.claims_to_be(t, self.N)], [])

    def test_disclosure_names_the_person_and_says_bot(self):
        d = identity.disclosure_reply("Tanvi")
        self.assertIn("bot", d)
        self.assertIn("not Tanvi", d)

    def test_ai_internals(self):
        self.assertTrue(identity.mentions_ai_internals("As an AI language model I cannot"))
        self.assertFalse(identity.mentions_ai_internals("haha ok sure"))


# ── safety ───────────────────────────────────────────────────────────────────
class TestSafety(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(safety.clean_text('Pat: "**haan** yaar"', 80, ["Pat"]), "haan yaar")
        self.assertEqual(safety.clean_text("first\n\nsecond paragraph", 80), "first")
        self.assertEqual(safety.clean_text("<|im_end|>ok", 80), "ok")
        self.assertLessEqual(len(safety.clean_text("word " * 100, 40)), 40)
        self.assertEqual(safety.clean_text("   ", 80), "")

    def test_hard_gates(self):
        g = lambda t: safety.hard_gates(t, blocked=["darnit"], names=["Pat"], max_chars=40)  # noqa: E731
        self.assertEqual(g("haan yaar"), [])
        self.assertIn("contact_data", g("call 9876543210"))
        self.assertIn("contact_data", g("see https://x.io"))
        self.assertIn("contact_data", g("hi <user>"))
        self.assertIn("blocked_token", g("oh darnit"))
        self.assertIn("claims_real_person", g("I am Pat"))
        self.assertIn("ai_talk", g("as an AI I can't"))
        self.assertIn("too_long", g("x" * 41))
        self.assertEqual(g(""), ["empty"])

    def test_list_and_markup_detection(self):
        self.assertTrue(safety.looks_like_list_or_markup("- one\n- two"))
        self.assertTrue(safety.looks_like_list_or_markup("```code```"))
        self.assertFalse(safety.looks_like_list_or_markup("just a message"))


# ── store + retrieval ────────────────────────────────────────────────────────
class TestStoreAndRetrieval(_Tmp):
    def test_from_corpus_maps_fields_and_keeps_only_safe_trigger_examples_as_evidence(self):
        rows = [
            {"id": "a", "acct": "7", "chat": "c1", "kind": "text", "text": "kuch nahi", "ts": "2025-01-01T00:00:00Z", "ctx": "kya kar rahi ho?", "ctx_kind": "text", "rp": 1},
            {"id": "b", "acct": "7", "chat": "c1", "kind": "text", "text": "ambient message", "ts": "2025-01-01T00:00:00Z", "rp": 1},
            {"id": "c", "acct": "7", "chat": "c1", "kind": "text", "text": "oh darnit", "ts": "2025-01-01T00:00:00Z", "ctx": "something", "ctx_kind": "text", "rp": 1},
            {"id": "d", "acct": "7", "chat": "c1", "kind": "media", "text": "[media/non-text]", "ts": "2025-01-01T00:00:00Z", "ctx": "pic?", "ctx_kind": "text", "rp": 0},
            {"id": "e", "acct": "7", "chat": "c1", "kind": "text", "text": "fine", "ts": "2025-01-01T00:00:00Z", "ctx": "/cmd", "ctx_kind": "structured", "rp": 1},
        ]
        p = self.tmp / "corpus.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        ex = {e.id: e for e in st.from_corpus(p, "p", {"7": "a1"}, ["darnit"])}
        self.assertEqual(ex["a"].acct, "a1")
        self.assertTrue(ex["a"].usable_evidence)
        self.assertFalse(ex["b"].usable_evidence)      # ambient: no trigger
        self.assertFalse(ex["c"].usable_evidence)      # blocked token
        self.assertFalse(ex["d"].usable_evidence)      # media
        self.assertFalse(ex["e"].usable_evidence)      # structured trigger is not a situation
        self.assertEqual(st.ExampleStore(ex.values()).n_trigger_examples, 1)

    def test_from_prepared_respects_requested_splits(self):
        d = self.tmp / "p"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({"person": "p"}), encoding="utf-8")
        for split, tgt in (("train", "from train"), ("val", "from val"), ("test", "from test")):
            (d / f"{split}.jsonl").write_text(json.dumps({"id": split, "conv": split, "acct": "a1", "chat": "c", "ctx": [["u1", "q?"]], "reply_to": None,
                                                          "target": tgt, "tk": "text", "ts": "2025-01-01T00:00:00Z", "lat": 4}) + "\n", encoding="utf-8")
        self.assertEqual([e.reply for e in st.from_prepared(d, ("train",))], ["from train"])
        self.assertEqual({e.reply for e in st.from_prepared(d, ("train", "val"))}, {"from train", "from val"})
        e = st.from_prepared(d, ("train",))[0]
        self.assertEqual(e.trigger, "q?")
        self.assertEqual(e.lat, 4)

    def test_retrieval_matches_the_trigger_not_the_reply(self):
        ex = synth_examples(400)
        R = st.SelectiveRetriever(st.ExampleStore(ex), st.RetrievalConfig(k=4))
        ev = R.retrieve(st.Query("kya kar rahi ho?", core.analyze("kya kar rahi ho?")))
        self.assertTrue(ev)
        self.assertGreaterEqual(sum(1 for e in ev if e.example.trigger_sit.act == "question_wh"), 3)

    def test_selection_is_diverse_and_never_repeats_a_reply(self):
        ex = synth_examples(600)
        R = st.SelectiveRetriever(st.ExampleStore(ex), st.RetrievalConfig(k=4))
        for msg in ("kya kar rahi ho?", "good morning", "i got a new phone"):
            ev = R.retrieve(st.Query(msg, core.analyze(msg)))
            replies = [core.norm(e.example.reply) for e in ev]
            self.assertEqual(len(replies), len(set(replies)), msg)
            self.assertLessEqual(len(ev), 4)

    def test_stale_examples_are_demoted_not_banned(self):
        ex = synth_examples(600)
        R = st.SelectiveRetriever(st.ExampleStore(ex), st.RetrievalConfig(k=4))
        q = st.Query("kya kar rahi ho?", core.analyze("kya kar rahi ho?"))
        first = R.retrieve(q)
        used = frozenset(e.example.id for e in first)
        second = R.retrieve(st.Query(q.text, q.sit, used_ids=used))
        self.assertLess(len(used & {e.example.id for e in second}), len(first))

    def test_other_chats_are_not_filtered_out(self):
        ex = synth_examples(600, chats=("c1", "c2", "c3"))
        R = st.SelectiveRetriever(st.ExampleStore(ex), st.RetrievalConfig(k=6, chat_bonus=0.0))
        ev = R.retrieve(st.Query("kya kar rahi ho?", core.analyze("kya kar rahi ho?"), chat="c1"))
        self.assertGreater(len({e.example.chat for e in ev}), 1)
        # even a strong soft preference never excludes: the only relevant evidence lives in another chat and is still found first
        only_c2 = st.Example(id="x1", person="p", acct=None, chat="c2", conv="c2-x", ts=None, trigger="quantum fishing tournament schedule",
                             reply="arre wah", kind="text").finalize()
        R2 = st.SelectiveRetriever(st.ExampleStore(list(ex) + [only_c2]), st.RetrievalConfig(k=4, chat_bonus=0.1, acct_bonus=0.1))
        got = R2.retrieve(st.Query("quantum fishing tournament schedule", core.analyze("quantum fishing tournament schedule"), chat="c1"))
        self.assertEqual(got[0].example.id, "x1")

    def test_irrelevant_queries_return_little_rather_than_noise(self):
        ex = synth_examples(300)
        R = st.SelectiveRetriever(st.ExampleStore(ex), st.RetrievalConfig(k=4, min_relevance=0.6))
        self.assertEqual(R.retrieve(st.Query("zzzz qqqq", core.analyze("zzzz qqqq"))), [])

    def test_retrieval_is_deterministic(self):
        R = st.SelectiveRetriever(st.ExampleStore(synth_examples(400)), st.RetrievalConfig(k=4))
        q = st.Query("kal kahan ja rahe ho?", core.analyze("kal kahan ja rahe ho?"))
        self.assertEqual([e.example.id for e in R.retrieve(q)], [e.example.id for e in R.retrieve(q)])


# ── behaviour profile ────────────────────────────────────────────────────────
class TestProfile(unittest.TestCase):
    def test_conditional_distribution_follows_the_data(self):
        ex = []
        for i in range(300):
            ex.append(st.Example(id=f"g{i}", person="p", acct=None, chat="c", conv=f"c{i // 5}", ts=None, trigger="good morning",
                                 reply="hello" if i % 4 else "😂", kind="text" if i % 4 else "emoji_only").finalize())
        for i in range(300):
            ex.append(st.Example(id=f"q{i}", person="p", acct=None, chat="c", conv=f"d{i // 5}", ts=None, trigger="why is it late?",
                                 reply="kya hua?" if i % 2 else "no idea", kind="text").finalize())
        prof = BehaviorProfile.build(ex, person="p", n_perm=20)
        dg = prof.reply_act_dist(core.analyze("good morning"))
        self.assertGreater(dg["greeting"], 0.6)                                   # replies to greetings are mostly greetings
        dq = prof.reply_act_dist(core.analyze("why is it late?"))
        self.assertGreater(dq["question"], 0.3)
        self.assertAlmostEqual(sum(dq.values()), 1.0, places=6)

    def test_sampled_plans_match_the_distribution_and_are_seed_deterministic(self):
        prof = make_profile()
        sit = core.analyze("good morning")
        plans = [prof.sample_plan(sit, random.Random(i), "a1") for i in range(400)]
        again = [prof.sample_plan(sit, random.Random(i), "a1") for i in range(400)]
        self.assertEqual(plans, again)
        dist = prof.reply_act_dist(sit, "a1")
        for act, p in dist.items():
            if p > 0.1:
                self.assertAlmostEqual(sum(1 for pl in plans if pl.reply_act == act) / 400, p, delta=0.08)
        self.assertGreater(len({pl.length_bucket for pl in plans}), 1)             # variation, not one fixed length

    def test_context_dependence_is_detected_only_when_real(self):
        with_effect = synth_examples(1800, seed=2, chats=("c1", "c2", "c3"), emoji_by_chat={"c1": 0.7, "c2": 0.05, "c3": 0.05})
        no_effect = synth_examples(1800, seed=3, chats=("c1", "c2", "c3"), emoji_by_chat={"c1": 0.2, "c2": 0.2, "c3": 0.2})
        a = context_dependence_test(with_effect, lambda e: e.chat, features=("emoji", "question"), min_group=100, n_perm=200, seed=1)
        b = context_dependence_test(no_effect, lambda e: e.chat, features=("emoji", "question"), min_group=100, n_perm=200, seed=1)
        self.assertTrue(a["features"]["emoji"]["dependent"])
        self.assertFalse(b["features"]["emoji"]["dependent"])
        self.assertFalse(a["features"]["question"]["dependent"])

    def test_too_few_groups_is_reported_not_guessed(self):
        r = context_dependence_test(synth_examples(300, chats=("only",)), lambda e: e.chat, min_group=50, n_perm=20)
        self.assertIn("note", r)
        self.assertEqual(r["features"], {})

    def test_retrieval_is_not_biased_toward_the_same_chat_by_default(self):
        prof = make_profile(synth_examples(1800, seed=2, emoji_by_chat={"c1": 0.7, "c2": 0.05, "c3": 0.05}), n_perm=120)
        self.assertEqual(prof.retrieval_weights(), {"chat_bonus": 0.0, "acct_bonus": 0.0})   # validation showed no gain; see EVALUATION_REPORT

    def test_chat_specific_rates_apply_only_to_validated_features(self):
        prof = make_profile(synth_examples(1800, seed=2, emoji_by_chat={"c1": 0.7, "c2": 0.05, "c3": 0.05}), n_perm=120)
        self.assertIn("emoji", prof.chat_dependent())
        self.assertNotIn("question", prof.chat_dependent())
        self.assertGreater(prof.chat_rate_ratio("c1", "emoji"), 1.2)
        self.assertLess(prof.chat_rate_ratio("c2", "emoji"), 0.8)
        self.assertEqual(prof.chat_rate_ratio("c1", "question"), 1.0)       # not context-dependent: never adjusted
        self.assertEqual(prof.chat_rate_ratio("unknown-chat", "emoji"), 1.0)  # unseen chat: person-wide rate
        self.assertEqual(prof.chat_rate_ratio(None, "emoji"), 1.0)
        sit = core.analyze("good morning")
        rate = lambda chat: sum(prof.sample_plan(sit, random.Random(i), None, chat).emoji for i in range(600)) / 600  # noqa: E731
        self.assertGreater(rate("c1"), rate("c2") + 0.15)

    def test_no_chat_effect_means_no_adjustment_anywhere(self):
        prof = make_profile(synth_examples(1800, seed=3, emoji_by_chat={"c1": 0.2, "c2": 0.2, "c3": 0.2}), n_perm=120)
        self.assertEqual(prof.chat_dependent(), frozenset())
        self.assertEqual(prof.chat_rate_ratio("c1", "emoji"), 1.0)

    def test_cross_validated_gain_separates_real_effects_from_noise(self):
        from bot.persona_engine.profile import _cv_gain
        rng = random.Random(4)
        labels = [f"g{i % 3}" for i in range(300)]
        n_u = [10] * 300
        real = [sum(rng.random() < (0.7 if labels[i] == "g0" else 0.1) for _ in range(10)) for i in range(300)]
        noise = [sum(rng.random() < 0.2 for _ in range(10)) for i in range(300)]
        g_real, pos_real = _cv_gain(labels, real, n_u, random.Random(1))
        g_noise, pos_noise = _cv_gain(labels, noise, n_u, random.Random(1))
        self.assertGreater(g_real, 0.05)
        self.assertEqual(pos_real, 1.0)
        self.assertLess(g_noise, 0.01)
        self.assertLess(pos_noise, 0.8)

    def test_json_round_trip_and_schema_check(self):
        prof = make_profile()
        again = BehaviorProfile.from_json(prof.to_json())
        sit = core.analyze("kya kar rahi ho?")
        self.assertEqual(prof.reply_act_dist(sit, "a1"), again.reply_act_dist(sit, "a1"))
        bad = json.loads(prof.to_json())
        bad["schema"] = 99
        with self.assertRaises(ValueError):
            BehaviorProfile.from_json(json.dumps(bad))

    def test_with_latency_adds_quantiles_and_provenance_without_touching_the_rest(self):
        prof = make_profile()
        new = prof.with_latency([float(i % 90) for i in range(400)] + [None, -3.0, 99999.0], {"dataset_sha256": "abc"})
        self.assertEqual(new.latency()["n"], 400)                          # None, negatives and > 1 h are not timing data
        self.assertEqual(new.source["latency"]["dataset_sha256"], "abc")
        self.assertEqual(new.reply_act_dist(core.analyze("hi")), prof.reply_act_dist(core.analyze("hi")))
        self.assertEqual(prof.with_latency([1.0, 2.0]).latency(), {})      # too few observations: no invented numbers

    def test_latency_and_delay(self):
        prof = make_profile()
        lat = prof.latency()
        self.assertTrue(lat and lat["p10"] <= lat["p50"] <= lat["p90"])
        delays = [prof.suggest_delay(random.Random(i)) for i in range(200)]
        self.assertTrue(0 <= min(delays) and max(delays) <= 6.0)

    def test_blocked_words_never_enter_the_lexicon(self):
        ex = synth_examples(400)
        for e in ex[:60]:
            e.reply = "darnit " + e.reply
        prof = BehaviorProfile.build(ex, person="p", blocked=["darnit"], background=[e.trigger for e in ex], n_perm=10)
        blob = json.dumps(prof.data["lexicon"])
        self.assertNotIn("darnit", blob)

    def test_description_is_measured_not_asserted(self):
        facts = " ".join(make_profile().describe(core.analyze("good morning"), "a1"))
        self.assertIn("Median message length", facts)
        self.assertNotIn("sarcastic", facts.lower())
        self.assertNotIn("introvert", facts.lower())


# ── scoring ──────────────────────────────────────────────────────────────────
class TestScoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ex = synth_examples(800)
        cls.prof = make_profile(cls.ex)
        own, bg = [e.reply for e in cls.ex], [e.trigger for e in cls.ex]
        cls.lm = scoring.PersonaLM(own, bg)
        cls.scorer = scoring.CandidateScorer(cls.prof, cls.lm, frozenset(w for t in own for w in core.words(t)),
                                             frozenset(w for t in bg for w in core.words(t)), blocked=["darnit"], names=["Pat"], max_chars=80)

    def test_persona_lm_prefers_the_persons_style_over_the_others(self):
        own = [self.lm.score(t) for t in ("kuch nahi yaar", "bas padh rhi", "ghar pe hu", "kya baat hai 😂")]
        other = [self.lm.score(t) for t in ("Thank you for your inquiry regarding this matter.", "I would be delighted to assist you today.")]
        self.assertGreater(sum(own) / len(own), sum(other) / len(other) + 0.1)

    def test_authentic_reply_outranks_generic_assistant_replies(self):
        sit = core.analyze("kya kar rahi ho?")
        plan = self.prof.sample_plan(sit, random.Random(1), "a1")
        trig = core.content_tokens("kya kar rahi ho?")

        def comp(t):
            return self.scorer.score(t, sit, plan, trig, 0.0, "a1").composite
        good = max(comp("kuch nahi yaar"), comp("bas padh rhi"))
        bad = comp("Sure! Here's what I'm doing: I am studying. Let me know if you need anything else!")
        self.assertGreater(good, bad + 0.15)

    def test_gates_and_copy_detection(self):
        sit = core.analyze("kya kar rahi ho?")
        plan = self.prof.sample_plan(sit, random.Random(1), None)
        trig = core.content_tokens("kya kar rahi ho?")
        sc = lambda t, ev=(): self.scorer.score(t, sit, plan, trig, 0.0, None, ev)  # noqa: E731
        self.assertEqual(sc("kuch nahi yaar").gates, [])
        self.assertIn("claims_real_person", sc("I am Pat").gates)
        self.assertIn("contact_data", sc("call 9876543210").gates)
        self.assertIn("copies_evidence", sc("bas padh rhi hu aaj kal", ["bas padh rhi hu aaj kal"]).gates)
        self.assertNotIn("copies_evidence", sc("ok", ["ok"]).gates)       # very short replies are legitimately identical

    def test_naturalness_penalises_assistant_voice_and_runaway_length(self):
        self.assertLess(self.scorer.naturalness("Certainly! I'd be happy to help with that. Let me know if you need more."), 0.7)
        self.assertEqual(self.scorer.naturalness("kuch nahi"), 1.0)

    def test_weights_are_normalised(self):
        w = scoring.ScoreWeights(1, 1, 1, 1, 1, 1)
        s = scoring.CandidateScorer(self.prof, self.lm, frozenset(), frozenset(), weights=w)
        sit = core.analyze("hi")
        sc = s.score("hello", sit, self.prof.sample_plan(sit, random.Random(1)), frozenset(), 0.0)
        self.assertLessEqual(sc.composite, 1.0)


# ── repetition ───────────────────────────────────────────────────────────────
class TestAntiRepeat(unittest.TestCase):
    def test_similarity(self):
        self.assertEqual(antirepeat.similarity("Haha yes!", "haha  YES!"), 1.0)
        self.assertGreater(antirepeat.similarity("kal milte hai", "kal milte hain"), 0.5)
        self.assertLess(antirepeat.similarity("kal milte hai", "pizza khana hai"), 0.5)

    def test_repeats_are_penalised_and_fresh_replies_are_not(self):
        g = antirepeat.RepetitionGuard()
        g.remember(1, "haha yes 😂")
        self.assertGreaterEqual(g.penalty(1, "haha yes 😂"), 0.9)
        self.assertLess(g.penalty(1, "kal milte hai"), 0.3)
        self.assertTrue(g.too_similar(1, "Haha yes 😂"))
        self.assertFalse(g.too_similar(2, "haha yes 😂"))          # other chats are independent

    def test_repeated_openings_and_emoji(self):
        g = antirepeat.RepetitionGuard()
        for t in ("arre yaar kya hua", "arre nahi bhai", "arre sahi hai"):
            g.remember(1, t)
        self.assertGreaterEqual(g.penalty(1, "arre ruko zara"), 0.5)
        g2 = antirepeat.RepetitionGuard()
        g2.remember(1, "ok 😂")
        self.assertGreaterEqual(g2.penalty(1, "bas 😂"), 0.4)
        self.assertEqual(g2.diversify(1, "bas yaar 😂"), "bas yaar")

    def test_catchphrase_budget_uses_the_persons_base_rate(self):
        g = antirepeat.RepetitionGuard()
        for t in ("lol sahi", "lol kya", "lol haan", "lol okay"):
            g.remember(1, t)
        rec = [{"gram": "lol", "rate": 0.05, "n": 50}]
        self.assertGreaterEqual(g.penalty(1, "lol ye", rec), 0.6)
        self.assertLess(g.penalty(1, "kuch nahi", rec), 0.5)

    def test_ttl_and_lru_bounds(self):
        now = [0.0]
        g = antirepeat.RepetitionGuard(ttl_seconds=100, max_chats=3, clock=lambda: now[0])
        g.remember(1, "hello there")
        now[0] = 200
        self.assertEqual(g.recent(1), [])
        for c in range(10):
            g.remember(c, f"msg {c}")
        self.assertLessEqual(len(g._replies), 3)

    def test_evidence_usage_is_remembered_per_chat(self):
        g = antirepeat.RepetitionGuard()
        g.remember(1, "x", ["e1", "e2"])
        self.assertEqual(g.used_evidence(1), frozenset({"e1", "e2"}))
        self.assertEqual(g.used_evidence(2), frozenset())


# ── memory ───────────────────────────────────────────────────────────────────
class TestMemory(_Tmp):
    def test_short_term_pseudonyms_ttl_and_questions(self):
        now = [0.0]
        m = memory.ConversationMemory(ttl_seconds=60, clock=lambda: now[0])
        m.observe_user(1, "1001", "kya kar rahi ho?")
        m.observe_user(1, "2002", "hello")
        self.assertEqual([t.who for t in m.turns(1)], ["u1", "u2"])
        self.assertNotIn("1001", json.dumps([t.__dict__ for t in m.turns(1)]))
        self.assertIsNone(m.open_question(1))                     # the latest message is not a question
        m.observe_user(1, "2002", "why is that?")
        self.assertEqual(m.open_question(1), "why is that?")
        m.observe_bot(1, "pata nahi")
        self.assertIsNone(m.open_question(1))
        self.assertEqual(m.previous_bot_reply(1), "pata nahi")
        self.assertEqual(m.turns(1)[-1].provenance, core.GENERATED_CONTENT)
        self.assertEqual(m.turns(1)[0].provenance, core.CURRENT_CONTEXT)
        now[0] = 500
        self.assertEqual(m.turns(1), [])

    def test_long_term_memory_roundtrip_limits_and_privacy(self):
        lt = memory.LongTermMemory(self.tmp / "m.sqlite3")
        a = lt.add(1, "p", "we are planning a trip to goa in march")
        lt.add(2, "p", "chat two secret")
        self.assertEqual([t for _i, t in lt.list(1, "p")], ["we are planning a trip to goa in march"])
        self.assertEqual(lt.search(1, "p", "when is the goa trip", 3), ["we are planning a trip to goa in march"])
        self.assertEqual(lt.search(1, "p", "unrelated words", 3), [])
        for bad in ("", "x" * 201, "call me 9876543210", "see https://x.io", "ping @someone"):
            with self.assertRaises(memory.MemoryError_):
                lt.add(1, "p", bad)
        self.assertEqual(lt.forget(1, "p", a), 1)
        self.assertEqual(lt.list(1, "p"), [])
        self.assertEqual(len(lt.list(2, "p")), 1)                # other chats untouched
        self.assertEqual(memory.LongTermMemory(self.tmp / "m.sqlite3").list(2, "p")[0][1], "chat two secret")   # persisted

    def test_long_term_memory_expiry_and_capacity(self):
        now = [1000.0]
        lt = memory.LongTermMemory(self.tmp / "e.sqlite3", clock=lambda: now[0])
        lt.add(1, "p", "short lived fact", ttl_days=1)
        self.assertEqual(len(lt.list(1, "p")), 1)
        now[0] += 3 * 86400
        self.assertEqual(lt.list(1, "p"), [])
        for i in range(memory.LongTermMemory.MAX_PER_CHAT):
            lt.add(2, "p", f"fact number {i}")
        with self.assertRaises(memory.MemoryError_):
            lt.add(2, "p", "one too many")
        self.assertEqual(lt.forget(2), memory.LongTermMemory.MAX_PER_CHAT)


# ── prompt ───────────────────────────────────────────────────────────────────
class TestPrompt(unittest.TestCase):
    def build(self, **kw):
        ex = synth_examples(300)
        prof = make_profile(ex)
        sit = core.analyze("kya kar rahi ho?")
        plan = prof.sample_plan(sit, random.Random(2), "a1")
        R = st.SelectiveRetriever(st.ExampleStore(ex), st.RetrievalConfig(k=4))
        ev = R.retrieve(st.Query("kya kar rahi ho?", sit))
        args = dict(current="kya kar rahi ho?", reply_to=None, turns=[memory.Turn("u1", "hello", 0.0, core.CURRENT_CONTEXT)], sit=sit, plan=plan,
                    profile_facts=prof.describe(sit, "a1"), evidence=ev)
        args.update(kw)
        return prompt.PromptBuilder("Pat", **kw.pop("pb", {})).build(**args), ev

    def test_sections_and_rules(self):
        msgs, ev = self.build(memory_facts=["trip to goa in march"])
        sysm, usr = msgs[0]["content"], msgs[1]["content"]
        self.assertIn("You are not Pat", sysm)
        self.assertIn("do not copy", sysm.lower())
        for sec in ("[PERSONA PROFILE", "[BOT MEMORY", "[CONVERSATION", "[SITUATION]", "[PLAN FOR THIS REPLY", "[HISTORICAL EVIDENCE"):
            self.assertIn(sec, usr)
        self.assertIn("answer this one", usr)
        self.assertTrue(usr.index("[PERSONA PROFILE") < usr.index("[CONVERSATION") < usr.index("[PLAN FOR") < usr.index("[HISTORICAL"))
        self.assertIn("NOT things Pat said", usr)             # memory is labelled as not coming from the person

    def test_no_memory_section_when_nothing_is_stored_and_no_ids_leak(self):
        msgs, ev = self.build()
        usr = msgs[1]["content"]
        self.assertNotIn("[BOT MEMORY", usr)
        for e in ev:
            self.assertNotIn(e.example.id, usr)
            self.assertNotIn(e.example.chat + ":", usr)

    def test_oversized_prompts_shed_evidence_first(self):
        ex = synth_examples(300)
        prof = make_profile(ex)
        sit = core.analyze("kya kar rahi ho?")
        plan = prof.sample_plan(sit, random.Random(2), None)
        evid = st.SelectiveRetriever(st.ExampleStore(ex), st.RetrievalConfig(k=4)).retrieve(st.Query("kya kar rahi ho?", sit))
        small = prompt.PromptBuilder("Pat", max_prompt_chars=900).build(current="kya kar rahi ho?", reply_to=None, turns=[], sit=sit, plan=plan,
                                                                          profile_facts=prof.describe(sit), evidence=evid)
        usr = small[1]["content"]
        self.assertIn("answer this one", usr)
        self.assertIn("[PLAN FOR THIS REPLY", usr)
        self.assertLessEqual(usr.count("when someone said"), len(evid))


# ── providers ────────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class TestProviders(unittest.TestCase):
    def install_fake_httpx(self, honor_n=True, status=200):
        calls = []

        class Client:
            async def post(self, url, headers=None, timeout=None, json=None):
                calls.append(json)
                n = json["n"] if honor_n else 1
                return FakeResponse({"choices": [{"message": {"content": f"reply {i}"}} for i in range(n)]}, status)

            async def aclose(self):
                pass
        sys.modules["httpx"] = NS(AsyncClient=Client)
        self.addCleanup(sys.modules.pop, "httpx", None)
        return calls

    def test_n_candidates_in_one_call_when_the_server_supports_it(self):
        calls = self.install_fake_httpx(honor_n=True)
        out = run(OpenAICompatProvider("http://x/v1", "m", "key").generate([{"role": "user", "content": "hi"}], n=3, temperature=0.8, max_tokens=50, timeout=5))
        self.assertEqual(out, ["reply 0", "reply 1", "reply 2"])
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["model"], calls[0]["n"], calls[0]["temperature"]), ("m", 3, 0.8))

    def test_falls_back_to_separate_calls_when_the_server_ignores_n(self):
        self.install_fake_httpx(honor_n=False)
        p = OpenAICompatProvider("http://x/v1", "m")
        out = run(p.generate([{"role": "user", "content": "hi"}], n=3, temperature=0.8, max_tokens=50, timeout=5))
        self.assertEqual(len(out), 3)
        self.assertFalse(p._n_supported)

    def test_http_errors_propagate_so_the_engine_can_fall_back(self):
        self.install_fake_httpx(status=500)
        with self.assertRaises(RuntimeError):
            run(OpenAICompatProvider("http://x/v1", "m").generate([{"role": "user", "content": "hi"}], n=1, temperature=0.8, max_tokens=50, timeout=5))

    def test_provider_selection(self):
        s = NS(persona_engine_provider="auto", persona_engine_base_url="http://x/v1", persona_engine_model="m", persona_engine_api_key=None, default_ai_model="")
        self.assertIsInstance(choose_provider(s), OpenAICompatProvider)
        s.persona_engine_base_url = ""
        self.assertIsNone(choose_provider(s) if "bot.plugins.ai" not in sys.modules else None)   # nothing configured here
        s.persona_engine_provider = "none"
        self.assertIsNone(choose_provider(NS(**{**s.__dict__, "persona_engine_base_url": "http://x/v1"})))
        s2 = NS(persona_engine_provider="openai_compat", persona_engine_base_url="", persona_engine_model="", persona_engine_api_key=None, default_ai_model="")
        self.assertIsNone(choose_provider(s2))                 # asked for an endpoint, none given: never silently use another provider


# ── engine ───────────────────────────────────────────────────────────────────
class TestEngine(_Tmp):
    def req(self, text="kya kar rahi ho?", chat=1, **kw):
        return EngineRequest(chat_id=chat, text=text, user_key="u", acct="a1", request_id=text, **kw)

    def test_picks_the_best_scoring_candidate_not_the_first(self):
        batch = ["Sure! Here's a detailed answer: I am currently working on my studies. Let me know if you need anything else!", "kuch nahi yaar", "I am Pat"]
        e = make_engine(scripted(batch), cfg=EngineConfig(seed=1, candidates=3))
        r = run(e.respond(self.req()))
        self.assertEqual(r.text, "kuch nahi yaar")
        self.assertEqual(r.source, "llm")
        self.assertGreater(r.scores.composite, 0.4)

    def test_gated_candidates_are_never_sent_and_failure_falls_back_to_none(self):
        bad = ["I am Pat", "call 9876543210", "oh darnit", "https://evil.example"]
        e = make_engine(scripted(bad), cfg=EngineConfig(seed=1, regenerate=1))
        self.assertIsNone(run(e.respond(self.req())))
        self.assertEqual(e.stats["all_rejected"], 1)
        self.assertEqual(e.stats["regenerated"], 1)

    def test_regeneration_recovers_from_a_bad_first_batch(self):
        e = make_engine(scripted(["I am Pat"], ["bas padh rhi"]), cfg=EngineConfig(seed=1, regenerate=1))
        r = run(e.respond(self.req()))
        self.assertEqual(r.text, "bas padh rhi")
        self.assertEqual(len(r.trace["attempts"]), 2)

    def test_identity_questions_get_the_disclosure_without_a_model_call(self):
        rec = []
        e = make_engine(scripted(["kuch nahi"], record=rec))
        for q in ("are you a bot?", "is this Pat", "kya tum real ho"):
            r = run(e.respond(self.req(q)))
            self.assertEqual(r.source, "identity_probe")
            self.assertIn("not Pat", r.text)
        self.assertEqual(rec, [])

    def test_no_provider_returns_none_without_raising(self):
        e = make_engine(None)
        self.assertIsNone(run(e.respond(self.req())))
        self.assertEqual(e.stats["no_provider"], 1)

    def test_breaker_opens_after_repeated_provider_errors_and_recovers(self):
        now = [0.0]
        e = make_engine(scripted(TimeoutError("slow")), breaker=CircuitBreaker(3, 60.0, clock=lambda: now[0]))
        for _ in range(3):
            self.assertIsNone(run(e.respond(self.req())))
        self.assertEqual(e.stats["provider_error"], 3)
        self.assertIsNone(run(e.respond(self.req())))
        self.assertEqual((e.stats["provider_error"], e.stats["breaker_open"]), (3, 1))     # not hit while open
        now[0] += 61
        e.provider = scripted(["kuch nahi yaar"])
        self.assertEqual(run(e.respond(self.req())).text, "kuch nahi yaar")

    def test_identical_model_output_is_not_sent_twice_in_a_row(self):
        e = make_engine(scripted(["kuch nahi yaar", "bas padh rhi"]), cfg=EngineConfig(seed=1, candidates=2, regenerate=0))
        first = run(e.respond(self.req("kya kar rahi ho?")))
        second = run(e.respond(self.req("kaise ho aaj?")))
        self.assertTrue(first and second)
        self.assertNotEqual(core.norm(first.text), core.norm(second.text))
        self.assertGreaterEqual(e.stats["repeat_avoided"], 1)

    def test_replies_that_copy_a_shown_example_are_rejected(self):
        ex = synth_examples(300)
        for i in range(40):
            ex.append(st.Example(id=f"dn{i}", person="p", acct="a1", chat="c1", conv=f"dn{i}", ts="2025-03-03T10:00:00Z",
                                 trigger="what are you doing tonight", reply="going out for dinner with friends", kind="text").finalize())
        rec = []
        e = make_engine(scripted(["going out for dinner with friends", "bas ghar pe hu"], record=rec),
                        cfg=EngineConfig(seed=1, candidates=2, regenerate=0), examples=ex)
        r = run(e.respond(self.req("what are you doing tonight")))
        shown = rec[0]["messages"][1]["content"]
        self.assertIn("going out for dinner with friends", shown)           # it really was in the evidence block
        self.assertEqual(r.text, "bas ghar pe hu")
        self.assertEqual(r.trace["attempts"][0]["rejected"].get("copies_evidence"), 1)

    def test_trace_and_status_contain_metadata_only(self):
        e = make_engine(scripted(["kuch nahi yaar"]))
        r = run(e.respond(self.req()))
        blob = json.dumps(r.trace) + json.dumps(e.stats)
        for ex in e.store.examples:
            if len(ex.trigger) > 12:
                self.assertNotIn(ex.trigger, blob)
        self.assertTrue(all(set(x) == {"id", "score"} for x in r.trace["evidence"]))
        self.assertNotIn("kya kar rahi ho?", json.dumps(e.stats))

    def test_prompt_contains_evidence_but_marks_it_as_behaviour_only(self):
        rec = []
        e = make_engine(scripted(["kuch nahi yaar"], record=rec), cfg=EngineConfig(seed=1, candidates=2))
        run(e.respond(self.req()))
        usr = rec[0]["messages"][1]["content"]
        self.assertEqual(rec[0]["n"], 2)
        self.assertIn("[HISTORICAL EVIDENCE - real past reactions in similar situations; for behaviour only, never copy]", usr)
        self.assertIn("[PLAN FOR THIS REPLY", usr)

    def test_short_term_memory_is_recorded_and_used(self):
        rec = []
        e = make_engine(scripted(["kuch nahi yaar", "bas padh rhi"], record=rec), cfg=EngineConfig(seed=1, candidates=1))
        r1 = self.req("kya kar rahi ho?")
        e.observe_user(r1)
        a = run(e.respond(r1))
        r2 = self.req("aur batao")
        e.observe_user(r2)
        run(e.respond(r2))
        second = rec[1]["messages"][1]["content"]
        self.assertIn("kya kar rahi ho?", second)
        self.assertIn(f"bot: {a.text}", second)
        self.assertEqual(second.count("<- answer this one"), 1)
        self.assertEqual(second.count("aur batao"), 1)       # the current message appears once, as the target

    def test_history_override_is_used_for_evaluation_and_does_not_touch_memory(self):
        rec = []
        e = make_engine(scripted(["kuch nahi yaar"], record=rec))
        run(e.respond(EngineRequest(chat_id=9, text="kya kar rahi ho?", history=[("u1", "hello"), ("me", "hii")], request_id="x")))
        self.assertIn("u1: hello", rec[0]["messages"][1]["content"])
        self.assertIn("bot: hii", rec[0]["messages"][1]["content"])
        self.assertEqual(e.short.turns(9), [])

    def test_long_term_memory_reaches_the_prompt_labelled_as_bot_memory(self):
        lt = memory.LongTermMemory(self.tmp / "m.sqlite3")
        lt.add(1, "p", "the group is planning a trip to goa")
        rec = []
        e = make_engine(scripted(["kuch nahi yaar"], record=rec), long_memory=lt)
        run(e.respond(self.req("when is the goa trip")))
        usr = rec[0]["messages"][1]["content"]
        self.assertIn("[BOT MEMORY", usr)
        self.assertIn("the group is planning a trip to goa", usr)
        self.assertLess(usr.index("[BOT MEMORY"), usr.index("[HISTORICAL EVIDENCE"))
        self.assertNotIn("trip to goa", usr[usr.index("[HISTORICAL EVIDENCE"):])     # never mixed into the evidence block
        for ex in e.store.examples:
            self.assertNotIn("goa", ex.reply)                                       # the store is never written to

    def test_reproducible_with_a_seed(self):
        def go():
            e = make_engine(scripted(["kuch nahi yaar", "bas padh rhi", "ghar pe hu"]), cfg=EngineConfig(seed=11, candidates=3))
            return run(e.respond(self.req())).trace["plan"]
        self.assertEqual(go(), go())


# ── runtime wiring + shipped data ────────────────────────────────────────────
def persona_dir(tmp: Path, n=500) -> NS:
    d = tmp / "persons" / "p"
    d.mkdir(parents=True)
    ex = synth_examples(n)
    rows = [{"id": e.id, "acct": "1001" if e.acct == "a1" else "1002", "chat": e.chat, "kind": e.kind, "text": e.reply, "ts": e.ts,
             "ctx": e.trigger, "ctx_kind": "text", "rp": 1} for e in ex]
    (d / "corpus.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    person = NS(id="p", real_name="Pat", aliases=["Patty"], accounts=[NS(id=1001), NS(id=1002)])
    return NS(dir=d, person=person, max_chars=lambda aid=None: 80)


class TestRuntime(_Tmp):
    def test_builds_from_corpus_and_answers(self):
        data = persona_dir(self.tmp)
        eng = runtime.build_components(data, ["darnit"], scripted(["kuch nahi yaar"]), EngineConfig(seed=1), guard=antirepeat.RepetitionGuard(),
                                       short=memory.ConversationMemory(), long=None, n_perm=20)
        self.assertGreater(eng.store.n_trigger_examples, 300)
        self.assertEqual(run(eng.respond(EngineRequest(chat_id=1, text="kya kar rahi ho?", acct="a1", request_id="r"))).text, "kuch nahi yaar")

    def test_shipped_profile_is_used_only_when_it_matches_the_corpus(self):
        data = persona_dir(self.tmp)
        ex = st.from_corpus(data.dir / "corpus.jsonl", "p", {"1001": "a1", "1002": "a2"})
        prof = BehaviorProfile.build(ex, person="p", n_perm=10, source={"corpus_sha256": runtime.sha256_file(data.dir / "corpus.jsonl")})
        (data.dir / runtime.PROFILE_FILE).write_text(prof.to_json(), encoding="utf-8")
        kw = dict(guard=antirepeat.RepetitionGuard(), short=memory.ConversationMemory(), long=None, n_perm=10)
        e1 = runtime.build_components(data, [], None, EngineConfig(), **kw)
        self.assertEqual(e1.profile.source.get("corpus_sha256"), runtime.sha256_file(data.dir / "corpus.jsonl"))
        with open(data.dir / "corpus.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "new", "acct": "1001", "chat": "c1", "kind": "text", "text": "new row", "ts": None, "ctx": "x?", "ctx_kind": "text", "rp": 1}) + "\n")
        e2 = runtime.build_components(data, [], None, EngineConfig(), **kw)       # corpus changed -> profile rebuilt, no crash
        self.assertEqual(e2.profile.source.get("corpus_sha256"), runtime.sha256_file(data.dir / "corpus.jsonl"))

    def test_service_builds_lazily_survives_broken_data_and_reports_counters_only(self):
        data = persona_dir(self.tmp)
        settings = NS(persona_engine_candidates=2, persona_engine_examples=4, persona_engine_temperature=0.8, persona_engine_timeout_seconds=5,
                      persona_engine_regenerate=0, persona_engine_memory_enabled=True, data_dir=str(self.tmp), persona_engine_provider="none",
                      persona_engine_base_url="", persona_engine_model="", persona_engine_api_key=None, default_ai_model="")
        svc = runtime.EngineService(settings, [], provider=scripted(["kuch nahi yaar"]))
        eng = run(svc.engine_for(data))
        self.assertIsNotNone(eng)
        self.assertIs(run(svc.engine_for(data)), eng)                            # cached
        self.assertIsNotNone(svc.long)
        (data.dir / "corpus.jsonl").write_text("{not json\n", encoding="utf-8")
        broken = NS(dir=data.dir, person=NS(id="q", real_name="Q", aliases=[], accounts=[]), max_chars=lambda aid=None: 80)
        (data.dir / "corpus.jsonl").unlink()
        self.assertIsNone(run(svc.engine_for(broken)))                           # no corpus: no engine, no exception
        st_ = svc.status()
        self.assertIn("p", st_["engines"])
        self.assertNotIn("kya kar", json.dumps(st_))


class TestShippedData(unittest.TestCase):
    def test_behaviour_profiles_exist_match_the_corpus_and_are_clean(self):
        reg = json.loads((ROOT / "data/personality/persons/registry.json").read_text(encoding="utf-8"))
        blocked = set(reg["replay_filter"]["abusive_tokens"])
        for pid in reg["persons"]:
            d = ROOT / "data/personality/persons" / pid
            prof = BehaviorProfile.from_json((d / runtime.PROFILE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(prof.source["corpus_sha256"], runtime.sha256_file(d / "corpus.jsonl"),
                             f"{pid}: behavior_profile.json is stale - run scripts/build_behavior_profiles.py")
            lex = {w for w, *_ in prof.data["lexicon"]["unusual_words"]} | {t for r in prof.data["lexicon"]["recurring"] for t in r["gram"].split()}
            self.assertFalse(lex & blocked, pid)
            self.assertTrue(all(safety.RE_CONTACT.search(w) is None for w in lex), pid)
            self.assertGreater(prof.n_messages, 1000)
            self.assertAlmostEqual(sum(prof.reply_act_dist(core.analyze("kya kar rahi ho?"), None).values()), 1.0, places=6)

    def test_shipped_profiles_carry_reply_timing_quantiles(self):
        for pid in ("tanvi", "yashmeet"):
            prof = BehaviorProfile.from_json((ROOT / "data/personality/persons" / pid / runtime.PROFILE_FILE).read_text(encoding="utf-8"))
            lat = prof.latency()
            self.assertTrue(lat, pid)
            self.assertTrue(lat["p10"] <= lat["p25"] <= lat["p50"] <= lat["p75"] <= lat["p90"], pid)
            self.assertGreater(lat["n"], 500, pid)
            self.assertLessEqual(max(prof.suggest_delay(random.Random(i)) for i in range(300)), 6.0)

    def test_profile_files_hold_aggregates_only(self):
        for pid in ("tanvi", "yashmeet"):
            text = (ROOT / "data/personality/persons" / pid / runtime.PROFILE_FILE).read_text(encoding="utf-8")
            corpus = [json.loads(l)["text"] for l in (ROOT / "data/personality/persons" / pid / "corpus.jsonl").read_text(encoding="utf-8").splitlines()[:4000]]
            long_msgs = [t for t in corpus if len(t) >= 25]
            self.assertTrue(long_msgs)
            self.assertEqual([t for t in long_msgs if t in text], [], "a full message was copied into the profile")


class TestPackaging(unittest.TestCase):
    def test_engine_modules_import_only_the_standard_library_at_module_level(self):
        import ast
        allowed = {"__future__", "asyncio", "dataclasses", "hashlib", "importlib", "json", "logging", "math", "random", "re", "sqlite3", "sys",
                   "threading", "time", "typing", "pathlib", "collections", "zlib", "argparse", "types"}
        for p in (ROOT / "bot" / "persona_engine").glob("*.py"):
            for node in ast.parse(p.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [(node.module or "").split(".")[0]]
                else:
                    continue
                for m in mods:
                    self.assertTrue(m in allowed or m == "bot", f"{p.name}: module-level import of {m!r}")

    def test_engine_never_imports_the_training_toolkit_or_the_heavy_personality_package(self):
        import re
        for p in (ROOT / "bot" / "persona_engine").glob("*.py"):
            src = p.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?m)^\s*(?:from|import)\s+training\b", src), p.name)

    def test_new_settings_default_to_v210_behaviour(self):
        src = (ROOT / "bot" / "config.py").read_text(encoding="utf-8")
        self.assertIn('persona_engine_enabled: bool = Field(False, alias="PERSONA_ENGINE_ENABLED")', src)
        self.assertIn('persona_engine_memory_enabled: bool = Field(False, alias="PERSONA_ENGINE_MEMORY_ENABLED")', src)
        self.assertIn('persona_engine_human_delay: bool = Field(False, alias="PERSONA_ENGINE_HUMAN_DELAY")', src)


if __name__ == "__main__":
    unittest.main()
