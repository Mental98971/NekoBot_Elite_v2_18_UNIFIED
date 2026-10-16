#!/usr/bin/env python3
"""Predictions on a person's held-out test split from the base model alone or base + adapter (GPU).

    # the untouched base model (baseline "original model")
    python -m training.generate --data training/_private/data/tanvi --base-model <id> --out preds/base.jsonl
    # the fine-tuned adapter
    python -m training.generate --data training/_private/data/tanvi --adapter data/models/persona_adapters/tanvi/current --out preds/adapter.jsonl

Leakage guard: an adapter may only be evaluated on a dataset whose dataset_sha256 equals the one it was
trained on.  Re-running prepare_sft can move examples between splits; without this check a re-prepared
test split could contain examples the adapter was trained on.

Prediction lines are {"id", "pred"}.  Sampling is seeded per example so a run is repeatable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import common  # noqa: E402
from training.train_lora import PreflightError, render_prompt, validate_adapter_dir  # noqa: E402


def check_adapter_matches_dataset(adapter_dir: Path, data_dir: Path) -> Dict[str, Any]:
    meta = validate_adapter_dir(adapter_dir)
    manifest = common.verify_prepared_dir(data_dir, ("train", "test"))
    if meta["person"] != manifest["person"]:
        raise common.TrainingDataError(f"adapter is for '{meta['person']}' but the data is '{manifest['person']}'")
    if meta["dataset_sha256"] != manifest["dataset_sha256"]:
        raise common.TrainingDataError(
            "adapter was trained on a different dataset build than this test split "
            f"({meta['dataset_sha256'][:12]} vs {manifest['dataset_sha256'][:12]}): evaluating would risk train/test leakage")
    if meta["format_version"] != common.fmt_module().FORMAT_VERSION:
        raise common.TrainingDataError("adapter prompt-format version differs from the code")
    return meta


def seed_for(seed: int, example_id: str) -> int:
    return int(hashlib.sha256(f"{seed}|gen|{example_id}".encode()).hexdigest()[:8], 16)


def clean_generation(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    for stop in ("\nme:", "\n### "):
        if stop in t:
            t = t.split(stop, 1)[0].strip()
    return t[:max_chars].strip()


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--adapter", help="adapter directory (omit to evaluate the base model alone)")
    ap.add_argument("--base-model", help="required without --adapter")
    ap.add_argument("--split", choices=("test", "val"), default="test")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-new-tokens", type=int, default=96)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top-p", type=float, default=0.9)
    ap.add_argument("--greedy", action="store_true")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        data = Path(args.data)
        base = args.base_model
        if args.adapter:
            meta = check_adapter_matches_dataset(Path(args.adapter), data)
            base = meta["base_model"]
        elif not base:
            raise PreflightError("--base-model is required without --adapter")
        else:
            common.verify_prepared_dir(data, ("train", args.split))
        rows = list(common.iter_jsonl(data / f"{args.split}.jsonl"))
        if args.limit:
            rows = rows[: args.limit]
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise PreflightError(f"missing package ({exc.name}); pip install -r training/requirements-train.txt") from exc
        if not torch.cuda.is_available():
            raise PreflightError("no CUDA GPU detected; generation needs one")
        fmt = common.fmt_module()
        tok = AutoTokenizer.from_pretrained(args.adapter or base, use_fast=True)
        tok.padding_side = "left"
        if tok.pad_token_id is None:
            tok.pad_token = tok.eos_token
        model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16, device_map={"": 0})
        if args.adapter:
            from peft import PeftModel
            model = PeftModel.from_pretrained(model, args.adapter)
        model.eval()
        preds: Dict[str, str] = {}
        cap = int(json.loads((data / "manifest.json").read_text(encoding="utf-8"))["config"]["target_max_chars"])
        for i in range(0, len(rows), args.batch):
            batch = rows[i:i + args.batch]
            prompts = [render_prompt(tok, fmt.example_messages(ex)) for ex in batch]
            enc = tok(prompts, return_tensors="pt", padding=True, add_special_tokens=False).to(model.device)
            outs: List[str] = []
            for k, ex in enumerate(batch):  # per-example seeds keep sampling repeatable regardless of batching
                torch.manual_seed(seed_for(args.seed, ex["id"]))
                kw: Dict[str, Any] = dict(max_new_tokens=args.max_new_tokens, pad_token_id=tok.pad_token_id)
                if args.greedy:
                    kw["do_sample"] = False
                else:
                    kw.update(do_sample=True, temperature=args.temperature, top_p=args.top_p)
                with torch.no_grad():
                    g = model.generate(input_ids=enc["input_ids"][k:k + 1], attention_mask=enc["attention_mask"][k:k + 1], **kw)
                outs.append(tok.decode(g[0, enc["input_ids"].shape[1]:], skip_special_tokens=True))
            for ex, o in zip(batch, outs):
                preds[ex["id"]] = clean_generation(o, cap)
        common.write_jsonl(Path(args.out), ({"id": k, "pred": v} for k, v in sorted(preds.items())))
        print(f"wrote {len(preds)} predictions to {args.out}")
    except (common.TrainingDataError, PreflightError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
