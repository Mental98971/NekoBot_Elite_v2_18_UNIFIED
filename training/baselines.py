#!/usr/bin/env python3
"""Offline reference systems for the held-out split (no model, no GPU, no network).

  retrieval  re-implementation of the v2.9 strategy: replay one of the person's own short replies to a
             message of the same class.  Built from the TRAIN split only, so it is a fair comparison
             (the shipped corpus contains the test messages and would trivially replay them).
  modal      the single most frequent train reply for the class (degenerate floor).
  shuffled   real replies from the test split assigned to the wrong examples: perfect style
             statistics, zero context use.  It shows what a context-blind but stylistically perfect
             system scores, so distribution metrics should be read against it.

    python -m training.baselines --data training/_private/data/tanvi
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import common  # noqa: E402

MAX_REPLAY_WORDS = 8


def context_class(ex: Dict[str, Any]) -> str:
    tm = common.text_module()
    probe = (ex.get("reply_to") or [None, ""])[1] or (ex["ctx"][-1][1] if ex.get("ctx") else "")
    return tm.classify_context(probe) if probe else "empty"


def _pool(train: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    pool: Dict[str, List[str]] = defaultdict(list)
    for ex in train:
        if ex["tk"] == "text" and len(ex["target"].split()) <= MAX_REPLAY_WORDS and "\n" not in ex["target"]:
            pool[context_class(ex)].append(ex["target"])
    return pool


def _rng(seed: int, ex_id: str) -> random.Random:
    return random.Random(int(hashlib.sha256(f"{seed}|{ex_id}".encode()).hexdigest()[:12], 16))


def retrieval(train: List[Dict[str, Any]], test: List[Dict[str, Any]], seed: int = 11) -> Dict[str, str]:
    pool = _pool(train)
    everything = [t for v in pool.values() for t in v]
    out = {}
    for ex in test:
        cands = pool.get(context_class(ex)) or everything
        out[ex["id"]] = _rng(seed, ex["id"]).choice(cands) if cands else ""
    return out


def modal(train: List[Dict[str, Any]], test: List[Dict[str, Any]]) -> Dict[str, str]:
    pool = _pool(train)
    best = {k: Counter(v).most_common(1)[0][0] for k, v in pool.items() if v}
    fallback = Counter(t for v in pool.values() for t in v).most_common(1)[0][0] if pool else ""
    return {ex["id"]: best.get(context_class(ex), fallback) for ex in test}


def shuffled(test: List[Dict[str, Any]], seed: int = 13) -> Dict[str, str]:
    refs = [ex["target"] for ex in test]
    rng = random.Random(seed)
    perm = list(range(len(refs)))
    for _ in range(5):  # derange: no example may keep its own reply
        rng.shuffle(perm)
        if all(i != j for i, j in enumerate(perm)):
            break
    else:
        perm = [(i + 1) % len(refs) for i in range(len(refs))]
    return {ex["id"]: refs[perm[i]] for i, ex in enumerate(test)}


def write_preds(path: Path, preds: Dict[str, str]) -> None:
    common.write_jsonl(path, ({"id": k, "pred": v} for k, v in sorted(preds.items())))


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="prepared per-person directory (train.jsonl / test.jsonl)")
    ap.add_argument("--out", help="prediction directory (default: <data>/preds)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    d = Path(args.data)
    common.verify_prepared_dir(d, ("train", "test"))
    train, test = list(common.iter_jsonl(d / "train.jsonl")), list(common.iter_jsonl(d / "test.jsonl"))
    out = Path(args.out) if args.out else d / "preds"
    write_preds(out / "retrieval.jsonl", retrieval(train, test))
    write_preds(out / "modal.jsonl", modal(train, test))
    write_preds(out / "shuffled.jsonl", shuffled(test))
    print(f"wrote retrieval / modal / shuffled predictions for {len(test)} test examples to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
