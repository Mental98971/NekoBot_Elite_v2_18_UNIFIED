#!/usr/bin/env python3
"""LoRA / QLoRA fine-tuning of one person on their own data (needs a CUDA GPU and a base model).

    python scripts/persona_ft/02_train_lora.py --person tanvi --base_model <hub id or local path> \\
        --output_dir data/models/persona_adapters --epochs 2 --lr 2e-4 --batch_size 4 --grad_accum 4 [--qlora]
    python scripts/persona_ft/02_train_lora.py --person tanvi --base_model <id> --check    # preflight only, trains nothing

Delegates to ``training.train_lora``, which refuses to run unless training/authorizations.json holds a granted
record for the person that covers weight training (and verifies data hashes, GPU memory and the environment first).
``--output_dir`` is the adapter store root; the adapter is written below it.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bridge  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", required=True)
    ap.add_argument("--base_model", default=None, help="hub id or local path of an instruct/chat model")
    ap.add_argument("--output_dir", default=None, help="adapter store root (default: the toolkit's data/models/persona_adapters)")
    ap.add_argument("--data", default=None, help="prepared directory (default: training/_private/data/<person>)")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch_size", type=int, default=4)
    ap.add_argument("--grad_accum", type=int, default=4)
    ap.add_argument("--max_seq_len", type=int, default=512)
    ap.add_argument("--lora_r", type=int, default=16)
    ap.add_argument("--lora_alpha", type=int, default=32)
    ap.add_argument("--qlora", action="store_true", help="4-bit QLoRA")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--check", action="store_true", help="verify authorization, data hashes and environment; train nothing")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--allow_cpu", action="store_true")
    args = ap.parse_args(argv)

    from training import train_lora

    cfg = {"method": "qlora" if args.qlora else "lora", "max_seq_len": args.max_seq_len, "seed": args.seed,
           "lora": {"r": args.lora_r, "alpha": args.lora_alpha},
           "optim": {"learning_rate": args.lr, "epochs": args.epochs, "per_device_batch": args.batch_size, "grad_accum": args.grad_accum}}
    with tempfile.TemporaryDirectory(prefix="persona_ft_cfg_") as tmp:
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        cmd = ["--person", args.person, "--data", args.data or str(_bridge.PRIVATE_DATA / args.person), "--config", str(cfg_path)]
        if args.base_model:
            cmd += ["--base-model", args.base_model]
        if args.output_dir:
            cmd += ["--out", args.output_dir]
        cmd += [f for f, on in (("--check", args.check), ("--resume", args.resume), ("--allow-cpu", args.allow_cpu)) if on]
        return train_lora.main(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
