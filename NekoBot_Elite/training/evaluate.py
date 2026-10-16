#!/usr/bin/env python3
"""Score one or more prediction files against a person's held-out test split.

    python -m training.evaluate --data training/_private/data/tanvi \\
        --pred retrieval=.../preds/retrieval.jsonl --pred adapter=.../preds/adapter.jsonl \\
        --separation-from training/_private/data/yashmeet \\
        --out training/_private/data/tanvi/eval.json [--candidate adapter --gates training/configs/gates.json]

Prediction files are JSONL lines {"id": <example id>, "pred": <reply text>}.  The report holds
aggregate numbers only (no message text), so it is safe to keep alongside the adapter and to share.
With --candidate/--gates the exit status is 0 only if the candidate passes every gate.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import common, evalkit  # noqa: E402


def load_preds(path: Path) -> Dict[str, str]:
    return {str(r["id"]): str(r.get("pred") or "") for r in common.iter_jsonl(path)}


def evaluate_dir(data: Path, preds: Dict[str, Dict[str, str]], separation_from: List[Path], registry_path: Optional[Path] = None) -> Dict[str, Any]:
    manifest = common.verify_prepared_dir(data, ("train", "test"))
    person = manifest["person"]
    train = list(common.iter_jsonl(data / "train.jsonl"))
    test = list(common.iter_jsonl(data / "test.jsonl"))
    train_texts = [e["target"] for e in train]
    by_person = {person: train_texts}
    for other in separation_from:
        m = common.verify_prepared_dir(other, ("train",))
        if m["person"] == person:
            raise common.TrainingDataError(f"{other} holds the same person as {data}")
        by_person[m["person"]] = [e["target"] for e in common.iter_jsonl(other / "train.jsonl")]
    nb = evalkit.fit_attribution(by_person)
    abusive = common.load_registry(registry_path).get("replay_filter", {}).get("abusive_tokens", [])
    return {
        "schema": 1, "person": person, "dataset_sha256": manifest["dataset_sha256"],
        "test_sha256": manifest["splits"]["test"]["sha256"], "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attribution_labels": sorted(by_person),
        "systems": {name: evalkit.evaluate_system(p, test, train_texts, nb, person, abusive) for name, p in preds.items()},
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--pred", action="append", required=True, metavar="NAME=FILE")
    ap.add_argument("--separation-from", action="append", default=[], help="another person's prepared dir (persona-contamination check)")
    ap.add_argument("--out")
    ap.add_argument("--candidate", help="system name to gate")
    ap.add_argument("--gates", default=str(common.ROOT / "training" / "configs" / "gates.json"))
    ap.add_argument("--registry", default=str(common.DEFAULT_REGISTRY))
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        preds = {}
        for spec in args.pred:
            name, _, path = spec.partition("=")
            if not name or not path:
                raise common.TrainingDataError(f"--pred expects NAME=FILE, got '{spec}'")
            preds[name] = load_preds(Path(path))
        report = evaluate_dir(Path(args.data), preds, [Path(p) for p in args.separation_from], Path(args.registry))
        if args.candidate:
            if args.candidate not in report["systems"]:
                raise common.TrainingDataError(f"--candidate '{args.candidate}' is not among the --pred names")
            gates = json.loads(Path(args.gates).read_text(encoding="utf-8"))
            others = {k: v for k, v in report["systems"].items() if k != args.candidate}
            report["gate"] = evalkit.check_gates(report["systems"][args.candidate], others, gates)
            report["gate"]["candidate"] = args.candidate
    except (common.TrainingDataError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        common.atomic_write_json(Path(args.out), report)
    for name, s in report["systems"].items():
        sub = s["subsets"].get("novel") or {}
        print(f"{name:10s} answered={s['answered']}/{s['test_examples']} novel: length_js={sub.get('length_js', float('nan')):.3f} "
              f"unigram_js={sub.get('unigram_js', float('nan')):.3f} char3_js={sub.get('char3_js', float('nan')):.3f} "
              f"feature_gap={sub.get('feature_gap', float('nan')):.3f} f1={sub.get('token_f1_mean', float('nan')):.3f} "
              f"copy_long={s['memorization']['exact_copy_rate_long']:.2f} own_share={s['attribution'].get('own_share', float('nan')):.2f}")
    if "gate" in report:
        for c in report["gate"]["checks"]:
            print(("PASS " if c["passed"] else "FAIL ") + c["name"] + ": " + c["detail"])
        return 0 if report["gate"]["passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
