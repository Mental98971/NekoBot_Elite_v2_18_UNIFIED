#!/usr/bin/env python3
"""Weight-level persona adaptation: LoRA / QLoRA supervised fine-tuning of one person on their own data.

    python -m training.train_lora --person tanvi --data training/_private/data/tanvi \\
        --base-model <local path or hub id of an instruct/chat model> --out data/models/persona_adapters
    python -m training.train_lora ... --check      # verify authorization, data hashes, environment; train nothing

Design
  * One adapter per person on a shared base model (no cross-person mixing); the account tag in the
    prompt (a1, a2 ...) conditions on which account's habits to follow and is randomly omitted
    ``account_tag_dropout`` of the time so the adapter also works when the account is unknown.
  * Loss is computed on the person's reply only (prompt tokens are masked with -100).
  * Only train.jsonl and val.jsonl are ever opened.  The held-out test split stays untouched until
    generate.py / evaluate.py run, and the manifest hashes are re-verified before training starts.
  * Training requires a recorded authorization for the person (training/authorizations.json): a model
    cannot forget one person's messages later, so "no opt-out yet" is not enough.
  * The adapter store is crash-safe: a run writes to a staging directory, validates it, and only then
    swaps it in as <person>/current, keeping the previous adapter as <person>/previous.  An interrupted
    run never damages the last known-good adapter.
  * The trained adapter is NOT served until training/promote.py has checked its evaluation against the gates.

Needs a GPU box with training/requirements-train.txt installed.  Nothing here runs in the bot process.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training import common  # noqa: E402

DEFAULT_CONFIG_PATH = common.ROOT / "training" / "configs" / "lora_default.json"
ADAPTER_META = "persona_adapter.json"
ADAPTER_SCHEMA = 1
IGNORE_INDEX = -100
CODE_FILES = ("training/train_lora.py", "training/common.py", "bot/personality/persona_sft_format.py")


class PreflightError(RuntimeError):
    """The environment, data or configuration cannot support the requested training run."""


# ── configuration ────────────────────────────────────────────────────────────
DEFAULTS: Dict[str, Any] = {
    "base_model": None, "revision": None, "method": "lora",
    "max_seq_len": 512, "seed": 1337, "bf16": "auto", "end_of_turn_token": None,
    "account_tag_dropout": 0.10,
    "short_context_rate": 0.25,   # share of training examples cut to the last turn: the live bot supplies one turn
    "lora": {"r": 16, "alpha": 32, "dropout": 0.05, "target_modules": "all-linear"},
    "optim": {"learning_rate": 2e-4, "weight_decay": 0.0, "warmup_ratio": 0.03, "lr_scheduler": "cosine",
              "epochs": 3, "per_device_batch": 8, "grad_accum": 4, "max_grad_norm": 1.0},
    "eval_steps": 100, "save_steps": 100, "logging_steps": 10, "early_stopping_patience": 3,
    "min_train_examples": 500, "min_val_examples": 50,
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(base))
    for k, v in over.items():
        if k.startswith("_"):
            continue
        if k not in out:
            raise PreflightError(f"unknown config key '{k}'")
        if isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    errs: List[str] = []
    if cfg["method"] not in ("lora", "qlora"):
        errs.append("method must be 'lora' or 'qlora'")
    if not cfg.get("base_model"):
        errs.append("base_model is required (--base-model or config)")
    if not 64 <= int(cfg["max_seq_len"]) <= 8192:
        errs.append("max_seq_len must be within 64..8192")
    for key in ("account_tag_dropout", "short_context_rate"):
        if not 0.0 <= float(cfg[key]) <= 1.0:
            errs.append(f"{key} must be within 0..1")
    lo, op = cfg["lora"], cfg["optim"]
    if int(lo["r"]) < 1 or int(lo["alpha"]) < 1 or not 0.0 <= float(lo["dropout"]) < 1.0:
        errs.append("lora.r / lora.alpha must be >= 1 and lora.dropout within [0,1)")
    if float(op["learning_rate"]) <= 0 or int(op["epochs"]) < 1 or int(op["per_device_batch"]) < 1 or int(op["grad_accum"]) < 1:
        errs.append("optim.learning_rate > 0 and epochs / per_device_batch / grad_accum >= 1 are required")
    if cfg["bf16"] not in ("auto", True, False):
        errs.append("bf16 must be 'auto', true or false")
    if errs:
        raise PreflightError("invalid configuration: " + "; ".join(errs))
    return cfg


def load_config(path: Optional[Path], overrides: Dict[str, Any]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if p.exists():
        cfg = _merge(cfg, json.loads(p.read_text(encoding="utf-8")))
    elif path:
        raise PreflightError(f"config not found: {p}")
    cfg = _merge(cfg, {k: v for k, v in overrides.items() if v is not None})
    return validate_config(cfg)


# ── tokenisation (pure; tested with a fake tokenizer) ────────────────────────
def plain_prompt(messages: Sequence[Dict[str, str]]) -> str:
    """Fallback for tokenizers without a chat template."""
    parts = [f"### {m['role']}\n{m['content']}" for m in messages]
    return "\n\n".join(parts) + "\n\n### assistant\n"


def render_prompt(tokenizer: Any, messages: Sequence[Dict[str, str]]) -> str:
    if getattr(tokenizer, "chat_template", None):
        return tokenizer.apply_chat_template(list(messages), tokenize=False, add_generation_prompt=True)
    return plain_prompt(messages)


def end_token_id(tokenizer: Any, end_token: Optional[str]) -> int:
    if end_token:
        tid = tokenizer.convert_tokens_to_ids(end_token)
        if tid is None or tid == getattr(tokenizer, "unk_token_id", None):
            raise PreflightError(f"end_of_turn_token '{end_token}' is not in the tokenizer vocabulary")
        return int(tid)
    if tokenizer.eos_token_id is None:
        raise PreflightError("tokenizer has no eos token; set end_of_turn_token in the config")
    return int(tokenizer.eos_token_id)


def tokenize_example(
    tokenizer: Any, fmt: Any, example: Dict[str, Any], max_len: int, end_id: int, drop_account_tag: bool = False,
    last_turn_only: bool = False,
) -> Optional[Dict[str, List[int]]]:
    """input_ids / labels for one example, loss on the reply only.  Returns None if it cannot fit.

    When the sequence is too long the OLDEST context turns are removed first (the reply target and the
    person's reply are never truncated).
    """
    target_ids = tokenizer(example["target"], add_special_tokens=False)["input_ids"] + [end_id]
    ctx = [list(t) for t in (example.get("ctx") or [])]
    if last_turn_only:
        ctx = ctx[-1:]
    acct = None if drop_account_tag else example.get("acct")
    while True:
        msgs = fmt.build_messages(ctx, example.get("reply_to"), example.get("chat_kind", "group"), acct)
        prompt_ids = tokenizer(render_prompt(tokenizer, msgs), add_special_tokens=False)["input_ids"]
        if len(prompt_ids) + len(target_ids) <= max_len:
            ids = prompt_ids + target_ids
            return {"input_ids": ids, "attention_mask": [1] * len(ids), "labels": [IGNORE_INDEX] * len(prompt_ids) + target_ids}
        if not ctx:
            return None
        ctx.pop(0)


def coin(seed: int, example_id: str, salt: str, rate: float) -> bool:
    """Deterministic per-example coin flip (so a resumed run sees exactly the same data)."""
    import hashlib
    return int(hashlib.sha256(f"{seed}|{salt}|{example_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def drops_account_tag(seed: int, example_id: str, rate: float) -> bool:
    return coin(seed, example_id, "tag", rate)


def collate(batch: Sequence[Dict[str, List[int]]], pad_id: int) -> Dict[str, List[List[int]]]:
    """Right-pad to the longest sequence in the batch (labels with IGNORE_INDEX)."""
    n = max(len(b["input_ids"]) for b in batch)
    return {
        "input_ids": [b["input_ids"] + [pad_id] * (n - len(b["input_ids"])) for b in batch],
        "attention_mask": [b["attention_mask"] + [0] * (n - len(b["attention_mask"])) for b in batch],
        "labels": [b["labels"] + [IGNORE_INDEX] * (n - len(b["labels"])) for b in batch],
    }


# ── crash-safe adapter store ─────────────────────────────────────────────────
def validate_adapter_dir(d: Path) -> Dict[str, Any]:
    """Raise unless ``d`` is a complete adapter directory; return its persona_adapter.json."""
    d = Path(d)
    try:
        cfg = json.loads((d / "adapter_config.json").read_text(encoding="utf-8"))
        meta = json.loads((d / ADAPTER_META).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise common.TrainingDataError(f"{d}: incomplete adapter ({exc})") from exc
    weights = [d / n for n in ("adapter_model.safetensors", "adapter_model.bin") if (d / n).exists()]
    if not weights or weights[0].stat().st_size == 0:
        raise common.TrainingDataError(f"{d}: adapter weights missing or empty")
    if not cfg.get("base_model_name_or_path"):
        raise common.TrainingDataError(f"{d}: adapter_config.json has no base_model_name_or_path")
    for key in ("schema", "person", "dataset_sha256", "format_version", "base_model", "authorization"):
        if key not in meta:
            raise common.TrainingDataError(f"{d}: {ADAPTER_META} lacks '{key}'")
    return meta


def recover_store(person_dir: Path) -> str:
    """Repair a store left mid-swap by a crash: if 'current' is missing but 'previous' exists, restore it."""
    cur, prev = person_dir / "current", person_dir / "previous"
    if not cur.exists() and prev.exists():
        os.replace(prev, cur)
        return "restored previous -> current"
    return "ok"


def promote_staging(person_dir: Path, staging: Path) -> None:
    """Validate ``staging`` and swap it in as 'current' (old current becomes 'previous')."""
    validate_adapter_dir(staging)
    cur, prev = person_dir / "current", person_dir / "previous"
    if cur.exists():
        if prev.exists():
            shutil.rmtree(prev)
        os.replace(cur, prev)
    os.replace(staging, cur)


# ── preflight ────────────────────────────────────────────────────────────────
def estimate_vram_gb(model_id: str, method: str) -> Optional[float]:
    """Rough working-set estimate from a parameter count in the model name (e.g. '7b'); None if unknown."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])", model_id)
    if not m:
        return None
    params_b = float(m.group(1))
    return round(params_b * (0.6 if method == "qlora" else 2.0) + 3.0, 1)


def inspect_environment() -> Dict[str, Any]:
    env: Dict[str, Any] = {"python": sys.version.split()[0], "missing": [], "cuda": False}
    for mod in ("torch", "transformers", "peft", "accelerate"):
        try:
            env[mod] = __import__(mod).__version__
        except Exception:
            env["missing"].append(mod)
    if "torch" not in env["missing"]:
        import torch
        env["cuda"] = bool(torch.cuda.is_available())
        if env["cuda"]:
            props = torch.cuda.get_device_properties(0)
            env["gpu"] = props.name
            env["vram_gb"] = round(props.total_memory / 2**30, 1)
            env["bf16_supported"] = bool(torch.cuda.is_bf16_supported())
    try:
        __import__("bitsandbytes")
        env["bitsandbytes"] = True
    except Exception:
        env["bitsandbytes"] = False
    return env


def preflight(person: str, data: Path, cfg: Dict[str, Any], auth_path: Optional[Path], allow_cpu: bool, ignore_vram: bool, check_only: bool) -> Dict[str, Any]:
    """Everything that can be verified before loading a model.  Raises on any blocking problem."""
    plan: Dict[str, Any] = {"person": person, "problems": [], "warnings": []}
    auth = common.require_authorization(person, auth_path)  # raises AuthorizationError
    manifest = common.verify_prepared_dir(data, ("train", "val"))  # raises TrainingDataError
    if manifest["person"] != person:
        raise PreflightError(f"{data} holds '{manifest['person']}', not '{person}'")
    fmt = common.fmt_module()
    if manifest.get("format_version") != fmt.FORMAT_VERSION:
        raise PreflightError(f"dataset format_version {manifest.get('format_version')} != code {fmt.FORMAT_VERSION}; re-run prepare_sft")
    n_tr, n_va = manifest["splits"]["train"]["examples"], manifest["splits"]["val"]["examples"]
    if n_tr < int(cfg["min_train_examples"]):
        plan["problems"].append(f"only {n_tr} training examples (min_train_examples={cfg['min_train_examples']})")
    if n_va < int(cfg["min_val_examples"]):
        plan["problems"].append(f"only {n_va} validation examples (min_val_examples={cfg['min_val_examples']})")
    env = inspect_environment()
    if env["missing"]:
        plan["problems"].append("missing packages: " + ", ".join(env["missing"]) + " (pip install -r training/requirements-train.txt)")
    elif not env["cuda"] and not allow_cpu:
        plan["problems"].append("no CUDA GPU detected (use --allow-cpu only for a tiny smoke test)")
    if cfg["method"] == "qlora" and not env["bitsandbytes"]:
        plan["problems"].append("method=qlora needs bitsandbytes")
    need = estimate_vram_gb(str(cfg["base_model"]), cfg["method"])
    if need is not None and env.get("vram_gb") is not None and env["vram_gb"] < need and not ignore_vram:
        plan["problems"].append(f"GPU has {env['vram_gb']} GB but ~{need} GB is typically needed for this model with {cfg['method']} (--ignore-vram-check to override)")
    elif need is None:
        plan["warnings"].append("model size not inferable from its name; VRAM check skipped")
    plan.update(env=env, examples={"train": n_tr, "val": n_va}, dataset_sha256=manifest["dataset_sha256"],
                authorization={k: auth.get(k) for k in ("status", "granted_by", "date", "scope")}, estimated_vram_gb=need)
    if plan["problems"] and not check_only:
        raise PreflightError("; ".join(plan["problems"]))
    return plan


# ── training (GPU) ───────────────────────────────────────────────────────────
def _training_arguments(cfg: Dict[str, Any], work: Path, env: Dict[str, Any]):
    import inspect
    from transformers import TrainingArguments
    op = cfg["optim"]
    bf16 = env.get("bf16_supported", False) if cfg["bf16"] == "auto" else bool(cfg["bf16"])
    params = inspect.signature(TrainingArguments.__init__).parameters
    kw: Dict[str, Any] = dict(
        output_dir=str(work / "ckpt"), per_device_train_batch_size=int(op["per_device_batch"]),
        per_device_eval_batch_size=int(op["per_device_batch"]), gradient_accumulation_steps=int(op["grad_accum"]),
        learning_rate=float(op["learning_rate"]), weight_decay=float(op["weight_decay"]), warmup_ratio=float(op["warmup_ratio"]),
        lr_scheduler_type=op["lr_scheduler"], num_train_epochs=float(op["epochs"]), max_grad_norm=float(op["max_grad_norm"]),
        logging_steps=int(cfg["logging_steps"]), eval_steps=int(cfg["eval_steps"]), save_steps=int(cfg["save_steps"]),
        save_strategy="steps", save_total_limit=2, load_best_model_at_end=True, metric_for_best_model="eval_loss",
        greater_is_better=False, seed=int(cfg["seed"]), data_seed=int(cfg["seed"]), bf16=bf16, fp16=(not bf16 and env.get("cuda", False)),
        report_to=[], remove_unused_columns=False, gradient_checkpointing=True,
    )
    kw["eval_strategy" if "eval_strategy" in params else "evaluation_strategy"] = "steps"
    if "gradient_checkpointing_kwargs" in params:
        kw["gradient_checkpointing_kwargs"] = {"use_reentrant": False}
    return TrainingArguments(**kw)


def run_training(args: argparse.Namespace, cfg: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, EarlyStoppingCallback, Trainer, set_seed

    person, data, root = args.person, Path(args.data), Path(args.out)
    person_dir = root / person
    note = recover_store(person_dir)
    fmt = common.fmt_module()
    set_seed(int(cfg["seed"]))
    random.seed(int(cfg["seed"]))
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    work, staging = person_dir / "work", person_dir / f"staging-{run_id}"
    work.mkdir(parents=True, exist_ok=True)
    if not args.resume:  # stale checkpoints from an abandoned run must not be rotated into this one
        shutil.rmtree(work / "ckpt", ignore_errors=True)

    tok = AutoTokenizer.from_pretrained(cfg["base_model"], revision=cfg["revision"], use_fast=True)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    end_id = end_token_id(tok, cfg["end_of_turn_token"])
    train_rows = list(common.iter_jsonl(data / "train.jsonl"))   # test.jsonl is deliberately never opened here
    val_rows = list(common.iter_jsonl(data / "val.jsonl"))
    skipped = {"train": 0, "val": 0}

    def encode(rows: List[Dict[str, Any]], split: str) -> List[Dict[str, List[int]]]:
        out = []
        for ex in rows:
            train = split == "train"
            drop = train and drops_account_tag(int(cfg["seed"]), ex["id"], float(cfg["account_tag_dropout"]))
            short = train and coin(int(cfg["seed"]), ex["id"], "ctx", float(cfg["short_context_rate"]))
            item = tokenize_example(tok, fmt, ex, int(cfg["max_seq_len"]), end_id, drop, short)
            if item is None:
                skipped[split] += 1
            else:
                out.append(item)
        return out

    train_ds, val_ds = encode(train_rows, "train"), encode(val_rows, "val")
    if not train_ds or not val_ds:
        raise PreflightError("no encodable training or validation examples (raise max_seq_len?)")

    class _DS(torch.utils.data.Dataset):
        def __init__(self, items): self.items = items
        def __len__(self): return len(self.items)
        def __getitem__(self, i): return self.items[i]

    def collator(batch):
        c = collate(batch, tok.pad_token_id)
        return {k: torch.tensor(v, dtype=torch.long) for k, v in c.items()}

    env = plan["env"]
    dtype = torch.bfloat16 if (env.get("bf16_supported") if cfg["bf16"] == "auto" else cfg["bf16"]) else torch.float16
    qargs: Dict[str, Any] = {}
    if cfg["method"] == "qlora":
        from transformers import BitsAndBytesConfig
        qargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype)
    model = AutoModelForCausalLM.from_pretrained(
        cfg["base_model"], revision=cfg["revision"], torch_dtype=dtype,
        device_map=({"": 0} if env.get("cuda") else None), **qargs)
    model.config.use_cache = False
    if cfg["method"] == "qlora":
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    else:
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()
    lo = cfg["lora"]
    model = get_peft_model(model, LoraConfig(
        r=int(lo["r"]), lora_alpha=int(lo["alpha"]), lora_dropout=float(lo["dropout"]),
        target_modules=lo["target_modules"], bias="none", task_type="CAUSAL_LM"))

    trainer = Trainer(
        model=model, args=_training_arguments(cfg, work, env), train_dataset=_DS(train_ds), eval_dataset=_DS(val_ds),
        data_collator=collator, callbacks=[EarlyStoppingCallback(early_stopping_patience=int(cfg["early_stopping_patience"]))])
    resume = None
    if args.resume:
        ckpts = sorted((work / "ckpt").glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[-1])) if (work / "ckpt").exists() else []
        ckpts = [c for c in ckpts if (c / "trainer_state.json").exists()]
        resume = str(ckpts[-1]) if ckpts else None
    started = time.time()
    trainer.train(resume_from_checkpoint=resume)
    final_eval = trainer.evaluate()

    trainer.model.save_pretrained(str(staging))
    tok.save_pretrained(str(staging))
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    meta = {
        "schema": ADAPTER_SCHEMA, "person": person, "run_id": run_id, "format_version": fmt.FORMAT_VERSION,
        "base_model": cfg["base_model"], "base_revision": cfg["revision"], "method": cfg["method"],
        "dataset_sha256": manifest["dataset_sha256"], "dataset_splits": manifest["splits"],
        "config": cfg, "code_sha256": common.code_hash(CODE_FILES), "authorization": plan["authorization"],
        "accounts_trained": sorted({e["acct"] for e in train_rows}),
        "examples": {"train": len(train_ds), "val": len(val_ds), "skipped_too_long": skipped},
        "final_eval_loss": final_eval.get("eval_loss"), "best_checkpoint": trainer.state.best_model_checkpoint,
        "global_step": trainer.state.global_step, "train_seconds": round(time.time() - started, 1),
        "environment": {k: env.get(k) for k in ("python", "torch", "transformers", "peft", "gpu", "vram_gb")},
        "log_history": trainer.state.log_history, "status": "trained",
    }
    common.atomic_write_json(staging / ADAPTER_META, meta)
    promote_staging(person_dir, staging)
    reg_path = root / "registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {"schema": 1, "persons": {}}
    reg["persons"][person] = {
        "status": "trained", "model": f"persona-{person}", "adapter_dir": f"{person}/current",
        "format_version": fmt.FORMAT_VERSION, "dataset_sha256": manifest["dataset_sha256"], "run_id": run_id,
        "base_model": cfg["base_model"], "trained_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chat_kinds": sorted({e["chat_kind"] for e in train_rows}),
    }
    common.atomic_write_json(reg_path, reg)
    shutil.rmtree(work / "ckpt", ignore_errors=True)  # checkpoints are only needed to resume an unfinished run
    return {"adapter": str(person_dir / "current"), "eval_loss": final_eval.get("eval_loss"), "store": note}


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--person", required=True)
    ap.add_argument("--data", required=True, help="prepared per-person directory from prepare_sft")
    ap.add_argument("--out", default=str(common.ROOT / "data" / "models" / "persona_adapters"), help="adapter store root")
    ap.add_argument("--base-model")
    ap.add_argument("--revision")
    ap.add_argument("--method", choices=("lora", "qlora"))
    ap.add_argument("--config", help="JSON overriding training/configs/lora_default.json")
    ap.add_argument("--authorization", default=str(common.DEFAULT_AUTH))
    ap.add_argument("--resume", action="store_true", help="continue an interrupted run from its newest valid checkpoint")
    ap.add_argument("--check", action="store_true", help="verify authorization / data / environment and print the plan; train nothing")
    ap.add_argument("--allow-cpu", action="store_true")
    ap.add_argument("--ignore-vram-check", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        cfg = load_config(Path(args.config) if args.config else None,
                          {"base_model": args.base_model, "revision": args.revision, "method": args.method})
        plan = preflight(args.person, Path(args.data), cfg, Path(args.authorization), args.allow_cpu, args.ignore_vram_check, args.check)
        if args.check:
            print(json.dumps({"ok": not plan["problems"], **plan}, indent=1, default=str))
            return 0 if not plan["problems"] else 1
        result = run_training(args, cfg, plan)
    except (common.AuthorizationError, common.TrainingDataError, PreflightError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
