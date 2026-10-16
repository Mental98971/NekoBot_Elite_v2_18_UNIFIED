#!/usr/bin/env python3
"""Build data/personality/persons/<id>/behavior_profile.json from each person's shipped corpus.

    python scripts/build_behavior_profiles.py [--person tanvi] [--perms 300]

Deterministic (fixed seed), standard library only.  The profile holds aggregate statistics only: counts,
distributions, a short list of unusual words / recurring expressions (specificity-tested, name- and
abusive-token-filtered).  It records the sha256 of the corpus it was built from; the bot rebuilds in memory if they differ.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.persona_engine.profile import BehaviorProfile  # noqa: E402
from bot.persona_engine.runtime import PROFILE_FILE, account_tags, sha256_file  # noqa: E402
from bot.persona_engine.store import from_corpus  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person")
    ap.add_argument("--perms", type=int, default=300)
    ap.add_argument("--latency-from", help="prepared-data directory (training/_private/data): add reply-timing quantiles (aggregate numbers only)")
    ap.add_argument("--registry", default=str(ROOT / "data" / "personality" / "persons" / "registry.json"))
    args = ap.parse_args()
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    blocked = reg.get("replay_filter", {}).get("abusive_tokens", [])
    for pid, p in reg["persons"].items():
        if args.person and pid != args.person:
            continue
        d = ROOT / "data" / "personality" / "persons" / pid
        corpus = d / "corpus.jsonl"
        if not corpus.exists():
            print(f"{pid}: no corpus, skipped")
            continue

        class _P:  # just the account list
            accounts = [type("A", (), {"id": a["id"]}) for a in p["accounts"]]
        examples = from_corpus(corpus, pid, account_tags(_P), blocked)
        prof = BehaviorProfile.build(examples, person=pid, blocked=blocked, background=[e.trigger for e in examples if e.trigger],
                                     n_perm=args.perms, source={"corpus_sha256": sha256_file(corpus), "corpus_rows": len(examples)})
        if args.latency_from:
            pdir = Path(args.latency_from) / pid
            manifest = json.loads((pdir / "manifest.json").read_text(encoding="utf-8"))
            vals = []
            for split in ("train", "val", "test"):
                with (pdir / f"{split}.jsonl").open(encoding="utf-8") as fh:
                    vals += [json.loads(line).get("lat") for line in fh if line.strip()]
            prof = prof.with_latency(vals, {"dataset_sha256": manifest["dataset_sha256"], "meaning": "seconds since the previous message in the same conversation"})
        out = d / PROFILE_FILE
        out.write_text(prof.to_json() + "\n", encoding="utf-8")
        print(f"{pid}: {prof.n_messages} messages -> {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
