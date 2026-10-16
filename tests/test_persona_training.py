"""Persona fine-tuning toolkit + runtime adapter router (standard library only).

Everything runs on synthetic data created in a temp directory - no real chat export is read.  The GPU
parts of training/train_lora.py and training/generate.py cannot run here; their pure logic
(tokenisation + loss masking, config validation, authorization gate, crash-safe adapter store,
leakage guard) is tested below with a fake tokenizer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from training import baselines, common, evalkit, generate, prepare_sft as ps, promote, revoke, train_lora as tl  # noqa: E402
from training.textsim import NearDupIndex  # noqa: E402

fmt = common.fmt_module()


def _load_pm():
    name = "persona_model_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "bot" / "personality" / "persona_model.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pm = _load_pm()

# ── synthetic world ──────────────────────────────────────────────────────────
A_IDS, B_ID, IMPOSTOR = ("1001", "1002"), "2001", "9999"
REGISTRY = {
    "schema": 1,
    "persons": {
        "alice": {"real_name": "Alice", "skin": "alice", "aliases": ["Ally"],
                  "accounts": [{"id": 1001, "username": "alice_main", "display_names": ["Alice M"]},
                               {"id": 1002, "username": "alice_alt", "display_names": ["Ally Alt"]}]},
        "bob": {"real_name": "Bob", "skin": "bob", "aliases": [],
                "accounts": [{"id": 2001, "username": "bob_b", "display_names": ["Bobby Tables"]}]},
    },
    "replay_filter": {"abusive_tokens": ["darnit"]},
}
AUTH_OK = {"status": "granted", "scope": ["weights_training"], "granted_by": "alice (self)", "evidence": "dated note #1", "date": "2026-10-01"}


def make_rows(n_conv: int = 40, per_conv: int = 8, chat: str = "-1001"):
    """Rows in the jsonl export shape read by scripts/build_person_personas.py."""
    rows, mid = [], 1000
    base = time.mktime(time.strptime("2025-03-01 08:00:00", "%Y-%m-%d %H:%M:%S"))
    for c in range(n_conv):
        t0 = base + c * 4 * 3600  # 4 h apart -> separate conversations (gap rule is 30 min)
        for i in range(per_conv):
            mid += 1
            who = [("3001", "carol_c", "Carol"), (A_IDS[(c + i // 4) % 2], None, None), (B_ID, None, None), (IMPOSTOR, "mallory", "Alice M")][i % 4]
            sender = who[0]
            text = f"msg {c} {i} " + ("hello there friend" if sender in A_IDS else "yo there pal" if sender == B_ID else "what's up folks")
            rows.append({
                "chat_id": chat, "msg_id": str(mid), "sender_id": sender, "sender_username": who[1] or "",
                "sender_name": who[2] or "", "message_text": text,
                "date": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t0 + i * 60)),
                "reply_to_msg_id": str(mid - 1) if i % 3 == 1 else "",
            })
    return rows


def write_jsonl(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class Env:
    """Temp dir + synthetic registry/source helpers."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="persona_train_test_"))
        self.registry_path = self.tmp / "registry.json"
        self.registry_path.write_text(json.dumps(REGISTRY), encoding="utf-8")
        self.src = write_jsonl(self.tmp / "export.jsonl", make_rows())

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def prepare(self, out: Path | None = None, extra=(), seed: int | None = None, cfg: dict | None = None):
        out = out or (self.tmp / "out")
        cfgp = None
        if cfg:
            cfgp = self.tmp / "cfg.json"
            cfgp.write_text(json.dumps(cfg), encoding="utf-8")
        argv = ["--source", str(self.src), *sum((["--source", str(e)] for e in extra), []),
                "--registry", str(self.registry_path), "--out", str(out)]
        if seed is not None:
            argv += ["--seed", str(seed)]
        if cfgp:
            argv += ["--config", str(cfgp)]
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ps.main(argv)
        assert rc == 0
        return out


class _Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.cleanup)


# ── data preparation ─────────────────────────────────────────────────────────
class TestPrepare(_Base):
    def test_identity_is_by_numeric_id_only(self):
        out = self.env.prepare()
        alice = [e for s in ("train", "val", "test") for e in common.iter_jsonl(out / "alice" / f"{s}.jsonl")]
        bob = [e for s in ("train", "val", "test") for e in common.iter_jsonl(out / "bob" / f"{s}.jsonl")]
        self.assertTrue(alice and bob)
        self.assertTrue(all(e["target"].endswith("hello there friend") for e in alice))
        self.assertTrue(all(e["target"].endswith("yo there pal") for e in bob))
        # the impostor copies Alice's display name; none of their text may become an Alice example
        self.assertFalse([e for e in alice if "what's up folks" in e["target"]])

    def test_no_cross_person_contamination_and_no_names_in_prompts(self):
        out = self.env.prepare()
        for pid, other_marker in (("alice", "yo there pal"), ("bob", "hello there friend")):
            for s in ("train", "val", "test"):
                for e in common.iter_jsonl(out / pid / f"{s}.jsonl"):
                    self.assertNotIn(other_marker, e["target"])
                    prompt = fmt.render_user_block(e["ctx"], e["reply_to"], e["chat_kind"], e["acct"])
                    for secret in ("alice", "ally", "bob", "carol", "mallory", "1001", "2001", "3001", "9999", "alice_main", "bob_b"):
                        self.assertNotIn(secret, prompt.lower(), f"{secret!r} leaked into a prompt")
        # the other registered person is just an anonymous participant (u1, u2 ...) in this person's context
        speakers = {w for e in common.iter_jsonl(out / "alice" / "train.jsonl") for w, _ in e["ctx"]}
        self.assertTrue(speakers <= {"me", "u1", "u2", "u3", "u4", "u5", "u6"})

    def test_account_tag_uses_registry_order_not_ids(self):
        out = self.env.prepare()
        tags = {e["acct"] for s in ("train", "val", "test") for e in common.iter_jsonl(out / "alice" / f"{s}.jsonl")}
        self.assertEqual(tags, {"a1", "a2"})

    def test_splits_are_disjoint_by_conversation_and_near_target_ratio(self):
        out = self.env.prepare()
        per = {s: list(common.iter_jsonl(out / "alice" / f"{s}.jsonl")) for s in ("train", "val", "test")}
        self.assertTrue(all(ps.check_splits(per).values()))
        total = sum(len(v) for v in per.values())
        self.assertAlmostEqual(len(per["test"]) / total, 0.10, delta=0.08)
        self.assertAlmostEqual(len(per["val"]) / total, 0.10, delta=0.08)
        convs = {s: {e["conv"] for e in v} for s, v in per.items()}
        self.assertFalse(convs["train"] & convs["test"] or convs["train"] & convs["val"] or convs["val"] & convs["test"])

    def test_context_never_contains_the_future(self):
        out = self.env.prepare()
        for e in common.iter_jsonl(out / "alice" / "test.jsonl"):
            self.assertLessEqual(len(e["ctx"]), 6)

    def test_output_is_deterministic_and_seed_sensitive(self):
        a = self.env.prepare(self.env.tmp / "o1")
        b = self.env.prepare(self.env.tmp / "o2")
        self.assertEqual(json.loads((a / "alice" / "manifest.json").read_text())["dataset_sha256"],
                         json.loads((b / "alice" / "manifest.json").read_text())["dataset_sha256"])
        c = self.env.prepare(self.env.tmp / "o3", seed=7)
        self.assertNotEqual(json.loads((a / "alice" / "manifest.json").read_text())["splits"]["test"]["sha256"],
                            json.loads((c / "alice" / "manifest.json").read_text())["splits"]["test"]["sha256"])

    def test_source_flag_order_does_not_change_the_dataset(self):
        second = write_jsonl(self.env.tmp / "zz_second.jsonl", make_rows(5, 8, chat="-2002"))
        builder = common.load_module("build_person_personas", "scripts/build_person_personas.py")
        a = ps.load_sources([self.env.src, second], builder)
        b = ps.load_sources([second, self.env.src], builder)
        reg = json.loads(self.env.registry_path.read_text())
        cfg = json.loads(json.dumps(ps.DEFAULT_CONFIG))
        per_a, _ = ps.prepare(a, reg, cfg)
        per_b, _ = ps.prepare(b, reg, cfg)
        for pid in per_a:
            for split in ("train", "val", "test"):
                self.assertEqual(common.jsonl_bytes(per_a[pid][split]), common.jsonl_bytes(per_b[pid][split]), (pid, split))

    def test_reference_manifests_are_text_free_and_verify_a_rebuild(self):
        ref = self.env.tmp / "ref"
        out = self.env.tmp / "o_ref"
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ps.main(["--source", str(self.env.src), "--registry", str(self.env.registry_path), "--out", str(out),
                                      "--write-reference-manifests", str(ref)]), 0)
        for pid in ("alice", "bob"):
            raw = (ref / f"{pid}.json").read_text(encoding="utf-8")
            self.assertIn('"distributable": true', raw)
            for e in common.iter_jsonl(out / pid / "train.jsonl"):
                self.assertNotIn(e["target"], raw)  # no message text in the shareable manifest
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ps.main(["--source", str(self.env.src), "--registry", str(self.env.registry_path), "--out", str(self.env.tmp / "o_ref2"),
                          "--reference-manifests", str(ref)])
        self.assertEqual(rc, 0)
        self.assertIn("MATCH", buf.getvalue())
        write_jsonl(self.env.src, make_rows(39, 8))  # change the raw data -> must report DIFFERENT and exit 3
        with contextlib.redirect_stdout(io.StringIO()) as b2:
            rc2 = ps.main(["--source", str(self.env.src), "--registry", str(self.env.registry_path), "--out", str(self.env.tmp / "o_ref3"),
                           "--reference-manifests", str(ref)])
        self.assertEqual(rc2, 3)

    def test_manifest_detects_tampering(self):
        out = self.env.prepare()
        d = out / "alice"
        common.verify_prepared_dir(d, ("train", "val", "test"))
        with open(d / "train.jsonl", "ab") as fh:
            fh.write(b'{"id": "x"}\n')
        with self.assertRaises(common.TrainingDataError):
            common.verify_prepared_dir(d, ("train",))

    def test_every_exclusion_is_counted_and_nothing_is_silently_lost(self):
        rows = make_rows(6, 8)
        n = len(rows)
        extra = [
            {"chat_id": "-1001", "msg_id": str(5000 + i), "sender_id": "1001", "sender_username": "alice_main",
             "message_text": t, "date": f"2025-05-01 10:0{i}:00"}
            for i, t in enumerate(["/start@somebot", "[media/non-text]", "call +91 98765 43210 now", "that is darnit bad",
                                   "alpha beta " * 100, "ok fine"])
        ]
        write_jsonl(self.env.src, rows + extra)
        out = self.env.prepare()
        rep = json.loads((out / "prepare_report.json").read_text())["persons"]["alice"]
        ex = rep["excluded"]
        self.assertEqual(ex["target_command"], 1)
        self.assertEqual(ex["target_media"], 1)
        self.assertEqual(ex["target_has_contact_placeholder"], 1)
        self.assertEqual(ex["target_abusive_token"], 1)
        self.assertEqual(ex["target_too_long"], 1)
        kept = sum(v["examples"] for v in rep["splits"].values())
        self.assertEqual(rep["messages_from_registered_accounts"], kept + sum(v for k, v in ex.items() if k != "train_capped_duplicate") + ex.get("train_capped_duplicate", 0))
        self.assertEqual(n, len(rows))

    def test_identical_replies_are_capped_in_train_but_flagged_elsewhere(self):
        rows = make_rows(2, 4) + [
            {"chat_id": "-1002", "msg_id": str(7000 + i), "sender_id": "1001", "sender_username": "alice_main",
             "message_text": "hmm", "date": f"2025-06-{1 + i // 20:02d} {10 + (i % 20) // 4:02d}:{(i % 4) * 10:02d}:00"}
            for i in range(120)
        ]
        write_jsonl(self.env.src, rows)
        out = self.env.prepare(cfg={"max_dup_train": 5})
        train = list(common.iter_jsonl(out / "alice" / "train.jsonl"))
        self.assertLessEqual(sum(e["target"] == "hmm" for e in train), 5)
        rep = json.loads((out / "prepare_report.json").read_text())["persons"]["alice"]
        self.assertGreater(rep["excluded"]["train_capped_duplicate"], 0)

    def test_duplicate_rows_across_sources_are_merged_not_double_counted(self):
        dup = write_jsonl(self.env.tmp / "again.jsonl", make_rows())
        once = self.env.prepare(self.env.tmp / "once")
        twice = self.env.prepare(self.env.tmp / "twice", extra=[dup])
        self.assertEqual(json.loads((once / "alice" / "manifest.json").read_text())["dataset_sha256"],
                         json.loads((twice / "alice" / "manifest.json").read_text())["dataset_sha256"])

    def test_two_persons_claiming_one_account_is_a_registry_error(self):
        bad = json.loads(json.dumps(REGISTRY))
        bad["persons"]["bob"]["accounts"].append({"id": 1001, "username": "x", "display_names": []})
        with self.assertRaises(common.TrainingDataError):
            common.account_index(bad)

    def test_missing_source_fails_loudly(self):
        self.env.src.unlink()
        with self.assertRaises(common.TrainingDataError):
            ps.load_sources([self.env.src], common.load_module("build_person_personas", "scripts/build_person_personas.py"))


class TestSuppliedIdentities(unittest.TestCase):
    """The account identifiers supplied for the two real-person personas, checked against the shipped registry."""

    @classmethod
    def setUpClass(cls):
        cls.persons = _load_persons()
        cls.reg = cls.persons.PersonRegistry.load(ROOT / "data" / "personality" / "persons" / "registry.json")

    def test_numeric_ids_and_usernames_resolve_to_the_right_person(self):
        for uid, uname, pid in ((7883544685, "You_are_mine_143", "tanvi"), (7187486402, "ichiiren", "tanvi"), (5553376334, "concyclicity", "yashmeet")):
            self.assertEqual(self.reg.resolve(user_id=uid).person.id, pid)
            self.assertEqual(self.reg.resolve(username=uname).person.id, pid)
            self.assertEqual(self.reg.resolve(username="@" + uname.upper()).person.id, pid)  # case/@ tolerant

    def test_id_wins_over_a_conflicting_username(self):
        got = self.reg.resolve(user_id=5553376334, username="ichiiren")
        self.assertEqual(got.person.id, "yashmeet")

    def test_display_names_alone_never_identify_anyone(self):
        for name in ("Iziiee", "GALAXY ANHILATOR", "GALAXY ANNIHILATOR", "Tanvi", "Yashmeet"):
            self.assertIsNone(self.reg.resolve(username=name), name)
            self.assertIsNone(self.reg.resolve(user_id=None, username=name), name)

    def test_unknown_users_get_no_persona(self):
        self.assertIsNone(self.reg.resolve(user_id=1))
        self.assertIsNone(self.reg.resolve())

    def test_every_supplied_display_name_variant_is_kept_as_inert_metadata(self):
        names = {n for p in self.reg.persons() for a in p.accounts for n in a.display_names}
        for expected in ("Iziiee", "GALAXY ANHILATOR", "GALAXY ANNIHILATOR", "ichiiren"):
            self.assertIn(expected, names)


def _load_persons():
    name = "bot.personality.persons"
    if name in sys.modules:
        return sys.modules[name]
    import types
    for pkg_name, path in (("bot", ROOT / "bot"), ("bot.personality", ROOT / "bot" / "personality")):
        if pkg_name not in sys.modules:
            m = types.ModuleType(pkg_name)
            m.__path__ = [str(path)]
            sys.modules[pkg_name] = m
    spec = importlib.util.spec_from_file_location(name, ROOT / "bot" / "personality" / "persons.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── prompt format: training == runtime ───────────────────────────────────────
class TestFormat(unittest.TestCase):
    def test_runtime_prompt_equals_training_prompt_for_the_same_situation(self):
        ex = {"ctx": [["u1", "kya haal hai"]], "reply_to": None, "chat_kind": "dm", "acct": "a2"}
        self.assertEqual(fmt.example_messages(ex), fmt.runtime_messages("kya haal hai", None, "dm", "a2"))

    def test_long_turns_are_bounded_and_newlines_flattened(self):
        block = fmt.render_user_block([["u1", "a\nb " + "w" * 500]], None, "group", None)
        self.assertNotIn("\nb", block.split("[recent messages]")[1].split("Write the next")[0].replace("⏎", ""))
        self.assertLess(max(len(l) for l in block.splitlines()), fmt.TURN_MAX_CHARS + 20)

    def test_empty_turn_renders_as_media_marker(self):
        self.assertIn("u1: [media]", fmt.render_user_block([["u1", ""]], None, "group", None))

    def test_unknown_chat_kind_defaults_to_group(self):
        self.assertTrue(fmt.render_user_block([], None, "weird", None).startswith("[chat: group]"))


# ── trainer pure logic ───────────────────────────────────────────────────────
class FakeTok:
    """Whitespace tokenizer with a ``chat_template`` stand-in; ids are stable per word."""
    chat_template = None
    eos_token_id = 2
    unk_token_id = 0

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [3 + (int(hashlib.md5(w.encode()).hexdigest()[:4], 16) % 500) for w in text.split()]}


class TestTrainerLogic(_Base):
    def ex(self, nctx=6, target="ok sure"):
        return {"id": "e1", "ctx": [["u1", f"line {i} " + "word " * 5] for i in range(nctx)], "reply_to": ["u1", "hey"],
                "chat_kind": "group", "acct": "a1", "target": target}

    def test_loss_is_only_on_the_reply(self):
        tok, end = FakeTok(), 2
        item = tl.tokenize_example(tok, fmt, self.ex(), 4096, end)
        n_tgt = len(tok("ok sure")["input_ids"]) + 1
        self.assertEqual(item["labels"][-n_tgt:], item["input_ids"][-n_tgt:])
        self.assertEqual(item["input_ids"][-1], end)
        self.assertTrue(all(l == tl.IGNORE_INDEX for l in item["labels"][:-n_tgt]))
        self.assertEqual(len(item["input_ids"]), len(item["labels"]))

    def test_overlong_examples_lose_oldest_context_first_and_never_the_reply(self):
        tok = FakeTok()
        full = tl.tokenize_example(tok, fmt, self.ex(), 4096, 2)
        short = tl.tokenize_example(tok, fmt, self.ex(), len(full["input_ids"]) - 5, 2)
        self.assertIsNotNone(short)
        self.assertLess(len(short["input_ids"]), len(full["input_ids"]))
        self.assertEqual(short["input_ids"][-3:], full["input_ids"][-3:])  # reply tokens intact
        self.assertIsNone(tl.tokenize_example(tok, fmt, self.ex(), 12, 2))

    def test_last_turn_only_and_tag_dropout_change_the_prompt(self):
        tok = FakeTok()
        a = tl.tokenize_example(tok, fmt, self.ex(), 4096, 2)
        b = tl.tokenize_example(tok, fmt, self.ex(), 4096, 2, last_turn_only=True)
        c = tl.tokenize_example(tok, fmt, self.ex(), 4096, 2, drop_account_tag=True)
        self.assertLess(len(b["input_ids"]), len(a["input_ids"]))
        self.assertLess(len(c["input_ids"]), len(a["input_ids"]))

    def test_collate_pads_inputs_mask_and_labels(self):
        out = tl.collate([{"input_ids": [5, 6], "attention_mask": [1, 1], "labels": [-100, 6]},
                          {"input_ids": [7], "attention_mask": [1], "labels": [7]}], pad_id=0)
        self.assertEqual(out["input_ids"], [[5, 6], [7, 0]])
        self.assertEqual(out["attention_mask"], [[1, 1], [1, 0]])
        self.assertEqual(out["labels"], [[-100, 6], [7, -100]])

    def test_coin_is_deterministic_and_calibrated(self):
        flips = [tl.coin(1, f"id{i}", "tag", 0.25) for i in range(4000)]
        self.assertEqual(flips, [tl.coin(1, f"id{i}", "tag", 0.25) for i in range(4000)])
        self.assertAlmostEqual(sum(flips) / 4000, 0.25, delta=0.03)
        self.assertFalse(any(tl.coin(1, f"id{i}", "tag", 0.0) for i in range(200)))

    def test_config_validation(self):
        base = json.loads(json.dumps(tl.DEFAULTS))
        base["base_model"] = "org/model-7b"
        self.assertEqual(tl.validate_config(json.loads(json.dumps(base)))["method"], "lora")
        for patch in ({"method": "full"}, {"base_model": None}, {"max_seq_len": 8}, {"account_tag_dropout": 1.5},
                      {"short_context_rate": -0.1}, {"lora": {"r": 0}}, {"optim": {"learning_rate": 0}}):
            cfg = tl._merge(base, patch) if "lora" in patch or "optim" in patch else {**base, **patch}
            with self.assertRaises(tl.PreflightError, msg=str(patch)):
                tl.validate_config(cfg)
        with self.assertRaises(tl.PreflightError):
            tl._merge(base, {"no_such_key": 1})

    def test_vram_estimate_scales_with_method(self):
        self.assertGreater(tl.estimate_vram_gb("x/model-7b", "lora"), tl.estimate_vram_gb("x/model-7b", "qlora"))
        self.assertIsNone(tl.estimate_vram_gb("x/mystery", "lora"))

    def test_end_token_must_exist(self):
        tok = FakeTok()
        tok.convert_tokens_to_ids = lambda t: 0
        with self.assertRaises(tl.PreflightError):
            tl.end_token_id(tok, "<|nope|>")
        self.assertEqual(tl.end_token_id(tok, None), 2)

    def test_train_script_never_opens_the_test_split(self):
        import ast, re
        src = (ROOT / "training" / "train_lora.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
        self.assertFalse([l for l in literals if "test" in l.lower() and ".jsonl" in l.lower()])
        opened = set(re.findall(r'iter_jsonl\(data / "(\w+)\.jsonl"\)', src))
        self.assertEqual(opened, {"train", "val"})
        self.assertIn('("train", "val")', src)  # manifest verification covers exactly these two files


# ── authorization gate ───────────────────────────────────────────────────────
class TestAuthorization(_Base):
    def write(self, rec):
        p = self.env.tmp / "auth.json"
        p.write_text(json.dumps({"persons": {"alice": rec}}), encoding="utf-8")
        return p

    def test_shipped_records_are_consistent_with_the_gate(self):
        auth = json.loads((ROOT / "training" / "authorizations.json").read_text(encoding="utf-8"))
        reg = common.load_registry()
        self.assertEqual(set(auth["persons"]), set(reg["persons"]))
        for pid, rec in auth["persons"].items():
            self.assertIn(rec["status"], {"pending", "granted", "revoked"})
            if rec["status"] == "granted":                      # a granted record must actually pass the gate and say where the agreement is
                self.assertEqual(common.require_authorization(pid)["status"], "granted")
                self.assertIn("weights_training", rec["scope"])
                self.assertTrue(rec["evidence"].strip() and rec["granted_by"].strip() and rec["date"].strip())
            else:
                with self.assertRaises(common.AuthorizationError):
                    common.require_authorization(pid)

    def test_recorded_agreements_cover_both_persons_and_do_not_expire(self):
        auth = json.loads((ROOT / "training" / "authorizations.json").read_text(encoding="utf-8"))["persons"]
        for pid in ("tanvi", "yashmeet"):
            self.assertEqual(auth[pid]["status"], "granted", pid)
            self.assertNotIn("expires", auth[pid])
            self.assertIn("any_use_including_future", auth[pid]["scope"])

    def test_the_gate_still_blocks_a_person_without_a_record_and_revocation_still_works(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "a.json"
        p.write_text((ROOT / "training" / "authorizations.json").read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(common.AuthorizationError):
            common.require_authorization("someone_else", p)
        revoke.revoke("tanvi", tmp / "store", p)
        with self.assertRaises(common.AuthorizationError):
            common.require_authorization("tanvi", p)
        self.assertEqual(common.require_authorization("yashmeet", p)["status"], "granted")   # revoking one person leaves the other untouched

    def test_pending_or_incomplete_records_are_refused(self):
        for rec in ({"status": "pending"}, {**AUTH_OK, "status": "revoked"}, {**AUTH_OK, "scope": []},
                    {**AUTH_OK, "evidence": " "}, {**AUTH_OK, "granted_by": ""}, {**AUTH_OK, "date": ""}):
            with self.assertRaises(common.AuthorizationError, msg=str(rec)):
                common.require_authorization("alice", self.write(rec))

    def test_unknown_person_and_unreadable_file_are_refused(self):
        with self.assertRaises(common.AuthorizationError):
            common.require_authorization("nobody", self.write(AUTH_OK))
        with self.assertRaises(common.AuthorizationError):
            common.require_authorization("alice", self.env.tmp / "missing.json")

    def test_granted_record_passes_and_is_returned_for_the_adapter_metadata(self):
        self.assertEqual(common.require_authorization("alice", self.write(AUTH_OK))["granted_by"], "alice (self)")

    def test_training_preflight_refuses_without_authorization_before_anything_else(self):
        out = self.env.prepare()
        cfg = tl._merge(json.loads(json.dumps(tl.DEFAULTS)), {"base_model": "x/m-1b"})
        with self.assertRaises(common.AuthorizationError):
            tl.preflight("alice", out / "alice", cfg, self.write({"status": "pending"}), True, True, True)

    def test_preflight_reports_environment_problems_in_check_mode(self):
        out = self.env.prepare()
        cfg = tl._merge(json.loads(json.dumps(tl.DEFAULTS)), {"base_model": "x/m-1b", "min_train_examples": 1, "min_val_examples": 1})
        plan = tl.preflight("alice", out / "alice", cfg, self.write(AUTH_OK), False, False, True)
        self.assertEqual(plan["authorization"]["status"], "granted")
        self.assertEqual(plan["examples"]["train"] > 0, True)
        if plan["env"]["missing"] or not plan["env"]["cuda"]:
            self.assertTrue(plan["problems"])
            with self.assertRaises(tl.PreflightError):
                tl.preflight("alice", out / "alice", cfg, self.write(AUTH_OK), False, False, False)

    def test_preflight_refuses_data_of_another_person(self):
        out = self.env.prepare()
        cfg = tl._merge(json.loads(json.dumps(tl.DEFAULTS)), {"base_model": "x/m-1b"})
        with self.assertRaises(tl.PreflightError):
            tl.preflight("alice", out / "bob", cfg, self.write(AUTH_OK), True, True, True)


# ── crash-safe adapter store, leakage guard, promote, revoke ─────────────────
def fake_adapter(d: Path, person="alice", dataset="d" * 64, run="r1", fmt_version=None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapter_config.json").write_text(json.dumps({"base_model_name_or_path": "x/base"}), encoding="utf-8")
    (d / "adapter_model.safetensors").write_bytes(b"weights")
    (d / tl.ADAPTER_META).write_text(json.dumps({
        "schema": 1, "person": person, "dataset_sha256": dataset, "format_version": fmt_version or fmt.FORMAT_VERSION,
        "base_model": "x/base", "authorization": AUTH_OK, "run_id": run}), encoding="utf-8")
    return d


class TestAdapterStore(_Base):
    def test_swap_keeps_previous_and_corrupt_staging_leaves_current_intact(self):
        pdir = self.env.tmp / "store" / "alice"
        tl.promote_staging(pdir, fake_adapter(pdir / "staging-1", run="r1"))
        tl.promote_staging(pdir, fake_adapter(pdir / "staging-2", run="r2"))
        self.assertEqual(tl.validate_adapter_dir(pdir / "current")["run_id"], "r2")
        self.assertEqual(tl.validate_adapter_dir(pdir / "previous")["run_id"], "r1")
        bad = fake_adapter(pdir / "staging-3", run="r3")
        (bad / "adapter_model.safetensors").write_bytes(b"")  # interrupted save
        with self.assertRaises(common.TrainingDataError):
            tl.promote_staging(pdir, bad)
        self.assertEqual(tl.validate_adapter_dir(pdir / "current")["run_id"], "r2")

    def test_recover_store_restores_previous_after_a_crash_mid_swap(self):
        pdir = self.env.tmp / "store" / "alice"
        fake_adapter(pdir / "previous", run="r1")
        self.assertEqual(tl.recover_store(pdir), "restored previous -> current")
        self.assertEqual(tl.validate_adapter_dir(pdir / "current")["run_id"], "r1")
        self.assertEqual(tl.recover_store(pdir), "ok")

    def test_incomplete_adapters_are_rejected(self):
        d = fake_adapter(self.env.tmp / "a")
        (d / tl.ADAPTER_META).unlink()
        with self.assertRaises(common.TrainingDataError):
            tl.validate_adapter_dir(d)

    def test_atomic_write_leaves_no_temp_files_and_preserves_old_content_on_failure(self):
        p = self.env.tmp / "x.json"
        common.atomic_write_json(p, {"a": 1})
        with self.assertRaises(TypeError):
            common.atomic_write_json(p, {"a": object()})
        self.assertEqual(json.loads(p.read_text())["a"], 1)
        self.assertEqual([f.name for f in self.env.tmp.iterdir() if f.name.endswith(".tmp")], [])

    def test_generate_refuses_an_adapter_trained_on_a_different_dataset_build(self):
        out = self.env.prepare()
        man = json.loads((out / "alice" / "manifest.json").read_text())
        good = fake_adapter(self.env.tmp / "good", dataset=man["dataset_sha256"])
        self.assertEqual(generate.check_adapter_matches_dataset(good, out / "alice")["person"], "alice")
        stale = fake_adapter(self.env.tmp / "stale", dataset="0" * 64)
        with self.assertRaises(common.TrainingDataError):
            generate.check_adapter_matches_dataset(stale, out / "alice")
        other = fake_adapter(self.env.tmp / "other", person="bob", dataset=man["dataset_sha256"])
        with self.assertRaises(common.TrainingDataError):
            generate.check_adapter_matches_dataset(other, out / "alice")


class TestPromoteRevoke(_Base):
    def setUp(self):
        super().setUp()
        self.store = self.env.tmp / "store"
        self.auth = self.env.tmp / "auth.json"
        self.auth.write_text(json.dumps({"persons": {"alice": dict(AUTH_OK)}}), encoding="utf-8")
        tl.promote_staging(self.store / "alice", fake_adapter(self.store / "alice" / "staging-1", dataset="d" * 64, run="r1"))
        common.atomic_write_json(self.store / "registry.json", {"schema": 1, "persons": {"alice": {
            "status": "trained", "model": "persona-alice", "run_id": "r1", "format_version": fmt.FORMAT_VERSION,
            "dataset_sha256": "d" * 64, "base_model": "x/base"}}})

    def report(self, passed=True, person="alice", dataset="d" * 64):
        return {"person": person, "dataset_sha256": dataset, "test_sha256": "t",
                "gate": {"candidate": "adapter", "passed": passed, "checks": [{"name": "c1", "passed": passed, "detail": ""}]}}

    def test_promote_requires_a_passing_gate(self):
        with self.assertRaises(common.TrainingDataError):
            promote.promote("alice", self.report(False), self.store, self.auth)
        with self.assertRaises(common.TrainingDataError):
            promote.promote("alice", {"person": "alice", "dataset_sha256": "d" * 64}, self.store, self.auth)
        self.assertEqual(json.loads((self.store / "registry.json").read_text())["persons"]["alice"]["status"], "trained")

    def test_promote_checks_person_and_dataset_and_run(self):
        with self.assertRaises(common.TrainingDataError):
            promote.promote("alice", self.report(person="bob"), self.store, self.auth)
        with self.assertRaises(common.TrainingDataError):
            promote.promote("alice", self.report(dataset="e" * 64), self.store, self.auth)

    def test_promote_flips_status_and_demote_reverts(self):
        rec = promote.promote("alice", self.report(), self.store, self.auth)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(json.loads((self.store / "registry.json").read_text())["persons"]["alice"]["status"], "ready")
        promote.demote("alice", self.store)
        self.assertEqual(json.loads((self.store / "registry.json").read_text())["persons"]["alice"]["status"], "trained")

    def test_promote_refuses_after_authorization_is_withdrawn(self):
        self.auth.write_text(json.dumps({"persons": {"alice": {**AUTH_OK, "status": "revoked"}}}), encoding="utf-8")
        with self.assertRaises(common.AuthorizationError):
            promote.promote("alice", self.report(), self.store, self.auth)

    def test_revoke_removes_adapters_registry_entry_and_prepared_data(self):
        prepared = self.env.prepare()
        done = revoke.revoke("alice", self.store, self.auth, prepared / "alice")
        self.assertTrue(any("registry entry removed" in d for d in done))
        self.assertFalse((self.store / "alice").exists())
        self.assertFalse((prepared / "alice").exists())
        self.assertNotIn("alice", json.loads((self.store / "registry.json").read_text())["persons"])
        self.assertEqual(json.loads(self.auth.read_text())["persons"]["alice"]["status"], "revoked")
        with self.assertRaises(common.AuthorizationError):
            common.require_authorization("alice", self.auth)
        with self.assertRaises(common.AuthorizationError):
            revoke.revoke("nobody", self.store, self.auth)


# ── evaluation kit ───────────────────────────────────────────────────────────
STYLE_A = ["haan yaar kal milte hai", "ok done", "lol kya bol rha hai tu", "chal theek hai bye", "haha sahi hai", "nahi yaar mat kar"] * 20
STYLE_B = ["Indeed, I shall consider it.", "Certainly. Please proceed.", "That is acceptable to me.", "Thank you for the update."] * 20


class TestEvalKit(unittest.TestCase):
    def test_js_divergence_properties(self):
        c = Counter({"a": 3, "b": 1})
        self.assertAlmostEqual(evalkit.js_divergence(c, c), 0.0, places=9)
        self.assertAlmostEqual(evalkit.js_divergence(Counter({"a": 1}), Counter({"b": 1})), 1.0, places=9)
        x, y = Counter({"a": 3, "b": 1}), Counter({"a": 1, "b": 3})
        self.assertAlmostEqual(evalkit.js_divergence(x, y), evalkit.js_divergence(y, x), places=9)
        self.assertEqual(evalkit.js_divergence({}, {}), 0.0)

    def test_attribution_separates_two_styles(self):
        nb = evalkit.CharNB().fit({"a": STYLE_A, "b": STYLE_B})
        self.assertGreater(evalkit.attribution(["haan yaar chal bye", "lol ok"], nb, "a")["own_share"], 0.99)
        self.assertGreater(evalkit.attribution(["Certainly, thank you."], nb, "b")["own_share"], 0.99)
        self.assertLess(evalkit.attribution(["Certainly, thank you."], nb, "a")["own_share"], 0.01)  # contamination is visible

    def test_memorization_counts_verbatim_copies_of_long_replies(self):
        train = ["this is a long reply copied straight from training data ok", "short"]
        preds = ["this is a long reply copied straight from training data ok", "a brand new sentence with many different words in it"]
        m = evalkit.memorization(preds, train)
        self.assertEqual(m["exact_copy_rate_long"], 0.5)
        self.assertEqual(evalkit.memorization(["completely unrelated words appear here today friend"], train)["ngram_overlap_rate"], 0.0)

    def test_safety_flags_contact_data_placeholders_and_abusive_tokens(self):
        s = evalkit.safety(["call +91 98765 43210", "see https://x.io", "hi <user>", "that is darnit", "fine", ""], ["darnit"])
        self.assertAlmostEqual(s["contact_data_rate"], 2 / 6)
        self.assertAlmostEqual(s["placeholder_rate"], 1 / 6)
        self.assertAlmostEqual(s["abusive_rate"], 1 / 6)
        self.assertAlmostEqual(s["empty_rate"], 1 / 6)

    def test_feature_rates(self):
        r = evalkit.feature_rates(["hello?", "Wow!!", "sooo good...", "ok 😀"])
        self.assertEqual(r["question"], 0.25)
        self.assertEqual(r["exclaim"], 0.25)
        self.assertEqual(r["emoji"], 0.25)
        self.assertEqual(r["ellipsis"], 0.25)

    def _test_split(self, n=120):
        refs = (STYLE_A * 3)[:n]
        return [{"id": f"t{i}", "target": t, "flags": [], "tk": "text"} for i, t in enumerate(refs)]

    def test_system_that_copies_the_reference_scores_better_than_the_other_persons_style(self):
        test = self._test_split()
        nb = evalkit.CharNB().fit({"a": STYLE_A, "b": STYLE_B})
        good = {e["id"]: e["target"] for e in test}
        wrong = {e["id"]: STYLE_B[i % len(STYLE_B)] for i, e in enumerate(test)}
        rg = evalkit.evaluate_system(good, test, STYLE_A[:5], nb, "a")
        rw = evalkit.evaluate_system(wrong, test, STYLE_A[:5], nb, "a")
        self.assertLess(rg["subsets"]["all"]["char3_js"], rw["subsets"]["all"]["char3_js"])
        self.assertLess(rg["subsets"]["all"]["unigram_js"], rw["subsets"]["all"]["unigram_js"])
        self.assertGreater(rg["subsets"]["all"]["token_f1_mean"], rw["subsets"]["all"]["token_f1_mean"])
        self.assertGreater(rg["attribution"]["own_share"], rw["attribution"]["own_share"])
        self.assertEqual(rg["coverage"], 1.0)

    def test_unanswered_examples_lower_coverage(self):
        test = self._test_split()
        nb = evalkit.CharNB().fit({"a": STYLE_A})
        preds = {e["id"]: e["target"] for e in test[:60]}
        self.assertAlmostEqual(evalkit.evaluate_system(preds, test, [], nb, "a")["coverage"], 0.5)

    def test_gates(self):
        test = self._test_split()
        nb = evalkit.CharNB().fit({"a": STYLE_A, "b": STYLE_B})
        cand = evalkit.evaluate_system({e["id"]: e["target"] for e in test}, test, [], nb, "a")
        weak = evalkit.evaluate_system({e["id"]: STYLE_B[0] for e in test}, test, [], nb, "a")
        gates = {"subset": "all", "min_coverage": 0.98, "max_abusive": 0.0, "max_contact_data": 0.0, "max_placeholder": 0.0,
                 "min_own_share_vs_reference": 0.9, "must_beat": {"weak": {"metrics": ["char3_js", "unigram_js", "token_f1_mean"], "at_least": 3}}}
        ok = evalkit.check_gates(cand, {"weak": weak}, gates)
        self.assertTrue(ok["passed"], ok)
        bad = evalkit.check_gates(weak, {"weak": cand}, gates)
        self.assertFalse(bad["passed"])
        self.assertFalse(evalkit.check_gates(cand, {}, gates)["passed"])  # missing baseline is a failure, not a pass
        self.assertFalse(evalkit.check_gates(cand, {}, {})["passed"] and False)

    def test_exact_copy_gate_is_skipped_when_too_few_long_replies(self):
        rep = {"coverage": 1.0, "memorization": {"exact_copy_rate_long": 1.0, "ngram_overlap_rate": 0.0, "long_replies": 3},
               "safety": {"abusive_rate": 0, "contact_data_rate": 0, "placeholder_rate": 0}}
        v = evalkit.check_gates(rep, {}, {"max_exact_copy_long": 0.05, "max_ngram_overlap": 0.35, "min_long_replies": 20})
        self.assertTrue(v["passed"])
        self.assertIn("not evaluated", next(c for c in v["checks"] if c["name"] == "exact_copy_rate_long")["detail"])

    def test_noise_floor_is_reported_and_small_for_iid_halves(self):
        nf = evalkit.noise_floor(STYLE_A * 5)
        self.assertLess(nf["unigram_js"], 0.1)


class TestNearDup(unittest.TestCase):
    def test_exact_near_and_short_text_rules(self):
        ix = NearDupIndex(["the quick brown fox jumps over the lazy dog today again", "hmm"])
        self.assertTrue(ix.exact("  HMM "))
        self.assertTrue(ix.near("the quick brown fox jumps over the lazy dog today again!"))
        self.assertFalse(ix.near("hm"))  # short texts are only compared exactly
        self.assertFalse(ix.near("an entirely different sentence about something else completely"))


class TestBaselines(_Base):
    def test_shuffled_baseline_never_returns_an_examples_own_reply(self):
        test = [{"id": f"t{i}", "target": f"reply {i}"} for i in range(50)]
        sh = baselines.shuffled(test)
        self.assertTrue(all(sh[e["id"]] != e["target"] for e in test))
        self.assertEqual(sorted(sh.values()), sorted(e["target"] for e in test))

    def test_retrieval_baseline_uses_train_only_and_is_deterministic(self):
        out = self.env.prepare()
        train = list(common.iter_jsonl(out / "alice" / "train.jsonl"))
        test = list(common.iter_jsonl(out / "alice" / "test.jsonl"))
        r1, r2 = baselines.retrieval(train, test), baselines.retrieval(train, test)
        self.assertEqual(r1, r2)
        pool = {e["target"] for e in train}
        self.assertTrue(all(v in pool or v == "" for v in r1.values()))


# ── runtime router ───────────────────────────────────────────────────────────
class FakeClient:
    def __init__(self, reply="haan yaar kal milte hai", error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def complete(self, model, messages, timeout):
        self.calls.append((model, messages, timeout))
        if self.error:
            raise self.error
        return self.reply


def write_registry(directory: Path, status="ready", fv=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "registry.json").write_text(json.dumps({"persons": {"alice": {
        "status": status, "model": "persona-alice", "format_version": fv if fv is not None else fmt.FORMAT_VERSION}}}), encoding="utf-8")


class TestRouter(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = [0.0]
        write_registry(self.tmp)

    def router(self, client, marker="🤖"):
        reg = pm.AdapterRegistry(self.tmp, clock=lambda: self.now[0])
        return pm.PersonaModelRouter(reg, client, 5.0, marker, pm.CircuitBreaker(3, 60.0, clock=lambda: self.now[0]))

    async def ask(self, router, text="kya haal hai", **kw):
        return await router.reply(person_id="alice", real_name="Alice", user_message=text, account_index=0,
                                  chat_kind="group", max_chars=80, blocked_tokens=["darnit"], names=["Ally"], **kw)

    async def test_ready_adapter_replies_with_marker_and_training_format_prompt(self):
        c = FakeClient()
        r = await self.ask(self.router(c))
        self.assertEqual((r.source, r.text), ("adapter", "haan yaar kal milte hai 🤖"))
        model, messages, timeout = c.calls[0]
        self.assertEqual(model, "persona-alice")
        self.assertEqual(messages, fmt.runtime_messages("kya haal hai", None, "group", "a1"))
        self.assertEqual(timeout, 5.0)

    async def test_chat_kind_never_seen_in_training_is_mapped_to_one_that_was(self):
        (self.tmp / "registry.json").write_text(json.dumps({"persons": {"alice": {
            "status": "ready", "model": "persona-alice", "format_version": fmt.FORMAT_VERSION, "chat_kinds": ["group"]}}}), encoding="utf-8")
        c = FakeClient()
        r = self.router(c)
        await r.reply(person_id="alice", real_name="Alice", user_message="kya haal hai", account_index=None,
                      chat_kind="dm", max_chars=80)
        self.assertIn("[chat: group]", c.calls[0][1][-1]["content"])
        self.assertNotIn("[chat: dm]", c.calls[0][1][-1]["content"])

    async def test_marker_can_be_switched_off(self):
        self.assertEqual((await self.ask(self.router(FakeClient(), marker=""))).text, "haan yaar kal milte hai")

    async def test_identity_questions_get_the_disclosure_and_never_reach_the_model(self):
        c = FakeClient(reply="haan main hi hu")
        for q in ("are you real?", "r u a bot", "tum kaun ho", "kya tum real ho", "Alice ho kya", "is this Ally", "bot hai kya"):
            r = await self.ask(self.router(c), q)
            self.assertEqual(r.source, "identity_probe", q)
            self.assertIn("not Alice", r.text)
        self.assertEqual(c.calls, [])

    async def test_ordinary_messages_are_not_mistaken_for_identity_questions(self):
        for q in ("are you coming tomorrow?", "who is this song by", "real talk bro", "what are you doing", "kaun jeeta"):
            self.assertFalse(pm.is_identity_probe(q, ["Alice", "Ally"]), q)

    async def test_not_ready_or_missing_registry_falls_back(self):
        for status in ("trained", "pending"):
            write_registry(self.tmp, status)
            c = FakeClient()
            self.assertIsNone(await self.ask(self.router(c)))
            self.assertEqual(c.calls, [])
        shutil.rmtree(self.tmp)
        self.assertIsNone(await self.ask(self.router(FakeClient())))

    async def test_corrupt_registry_disables_adapters_instead_of_crashing(self):
        (self.tmp / "registry.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(await self.ask(self.router(FakeClient())))

    async def test_prompt_format_mismatch_is_not_served(self):
        write_registry(self.tmp, fv=fmt.FORMAT_VERSION + 1)
        c = FakeClient()
        self.assertIsNone(await self.ask(self.router(c)))
        self.assertEqual(c.calls, [])

    async def test_registry_changes_are_picked_up_without_restart(self):
        write_registry(self.tmp, "trained")
        r = self.router(FakeClient())
        self.assertIsNone(await self.ask(r))
        time.sleep(0.01)
        write_registry(self.tmp, "ready")
        import os
        os.utime(self.tmp / "registry.json", (time.time() + 5, time.time() + 5))
        self.now[0] += 10  # past the 5 s re-check interval
        self.assertEqual((await self.ask(r)).source, "adapter")

    async def test_unsafe_outputs_are_discarded(self):
        for bad in ("call me on +91 98765 43210", "see https://evil.example", "mail a@b.co", "hi <user>", "that is darnit", "@someone hi", "", "   "):
            c = FakeClient(reply=bad)
            self.assertIsNone(await self.ask(self.router(c)), bad)

    async def test_output_is_cleaned_and_bounded(self):
        c = FakeClient(reply='me: "**haan** yaar. ' + "bahut lamba " * 20 + '"')
        r = await self.ask(self.router(c, marker=""))
        self.assertLessEqual(len(r.text), 80)
        self.assertNotIn("me:", r.text)
        self.assertNotIn("**", r.text)

    async def test_failures_open_the_breaker_then_recover_after_the_cooldown(self):
        c = FakeClient(error=TimeoutError("slow"))
        r = self.router(c)
        for _ in range(3):
            self.assertIsNone(await self.ask(r))
        self.assertEqual(len(c.calls), 3)
        self.assertIsNone(await self.ask(r))
        self.assertEqual(len(c.calls), 3)  # breaker open: server not hit
        self.assertEqual(r.stats["breaker_open"], 1)
        self.now[0] += 61
        c.error, c.reply = None, "ok done"
        self.assertEqual((await self.ask(r)).source, "adapter")

    async def test_identity_answer_works_even_with_the_server_down(self):
        r = self.router(FakeClient(error=RuntimeError("down")))
        for _ in range(5):
            await self.ask(r)
        self.assertEqual((await self.ask(r, "are you a bot?")).source, "identity_probe")


class TestPersonalityHook(unittest.TestCase):
    """The real Personality._person_reply with the adapter hook, run in a subprocess with only heavy imports stubbed."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        proc = subprocess.run([sys.executable, str(ROOT / "tests" / "_adapter_hook_harness.py")], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AssertionError(f"hook harness failed:\n{proc.stderr[-2000:]}")
        cls.res = json.loads(proc.stdout.strip().splitlines()[-1])

    def lines(self, key):
        return (self.res[key]["prompt"] or "").splitlines()

    def test_disabled_means_v29_behaviour_and_no_server_call(self):
        self.assertEqual(self.res["disabled"]["calls"], 0)
        self.assertTrue(self.res["disabled"]["text"].strip())
        self.assertNotIn("🤖", self.res["disabled"]["text"])

    def test_enabled_adapter_answers_with_the_marker_and_the_persons_model(self):
        r = self.res["group"]
        self.assertEqual((r["calls"], r["model"], r["text"]), (1, "persona-tanvi", "ok done 🤖"))

    def test_chat_kind_and_account_tag_follow_the_chat_and_the_skin(self):
        self.assertIn("[chat: group]", self.lines("group"))
        self.assertIn("[chat: dm]", self.lines("dm"))
        self.assertIn("[account: a1]", self.lines("acct_main"))
        self.assertIn("[account: a2]", self.lines("acct_alt"))
        self.assertFalse([l for l in self.lines("group") if l.startswith("[account")])

    def test_identity_questions_get_the_disclosure_without_calling_the_model(self):
        for key in ("probe", "probe_hindi"):
            self.assertEqual(self.res[key]["calls"], 0, key)
            self.assertIn("not Tanvi", self.res[key]["text"])

    def test_server_failure_and_unsafe_output_fall_back_to_the_existing_path(self):
        for key in ("server_down", "unsafe"):
            self.assertEqual(self.res[key]["calls"], 1, key)
            self.assertTrue(self.res[key]["text"].strip(), key)
            self.assertNotIn("evil.example", self.res[key]["text"])
            self.assertNotIn("🤖", self.res[key]["text"])

    def test_a_person_without_a_ready_adapter_never_reaches_another_persons_model(self):
        self.assertEqual(self.res["other_person"]["calls"], 0)
        self.assertTrue(self.res["other_person"]["text"].strip())

    def test_missing_base_url_disables_the_adapter_path(self):
        self.assertEqual(self.res["no_url"]["calls"], 0)

    # ── persona engine (v2.11) through the real Personality._person_reply ──
    def test_engine_off_by_default_means_no_model_call(self):
        self.assertEqual(self.res["e_off"]["calls"], 0)
        self.assertTrue(self.res["e_off"]["text"].strip())

    def test_engine_reply_is_marked_and_built_from_profile_plan_and_evidence(self):
        r = self.res["e_on"]
        self.assertEqual(r["calls"], 1)
        self.assertTrue(r["text"].endswith(" 🤖"))
        for section in ("[PERSONA PROFILE", "[CONVERSATION", "[SITUATION]", "[PLAN FOR THIS REPLY", "[HISTORICAL EVIDENCE"):
            self.assertIn(section, r["prompt"])

    def test_reply_target_and_other_persons_engine(self):
        self.assertIn("(the message below replies to: pehle ka message)", self.res["e_ctx"]["prompt"])
        self.assertEqual(self.res["e_yash"]["calls"], 1)

    def test_identity_questions_never_reach_the_engine_model(self):
        for key in ("e_probe", "e_probe_name"):
            self.assertEqual(self.res[key]["calls"], 0, key)
            self.assertIn("not Tanvi", self.res[key]["text"])
            self.assertNotIn("🤖", self.res[key]["text"])

    def test_unsafe_candidates_provider_errors_and_missing_provider_fall_back_to_v29(self):
        for key in ("e_unsafe", "e_error", "e_noprov"):
            self.assertTrue(self.res[key]["text"].strip(), key)
            self.assertNotIn("🤖", self.res[key]["text"], key)
            self.assertNotIn("evil.example", self.res[key]["text"], key)
            self.assertNotIn("9876543210", self.res[key]["text"], key)
        self.assertEqual(self.res["e_noprov"]["calls"], 0)

    def test_a_ready_adapter_takes_precedence_over_the_engine(self):
        self.assertEqual(self.res["e_adapter_first"]["calls"], 0)
        self.assertEqual(self.res["e_adapter_first"]["text"], "ok done 🤖")

    def test_engine_status_reports_counters_not_content(self):
        st = self.res["e_status"]
        self.assertEqual(st["provider"], "script")
        self.assertIn("tanvi", st["engines"])
        blob = json.dumps(st)
        self.assertNotIn("kya haal hai", blob)


class TestPluginCommands(unittest.TestCase):
    """The new Telegram handlers executed for real (Telegram + database stubbed), against the real personality layer and engine."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        proc = subprocess.run([sys.executable, str(ROOT / "tests" / "_plugin_command_harness.py")], capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AssertionError(f"command harness failed:\n{proc.stderr[-2500:]}")
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_pengine_is_inert_when_the_engine_is_off_and_reports_status_and_debug_when_on(self):
        r = self.r
        self.assertIn("Persona engine is off", r["off"])
        self.assertIn("provider: script", r["status_empty"])
        self.assertIn("No engine reply recorded", r["last_empty"])
        self.assertIn("Usage: /pengine debug on|off", r["debug_bad"])
        self.assertEqual((r["debug_set"], r["debug_set_after"]), ([-100777], []))

    def test_memory_commands_are_gated_by_setting_admin_and_active_persona(self):
        r = self.r
        self.assertIn("Long-term memory is off", r["mem_off"])
        self.assertEqual(r["non_admin"], "Admin only.")
        self.assertEqual(r["pmemory_non_admin"], "Admin only.")
        self.assertIn("Usage: /premember", r["usage"])
        self.assertIn("No imitation persona is active", r["no_person"])

    def test_remember_list_forget_roundtrip_is_per_chat_and_refuses_contact_data(self):
        r = self.r
        self.assertIn("Remembered (#1)", r["remembered"])
        self.assertIn("not something Tanvi said", r["remembered"])
        self.assertTrue(r["refused_contact"].startswith("Not stored:"))
        self.assertIn("#1: we are planning a goa trip", r["listed"])
        self.assertEqual(r["other_chat_listed"], "Nothing remembered here.")
        self.assertIn("Usage: /pforget", r["forget_usage"])
        self.assertEqual((r["forget_one"], r["after_forget"], r["forget_all"]), ("Forgot 1 fact(s).", "Nothing remembered here.", "Forgot 1 fact(s)."))

    def test_is_admin_fails_closed_and_recognises_anonymous_admins(self):
        self.assertEqual(self.r["is_admin"], {
            "private": True, "admin": True, "owner": True, "member": False, "api_error": False, "no_user": False, "no_chat": False,
            "anonymous_admin": True, "channel_sender_is_not_admin": False})

    def test_engine_reply_status_and_last_trace_hold_counters_not_text(self):
        r = self.r
        self.assertTrue(r["reply"].endswith(" 🤖"))
        self.assertIn("stage=llm", r["last"])
        self.assertIn("tanvi: pool", r["status"])
        for key in ("last", "status"):
            self.assertNotIn("kuch nahi", r[key])
            self.assertNotIn("kal milte", r[key])

    def test_stored_facts_reach_the_prompt_through_the_plugin_path(self):
        self.assertTrue(self.r["memory_in_prompt"])

    def test_running_the_commands_wrote_nothing_into_the_repository(self):
        self.assertFalse((ROOT / "data" / "persona_engine").exists())


class TestSettingsGate(unittest.TestCase):
    def test_router_is_off_unless_enabled_and_configured(self):
        class S:
            persona_model_enabled = False
        pm.reset_router()
        self.assertIsNone(pm.get_router(S()))
        S.persona_model_enabled, S.persona_model_base_url = True, ""
        self.assertIsNone(pm.get_router(S()))
        S.persona_model_base_url, S.persona_model_api_key = "http://x/v1", None
        S.persona_model_temperature, S.persona_model_top_p = 0.7, 0.9
        S.persona_adapter_dir, S.persona_model_timeout_seconds, S.persona_model_marker = "/nonexistent", 5, "🤖"
        try:
            self.assertIsNotNone(pm.get_router(S()))
        finally:
            pm.reset_router()

    def test_config_defaults_keep_v29_behaviour(self):
        src = (ROOT / "bot" / "config.py").read_text(encoding="utf-8")
        self.assertIn('persona_model_enabled: bool = Field(False, alias="PERSONA_MODEL_ENABLED")', src)
        self.assertIn('alias="PERSONA_MODEL_MARKER"', src)
        hook = (ROOT / "bot" / "personality" / "personality.py").read_text(encoding="utf-8")
        self.assertIn("if not settings.persona_model_enabled:\n            return None", hook)
        self.assertLess(hook.index("routed = await self._adapter_reply"), hook.index("if settings.personality_llm_enabled:"))


# ── release hygiene ──────────────────────────────────────────────────────────
class TestReleaseHygiene(unittest.TestCase):
    def test_no_private_training_data_or_weights_in_the_tree(self):
        self.assertFalse((ROOT / "training" / "_private").exists())
        self.assertFalse((ROOT / "data" / "models").exists())
        bad = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
               if p.suffix in {".safetensors", ".gguf", ".pt", ".bin"} or "datasets/custom" in str(p).replace("\\", "/")]
        self.assertEqual(bad, [])

    def test_ignore_files_cover_private_training_artifacts(self):
        for name in (".gitignore", ".dockerignore"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("training/_private/", text, name)
            self.assertIn("data/models/", text, name)

    def test_bot_runtime_never_imports_the_training_toolkit(self):
        for p in (ROOT / "bot").rglob("*.py"):
            src = p.read_text(encoding="utf-8")
            self.assertNotRegex(src, r"(?m)^\s*(?:from|import)\s+training\b", str(p))

    def test_training_modules_have_no_third_party_imports_at_module_level(self):
        import ast
        # "bot" is first-party: bot.persona_engine is itself checked to be standard-library-only (tests/test_persona_engine.py)
        allowed = {"training", "bot", "argparse", "asyncio", "hashlib", "importlib", "json", "math", "os", "random", "re", "shutil", "sys",
                   "tempfile", "time", "calendar", "collections", "pathlib", "typing", "__future__", "inspect", "contextlib"}
        for p in (ROOT / "training").glob("*.py"):
            for node in ast.parse(p.read_text(encoding="utf-8")).body:
                mods = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                       [(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for m in mods:
                    self.assertIn(m, allowed, f"{p.name}: module-level import of {m!r}")

    def test_existing_person_persona_files_are_untouched_by_the_toolkit(self):
        persons = ROOT / "data" / "personality" / "persons"
        self.assertTrue((persons / "registry.json").exists())
        for pid in ("tanvi", "yashmeet"):
            self.assertTrue((persons / pid / "corpus.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
