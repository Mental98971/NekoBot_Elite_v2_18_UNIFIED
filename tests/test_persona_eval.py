"""Persona-engine evaluation framework (training/persona_eval.py) - structure, leakage and honesty checks on synthetic data.

These tests do not measure imitation quality.  They check that the framework is leak-free, reports what it ran and
what it did not, and that its scorers behave sanely on data where the right answer is known.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.persona_engine import core, scoring  # noqa: E402
from bot.persona_engine import store as st  # noqa: E402
from bot.persona_engine.engine import EngineConfig  # noqa: E402
from bot.persona_engine.providers import CallableProvider  # noqa: E402
from tests.test_persona_engine import synth_examples  # noqa: E402
from tests.test_persona_training import Env, make_rows, write_jsonl  # noqa: E402
from training import common, persona_eval as pe  # noqa: E402


class _Prepared(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.cleanup)
        write_jsonl(self.env.src, make_rows(120, 8))
        self.out = self.env.prepare()


class TestFramework(_Prepared):
    def run_cli(self, *extra):
        out = self.env.tmp / "report.json"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pe.main(["--data", str(self.out / "alice"), "--other", str(self.out / "bob"), "--out", str(out),
                          "--registry", str(self.env.registry_path), "--limit", "40", "--perms", "20", *extra])
        self.assertEqual(rc, 0)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_report_structure_and_unrun_arms_are_labelled_not_invented(self):
        r = self.run_cli()
        for key in ("scorer_discrimination", "retrieval", "context_dependence_validation", "repetition", "arms", "dataset_sha256", "test_sha256"):
            self.assertIn(key, r)
        for arm in ("baseline_llm", "pure_rag", "engine", "engine_rerank"):
            self.assertTrue(r["arms"][arm]["status"].startswith("NOT RUN"), arm)
            self.assertNotIn("subsets", r["arms"][arm])                   # no numbers without a model
        self.assertTrue(r["arms"]["retrieval_replay_v29_style"]["status"].startswith("run"))
        self.assertIn("composite", r["arms"]["retrieval_replay_v29_style"])

    def test_index_is_built_from_train_only(self):
        r = self.run_cli()
        leak = r["retrieval"]["index_leak_check"]
        self.assertEqual(leak, {"index_conversations_overlapping_test": 0, "index_ids_that_are_test_ids": 0})

    def test_report_holds_no_message_text(self):
        r = self.run_cli()
        blob = json.dumps(r)
        for split in ("train", "val", "test"):
            for e in common.iter_jsonl(self.out / "alice" / f"{split}.jsonl"):
                if len(e["target"]) >= 14:
                    self.assertNotIn(e["target"], blob)

    def test_refuses_a_tampered_dataset(self):
        with open(self.out / "alice" / "test.jsonl", "ab") as fh:
            fh.write(b'{"id": "x"}\n')
        with contextlib.redirect_stderr(io.StringIO()):
            rc = pe.main(["--data", str(self.out / "alice"), "--out", str(self.env.tmp / "r.json"), "--registry", str(self.env.registry_path)])
        self.assertEqual(rc, 2)

    def test_split_option_switches_to_validation(self):
        r = self.run_cli("--split", "val")
        self.assertGreater(r["sizes"]["test_total"], 0)


class TestMetrics(unittest.TestCase):
    def test_scorer_discrimination_prefers_real_replies_over_assistant_style(self):
        ex = synth_examples(900, seed=4)
        train, test = ex[:700], ex[700:]
        parts = pe.build_parts(train, [], ["Pat"], 80, [], n_perm=20)
        r = pe.scorer_discrimination(parts, test, [], train, n_each=3, limit=100)
        k = r["by_kind"]["assistant_style"]
        self.assertGreater(k["composite"]["mrr"], 0.85)
        self.assertGreater(k["persona"]["top1"], 0.6)
        self.assertEqual(r["by_kind"]["other_person"]["chance_top1"], 0.25)

    def test_repetition_guard_never_makes_things_worse(self):
        ex = synth_examples(900, seed=5)
        parts = pe.build_parts(ex[:700], [], ["Pat"], 80, [], n_perm=20)
        r = pe.repetition_report(parts, ex[:700], ex[700:], n_chats=10, per_chat=8)
        self.assertLessEqual(r["with_guard"]["near_duplicate_of_recent"], r["without_guard"]["near_duplicate_of_recent"])
        self.assertEqual(r["with_guard"]["replies"], r["without_guard"]["replies"])

    def test_composite_is_bounded_monotone_and_marks_missing_parts(self):
        base = {"subsets": {"all": {"n": 10, "length_js": 0.1, "char3_js": 0.2, "feature_gap": 0.05, "token_f1_mean": 0.1, "reply_class_js": 0.1}},
                "attribution": {"own_share": 0.8}, "safety": {"contact_data_rate": 0, "placeholder_rate": 0, "abusive_rate": 0},
                "memorization": {"exact_copy_rate_long": 0.0}, "coverage": 1.0}
        good = pe.composite_score(base, 0.8)
        worse = json.loads(json.dumps(base))
        worse["subsets"]["all"].update(length_js=0.6, char3_js=0.7, token_f1_mean=0.0)
        worse["safety"]["contact_data_rate"] = 0.2
        bad = pe.composite_score(worse, 0.8)
        self.assertTrue(0 <= bad["composite"] < good["composite"] <= 1)
        with_judge = json.loads(json.dumps(base))
        with_judge["context_judge"] = {"mean": 5.0}
        self.assertGreater(pe.composite_score(with_judge, 0.8)["parts"]["context"], good["parts"]["context"])
        self.assertIsNone(pe.composite_score({"subsets": {"all": {"n": 0}}}, 0.8)["composite"])

    def test_context_dependence_validation_reports_only_flagged_features(self):
        ex = synth_examples(1800, seed=2, emoji_by_chat={"c1": 0.7, "c2": 0.05, "c3": 0.05})
        parts = pe.build_parts(ex[:1400], [], ["Pat"], 80, [], n_perm=100)
        r = pe.context_dependence_validation(parts, ex[1400:])
        self.assertTrue(r["chat"]["emoji"]["flagged_context_dependent"])
        self.assertGreater(r["chat"]["emoji"]["mean_logloss_gain"], 0)         # real effect: chat-specific rate helps held-out data
        self.assertFalse(r["chat"]["question"]["flagged_context_dependent"])

    def test_generation_arms_run_with_a_scripted_provider_and_report_engine_stats(self):
        ex = synth_examples(900, seed=6)
        train, test = ex[:700], pe.eligible(ex[700:])[:12]
        parts = pe.build_parts(train, [], ["Pat"], 80, [], n_perm=20)

        async def fn(messages, n, temperature, max_tokens):
            return ["kuch nahi yaar", "bas padh rhi", "ghar pe hu"][:n]
        prov = CallableProvider(fn, "scripted")
        cfg = EngineConfig(seed=1)

        async def go():
            base = await pe.arm_baseline_llm(prov, test, cfg, 80)
            rag = await pe.arm_pure_rag(prov, parts, test, cfg, 80, "Pat")
            eng, stats = await pe.arm_engine(prov, parts, test, cfg, "Pat", [], 80, 3)
            judge = await pe.llm_judge(CallableProvider(lambda m, n, temperature, max_tokens: _async(["4"]), "j"), [("hi", "hello")], cfg)
            return base, rag, eng, stats, judge

        async def _async(v):
            return v
        base, rag, eng, stats, judge = asyncio.run(go())
        self.assertEqual(set(base), {e.id for e in test})
        self.assertEqual(set(rag), {e.id for e in test})
        self.assertTrue(set(eng) <= {e.id for e in test})
        self.assertEqual(stats["requests"], len(test))
        self.assertEqual(judge["mean"], 4.0)

    def test_request_for_gives_the_engine_exactly_what_the_bot_would_see(self):
        e = st.Example(id="x", person="p", acct="a1", chat="c", conv="v", ts=None, trigger="kya kar rahi ho?", reply="kuch nahi", kind="text",
                       ctx=(("u1", "hello"), ("me", "hii"), ("u1", "kya kar rahi ho?"))).finalize()
        req = pe.request_for(e)
        self.assertEqual(req.text, "kya kar rahi ho?")
        self.assertEqual(list(req.history), [("u1", "hello"), ("me", "hii")])
        self.assertNotIn("kuch nahi", json.dumps(req.history))          # the reply being predicted never leaks into the request


if __name__ == "__main__":
    unittest.main()
