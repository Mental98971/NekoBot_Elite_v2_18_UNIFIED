#!/usr/bin/env python3
"""Held-out evaluation of a person's persona systems.

    python scripts/persona_ft/03_evaluate.py --person tanvi                          # offline: retrieval / modal / shuffled baselines
    python scripts/persona_ft/03_evaluate.py --person tanvi --adapter_dir <adapter>  # + the adapter (GPU) and the gates

Runs ``training.baselines`` -> (``training.generate`` for an adapter) -> ``training.evaluate`` on the person's *test* split:
length and style statistics, n-gram overlap, emoji use, exact-copy and contact-data checks, persona separation from the other
people, and the configured acceptance gates.  Writes ``eval.json`` next to the prepared data.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bridge  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", required=True)
    ap.add_argument("--data", default=None, help="prepared directory (default: training/_private/data/<person>)")
    ap.add_argument("--adapter_dir", default=None, help="also score this adapter (needs a GPU and the base model)")
    ap.add_argument("--base_model", default=None)
    ap.add_argument("--separation_from", nargs="*", default=None, help="other prepared directories (default: the other shipped persons)")
    args = ap.parse_args(argv)

    from training import baselines, evaluate, generate

    data = Path(args.data) if args.data else _bridge.PRIVATE_DATA / args.person
    if not (data / "test.jsonl").exists():
        print(f"error: {data}/test.jsonl not found - run scripts/persona_ft/01_preprocess.py first", file=sys.stderr)
        return 2
    rc = baselines.main(["--data", str(data)])
    if rc:
        return rc
    preds = data / "preds"
    cmd = ["--data", str(data), "--pred", f"retrieval={preds / 'retrieval.jsonl'}", "--pred", f"modal={preds / 'modal.jsonl'}",
           "--pred", f"shuffled={preds / 'shuffled.jsonl'}", "--out", str(data / "eval.json")]
    if args.adapter_dir:
        gen = ["--data", str(data), "--adapter", args.adapter_dir, "--out", str(preds / "adapter.jsonl")]
        if args.base_model:
            gen += ["--base-model", args.base_model]
        rc = generate.main(gen)
        if rc:
            return rc
        cmd += ["--pred", f"adapter={preds / 'adapter.jsonl'}", "--candidate", "adapter"]
    others = args.separation_from if args.separation_from is not None else [
        str(_bridge.PRIVATE_DATA / p) for p in _bridge.shipped_persons() if p != args.person and (_bridge.PRIVATE_DATA / p / "test.jsonl").exists()]
    for o in others:
        cmd += ["--separation-from", o]
    return evaluate.main(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
