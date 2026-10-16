"""scripts/persona_ft/* wrappers and bot/personality/adapter_loader.py (standard library only; offline)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts" / "persona_ft"))
        cls.bridge = _load("scripts/persona_ft/_bridge.py", "_bridge_under_test")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        d = self.tmp / "ann"
        d.mkdir()
        rows = [
            {"acct": "42", "chat": "c1", "id": "a", "kind": "text", "text": "hi there", "ts": "2024-01-01T10:00:00Z"},
            {"acct": "42", "chat": "c1", "id": "b", "kind": "text", "text": "fine thanks", "ts": "2024-01-01T10:01:00Z", "ctx": "how are you?", "ctx_kind": "question", "reply": True},
            {"acct": "42", "chat": "c1", "id": "c", "kind": "text", "text": "ok", "ts": "2024-01-01T10:02:00Z", "ctx": "ready?", "ctx_kind": "question"},
            {"acct": "42", "chat": "c1", "id": "d", "kind": "text", "text": "x", "ts": "2024-01-01T10:03:00Z", "ctx": "{json}", "ctx_kind": "structured"},
            {"acct": "42", "chat": "c2", "id": "e", "kind": "text", "text": "other chat", "ts": "2024-01-01T09:00:00Z"},
        ]
        (d / "corpus.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        self.rows = list(self.bridge.export_rows("ann", self.tmp))

    def test_context_becomes_a_message_from_a_placeholder_sender(self):
        c1 = [r for r in self.rows if r["chat_id"] == "c1"]
        texts = [(r["sender_id"], r["message_text"]) for r in c1]
        self.assertEqual(texts, [("42", "hi there"), ("1", "how are you?"), ("42", "fine thanks"), ("1", "ready?"), ("42", "ok"), ("42", "x")])

    def test_explicit_replies_point_at_their_context_message(self):
        c1 = [r for r in self.rows if r["chat_id"] == "c1"]
        fine = next(r for r in c1 if r["message_text"] == "fine thanks")
        ctx = next(r for r in c1 if r["message_text"] == "how are you?")
        self.assertEqual(fine["reply_to_msg_id"], ctx["msg_id"])
        self.assertIsNone(next(r for r in c1 if r["message_text"] == "ok")["reply_to_msg_id"])  # flow context, not an explicit reply

    def test_message_ids_are_unique_per_chat_and_ordered(self):
        for chat in ("c1", "c2"):
            ids = [int(r["msg_id"]) for r in self.rows if r["chat_id"] == chat]
            self.assertEqual(ids, sorted(set(ids)))


class TestIdsAreUniqueAcrossPeople(unittest.TestCase):
    """People share chats (same chat hash) and prepare_sft keys messages by (chat, id): ids must not collide across people."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts" / "persona_ft"))
        cls.bridge = _load("scripts/persona_ft/_bridge.py", "_bridge_ids_under_test")

    def test_blocks_keep_two_people_in_one_chat_apart(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for k, pid in enumerate(("ann", "bob")):
            (tmp / pid).mkdir()
            rows = [{"acct": str(10 + k), "chat": "shared", "id": f"{pid}{i}", "kind": "text", "text": f"m{i}", "ts": f"2024-01-01T10:0{i}:00Z",
                     "ctx": "q", "ctx_kind": "question"} for i in range(5)]
            (tmp / pid / "corpus.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        keys = []
        for k, pid in enumerate(("ann", "bob")):
            keys.append({(r["chat_id"], r["msg_id"]) for r in self.bridge.export_rows(pid, tmp, id_base=(k + 1) * self.bridge.ID_BLOCK)})
        self.assertEqual(len(keys[0]), 10)
        self.assertFalse(keys[0] & keys[1])

    def test_every_shipped_message_survives_a_multi_person_run(self):
        import contextlib
        import io
        pre = _load("scripts/persona_ft/01_preprocess.py", "_pft01_ids")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pre.main(["--out", str(tmp)]), 0)
        report = json.loads((tmp / "prepare_report.json").read_text(encoding="utf-8"))
        for pid in self.bridge.shipped_persons():
            rows = sum(1 for line in (ROOT / "data/personality/persons" / pid / "corpus.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
            self.assertEqual(report["persons"][pid]["messages_from_registered_accounts"], rows, pid)


class TestShippedCorpusPipeline(unittest.TestCase):
    """The real chain on the shipped corpora: preprocess -> baselines -> evaluate (all offline, a few seconds)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts" / "persona_ft"))
        cls.pre = _load("scripts/persona_ft/01_preprocess.py", "_pft01")
        cls.ev = _load("scripts/persona_ft/03_evaluate.py", "_pft03")
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_preprocess_writes_leak_free_verified_splits(self):
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.pre.main(["--persons", "yashmeet", "--out", str(self.tmp)])
        self.assertEqual(rc, 0)
        d = self.tmp / "yashmeet"
        from training import common
        common.verify_prepared_dir(d, ("train", "val", "test"))                      # manifest hashes match the files
        convs = {s: {json.loads(l)["conv"] for l in (d / f"{s}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()} for s in ("train", "val", "test")}
        self.assertTrue(convs["train"] and convs["test"])
        self.assertFalse(convs["train"] & convs["test"] or convs["train"] & convs["val"] or convs["val"] & convs["test"])  # no conversation in two splits

    def test_02_evaluate_runs_offline_and_writes_eval_json(self):
        import io, contextlib
        d = self.tmp / "yashmeet"
        if not (d / "test.jsonl").exists():
            self.skipTest("preprocess did not run")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.ev.main(["--person", "yashmeet", "--data", str(d), "--separation_from"])
        self.assertEqual(rc, 0)
        self.assertTrue((d / "eval.json").exists())
        self.assertTrue((d / "preds" / "retrieval.jsonl").exists())

    def test_03_unknown_person_is_refused(self):
        import io, contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.pre.main(["--persons", "nobody", "--out", str(self.tmp / "x")]), 2)

    def test_04_training_wrapper_cannot_bypass_the_authorization_gate(self):
        src = (ROOT / "scripts/persona_ft/02_train_lora.py").read_text(encoding="utf-8")
        self.assertIn("train_lora.main(", src)           # the gated entry point is the only way in
        for forbidden in ("PeftModel", "Trainer(", "get_peft_model", "--authorization", "AUTHORIZATION"):
            self.assertNotIn(forbidden, src)                 # no training code of its own, no way to point the gate elsewhere


class TestAdapterLoader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("bot/personality/adapter_loader.py", "_adapter_loader_under_test")
        cls.pm = cls.mod._persona_model()

    def setUp(self):
        self.store = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.store, True)

    def _registry(self, **persons):
        (self.store / "registry.json").write_text(json.dumps({"persons": persons}), encoding="utf-8")

    def _adapter(self, pid="ann"):
        d = self.store / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "adapter_model.safetensors").write_bytes(b"x")
        return d

    def test_requires_a_ready_registry_entry(self):
        self._adapter()
        self.assertIsNone(self.mod.adapter_path("ann", self.store))                    # files without a registry entry: refused
        self._registry(ann={"status": "candidate", "model": "m", "format_version": self.pm.fmt.FORMAT_VERSION})
        self.assertFalse(self.mod.is_available("ann", self.store))                     # not promoted: refused
        self._registry(ann={"status": "ready", "model": "m", "format_version": self.pm.fmt.FORMAT_VERSION})
        self.assertEqual(self.mod.adapter_path("ann", self.store), self.store / "ann")
        self.assertTrue(self.mod.is_available("ann", self.store))

    def test_stale_prompt_format_is_refused(self):
        self._adapter()
        self._registry(ann={"status": "ready", "model": "m", "format_version": self.pm.fmt.FORMAT_VERSION + 99})
        self.assertFalse(self.mod.is_available("ann", self.store))

    def test_ready_entry_without_weights_is_not_available(self):
        self._registry(ann={"status": "ready", "model": "m", "format_version": self.pm.fmt.FORMAT_VERSION})
        self.assertFalse(self.mod.is_available("ann", self.store))

    def test_load_without_a_ready_adapter_raises_cleanly(self):
        with self.assertRaises(self.mod.AdapterUnavailable):
            self.mod.load("ann", store=self.store)


if __name__ == "__main__":
    unittest.main()
