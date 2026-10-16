#!/usr/bin/env python3
"""Prepare per-person training data (train / val / test) for the LoRA pipeline.

    python scripts/persona_ft/01_preprocess.py                      # from the shipped corpora of every registered person
    python scripts/persona_ft/01_preprocess.py --persons tanvi      # one person
    python scripts/persona_ft/01_preprocess.py --source exports/*.jsonl   # from raw chat exports (richer context; preferred)
    python scripts/persona_ft/01_preprocess.py --dry-run            # counts and warnings only

Delegates to ``training.prepare_sft``: conversation-level splits (no message of a test conversation is ever trained on),
near-duplicate and abusive-text filtering, hash-verified manifests.  Output goes to ``training/_private/data/<person>/``
(git-ignored, never shipped).  Preprocessing prepares data only; *training* additionally needs the person's authorization
record (see training/authorizations.json) - checked by 02_train_lora.py.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import json
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bridge  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persons", nargs="+", help="person ids (default: every person with a shipped corpus)")
    ap.add_argument("--source", action="append", default=[], help="raw chat export(s); when given, the shipped corpora are not used")
    ap.add_argument("--out", default=str(_bridge.PRIVATE_DATA))
    ap.add_argument("--seed", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from training import prepare_sft

    passthrough = ["--out", args.out] + (["--seed", str(args.seed)] if args.seed is not None else []) + (["--dry-run"] if args.dry_run else [])
    if args.source:
        return prepare_sft.main([x for s in args.source for x in ("--source", s)] + passthrough)

    persons = args.persons or _bridge.shipped_persons()
    unknown = [p for p in persons if p not in _bridge.shipped_persons()]
    if unknown or not persons:
        print(f"error: no shipped corpus for {unknown or 'any person'}", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="persona_ft_"))
    try:
        sources: List[str] = []
        for k, pid in enumerate(persons):
            path = tmp / f"{pid}.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for row in _bridge.export_rows(pid, id_base=(k + 1) * _bridge.ID_BLOCK):
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            sources += ["--source", str(path)]
        return prepare_sft.main(sources + passthrough)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # the export copy is temporary


if __name__ == "__main__":
    raise SystemExit(main())
