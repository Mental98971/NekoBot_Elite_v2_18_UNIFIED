"""Optional in-process loading of a person's LoRA adapter (PEFT) - for offline experiments on one machine.

The bot itself never loads weights: adapters are served by an OpenAI-compatible inference server and routed by
``persona_model.PersonaModelRouter``.  This module keeps the v2.11 PERSONA_FT overlay's ``adapter_path`` / ``is_available`` /
``load`` helpers for people who want to try an adapter directly with ``transformers`` + ``peft``.

Unlike the overlay version it is gated: an adapter is only reported or loaded when the registry written by
``training.train_lora`` / ``training.promote`` marks it ``ready`` (evaluation gates passed, prompt format current).  Where the files
live is just a location: ``<store>/<person>/`` (the toolkit's output) or the overlay's ``adapters/<person>_lora/``.  ``torch``,
``transformers`` and ``peft`` are imported on demand, so nothing here costs anything when they are not installed.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Tuple, Union

ROOT = Path(__file__).resolve().parents[2]
LEGACY_DIR = ROOT / "adapters"
ADAPTER_META = "persona_adapter.json"
_WEIGHT_FILES = ("adapter_model.safetensors", "adapter_model.bin")


class AdapterUnavailable(RuntimeError):
    """No ready adapter for the person, or the optional libraries needed to load it are missing."""


def _persona_model():
    try:
        from bot.personality import persona_model as mod
        return mod
    except Exception:  # dependency stack missing (tools, tests): load the stdlib-only file by path
        name = "_neko_persona_model_standalone"
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("persona_model.py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return sys.modules[name]


def default_store() -> Path:
    try:
        from bot.config import settings
        return Path(settings.persona_adapter_dir)
    except Exception:
        return Path(os.environ.get("PERSONA_ADAPTER_DIR", "./data/models/persona_adapters"))


def _has_weights(path: Path) -> bool:
    return path.is_dir() and any((path / n).exists() for n in _WEIGHT_FILES)


def _ready_entry(person_id: str, store: Path):
    return _persona_model().AdapterRegistry(store).ready(person_id)


def adapter_path(person_id: str, store: Optional[Union[str, Path]] = None) -> Optional[Path]:
    """Directory of the person's *ready* adapter, or None."""
    store = Path(store) if store is not None else default_store()
    if _ready_entry(person_id, store) is None:
        return None
    for candidate in (store / person_id, LEGACY_DIR / f"{person_id}_lora"):
        if _has_weights(candidate):
            return candidate
    return None


def is_available(person_id: str, store: Optional[Union[str, Path]] = None) -> bool:
    return adapter_path(person_id, store) is not None


def load(person_id: str, base_model: Optional[str] = None, store: Optional[Union[str, Path]] = None, device_map: str = "auto") -> Tuple[Any, Any]:
    """``(model, tokenizer)`` with the person's adapter applied.  Raises :class:`AdapterUnavailable` when it cannot."""
    store_path = Path(store) if store is not None else default_store()
    path = adapter_path(person_id, store_path)
    if path is None:
        raise AdapterUnavailable(f"no ready adapter for '{person_id}' (train it, evaluate it and promote it first)")
    try:
        transformers = importlib.import_module("transformers")
        peft = importlib.import_module("peft")
        torch = importlib.import_module("torch")
    except ImportError as exc:
        raise AdapterUnavailable(f"loading an adapter needs torch, transformers and peft ({exc})") from exc
    entry = _ready_entry(person_id, store_path)
    meta = {}
    if (path / ADAPTER_META).exists():
        meta = json.loads((path / ADAPTER_META).read_text(encoding="utf-8"))
    base = base_model or getattr(entry, "base_model", "") or meta.get("base_model")
    if not base:
        raise AdapterUnavailable("the base model is unknown: pass base_model=...")
    tokenizer = transformers.AutoTokenizer.from_pretrained(base)
    model = transformers.AutoModelForCausalLM.from_pretrained(
        base, torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32, device_map=device_map)
    model = peft.PeftModel.from_pretrained(model, str(path))
    model.eval()
    return model, tokenizer
